"""The physical side of siting a small datacenter on spare power: where plants are, how much they run, which are
retiring, and what generation is queued to connect (methodology/power-markets.md, "Physical sites").

    registry   the sources and their terms verdicts (EIA public; LBNL per edition; ERCOT public)
    eia860m    generator inventory: API v2 monthly operable inventory (2008 onward) + the newest EIA-860M file
    eia923     annual net generation per plant (API v2 facility-fuel)
    lbnl       Berkeley Lab Queued Up, one workbook per year (every US queue)
    ercot_gis  ERCOT's monthly Generator Interconnection Status report
    xlsx       a streaming xlsx reader on the standard library
    store      change-only writes; runner: sync / status / the weekly power_sites job; reads: plain-dict reads
    `python -m power.sites sync|status|sources`

Own tables only (power_generators, power_generator_history, power_plant_generation, power_queue_projects; fetches
in power_fetches as dataset "sites.*"). Nothing here touches GPU data or the power_series grid.
"""
