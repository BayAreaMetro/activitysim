# ActivitySim
# See full license in LICENSE.txt.
from builtins import range

import logging
import os
import itertools
import multiprocessing
import gc as _gc
import psutil
import time

from contextlib import contextmanager

import numpy as np
import pandas as pd

from activitysim.core import util
from activitysim.core import config
from activitysim.core import inject
from activitysim.core import simulate
from activitysim.core import tracing

logger = logging.getLogger(__name__)

RAWARRAY = False
DTYPE_NAME = 'float32'
RESCALE = 1000

DYNAMIC = 'dynamic'
STATIC = 'static'
TRACE = 'trace'


MEMO_STACK = []

@contextmanager
def memo(tag, console=False, disable_gc=True):
    t0 = time.time()

    MEMO_STACK.append(tag)
    if len(MEMO_STACK) > 1:
        logger.debug(f"nested memo call: {MEMO_STACK}")
        #bug

    gc_was_enabled = _gc.isenabled()
    if gc_was_enabled:
        _gc.collect()
        if disable_gc:
            _gc.disable()

    previous_mem = psutil.Process(os.getpid()).memory_info().rss
    try:
        yield
    finally:
        elapsed_time = time.time() - t0

        current_mem = (psutil.Process(os.getpid()).memory_info().rss)
        marginal_mem = current_mem - previous_mem
        mem_str = f"net {tracing.si_units(marginal_mem)} ({str(marginal_mem)}) total {tracing.si_units(current_mem)}"

        if gc_was_enabled and disable_gc:
            _gc.enable()
        if _gc.isenabled():
            _gc.collect()

        if console:
            print(f"MEMO {tag} Time: {tracing.si_units(elapsed_time, kind='s')} Memory: {mem_str} ")
        else:
            logger.debug(f"MEM  {tag} {mem_str} in {tracing.si_units(elapsed_time, kind='s')}")

        MEMO_STACK.pop()


