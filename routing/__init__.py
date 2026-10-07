"""Routing: capability registry, best-execution scoring, route preview/execute, deployments.

    capabilities  what each provider's API allows vs what OpenGrid implements
    scoring       rank_listings(): the transparent weighted best-execution model
    engine        preview() / route(): validate, rank, check, quote, (gated) provision, failover
    adapters/     one execution adapter per provider API
    deployments   deployment lifecycle (status, stop, terminate) and events
    audit         route_requests + routing_decisions
    transactions  execution records and the billing usage hook
    tracker       background job polling live deployments
"""
