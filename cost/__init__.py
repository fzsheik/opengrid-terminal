"""Full cost: what a job costs beyond the hourly price (methodology/full-cost.md).

    cost.sources     fetch machine-readable cost sources, store them raw (cost_snapshots); daily job
    cost.components  normalise API, provider-raw and hand-maintained cost facts into one component shape
    cost.manual      the hand-maintained table: every value with its source page and verified-at date
    cost.estimate    listing spec fields and the reference-job estimator (kind: estimated)

Read-only for the market: nothing here enters listings, indices, spreads, percentiles or routing.
"""
