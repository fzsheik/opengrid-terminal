"""Workload intelligence: ONE server-side engine every interface uses (docs/OPENGRID2_CONTRACTS.md).

    intelligence.requirements     WorkloadRequirements (the request body, contract section 1)
    intelligence.models_catalog   well-known open model configs, each with a source URL; lookup(name)
    intelligence.memory           modular VRAM estimator (weights, gradients, optimizer, activations, KV cache,
                                  framework overhead; sharding; margin) -> memory dict
    intelligence.estimate         estimate(req) -> Estimate (contract section 2); pure, no DB
    intelligence.recommend        recommend(req, who, *, limit=10, now=None) -> Recommendation (section 3);
                                  read-only market ranking via routing.scoring.rank_listings

Import the submodules directly; this package imports nothing heavy. See methodology/recommendations.md.
"""
