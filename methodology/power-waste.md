# Wasted energy: curtailment and gas flaring

OpenGrid measures two kinds of energy that is produced or available and then thrown away, because a small
datacenter placed next to it could use it: wind and solar output that grid operators curtailed, and natural gas
burned off at oil wells for lack of a pipeline (flaring). The figures rank candidate sites. They are internal: every
read is admin-only, and none of it is mixed into GPU prices, listings, indices or any GPU statistic.

Kind: **observed** (curtailment as reported, flared volumes), **estimated** (the ERCOT curtailment proxy), **derived**
(the electricity a generator could make from flared gas). Code: `power/sources/caiso_curtailment.py`,
`power/sources/spp_curtailment.py`, `power/sources/ercot_curtailment.py`, `power/flaring.py`, `power/waste.py`.
Reads: `GET /v1/ops/power/curtailment`, `GET /v1/ops/power/flaring` (admin only). Wholesale prices and the shared
storage rules are in [power markets](power-markets.md).

## Who may see what

Each dataset's terms decide whether it is pulled and its tier. A dataset whose terms forbid commercial use or
automated access is not pulled at all, not even for internal use. The verdicts live in code: the curtailment sources
in their modules (`TERMS`), the flaring sources in `power/flaring.py` (`SOURCES`), and the datasets considered and
declined in `power/registry.py` (`DECLINED`). `python -m power sources` and `/v1/ops/power/sources` print all three.

| Dataset | Pulled | Tier | Why (checked 2026-11-15) |
|---|---|---|---|
| CAISO production and curtailment workbooks + daily renewable reports | yes | internal | Same site terms as CAISO OASIS: no copying or redistribution except as authorised |
| SPP VER curtailments (marketplace file browser) | yes | internal | SPP terms: any commercial use needs SPP's written authorisation |
| ERCOT wind and solar production reports (NP4-732-CD, NP4-737-CD) | yes | public | ERCOT terms allow raw public data in compilations and analyses. The proxy itself is OpenGrid's arithmetic, labelled as an estimate |
| World Bank Global Gas Flaring Database v5 (Data Catalog 0037743, 2017-2021) | yes | public | "This dataset is licensed under Creative Commons Attribution 4.0". Attribution required |
| North Dakota Industrial Commission monthly production reports | yes | internal | Public reports, no account, no reuse licence stated (a disclaimer only), nothing forbids automated access. Redistribution is not clearly granted |
| EOG VIIRS Nightfire annual flare sites (2012-2025) | **no** | - | "License: CC BY-NC 4.0 ... Commercial use requires permission from the Earth Observation Group". Ranking sites for a commercial product is commercial use |
| World Bank flare files 2012-2025 (thedocs.worldbank.org) | **no** | - | No open licence on the files, so the World Bank site terms apply: "you may make non-commercial uses thereof, but you may not make any derivative work or commercial use" |
| Texas Railroad Commission PDQ dump | **no** (for now) | - | 3.45 GB monthly zip behind a session-based file portal. "RRC grants permission to copy and distribute the information on its website for noncommercial use". The PDQ query pages end sessions that use an automated tool |
| ERCOT production report history (api.ercot.com) | **no** | - | Needs an ERCOT account (subscription key). No account is created |

## Curtailment

Curtailment is stored as power series in `power_series` / `power_days` (kind `curtailment`). This needs no table of
its own: there is one row per series and market day, and an hourly `real[]` of MWh.

### CAISO (from 2014-05-01)

- **2014-05-01 to 2025-12-31: CAISO's "Production and Curtailments Data" workbooks.** There are ten files of
  21-68 MB each. They hold five-minute MW, and an hour's MWh is the sum of its intervals' MW / 12.
  - Curtailment is split by fuel and by reason. **Local** means transmission congestion; **system** means
    system-wide oversupply. The reason split runs from 2017-06; the 2014-05..2017-05 file gives totals only.
  - Only intervals with a curtailment are listed, so an hour with nothing listed is 0, not a gap.
  - The workbooks also give the wind and solar output CAISO took. CAISO's read-me says curtailment is not counted in
    production.
  - CAISO stopped the workbooks on 2025-06-01 and then posted one bulk curtailment file for all of 2025.
