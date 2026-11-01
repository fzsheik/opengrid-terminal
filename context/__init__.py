"""Market context: numeric series that explain the backdrop of GPU prices (methodology/market-context.md).

    context.eia     EIA API v2, retail electricity price by US state and sector, monthly (public domain)
    context.edgar   SEC EDGAR XBRL: NVIDIA / AMD data-center and total revenue, hyperscaler + CoreWeave capex
    context.geo     provider region -> US state -> wholesale market area
    context.series  fetch, store raw, parse, read; the context_series job

Context, never cause, and never an input to listings, indices, spreads, percentiles or routing.
"""