class TVPBCache(object):
    def __init__(self, network_los, uid_calculator, cache_tag):

        # lightweight until opened

        self.cache_tag = cache_tag

        self.network_los = network_los
        self.uid_calculator = uid_calculator

        self.is_open = False
        self.is_changed = False
        self._df = None

    def cache_path(self, cache_type):
        if cache_type == DYNAMIC:
            file_type = 'feather'
        elif cache_type == STATIC:
            file_type = 'mmap'
        elif cache_type == TRACE:
            file_type = 'csv'
        else:
            assert False, f"unknown cache_type {cache_type}"
        return os.path.join(self.network_los.get_cache_dir(), f'{self.cache_tag}.{file_type}')

    def cleanup(self):
        """
        Called prior to
        """
        if self.network_los.rebuild_tvpb_cache:
            for cache_type in [STATIC, DYNAMIC, TRACE]:
                if os.path.isfile(self.cache_path(cache_type)):
                    logger.debug(f"deleting cache {self.cache_path(cache_type)}")
                    os.unlink(self.cache_path(cache_type))

    def write_static_cache(self, data):

        assert not self.is_open
        assert self._df is None
        assert not self.is_changed

        data = data.reshape(self.uid_calculator.fully_populated_shape)

        logger.debug(f"#TVPB CACHE write_static_cache df {data.shape}")

        mm_data = np.memmap(self.cache_path(STATIC),
                            shape=data.shape,
                            dtype=DTYPE_NAME,
                            mode='w+')
        np.copyto(mm_data, data)
        mm_data._mmap.close()
        del mm_data

        logger.debug(f"#TVPB CACHE write_static_cache wrote static cache table "
                     f"({data.shape}) to {self.cache_path(STATIC)}")


    def open(self, for_rebuild=False):
        # MMAP only supported for fully_populated_uids (STATIC)
        # otherwise we would have to store uid index as float, which has roundoff issues for float32

        assert not self.is_open, f"TVPBCache open called but already open"
        self.is_open = True

        if for_rebuild:
            return

        data = None

        if self.network_los.multiprocess():
            # use preloaded fully_populated shared data buffer
            with memo("TVPBCache.open get_data_and_lock_from_buffers"):
                data, _ = self.get_data_and_lock_from_buffers()

            #uninitialized - EXPENSIVE!
            #with memo("TVPBCache.open assert not np.any"):
            #    assert not np.isnan(data).any()

            logger.info(f"TVBPCache.open {self.cache_tag} STATIC cache using existing data_buffers")

        elif os.path.isfile(self.cache_path(STATIC)):
            # read precomputed fully_populated STATIC cache from mmap file
            data = np.memmap(self.cache_path(STATIC),
                             dtype=DTYPE_NAME,
                             mode='r')
            logger.info(f"TVBPCache.open {self.cache_tag} read fully_populated data array from mmap file")

        elif os.path.isfile(self.cache_path(DYNAMIC)):
            # read DYNAMIC cache from feather file
            df = pd.read_feather(self.cache_path(DYNAMIC))
            df.set_index(df.columns[0], inplace=True)
            assert not df.index.duplicated().any()
            self._df = df

            logger.info(f"TVBPCache.open {self.cache_tag} loaded DYNAMIC cache.")

        if data is not None:
            # create no-copy pandas DataFrame from numpy wrapped RawArray or Memmap buffer
            column_names = self.uid_calculator.set_names
            with memo("TVPBCache.open data.reshape"):
                data = data.reshape((-1, len(column_names)))  # reshape so there is one column per set

            # data should be fully_populated and in canonical order - so we can assign canonical uid index
            with memo("TVPBCache.open uid_calculator.fully_populated_uids"):
                fully_populated_uids = self.uid_calculator.fully_populated_uids

            # check fully_populated, but we have to take order on faith (internal error if it is not)
            assert data.shape[0] == len(fully_populated_uids)

            # whether shared data buffer or memmap, we can use it as no-copy backing store for DataFrame
            with memo("TVPBCache.open DataFrame"):
                df = pd.DataFrame(data=data, columns=column_names, index=fully_populated_uids, copy=False)
            df.index.name = 'uid'
            self._df = df
            logger.debug(f"TVBPCache.open initialized STATIC cache table")

    def flush(self):
        """
        write any changes
        """

        assert self.is_open, f"TVPBCache close called but not open"
        assert not self._df.index.duplicated().any()

        if self.is_changed:

            if self.is_fully_populated:

                #BUG - remove this eventually. unlikely to accumulate via DYNAMIC, but still...
                assert False

                xdata = self._df.values
                data = np.memmap(self.cache_path(STATIC),
                                 shape=xdata.shape,
                                 dtype=DTYPE_NAME,
                                 mode='w+')
                np.copyto(data, xdata)
                data._mmap.close()
                del data
                self.is_changed = False
                logger.debug(f"#TVPB CACHE wrote static cache table "
                             f"({self._df.shape}) to {self.cache_path(STATIC)}")

            else:

                if self.network_los.rebuild_tvpb_cache:
                    self._df.reset_index().to_feather(self.cache_path(DYNAMIC))
                    self.is_changed = False
                    logger.debug(f"#TVPB CACHE wrote dynamic cache table "
                                 f"({self._df.shape}) to {self.cache_path(DYNAMIC)}")
                else:
                    logger.debug(f"Not flushing dynamic tvpb cache because rebuild_tvpb_cache flag is False"
                                 f" is not set to True in network_los settings")

            if self.network_los.setting('trace_tvpb_cache_as_csv', False):
                csv_path = self.cache_path(TRACE)
                self._df.to_csv(csv_path)
                logger.debug(f"#TVPB CACHE wrote trace cache table ({self._df.shape}) to {csv_path}")

        else:
            # not self.is_changed
            logger.debug(f"#TVPB CACHE not writing cache since unchanged.")

    def close(self, trace=False):
        """
        write any changes, free data, and mark as closed
        """

        assert self.is_open, f"TVPBCache close called but not open"
        self.flush()

        self.is_open = False
        self._df = None

    def table(self):
        return self._df

    @property
    def is_fully_populated(self):
        assert self.is_open
        return self._df is not None and len(self._df) == self.uid_calculator.fully_populated_shape[0]

    def extend_table(self, new_rows):

        assert len(new_rows) > 0
        assert self.is_open
        assert not self.is_fully_populated

        self.is_changed = True

        if self._df is None:
            self._df = new_rows.copy()
        else:
            self._df = pd.concat([self._df, new_rows], axis=0)

        assert not self._df.index.duplicated().any()

        logger.debug(f"#TVPB CACHE extended cache by {len(new_rows)} rows"
                     f" from {len(self._df)-len(new_rows)} to {len(self._df)} rows")

    def allocate_data_buffer(self, shared=False):
        """
        allocate data buffer for cached data and fill with np.nan
        Parameters
        ----------
        shared

        Returns
        -------

        """

        assert not self.is_open

        dtype_name = DTYPE_NAME
        dtype = np.dtype(DTYPE_NAME)

        # multiprocessing.RawArray argument buffer_size must be int, not np.int64
        shape = self.uid_calculator.fully_populated_shape
        buffer_size = util.iprod(self.uid_calculator.fully_populated_shape)

        csz = buffer_size * dtype.itemsize
        logger.info(f"TVPBCache.allocate_data_buffer allocating data buffer "
                    f"shape {shape} buffer_size {buffer_size} total size: {csz} ({tracing.si_units(csz)})")

        if shared:
            if dtype_name == 'float64':
                typecode = 'd'
            elif dtype_name == 'float32':
                typecode = 'f'
            else:
                raise RuntimeError("allocate_data_buffer unrecognized dtype %s" % dtype_name)

            if RAWARRAY:
                with memo("TVPBCache.allocate_data_buffer allocate RawArray"):
                    buffer = multiprocessing.RawArray(typecode, buffer_size)
                logger.info(f"TVPBCache.allocate_data_buffer allocated shared multiprocessing.RawArray as buffer")
            else:
                with memo("TVPBCache.allocate_data_buffer allocate Array"):
                    buffer = multiprocessing.Array(typecode, buffer_size)
                logger.info(f"TVPBCache.allocate_data_buffer allocated shared multiprocessing.Array as buffer")

        else:
            buffer = np.empty(buffer_size, dtype=dtype)
            np.copyto(buffer, np.nan)  # fill with np.nan

            logger.info(f"TVPBCache.allocate_data_buffer allocating non-shared numpy array as buffer")

        return buffer

    def load_data_to_buffer(self, data_buffer):
        # 1) we are called before initialize_los, there is a saved cache, and it will be honored
        # 2) we are called before initialize_los and there is no saved cache yet
        # 3) we are resuming after initialize_los and so there must be a saved cache

        assert not self.is_open

        # wrap multiprocessing.RawArray as a numpy array
        with memo("TVPBCache.load_data_to_buffer frombuffer"):
            if RAWARRAY:
                np_wrapped_data_buffer = np.ctypeslib.as_array(data_buffer)
            else:
                np_wrapped_data_buffer = np.ctypeslib.as_array(data_buffer.get_obj())

        if os.path.isfile(self.cache_path(STATIC)):
            with memo("TVPBCache.load_data_to_buffer copy memmap"):
                data = np.memmap(self.cache_path(STATIC), dtype=DTYPE_NAME, mode='r')
                np.copyto(np_wrapped_data_buffer, data)
                data._mmap.close()
                del data
            logger.debug(f"TVPBCache.load_data_to_buffer loaded data from {self.cache_path(STATIC)}")
        else:
            np.copyto(np_wrapped_data_buffer, np.nan)
            logger.debug(f"TVPBCache.load_data_to_buffer - saved cache file not found.")


    def get_data_and_lock_from_buffers(self):
        data_buffers = inject.get_injectable('data_buffers', None)
        assert self.cache_tag in data_buffers  # internal error
        logger.debug(f"TVPBCache.get_data_and_lock_from_buffers")
        data_buffer = data_buffers[self.cache_tag]
        if RAWARRAY:
            data = np.ctypeslib.as_array(data_buffer)
            #data = np.frombuffer(data_buffer, dtype=np.dtype(DTYPE_NAME))
            lock = None
        else:
            data = np.ctypeslib.as_array(data_buffer.get_obj())
            #data = np.frombuffer(data_buffer.get_obj(), dtype=np.dtype(DTYPE_NAME))
            lock = data_buffer.get_lock()

        return data, lock


