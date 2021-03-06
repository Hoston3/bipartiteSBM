import os
import re
import shutil
import subprocess
import hashlib
from collections import OrderedDict
import random
import numpy as np


class KL(object):
    """Base class for the Kernighan-Lin algorithm.

    Parameters
    ----------
    f_engine : ``str`` (required, default: ``"engines/bipartiteSBM-KL/biSBM"``)

    n_sweeps : ``int`` (required, default: ``4``)
        Note that this will generate ``<n_sweeps>`` output sub-folders in ``<f_kl_output>``.

    is_parallel : ``bool`` (required, default: ``True``)

    n_cores : ``int`` (required, default: ``4``)

    algm_name : ``str`` (required, default: ``kl``)

    kl_edgelist_delimiter : ``str`` (required, default: ``"\\t"``)
        Due to the KL code accepts 1-indexed nodes by default, we used the delimiter to transform our 0-indexed input.

    kl_steps : ``int`` (required, default: ``5``)
        Number of random initializations (refer to the README_cplusplus.txt file)

    kl_itertimes : ``int`` (required, default: ``1``)
        Number of KL runs (within each ``<outputFOLDER>``) for returning an optimal result.

    f_kl_output : ``str`` (required, default: ``"engines/bipartiteSBM-KL/f_kl_output"``)
        Path to the KL output dir; recommended to be in the same folder as the binary.

    kl_verbose : ``bool`` (required, default: ``True``)

    kl_is_parallel : ``bool`` (required, default: ``False``)

    """
    def __init__(self,
                 f_engine="engines/bipartiteSBM-KL/biSBM",
                 n_sweeps=1,
                 is_parallel=False,
                 n_cores=1,
                 algm_name="kl",
                 kl_edgelist_delimiter="\t",
                 kl_steps=5,
                 kl_itertimes=1,
                 f_kl_output="engines/bipartiteSBM-KL/f_kl_output",
                 kl_verbose=True,
                 kl_is_parallel=False):

        self.MAX_NUM_SWEEPS = int(n_sweeps)
        self.PARALLELIZATION = bool(is_parallel)
        self.NUM_CORES = int(n_cores)
        self.KL_PARALLELIZATION = bool(kl_is_parallel)
        self.ALGM_NAME = str(algm_name)

        # <kl_itertimes> is the number of KL sweeps (<kl_steps> per sweep) performed before returning the optimal result
        # during each engine run (Note that there are <n_sweeps> engines running in parallel via loky)
        self.MAX_KL_NUM_SWEEPS = int(kl_itertimes)

        if self.KL_PARALLELIZATION:
            raise NotImplementedError("[ERROR] KL calculation accross many cores is not supported.")

        # for KL
        if not os.path.isfile(f_engine):
            raise BaseException("[ERROR] KL engine binary not found!")

        self.f_engine = f_engine
        self.kl_steps = int(kl_steps)

        self.f_kl_output = str(f_kl_output)
        self.kl_verbose = bool(kl_verbose)
        self.kl_edgelist_delimiter = kl_edgelist_delimiter

        pass

    def prepare_engine(self, f_edgelist, na, nb, ka, kb, delimiter=None):
        """Output shell commands for graph partitioning calculation.

        Parameters
        ----------
        ka : ``int`` (required)
            Number of communities for type-`a` nodes to partition.

        kb : ``int`` (required)
            Number of communities for type-`b` nodes to partition.

        Returns
        -------
        action_str : ``str``
            The command line string that enables execution of the code.

        """
        if delimiter is None:
            delimiter = self.kl_edgelist_delimiter

        try:
            os.mkdir(self.f_kl_output)
        except OSError:
            pass
        finally:
            self.f_kl_output += "/" + hashlib.md5(str(random.random()).encode()).hexdigest()
            try:
                os.mkdir(self.f_kl_output)
            except OSError:
                # clear the working dir, and mkdir a new one
                shutil.rmtree(self.f_kl_output, ignore_errors=True)
                os.mkdir(self.f_kl_output)
                pass

        filename = hashlib.md5(f_edgelist.encode()).hexdigest()
        f_edgelist_1_indexed = self.f_kl_output + "/" + filename + "_1-indexed.edgelist"
        self._save_edgelist_as_1_indexed(f_edgelist, f_edgelist_1_indexed, delimiter)

        f_types = self.f_kl_output + "/" + filename + ".types"
        self.types = self._save_types(f_types, na, nb)

        action_list = [
            self.f_engine,
            f_edgelist_1_indexed,
            f_types,
            self.f_kl_output,
            str(ka),
            str(kb),
            '1',  # degree-corrected
            str(self.kl_steps)
        ]

        action_str = ' '.join(action_list)

        return action_str

    def engine(self, f_edgelist, na, nb, ka, kb, mb=None):  # TODO: bug when assigned verbose=False
        """Run the shell code.

        Parameters
        ----------
        ka : ``int`` (required)
            Number of communities for type-`a` nodes to partition.

        kb : ``int`` (required)
            Number of communities for type-`b` nodes to partition.

        Returns
        -------
        of_group : ``list[int]``

        """
        action_str = self.prepare_engine(f_edgelist, na, nb, ka, kb)

        num_sweeps_ = self.MAX_KL_NUM_SWEEPS
        verbose_ = self.kl_verbose
        parallelization_ = self.KL_PARALLELIZATION
        num_cores_ = self.NUM_CORES

        if not verbose_:
            stdout = open(os.devnull, "w")
        else:
            stdout = subprocess.PIPE

        def run(_):
            p = subprocess.Popen(
                action_str.split(' '),
                bufsize=2048,
                stdout=stdout
            )
            out, err = p.communicate()
            p.wait()
            return out, err, p

        kl_output = OrderedDict()
        num_sweep_ = 0
        while num_sweep_ < num_sweeps_:
            if not parallelization_:
                out, err, p = run("")
                if p.returncode == -11:  # when Exception raises from the KL code
                    raise RuntimeError("[ERROR] Exception from C++ program during inference! -- " + action_str)
                elif p.returncode == 0:
                    num_sweep_ += 1
                    assert type(self._get_score_by_index(num_sweep_)) == float
                    kl_output[self._get_score_by_index(num_sweep_)] = self._get_of_group_by_index(num_sweep_)

            else:  # spawn processes across cores, collect results, and return the best option
                # However, in optimalks.py main code, we may calculate each single point in parallel, which
                # might raise "AssertionError: daemonic processes are not allowed to have children"
                # TODO: For now, parallel calculation for KL is disabled. (Fix it?)
                raise NotImplementedError

        of_group = kl_output[max(kl_output)]

        try:
            shutil.rmtree(self.f_kl_output, ignore_errors=True)
        finally:
            return np.array(of_group)

    @staticmethod
    def gen_types(na, nb):
        types = [1] * int(na) + [2] * int(nb)
        return types

    def _get_of_group_by_index(self, num_sweep_):
        of_group = []
        f = self._open_biDCSBMcomms_file(num_sweep_)
        for ind, line in enumerate(f):
            of_group.append(int(line.split('\n')[0]))
        f.close()
        return of_group

    def _get_score_by_index(self, num_sweep_):
        f = self._get_bisbm_score_file(num_sweep_)
        for ind, line in enumerate(f):
            score = float(line.split('\n')[0])
        f.close()
        return score

    def _get_bisbm_score_file(self, num_sweep_):
        """:return: file handle"""
        f = open(
            self.f_kl_output + '/biDCSBMcomms' + str(int(num_sweep_)) + '.score', 'r'
        )
        return f

    def _open_biDCSBMcomms_file(self, num_sweep_):
        """:return: file handle"""
        f = open(
            self.f_kl_output + '/biDCSBMcomms' +
            str(int(num_sweep_)) + '.tsv', 'r'
        )
        return f

    @staticmethod
    def _save_edgelist_as_1_indexed(f_edgelist, f_target_edgelist, delimiter="\t"):
        """

        Note that this function always saves with delimiter.

        Parameters
        ----------
        f_edgelist : ``str``

        f_target_edgelist : ``str``

        delimiter : ``str``

        Returns
        -------

        """
        with open(f_target_edgelist, "w") as g:
            with open(f_edgelist, "r") as f:
                for line in f:
                    line = line.replace('\r', '').replace('\n', '')
                    edge = re.split(delimiter, line)
                    try:
                        g.write(str(int(edge[0]) + 1) + "\t" + str(int(edge[1]) + 1) + "\n")
                    except ValueError as e:
                        raise ValueError(
                            "[ERROR] Please check if the delimiter for the edgelist file is wrong -- {}".format(e)
                        )

    @staticmethod
    def _save_types(f_types, na, nb):
        assert na > 0, "[ERROR] Number of type-a nodes = 0, which is not allowed"
        assert nb > 0, "[ERROR] Number of type-b nodes = 0, which is not allowed"
        types = [1] * int(na) + [2] * int(nb)
        with open(f_types, "w") as f:
            for line in types:
                f.write(str(line) + '\n')
        return types
