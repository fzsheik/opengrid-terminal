"""Power source modules. Each declares SOURCE, TERMS, DATASETS, DEFAULT_ON and REQUIRES (power/registry.py).

Adding a source: write power/sources/<name>.py and add its name to MODULES (one line; nothing else to edit).
"""

MODULES = (
    "caiso",
    "eia",        # EIA-930 hourly grid data (public)
    "eia_gas",    # EIA natural gas (internal by choice)
    "nyiso",      # NYISO zonal LBMP (no signup; internal)
    "miso",       # MISO hub LMP; off: its site terms forbid automated access (Data Exchange API is the allowed route)
    "spp",        # SPP Integrated Marketplace hub LMPs (no signup; internal)
    "ercot",      # ERCOT MIS settlement point prices, hubs + load zones (no signup; public)
    "pjm",        # PJM Data Miner 2 hub + zone LMPs (free key; internal)
    "isone",      # ISO-NE ISO Express hub + zone LMPs (free account; internal)
)