class TapTapUidCalculator(object):

    def __init__(self, network_los):

        self.network_los = network_los

        # ensure that tap_df has been loaded
        # (during multiprocessing we are initialized before network_los.load_data is called)
        assert network_los.tap_df is not None
        self.tap_ids = network_los.tap_df['TAP'].values

        self.segmentation = \
            network_los.setting('TVPB_SETTINGS.tour_mode_choice.tap_tap_settings.attribute_segments')

        # e.g. [(0, 'AM', 'walk'), (0, 'AM', 'walk')...]) for attributes demographic_segment, tod, and access_mode
        self.attribute_combination_tuples = list(itertools.product(*list(self.segmentation.values())))

        # ordinalizers - for mapping attribute values to canonical ordinal values for uid computation
        # (pandas series of ordinal position with attribute value index (e.g. map tod value 'AM' to 0, 'MD' to 1,...)
        #FIXME dict might be faster than Series.map() and Series.at[]?
        self.ordinalizers = {}
        for k, v in self.segmentation.items():
            self.ordinalizers[k] = pd.Series(range(len(v)), index=v)
        # orig/dest go last so all rows in same 'skim' end up with adjacent uids
        self.ordinalizers['btap'] = pd.Series(range(len(self.tap_ids)), index=self.tap_ids)
        self.ordinalizers['atap'] = self.ordinalizers['btap']

        # for k,v in self.ordinalizers.items():
        #     print(f"\ordinalizer {k}\n{v}")

        spec_name = self.network_los.setting(f'TVPB_SETTINGS.tour_mode_choice.tap_tap_settings.SPEC')
        self.set_names = list(simulate.read_model_spec(file_name=spec_name).columns)

    @property
    def fully_populated_shape(self):
        # (num_combinations * num_orig_zones * num_dest_zones, num_sets)
        num_combinations = len(self.attribute_combination_tuples)
        num_orig_zones = num_dest_zones = len(self.tap_ids)
        num_rows = num_combinations * num_orig_zones * num_dest_zones
        num_sets = len(self.set_names)
        return (num_rows, num_sets)

    @property
    def skim_shape(self):
        # (num_combinations, num_od_rows, num_sets)
        num_combinations = len(self.attribute_combination_tuples)
        num_orig_zones = num_dest_zones = len(self.tap_ids)
        num_od_rows = num_orig_zones * num_dest_zones
        num_sets = len(self.set_names)
        return (num_combinations, num_od_rows, num_sets)

    @property
    def fully_populated_uids(self):
        num_combinations = len(self.attribute_combination_tuples)
        num_orig_zones = num_dest_zones = len(self.tap_ids)
        return np.arange(num_combinations * num_orig_zones * num_dest_zones)

    def get_unique_ids(self, df, scalar_attributes):
        """
        compute canonical unique_id for each row in df
        btap and atap will be in dataframe, but the other attributes may be either df columns or scalar_attributes

        Parameters
        ----------
        df: pandas DataFrame
            with btap, atap, and optionally additional attribute columns
        scalar_attributes: dict
            dict of scalar attributes e.g. {'tod': 'AM', 'demographic_segment': 0}
        Returns
        -------
        ndarray of integer uids
        """
        uid = np.zeros(len(df), dtype=int)

        # need to know cardinality and integer representation of each tap/attribute
        for name, ordinalizer in self.ordinalizers.items():

            cardinality = ordinalizer.max() + 1

            if name in df:
                # if there is a column, use it
                uid = uid * cardinality + np.asanyarray(df[name].map(ordinalizer))
            else:
                # otherwise it should be in scalar_attributes
                assert name in scalar_attributes, f"attribute '{name}' not found in df.columns or scalar_attributes."
                uid = uid * cardinality + ordinalizer.at[scalar_attributes[name]]

        return uid

    def get_od_dataframe(self, scalar_attributes):
        """
        return tap-tap od dataframe with unique_id index for 'skim_offset' for scalar_attributes

        i.e. a dataframe which may be used to compute utilities, together with scalar or column attributes

        Parameters
        ----------
        scalar_attributes: dict of scalar attribute name:value pairs

        Returns
        -------
        pandas.Dataframe
        """

        # create OD dataframe in ROW_MAJOR_LAYOUT
        num_taps = len(self.tap_ids)
        od_choosers_df = pd.DataFrame(
            data={
                'btap': np.repeat(self.tap_ids, num_taps),
                'atap': np.tile(self.tap_ids, num_taps)
            }
        )
        od_choosers_df.index = self.get_unique_ids(od_choosers_df, scalar_attributes)
        assert not od_choosers_df.index.duplicated().any()

        return od_choosers_df

    def get_skim_offset(self, scalar_attributes):
        # return ordinal position of this set of attributes in the list of attribute_combination_tuples
        offset = 0
        for name, ordinalizer in self.ordinalizers.items():
            cardinality = ordinalizer.max() + 1
            if name in scalar_attributes:
                offset = offset * cardinality + ordinalizer.at[scalar_attributes[name]]
        return offset

    def each_scalar_attribute_combination(self):
        # iterate through attribute_combination_tuples, yielding dict of scalar attribute name:value pairs

        # attribute names as list of strings
        attribute_names = list(self.segmentation.keys())
        for attribute_value_tuple in self.attribute_combination_tuples:

            # attribute_value_tuple is an tuple of attribute values - e.g. (0, 'AM', 'walk')
            # build dict of attribute name:value pairs - e.g. {'demographic_segment': 0, 'tod': 'AM', })
            scalar_attributes = {name: value for name, value in zip(attribute_names, attribute_value_tuple)}

            yield scalar_attributes

    def scalar_attribute_combinations(self):
        attribute_names = list(self.segmentation.keys())
        attribute_tuples = self.attribute_combination_tuples
        x = [list(t) for t in attribute_tuples]
        df = pd.DataFrame(data=x, columns=attribute_names)
        df.index.name = 'offset'
        return df
