"""Wholesale power markets: US hub / zone electricity prices and balancing-authority grid data
(methodology/power-markets.md).

    registry   the sources, their terms verdicts (public | internal) and their datasets
    sources/   one module per source (CAISO, EIA, ...)
    http       polite paced GETs, one at a time per source
    storage    the market-day grid, power_series / power_days / power_fetches
    runner     backfill / forward / status; the power_forward job
    `python -m power backfill|forward|status`

Power data lives in power_* tables only. It is never written into listings, rollups, indices, fixings or archived
prices and never votes in a GPU statistic. Internal-tier series (every ISO whose terms do not clearly permit
redistribution) are read by admins and the derived cross-analysis only.
"""
