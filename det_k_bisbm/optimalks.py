import os
import math
import random
import logging
import tempfile

from numba import jit, uint8
from numba.types import Tuple

from collections import OrderedDict
from loky import get_reusable_executor
from itertools import product

from det_k_bisbm.utils import *


class OptimalKs(object):
    r"""Base class for OptimalKs.

    Parameters
    ----------
    edgelist : list, required
        Edgelist (bipartite network) for model selection.

    types : list, required
        Types of each node specifying the type membership.

    init_ka : int, required
        Initial Ka for successive merging and searching for the optimum.

    init_kb : int, required
        Initial Ka for successive merging and searching for the optimum.

    i_th :  double, optional
        Threshold for the merging step (as described in the main text).

    logging_level : str, optional
        Logging level used. It can be one of "warning" or "info".

    """

    def __init__(self,
                 engine,
                 edgelist,
                 types,
                 init_ka=10,
                 init_kb=10,
                 i_th=0.1,
                 logging_level="INFO",
                 prior_args={}):

        self.engine_ = engine.engine  # TODO: check that engine is an object
        self.max_n_sweeps_ = engine.MAX_NUM_SWEEPS
        self.is_par_ = engine.PARALLELIZATION
        self.n_cores_ = engine.NUM_CORES

        # params for the heuristic
        self.ka = int(init_ka)
        self.kb = int(init_kb)
        self.i_0 = float(i_th)
        self.adaptive_ratio = 0.9  # adaptive parameter to make the "delta" smaller, if it's too large
        assert 0. <= self.i_0 < 1, "[ERROR] Allowed range for i_th is [0, 1)."

        # TODO: "types" is only used to compute na and nb. Can be made more generic.
        self.types = types
        self.n_a = 0
        self.n_b = 0
        for _type in types:
            if _type in ["1", 1]:
                self.n_a += 1
            elif _type in ["2", 2]:
                self.n_b += 1

        assert self.n_a > 0, "[ERROR] Number of type-a nodes = 0, which is not allowed"
        assert self.n_b > 0, "[ERROR] Number of type-b nodes = 0, which is not allowed"
        assert self.ka <= self.n_a, "[ERROR] Number of type-a communities must be smaller than the # nodes in type-a."
        assert self.kb <= self.n_b, "[ERROR] Number of type-a communities must be smaller than the # nodes in type-b."
        self.n = len(types)

        self.edgelist = edgelist
        self.e = len(self.edgelist)

        assert self.n == self.n_a + self.n_b, \
            "[ERROR] num_nodes ({}) does not equal to num_nodes_a ({}) plus num_nodes_b ({})".format(
                self.n, self.n_a, self.n_b
            )
        # arguments for setting the prior
        self.prior_args = prior_args

        # These confident_* variable are used to store the "true" data
        # that is, not the sloppy temporarily results via matrix merging
        self.confident_desc_len = OrderedDict()
        self.confident_m_e_rs = OrderedDict()
        self.confident_italic_i = OrderedDict()

        # These trace_* variable are used to store the data that we temporarily go through
        self.trace_mb = OrderedDict()

        # for debug/temp variables
        self.is_tempfile_existed = True
        self.f_edgelist = tempfile.NamedTemporaryFile(mode='w', delete=False)
        # To prevent "TypeError: cannot serialize '_io.TextIOWrapper' object" when using loky
        self._f_edgelist_name = self._get_tempfile_edgelist()

        # initialize other class attributes
        self.init_italic_i = 0.

        # logging
        self._logger = logging.Logger
        self.__set_logging_level(logging_level)

        # hard-coded parameters
        self._size_rows_to_run = 1
        self._k_th_nb_to_search = 1

        # look-up tables
        self.__q_cache_f_name = os.path.join(tempfile.mkdtemp(), '__q_cache.dat')  # for restricted integer partitions
        self.__q_cache = np.array([], ndmin=2)
        self.__q_cache_max_e_r = self.e
        pass

    def __set_logging_level(self, level):
        _level = 0
        if level.upper() == "INFO":
            _level = logging.INFO
        elif level.upper() == "WARNING":
            _level = logging.WARNING
        logging.basicConfig(
            level=_level,
            format="%(asctime)s:%(levelname)s:%(message)s"
        )
        self._logger = logging.getLogger(__name__)

    def set_params(self, init_ka=10, init_kb=10, i_th=0.1):
        # params for the heuristic
        self.ka = int(init_ka)
        self.kb = int(init_kb)
        self.i_0 = float(i_th)
        self.init_italic_i = 0.
        assert 0. <= self.i_0 < 1, "[ERROR] Allowed range for i_th is [0, 1)."
        assert self.ka <= self.n_a, "[ERROR] Number of type-a communities must be smaller than the # nodes in type-a."
        assert self.kb <= self.n_b, "[ERROR] Number of type-a communities must be smaller than the # nodes in type-b."

    def set_adaptive_ratio(self, adaptive_ratio):
        self.adaptive_ratio = float(adaptive_ratio)

    def set_k_th_neighbor_to_search(self, k):
        self._k_th_nb_to_search = int(k)

    def get__q_cache(self):
        return self.__q_cache

    def set__q_cache(self, q_cache):
        self.__q_cache = q_cache
        fp = np.memmap(self.__q_cache_f_name, dtype='uint8', mode="w+", shape=(q_cache.shape[0], q_cache.shape[1]))
        fp[:] = self.__q_cache[:]
        del fp

    def iterator(self):
        if not self.is_tempfile_existed:
            self._f_edgelist_name = self._get_tempfile_edgelist()

        while self.ka != 1 or self.kb != 1:
            ka_, kb_, m_e_rs_, diff_italic_i, mlist = self._moving_one_step_down(self.ka, self.kb)
            if abs(diff_italic_i) > self.i_0 * self.init_italic_i:
                self._update_current_state(ka_, kb_, m_e_rs_)
                desc_len_, _, _ = self._calc_and_update((self.ka, self.kb))
                if not self._is_this_mdl(desc_len_):
                    # merging predicates us to check (ka, kb), however, if it happens to have a higher desc_len
                    # then it is suspected to overshoot.
                    self.i_0 *= self.adaptive_ratio
                    ka_, kb_, _, desc_len_ = self._back_to_where_desc_len_is_lowest()
                is_local_minimum_found = self._check_if_local_minimum(ka_, kb_, desc_len_, self._k_th_nb_to_search)
                if is_local_minimum_found:
                    self._clean_up_and_record_mdl_point()
                    return self.confident_desc_len
            else:
                self._update_transient_state(ka_, kb_, m_e_rs_, mlist)

        self._check_if_random_bipartite()
        return self.confident_desc_len

    def clean(self):
        self.confident_desc_len = OrderedDict()
        self.confident_m_e_rs = OrderedDict()
        self.confident_italic_i = OrderedDict()
        self.trace_mb = OrderedDict()
        self.set_params(init_ka=10, init_kb=10, i_th=0.1)

    def compute_and_update(self, ka, kb, recompute=False):
        try:
            os.remove(self._f_edgelist_name)
        except FileNotFoundError as e:
            self._logger.warning("FileNotFoundError: {}".format(e))
        finally:
            self._f_edgelist_name = self._get_tempfile_edgelist()
            if recompute:
                self.confident_desc_len[(ka, kb)] = 0
            self._calc_and_update((ka, kb))

    @staticmethod
    def loky_executor(max_workers, timeout, func, feeds):
        assert type(feeds) is list, "[ERROR] feeds should be a Python list; here it is {}".format(str(type(feeds)))
        loky_executor = get_reusable_executor(max_workers=int(max_workers), timeout=int(timeout))
        results = loky_executor.map(func, feeds)
        return results

    @staticmethod
    def get_italic_i_from_m_e_rs(m_e_rs):
        assert type(m_e_rs) is np.ndarray, "[ERROR] input parameter (m_e_rs) should be of type numpy.ndarray"
        italic_i = 0.
        m_e_r = np.sum(m_e_rs, axis=1)
        num_edges = m_e_r.sum() / 2.
        for ind, e_val in enumerate(np.nditer(m_e_rs)):
            ind_i = int(math.floor(ind / (m_e_rs.shape[0])))
            ind_j = ind % (m_e_rs.shape[0])
            if e_val != 0.0:
                italic_i += e_val / 2. / num_edges * math.log(
                    e_val / m_e_r[ind_i] / m_e_r[ind_j] * 2 * num_edges
                )
        return italic_i

    @staticmethod
    def get_m_e_rs_from_mb(edgelist, mb):
        assert type(edgelist) is list, \
            "[ERROR] the type of the first input should be a list; however, it is {}".format(str(type(edgelist)))
        assert type(mb) is list, \
            "[ERROR] the type of the second input should be a list; however, it is {}".format(str(type(mb)))
        # construct e_rs matrix
        m_e_rs = np.zeros((max(mb) + 1, max(mb) + 1))
        for i in edgelist:
            # Please do check the index convention of the edgelist
            source_group = int(mb[int(i[0])])
            target_group = int(mb[int(i[1])])
            if source_group == target_group:
                raise ImportError("[ERROR] This is not a bipartite network!")
            m_e_rs[source_group][target_group] += 1
            m_e_rs[target_group][source_group] += 1

        m_e_r = np.sum(m_e_rs, axis=1)
        return m_e_rs, m_e_r

    @staticmethod
    @jit(Tuple((uint8, uint8, uint8[:, :], uint8[:]))(uint8, uint8, uint8[:, :]), cache=True, fastmath=True)
    def merge_matrix(ka, kb, m_e_rs):
        """
        Merge random two rows of the affinity matrix (dim = K) to gain a reduced matrix (dim = K - 1)
        Parameters
        ----------
        ka : int
            number of type-a communities in the affinity matrix
        kb : int
            number of type-b communities in the affinity matrix
        m_e_rs : numpy array
            the affinity matrix
        Returns
        -------
        new_ka : int
            the new number of type-a communities in the affinity matrix
        new_kb : int
            the new number of type-b communities in the affinity matrix
        c : numpy array
            the new affinity matrix
        merge_list : list(int, int)
            the two row-indexes of the original affinity matrix that were merged
        """
        assert type(m_e_rs) is np.ndarray, "[ERROR] input parameter (m_e_rs) should be of type numpy.ndarray"
        assert np.all(m_e_rs.transpose() == m_e_rs), "[ERROR] input m_e_rs matrix is not symmetric!"

        from_row = random.choices([0, ka], weights=[ka, kb], k=1)[0]
        a = m_e_rs[0:ka, ka:ka + kb]

        merge_list = list([0, 0])  # which two mb label should be merged together?
        mb_map = OrderedDict()
        new_ka = 0
        new_kb = 0

        if ka == 1:  # do not merge type-a rows (this happens when <i_th> is set too high)
            from_row = ka
        elif kb == 1:
            from_row = 0

        b = np.zeros([ka, kb])
        if from_row == 0:
            perm = np.arange(a.shape[0])
            np.random.shuffle(perm)
            for _ind in np.arange(a.shape[0]):
                mb_map[_ind] = perm[_ind]
            a = a[perm]

            new_ka = ka - 1
            new_kb = kb
            b = np.zeros([new_ka, new_kb])
            for ind_i, _a in enumerate(a):
                for ind_j, __a in enumerate(_a):
                    if ind_i < from_row:
                        b[ind_i][ind_j] = __a
                    elif ind_i > from_row + 1:
                        b[ind_i - 1][ind_j] = __a
                    elif ind_i == from_row:
                        b[ind_i][ind_j] += __a
                    elif ind_i == from_row + 1:
                        b[ind_i - 1][ind_j] += __a

            merge_list[0] = mb_map[from_row]
            merge_list[1] = mb_map[from_row + 1]

        elif from_row == ka:
            perm = np.arange(a.shape[1])
            np.random.shuffle(perm)
            for _ind in np.arange(a.shape[1]):
                mb_map[_ind] = perm[_ind]

            new_ka = ka
            new_kb = kb - 1
            a = a.transpose()

            a = a[perm]

            b = np.zeros([new_kb, new_ka])
            for ind_i, _a in enumerate(a):
                for ind_j, __a in enumerate(_a):
                    if ind_i < from_row - new_ka:
                        b[ind_i][ind_j] = __a
                    elif ind_i > from_row + 1 - new_ka:
                        b[ind_i - 1][ind_j] = __a
                    elif ind_i == from_row - new_ka:
                        b[ind_i][ind_j] += __a
                    elif ind_i == from_row + 1 - new_ka:
                        b[ind_i - 1][ind_j] += __a
            merge_list[0] = mb_map[from_row - ka] + ka
            merge_list[1] = mb_map[from_row + 1 - ka] + ka

        c = np.zeros([new_ka + new_kb, new_ka + new_kb])
        bt = b.transpose()
        if from_row == ka:
            b = bt
            bt = b.transpose()

        for ind_i, _c in enumerate(c):
            for ind_j, _ in enumerate(_c):
                if ind_j < new_ka <= ind_i:
                    c[ind_i][ind_j] = bt[ind_i - new_ka][ind_j]
                elif ind_i < new_ka <= ind_j:
                    c[ind_i][ind_j] = b[ind_i][ind_j - new_ka]

        assert new_ka + new_kb == c.shape[0], "new_ka = {}; new_kb = {}; new_mat.shape[0] = {}".format(
            new_ka, new_kb, c.shape[0]
        )
        assert new_ka + new_kb == c.shape[1], "new_ka = {}; new_kb = {}; new_mat.shape[1] = {}".format(
            new_ka, new_kb, c.shape[1]
        )
        assert np.all(c.transpose() == c), "Error: output m_e_rs matrix is not symmetric!"
        return new_ka, new_kb, c, merge_list

    def get_desc_len_from_data(self, na, nb, n_edges, ka, kb, edgelist, mb, diff=True, nr=None, allow_empty=False,
                            partition_dl_kind="distributed", degree_dl_kind="distributed", edge_dl_kind="bipartite"):
        """
        Description length difference to a randomized instance

        Parameters
        ----------
        na: `int`
            Number of nodes in type-a.
        nb: `int`
            Number of nodes in type-b.
        n_edges: `int`
            Number of edges.
        ka: `int`
            Number of communities in type-a.
        kb: `int`
            Number of communities in type-b.
        edgelist: `list`
            Edgelist in Python list structure.
        mb: `list`
            Community membership of each node in Python list structure.
        diff: `bool`
            When `diff == True`,
            the returned description value will be the difference to that of a random bipartite network. Otherwise, it will
            return the entropy (a.k.a. negative log-likelihood) associated with the current block partition.
        allow_empty: `bool`
        nr: `array-like`

        partition_dl_allow_empty: `bool` (optional, default: `False`)
        partition_dl_kind: `str` (optional, default: `"distributed"`)
            1. `partition_dl_kind == "uniform"`
            2. `partition_dl_kind == "distributed"` (default)


        degree_dl_kind: `str` (optional, default: `"distributed"`)
            1. `degree_dl_kind == "uniform"`
            2. `degree_dl_kind == "distributed"` (default)
            3. `degree_dl_kind == "entropy"`

        edge_dl_kind: `str` (optional, default: `"bipartite"`)
            1. `edge_dl_kind == "unipartite"`
            2. `edge_dl_kind == "bipartite"` (default)


        Returns
        -------
        desc_len_b: `float`
            Difference of the description length to the bipartite ER network, per edge.

        """
        edgelist = list(map(lambda e: [int(e[0]), int(e[1])], edgelist))

        italic_i = compute_profile_likelihood(edgelist, mb, ka=ka, kb=kb)
        desc_len = 0.

        # finally, we compute the description length
        if diff:  # todo: add more options to it; now, only uniform prior for P(e) is included.
            desc_len += (na * np.log(ka) + nb * np.log(kb) - n_edges * (italic_i - np.log(2))) / n_edges
            x = float(ka * kb) / n_edges
            desc_len += (1 + x) * np.log(1 + x) - x * np.log(x)
            desc_len -= (1 + 1 / n_edges) * np.log(1 + 1 / n_edges) - (1 / n_edges) * np.log(1 / n_edges)
        else:
            desc_len += fitting_entropy(edgelist, mb)
            # print("desc len from fitting {}".format(desc_len))
            desc_len += model_entropy(n_edges, ka=ka, kb=kb, na=na, nb=nb, nr=nr,
                                      allow_empty=allow_empty)  # P(e | b) + P(b | K)
            # desc_len += model_entropy(n_edges, k=ka+kb, n=na+nb, nr=nr,
            #                           allow_empty=allow_empty)  # P(e | b) + P(b | K)
            # P(k |e, b)
            ent = compute_degree_entropy(edgelist, mb, __q_cache=self.__q_cache, degree_dl_kind=degree_dl_kind)
            desc_len += ent
            # print("degree dl = {}".format(ent))
        return desc_len.__float__()

    @staticmethod
    def get_desc_len_from_data_uni(n, n_edges, k, edgelist, mb):
        """
        Description length difference to a randomized instance, via PRL 110, 148701 (2013).

        Parameters
        ----------
        n: `int`
            Number of nodes.
        n_edges: `int`
            Number of edges.
        k: `int`
            Number of communities.
        edgelist: `list`
            A list of edges.
        mb: `list`
            A list of node community membership.

        Returns
        -------
        desc_len_b: `float`
            Difference of the description length to the ER network, per edge.

        """
        italic_i = compute_profile_likelihood(edgelist, mb, k=k)

        # finally, we compute the description length
        desc_len_b = (n * np.log(k) - n_edges * italic_i) / n_edges
        x = float(k * (k + 1)) / 2. / n_edges
        desc_len_b += (1 + x) * np.log(1 + x) - x * np.log(x)
        desc_len_b -= (1 + 1 / n_edges) * np.log(1 + 1 / n_edges) - (1 / n_edges) * np.log(1 / n_edges)
        return desc_len_b

    @staticmethod
    def _calc_entropy_edge_counts(self):
        pass

    @staticmethod
    def _calc_entropy_node_degree(self):
        pass

    @staticmethod
    def _h_func(x):
        return (1 + x) * math.log(1 + x) - x * math.log(x)

    # def _cal_desc_len(self, ka, kb, italic_i):
    #     na = self.n_a
    #     nb = self.n_b
    #     e = self.e
    #     desc_len_b = na * math.log(ka) + nb * math.log(kb) - e * (italic_i - math.log(2))
    #     desc_len_b /= e
    #     x = float(ka * kb) / e
    #     desc_len_b += (1 + x) * math.log(1 + x) - x * math.log(x)
    #     desc_len_b -= (1. + 1. / e) * math.log(1. + 1. / e) - (1. / e) * math.log(1. / e)
    #     return desc_len_b

    def _calc_with_hook(self, ka, kb, old_desc_len=None):
        """
        Execute the partitioning code by spawning child processes in the shell; save its output afterwards.

        Parameters
        ----------
        ka : int
            Number of type-a communities that one wants to partition on the bipartite graph
        kb : int
            Number of type-b communities that one wants to partition on the bipartite graph

        Returns
        -------
        italic_i : float
            the profile likelihood of the found partition

        m_e_rs : numpy array
            the affinity matrix via the group membership vector found by the partitioning engine

        mb : list[int]
            group membership vector calculated by the partitioning engine

        """
        # each time when you calculate/search at particular ka and kb
        # the hood records relevant information for research
        try:
            self.confident_desc_len[(ka, kb)]
        except KeyError as _:
            pass
        else:
            if self.confident_desc_len[(ka, kb)] != 0:
                italic_i = self.confident_italic_i[(ka, kb)]
                m_e_rs = self.confident_m_e_rs[(ka, kb)]
                mb = self.trace_mb[(ka, kb)]
                self._logger.info("... fetch calculated data ...")
                return italic_i, m_e_rs, mb

        def run(ka, kb):
            mb = self.engine_(self._f_edgelist_name, self.n_a, self.n_b, ka, kb)
            return mb

        # Calculate the biSBM inference several times,
        # choose the maximum likelihood result.
        # In other words, we choose the state with minimum entropy.
        results = []
        if old_desc_len is None:
            if self.is_par_:
                # automatically shutdown after idling for 60s
                results = list(
                    self.loky_executor(self.n_cores_, 60, lambda x: run(ka, kb), list(range(self.max_n_sweeps_)))
                )
            else:
                results = [run(ka, kb)]
        else:
            old_desc_len = float(old_desc_len)
            if self.is_par_:
                self.__q_cache = np.array([], ndmin=2)
                results = list(
                    self.loky_executor(self.n_cores_, 60, lambda x: run(ka, kb), list(range(self.max_n_sweeps_)))
                )
            else:
                # if old_desc_len is passed
                # we compare the new_desc_len with the old one
                # --
                # this option is used when we want to decide whether
                # we should escape from the local minimum during the heuristic
                calculate_times = 0
                while calculate_times < self.max_n_sweeps_:
                    result = run(ka, kb)
                    results.append(result)
                    # new_desc_len = self._cal_desc_len(ka, kb, result[1])
                    nr = get_n_r_from_mb(result)
                    new_desc_len = self.get_desc_len_from_data(
                        self.n_a, self.n_b, self.e, ka, kb, list(self.edgelist), result, diff=False, nr=nr,
                        allow_empty=False)
                    if new_desc_len < old_desc_len:
                        # no need to go further
                        calculate_times = self.max_n_sweeps_
                    else:
                        calculate_times += 1

        max_e_r = self.__q_cache_max_e_r
        if old_desc_len is None and len(self.__q_cache) == 1:
            fp = np.memmap(self.__q_cache_f_name, dtype='uint8', mode="w+", shape=(max_e_r + 1, max_e_r + 1))
            self.__q_cache = init_q_cache(max_e_r, np.array([], ndmin=2))
            fp[:] = self.__q_cache[:]
            del fp
        else:
            self.__q_cache = np.memmap(self.__q_cache_f_name, dtype='uint8', mode='r', shape=(max_e_r + 1, max_e_r + 1))

        result_ = [self.__compute_desc_len(
            self.n_a, self.n_b, self.e, ka, kb, r
        ) for r in results]
        result = min(result_, key=lambda x: x[3])
        italic_i = result[0]
        m_e_rs = result[1]
        mb = result[2]
        return italic_i, m_e_rs, mb

    def __compute_desc_len(self, n_a, n_b, e, ka, kb, mb):
        m_e_rs, _ = self.get_m_e_rs_from_mb(self.edgelist, mb)
        italic_i = self.get_italic_i_from_m_e_rs(m_e_rs)
        nr = get_n_r_from_mb(mb)

        desc_len = self.get_desc_len_from_data(
            n_a, n_b, e, ka, kb, list(self.edgelist), mb, diff=False, nr=nr, allow_empty=False
        )
        return italic_i, m_e_rs, mb, desc_len

    def _moving_one_step_down(self, ka, kb):
        """
        Apply multiple merges of the original affinity matrix, return the one that least alters the entropy

        Parameters
        ----------
        ka : int
            number of type-a communities in the affinity matrix
        kb : int
            number of type-b communities in the affinity matrix

        Returns
        -------
        _ka : int
            the new number of type-a communities in the affinity matrix

        _kb : int
            the new number of type-b communities in the affinity matrix

        _m_e_rs : numpy array
            the new affinity matrix

        diff_italic_i : list(int, int)
            the difference of the new profile likelihood and the old one

        _mlist : list(int, int)
            the two row-indexes of the original affinity matrix that were finally chosen (and merged)

        """
        if self.init_italic_i == 0:
            # This is an important step, where we calculate the graph partition at init (ka, kb)
            _, m_e_rs, italic_i = self._calc_and_update((ka, kb))

            self.init_italic_i = italic_i
            self.m_e_rs = m_e_rs

        def _sample_and_merge():
            _ka, _kb, _m_e_rs, _mlist = self.merge_matrix(self.ka, self.kb, self.m_e_rs)
            _italic_I = self.get_italic_i_from_m_e_rs(_m_e_rs)
            diff_italic_i = _italic_I - self.init_italic_i  # diff_italic_i is always negative;
            return _ka, _kb, _m_e_rs, diff_italic_i, _mlist

        # how many times that a sample merging takes place
        indexes_to_run_ = range(0, (ka + kb) * self._size_rows_to_run)

        results = []
        for _ in indexes_to_run_:
            results.append(_sample_and_merge())

        _ka, _kb, _m_e_rs, _diff_italic_i, _mlist = max(results, key=lambda x: x[3])

        assert int(_m_e_rs.sum()) == int(self.e * 2), "__m_e_rs.sum() = {}; self.e * 2 = {}".format(
            str(int(_m_e_rs.sum())), str(self.e * 2)
        )

        return _ka, _kb, _m_e_rs, _diff_italic_i, _mlist

    def _check_if_random_bipartite(self):
        # if we reached (1, 1), check that it's the local optimal point, then we could return (1, 1).
        points_to_compute = [(1, 1), (1, 2), (2, 1), (2, 2)]
        for point in points_to_compute:
            self.compute_and_update(point[0], point[1], recompute=True)
        p_estimate = sorted(self.confident_desc_len, key=self.confident_desc_len.get)[0]

        if p_estimate != (1, 1):
            # TODO: write some documentation here
            raise UserWarning("[WARNING] merging reached (1, 1); cannot go any further, please set a smaller <i_th>.")
        self._clean_up_and_record_mdl_point()

    def _update_transient_state(self, ka_moving, kb_moving, t_m_e_rs, mlist):
        old_of_g = self.trace_mb[(self.ka, self.kb)]
        new_of_g = list(np.zeros(self.n))

        mlist.sort()
        for _node_id, _g in enumerate(old_of_g):
            if _g == mlist[1]:
                new_of_g[_node_id] = mlist[0]
            elif _g < mlist[1]:
                new_of_g[_node_id] = _g
            else:
                new_of_g[_node_id] = _g - 1
        assert max(new_of_g) + 1 == ka_moving + kb_moving, \
            "[ERROR] inconsistency between the membership indexes and the number of blocks."
        self.trace_mb[(ka_moving, kb_moving)] = new_of_g
        self._update_current_state(ka_moving, kb_moving, t_m_e_rs)

    def _check_if_local_minimum(self, ka, kb, old_desc_len, k_th):
        """The `neighborhood search` as described in the paper."""
        self.is_tempfile_existed = True
        items = map(lambda x: (x[0] + ka, x[1] + kb), product(range(-k_th, k_th + 1), repeat=2))
        # if any item has values less than 1, delete it. Also, exclude the suspected point.
        items = [(i, j) for i, j in items if i >= 1 and j >= 1 and (i, j) != (ka, kb)]
        ka_moving, kb_moving = 0, 0

        for item in items:
            self._calc_and_update(item, old_desc_len)
            if self._is_this_mdl(self.confident_desc_len[(item[0], item[1])]):
                p_estimate = sorted(self.confident_desc_len, key=self.confident_desc_len.get)[0]
                self._logger.info("Found {} that gives an even lower description length ...".format(p_estimate))
                ka_moving, kb_moving, _, _ = self._back_to_where_desc_len_is_lowest()
                break
        if ka_moving * kb_moving == 0:
            return True
        else:
            return False

    def _clean_up_and_record_mdl_point(self):
        try:
            os.remove(self._f_edgelist_name)
        except FileNotFoundError as e:
            self._logger.warning("FileNotFoundError: {}".format(e))
        finally:
            self.is_tempfile_existed = False
            p_estimate = sorted(self.confident_desc_len, key=self.confident_desc_len.get)[0]
            self._logger.info("DONE: the MDL point is {}".format(p_estimate))

    def _is_this_mdl(self, desc_len):
        """Check if `desc_len` is the minimal value so far."""
        return not any([i < desc_len for i in self.confident_desc_len.values()])

    def _back_to_where_desc_len_is_lowest(self):
        ka = sorted(self.confident_desc_len, key=self.confident_desc_len.get, reverse=False)[0][0]
        kb = sorted(self.confident_desc_len, key=self.confident_desc_len.get, reverse=False)[0][1]
        m_e_rs = self.confident_m_e_rs[(ka, kb)]
        self._update_current_state(ka, kb, m_e_rs)
        return ka, kb, m_e_rs, self.confident_desc_len[(self.ka, self.kb)]

    def _update_current_state(self, ka, kb, m_e_rs):
        self.ka = ka
        self.kb = kb
        self.m_e_rs = m_e_rs  # this will be used in _moving_one_step_down function

    def _calc_and_update(self, point, old_desc_len=0.):
        self._logger.info("Now computing graph partition at {} ...".format(point))
        if old_desc_len == 0.:
            italic_i, m_e_rs, mb = self._calc_with_hook(point[0], point[1], old_desc_len=None)
        else:
            italic_i, m_e_rs, mb = self._calc_with_hook(point[0], point[1], old_desc_len=old_desc_len)
        # candidate_desc_len = self._cal_desc_len(point[0], point[1], italic_i)
        nr = get_n_r_from_mb(mb)
        candidate_desc_len = self.get_desc_len_from_data(
            self.n_a, self.n_b, self.e, point[0], point[1], list(self.edgelist), mb, diff=False, nr=nr,
            allow_empty=False)

        self.confident_desc_len[point] = candidate_desc_len
        self.confident_italic_i[point] = italic_i
        self.confident_m_e_rs[point] = m_e_rs
        assert max(mb) + 1 == point[0] + point[1], "[ERROR] inconsistency between mb. indexes and #blocks."
        self.trace_mb[point] = mb
        self._logger.info("... DONE.")

        # update the predefined threshold value, DELTA:
        self.init_italic_i = italic_i

        return candidate_desc_len, m_e_rs, italic_i

    def _get_tempfile_edgelist(self):
        try:
            self.f_edgelist.seek(0)
        except AttributeError:
            self.f_edgelist = tempfile.NamedTemporaryFile(mode='w', delete=False)
        finally:
            for edge in self.edgelist:
                self.f_edgelist.write(str(edge[0]) + "\t" + edge[1] + "\n")
            self.f_edgelist.flush()
            f_edgelist_name = self.f_edgelist.name
            del self.f_edgelist
        return f_edgelist_name
