from det_k_bisbm.ioutils import *
from det_k_bisbm.optimalks import *

from engines.mcmc import *


mcmc = MCMC(f_engine="engines/bipartiteSBM-MCMC/bin/mcmc",
            n_sweeps=4,
            is_parallel=True,
            n_cores=4
        )

edgelist = get_edgelist("dataset/test/bisbm-n_1000-ka_4-kb_6-r-1.0-Ka_30-Ir_1.75.gt.edgelist", "\t")
types = mcmc.gen_types(500, 500)

oks = OptimalKs(mcmc, edgelist, types, default_args=True, random_init_k=False)


def test_answer():
    oks.iterator()
    ka = oks.summary()["ka"]
    kb = oks.summary()["kb"]
    # Note that we may not obtain (4, 6), as non-identifiable blocks may exist.
    assert (ka, kb) in [(4, 6)]