- **From 2025-06-01: the daily renewable report.** This is an HTML page posted around 10:00 PT the next day.
  - Its hourly MWh arrays are split by fuel, by reason and by dispatch type: economic bids, self-schedule cuts and
    operator instructions.
  - It also carries the metered five-minute output of the CAISO balancing area.
- **Checks by hand:**
  - Summed this way, the bulk 2025 file gives 130,267 MWh for January 2025, the same as CAISO's own monthly chart
    CSV.
  - For 2025-06-14, the report's hourly figures by fuel and reason equal the workbook's hour by hour, within
    rounding.
- **Two output series, never joined.** The workbook output (`generation`) and the report's telemetry
  (`generation_telemetry`) differ in level: at noon on 2025-06-14, solar was 20,399 vs 18,130 MWh. The curtailed
  share uses the workbook output where it exists and telemetry after that, so the share has a break at 2026-01-01.
- **Time and DST.**
  - Workbooks: hour ENDING, Pacific prevailing time. The fall-back day lists hour 2 twice (the second pass is the
    repeated hour), and the spring day has no hour 3.
  - Daily report: it sits on a fixed 24-hour wall-clock axis. On the spring day the missing 02:00 hour is dropped.
    On the fall-back day the repeated hour cannot be separated and stays NULL.

### SPP (from 2014-03)

- **Source:** SPP's VER curtailment files: one CSV per operating day, plus a zip per year through 2024. The year
  zips' monthly and annual roll-ups repeat the daily intervals and are not read.
- **Format:** five-minute, interval ending, in MW.
- **What is split:**
  - Fuel: wind and solar. The 2014 files have wind only; every 2024 file has solar columns, though some days leave
    them blank. The year solar columns first appear was not checked.
  - Reason: **redispatch** (the real-time market dispatched the unit down) and **manual** (an operator instruction
    for reliability).
  - **Curtailed for energy** is the oversupply part of redispatch. It is stored as its own series and never added
    to the total.
- **Blank values** are not given and are stored as NULL, never 0. On 2024-03-10, two whole hours are blank.
- **Understatement:** a plant's own economic self-curtailment is not in these files, so they understate
  curtailment.

### ERCOT (a proxy, from the day the forward job first runs)

- **Why a proxy:** ERCOT publishes no curtailment figure.
- **Source:** its hourly wind and solar production reports carry the system-wide actual output and the system-wide
  HSL (high sustained limit). HSL is what the plants report they could produce now; ERCOT calls it "uncurtailed
  power generation potential".
- **The proxy:**
  - It is max(0, HSL - output) per hour.
  - Solar hours with output below 50 MW count as 0. At night, solar HSL sits near 23 MW against about 0.5 MW of
    output, which is noise.
  - Every row is labelled `measure = proxy_hsl_minus_output`.
- **What it counts and misses:**
  - It counts economic dispatch below HSL, plus manual and congestion curtailment.
  - It also counts tracking and telemetry error as curtailment.
  - It misses a plant that lowers its own HSL to self-curtail.
- **System-wide only:** the regional HSL columns are the plants' operating plan, not actual HSL.
- **No history:** ERCOT keeps the documents about 7 days, and the archive needs an account. A week of missed runs is
  lost.

### Reads

`/v1/ops/power/curtailment?iso=&fuel=&since=&until=` returns one row per series and day: the curtailed MWh, fuel,
scope and type.

Rows that cover all reasons also carry:
- the fuel's output where it is known;
- `share_curtailed` = curtailed / (curtailed + output), the part of what the fleet could have produced that was
  thrown away.

`power.waste.curtailed_mwh(iso, since, until, fuel=None)` returns a region's total over a window as a plain dict,
for the site-ranking work.

## Gas flaring

