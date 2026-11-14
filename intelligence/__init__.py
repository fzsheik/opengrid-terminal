"""Workload intelligence: ONE server-side engine every interface uses (docs/OPENGRID2_CONTRACTS.md).

    intelligence.requirements     WorkloadRequirements (the request body, contract section 1)
    intelligence.models_catalog   well-known open model configs, each with a source URL; lookup(name)
    intelligence.memory           modular VRAM estimator (weights, gradients, optimizer, activations, KV cache,
                                  framework overhead; sharding; margin) -> memory dict
    intelligence.estimate         estimate(req) -> Estimate (contract section 2); pure, no DB
    intelligence.recommend        recommend(req, who, *, limit=10, now=None) -> Recommendation (section 3);
                                  read-only market ranking via routing.scoring.rank_listings
    intelligence.performance      theoretical throughput, scaling, runtime and whole-job cost per option
    intelligence.workload_fit     interconnect evidence, parallelism, communication, inference roofline, multi-GPU
    intelligence.measured         completed REAL jobs -> measured observations -> preferred over theoretical
    intelligence.explain          per-option confidence, factor deltas vs the recommended option, the explanation

Import the submodules directly; this package imports nothing heavy. See methodology/recommendations.md.
"""