| Table | One row per | Holds |
|---|---|---|
| `power_flare_sites` | site | coordinates (WGS84), country (ISO alpha-3), state, county, kind |
| `power_flare_volumes` | site and year | volume in m3 and MCF, the twelve months where the source is monthly, months reported, derived energy |
| `power_flare_areas` | county and month (North Dakota) | flared MCF, gas produced, wells flaring, the flared share |

Raw first: every file read is a `power_fetches` row with its URL and the SHA-256 of the bytes.

### World Bank Global Gas Flaring Database v5 (2017-2021)

- **Source:** one workbook per year. Every detected flare has its latitude, longitude, country, type (upstream oil,
  upstream gas, midstream, refinery, LNG ...) and BCM flared. The values are satellite estimates (VIIRS Nightfire).
- **What counts as a site:**
  - A flare's position moves slightly between years, and its id changes between releases. A site is therefore a
    0.01 degree cell (about 1.1 km north-south).
  - Flares in one cell in a year are summed. The cell keeps the volume-weighted position of the latest year loaded.
- **What is read:** only the per-flare sheets. Country summaries are skipped, and so is the 2019 "all flares"
  sheet, which repeats the upstream and downstream sheets.
- **Measured totals:** 151.4, 155.0, 160.9, 152.6 and 156.6 BCM for 2017 to 2021. The United States had 9.93 BCM in
  2021.

### North Dakota (monthly, from 2015-05)

- **Source:** NDIC's monthly production report. It lists every producing well with its county, PLSS location,
  coordinates, gas produced and gas flared (MCF), and is posted about six weeks after the month.
- **What counts as a site:** the PLSS section (one square mile) holding the flaring wells. Its position is the mean
  of those wells' coordinates. Its year row holds the twelve months; a month the section flared nothing in is 0.
- **County rows** carry the gas produced as well, so the flared share is known.
- **Caveat: confidential wells are left out of the report.** In August 2026 the report's oil summed to 31.73
  million barrels, against NDIC's statewide 36.00 million, so state and county totals here understate by roughly
  that much.
- **Check by hand:** Bowman County in August 2026 flared 159,584 MCF of 214,976 MCF produced (74%).

### Energy (derived)

- **What it is:** the electricity a gas generator could make from the flared gas. It is never observed.
- **Formula:** MWh = MCF x 0.1037, from:
  - a heat content of 1,037 Btu per cubic foot (EIA's U.S. average). Flared associated gas is usually richer
    (1,200-1,500), so this is conservative;
  - a generator heat rate of 10,000 Btu/kWh (a reciprocating gas genset or small turbine, about 34% efficient).
- **Scale:** 1 BCM is about 3.66 TWh, or 418 MW all year.

### Reads

`/v1/ops/power/flaring` has three views:
- **Default:** sites ranked by flared volume, with coordinates. Filter by `country`, `state`, `year` or `source`.
  The default year is each source's latest.
- **`view=counties`:** North Dakota counties for the latest month.
- **`lat`, `lon`, `radius_km`:** the volume near a point. The sites are not merged across sources, because a North
  Dakota section and a satellite cell can be the same flares measured twice.

The same reads are available as functions in `power.waste`: `flare_sites`, `flared_near` and `flared_by_county`.
They return plain dicts.

## Loading

```
python -m power backfill --source caiso_curtailment            # 2014-05 .. yesterday (resumable)
python -m power backfill --source spp_curtailment               # 2014-03 .. yesterday
python -m power backfill --source ercot_curtailment             # the last ~7 days, all ERCOT keeps
python -m power flaring                                         # World Bank 2017-2021 + North Dakota 2015-05 on
```

Forward runs:
- **Curtailment:** the `power_forward` job runs twice a day for every selected source.
- **Flaring:** the same job runs it at most once a week, because the reports are monthly. That run re-reads the
  last two North Dakota months and asks for the next one.

Re-runs store nothing new:
- A stored day, World Bank file or North Dakota month is not fetched again.
- A re-read that changed nothing is counted `unchanged`.
