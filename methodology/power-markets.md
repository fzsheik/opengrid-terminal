# Power markets

OpenGrid reads US wholesale electricity prices and grid data. The price series are hub-level and zone-level. For
ERCOT and SPP, every node is also summarised once a day (see "Nodal daily summaries"), but interval prices are
never stored per node. The grid data is hourly demand, generation and interchange per balancing authority. Each source stores the
data in its own tables (`power_*`), and the data is analysed beside GPU prices and capacity without being mixed
into them. Power data is never written into listings, rollups, indices, fixings or archived prices. It never votes
in a GPU statistic, and nothing here says that a power price moved a GPU price.

Wind and solar curtailment and gas flaring (energy thrown away that a small datacenter could use) are in
[wasted energy](power-waste.md): curtailment is stored in the same power series tables, flaring in its own.
The [site ranking](power-siting.md) joins the prices, the nodal summaries, the physical sites, the wasted energy and
the EIA-930 series into a county ranking for small datacenters on spare power.

Kind: **observed** (the series), **derived** (the cross-analysis). Code: `power/`. There is no public power route: every read is admin-only, under `/v1/ops/power/`
(sources, status, series, map, cross/regional, cross/leadlag, shifts, nodes/cheap).

## Who may see what

Each source's terms of use decide its tier. The founder's rule: data OpenGrid may not redistribute is still pulled
and used internally (for the cross-analysis and admin research), and it is **never exposed publicly**. The tier is
decided in one place, `power/registry.py`, per source. `python -m power sources` and `/v1/ops/power/sources` print
it. A source is public only where its terms clearly permit redistribution, and every other source is internal.
Every public read re-checks the registry, so tightening a verdict hides the data at once.

A second rule: a source whose terms forbid automated access is not pulled at all. Only an official API may be used
for such a source.

| Source | What OpenGrid reads | Tier | Why (terms, checked 2026-11-05) | Signup |
|---|---|---|---|---|
| **EIA API v2** (EIA-930) | Hourly demand, net generation, total interchange and day-ahead demand forecast per balancing authority and EIA region, plus net generation by fuel | **public** | U.S.-government work, in the public domain. EIA asks for attribution ([copyright and reuse](https://www.eia.gov/about/copyrights_reuse.php)) | Free API key (`EIA_API_KEY`) |
| **CAISO OASIS** | Day-ahead (hourly) and real-time (5-minute) LMP at the three trading hubs (NP15, SP15, ZP26) and the four default load aggregation points | internal | The site terms forbid copying or redistributing material except as authorised. The API terms say CAISO owns all CAISO Data and bar use "for any other benchmarking or competitive purposes" ([terms](https://www.caiso.com/privacy-terms-of-use)). Redistribution is not clearly permitted | None |
| **NYISO** (mis.nyiso.com public CSV) | Zonal day-ahead and real-time LBMP | per its module (`power/sources/nyiso.py`) | Recorded in the registry | None |
| **MISO** | Hub LMPs | **not pulled** | MISO's website terms forbid automated access ("You agree not use any automated means ... to access, monitor, or copy any part of this Website"). The public-file module stays `DEFAULT_ON = False`. **Do not enable it.** The allowed route is MISO's Data Exchange API (data-exchange.misoenergy.org, subscription key), which is not implemented yet | Data Exchange API subscription |
| **SPP** (marketplace files) | Hub LMPs | per its module | Recorded in the registry | None |
| **ERCOT** | Hub and load-zone settlement point prices, day-ahead and real-time (15-minute) | public; on by default | Read from ERCOT's keyless public MIS reports (no account) | None |
| **PJM** (Data Miner 2) | Hub and zone LMPs | per its module; off by default | Needs a free API key | [PJM API portal](https://apiportal.pjm.com): register, subscribe to Data Miner 2, then set `PJM_API_KEY` |
| **ISO-NE** (web services) | Hub and zone LMPs | per its module; off by default | Needs an ISO Express account | [ISO Express](https://www.iso-ne.com): register, then set `ISONE_USERNAME` and `ISONE_PASSWORD` (web services at webservices.iso-ne.com) |

Internal-tier series are readable only by admins, with `Cache-Control: private, no-store`. They never appear on a
public page, a public API or a keyed (non-admin) API. `tests/test_visibility.py` calls every public route and fails
if an internal power value shows. `tests/test_visibility.py` also asserts that no `/v1/power` route exists. Other internal-only datasets kept by choice are documented
outside the public site.

## Granularity and time

- **Interval prices are never stored per node; daily summaries are.** Nodal interval data runs to terabytes, so
  the interval series are hub and zone series. A hub or zone price is what a region's wholesale power costs. Where
  power is stranded is a nodal question, so ERCOT and SPP nodes get one summary row per node and day (see "Nodal
  daily summaries" below).
- **One row per (series, market day).** A row holds the day's values as a `real[]`. Slot *i* is the interval that
  starts at the market day's local midnight + *i* × interval, in absolute time:
  - an hourly day has 24 slots (23 or 25 on DST days);
  - a 5-minute day has 288 slots (276 or 300).
- The market day is the operator's own operating day, in local prevailing time (CAISO: Pacific). EIA-930 days are
  UTC days. EIA hours are **hour ending**: EIA's period T01 is 00:00 to 01:00 UTC, stored as slot 0.
- A value the source did not give is NULL, never 0 and never carried over. A day whose answer succeeded but has
  gaps at the source keeps them as NULL slots (`n` below the slot count, `complete` false). It is not re-asked once
  older than the overlap window.
- Each row also stores the day's mean, min and max, so day-level reads never unnest.
- Revisions: the forward run re-reads the last few days (CAISO 2, EIA 5). A day whose values changed is replaced,
  and the change is counted (`revisions`).

Depth, measured 2026-11-05:
- **EIA-930:** from 2019-01. The backfill asks from 2021-01.
- **CAISO OASIS:** a rolling ~39 months (from 2023-08). OASIS answered "no data" for 2023-07 and earlier, so CAISO
  cannot reach 2021 from OASIS.

## Raw first

Every request is stored in `power_fetches`, failures and "no data" answers too. The row holds the URL (keys
redacted), the status, the byte count, the SHA-256 of the exact bytes received and the fetch time. The bytes
themselves are kept only where they are small per day, and only on forward fetches:

| Dataset | Bytes per day (measured) | Forward keeps | Why |
|---|---|---|---|
| CAISO day-ahead | 8 KB zipped (7 nodes) | the zip as received | small |
| CAISO real-time | 113 KB zipped (7 nodes) | URL + SHA-256 | about 125 MB a year; OASIS serves the past 39 months for re-fetching |
| EIA-930 region and fuel | about 1.5 MB of JSON (about 100 KB gzipped) over 4 to 5 pages | URL + SHA-256 | EIA keeps the full history online, and the data is public domain: any day can be re-fetched |

- A forward re-read that changed nothing keeps the URL and SHA-256 only. Its data is already stored, backed by the
  earlier answer's bytes.
- **The backfill keeps URL + SHA-256 only, for every source.** Re-fetch to verify.
- `power_days.fetch_id` points at the answer the stored values came from.

## Storage, measured, and the 3-year projection

Measured on real rows (the live smoke test, scaled to tens of thousands of rows). Each figure includes the heap, the
primary key and the `day` index.

| Row kind | Bytes per series-day |
|---|---|
| Hourly (24 values: EIA-930, CAISO day-ahead) | 246 to 262 |
| 5-minute (288 values: CAISO real-time) | 1,417 (the `real[]` of 288 is 1,183 bytes, below the TOAST threshold, so not compressed) |
| Daily, weekly or monthly value (one value) | 158 |
| One `power_fetches` row without bytes | about 575 |

Projection: the backfill from 2021-01 (EIA) or 2023-08 (CAISO) to 2026-11, plus 3 years forward (3,229 days for
EIA).

| Part | Series-days per day | Rows | Size |
|---|---|---|---|
| EIA-930 region-data (measured 6,674 values/day) | 278 | 0.90 M | 225 MB |
| EIA-930 fuel-type-data (measured 11,112 values/day) | 463 | 1.50 M | 370 MB |
| CAISO day-ahead + real-time, 7 nodes each | 14 | 32 k | 27 MB |
| Allowance for the other ISOs' modules: 60 hourly + 60 five-minute hub/zone series | 120 | 0.39 M | 325 MB |
| Internal-only datasets kept by choice | | | 10 MB |
| `power_fetches` (about 8.5 k backfill pages + about 60 rows a day forward) | | 75 k | 45 MB |
| Raw bytes kept (CAISO day-ahead and small sources, about one copy per new day) | | | 40 MB |
| Derived: `power_gpu_region_daily` (about 300 region × GPU rows a day) | | 0.33 M | 100 MB |
| Derived: `power_leadlag` (35 daily windows, then one a month) | | 0.36 M | 70 MB |
| **Total** | | | **about 1.2 GB** (about 1.45 GB with 20% for updates and bloat) |

This is under the 3 GB budget for power data, in a 10 GB volume with about 2.9 GB in use. Without the other ISOs'
allowance it is about 0.9 GB. Two settings move the projection most:
- **EIA fuel mix:** turn it off with `--dataset eia.region_hourly` on the backfill, which saves about 370 MB.
- **5-minute real-time series:** about 0.5 MB per series-year.

The considered alternative was a self-compressed blob (cent-quantised deltas, zlib) instead of `real[]`. It would
cut a 5-minute day to about 400 bytes. It was not taken, because `real[]` keeps every value readable in SQL
(`vals[i]`, `unnest`) and the budget does not need it.

## Running it

- `python -m power backfill [--since 2021-01] [--until ...] [--source caiso ...] [--dataset ...] [--dry-run]`:
  - fetches every missing market day, one request at a time per source and paced (CAISO one request every 5
    seconds, EIA one a second);
  - logs one progress line per 10 requests;
  - commits each chunk on its own. An interrupted backfill (a deploy restarts the container) resumes: stored days
    are skipped, days the source has no data for are recorded and skipped, and failed days are re-asked;
  - streams: one chunk's answer (a few MB at most) is held in memory, and nothing is written to disk.
- `python -m power forward`: what the `power_forward` job does every 12 h when `POWER_ENABLED=true`. It reads from
  the last stored day minus the overlap, then rebuilds the cross-analysis.
- `python -m power status`: series, series-days, depth, raw bytes kept and table sizes, per dataset.
- `POWER_SOURCES` (comma-separated) chooses the sources. Empty means each module's default: the no-signup sources
  plus EIA. A source that needs a key stays off until its key is set.

## Nodal daily summaries (where power is stranded)

A hub average hides stranded power. Over the 12 months to 2026-11-16, SPP South hub cleared below $20/MWh in 46% of
real-time intervals and below zero in 17%, but the cheapest power sits at specific nodes behind congested lines.
To rank candidate sites for small data centres that would run on cheap excess power, OpenGrid summarises every
priced node of ERCOT and SPP once a day. Code: `power/nodal.py`. Tables: `power_nodes`, `power_node_days`
(migration 0047_power_nodal).

**What is stored.** One row per (market, day, node), where market is `rt` or `da`:
- intervals with a price (`n`);
- intervals priced below $0 (`n_neg`) and below $20/MWh (`n_lt20`), both strictly below;
- mean, p10, median (`p50`), min and max of the day's interval prices. Percentiles interpolate linearly between
  ranks, as Postgres `percentile_cont` does.

The interval prices themselves are never stored per node. A day is the operator's market day in Central prevailing
time, so DST days have 92 / 100 (ERCOT 15-minute), 276 / 300 (SPP 5-minute) or 23 / 25 (hourly) intervals.

**Sources** (the same public files as the hub data, keyless):

| Dataset | Report / file | Nodes | Interval | How far back |
|---|---|---|---|---|
| `ercot.rt_nodal` | NP6-905-CD, one zip per 15-minute interval (~8 KB) | ~1,131 settlement points | 15 min | MIS lists 7 days; the run reads the last 6 |
| `ercot.da_nodal` | NP4-190-CD, one zip per day (~190 KB) | ~1,135 | hourly | MIS lists about 31 days; the run reads the last 29 |
| `spp.rt_nodal` | RTBM LMP by settlement location, one CSV per day (39-51 MB) | ~1,626 locations | 5 min | day files for this year and last; yearly zips before that (from 2021 here) |
| `spp.da_nodal` | DA LMP by settlement location, one CSV per day (~4.2 MB) | ~1,626 locations | hourly | as SPP real-time |

- ERCOT's yearly archives (13060 / 13061) hold hubs and load zones only. ERCOT's deeper nodal archive is on the
  Public Data API (api.ercot.com), which needs a registration, so it is not used. **ERCOT nodal history therefore
  starts when the forward job starts** (plus the 6 / 29 days MIS still lists).
- ERCOT types are as the file states them: `RN` resource node, `PCCRN` / `LCCRN` physical / logical combined-cycle
  resource node, `PUN` private use network, `LZ` load zone, `HU` hub, `AH` hub average, `SH` hub bus average. The
  energy-weighted and DC-tie load zones (`LZEW`, `LZ_DC`, `LZ_DCEW`) are skipped, as in the hub data.
- SPP's files state no location type. A location named `..._HUB` is a `hub`, every other one a
  `settlement_location`. The SPP zone column holds the balancing authority area the file names (`SPP`, or `SWPW`
  for the RTO West expansion).
- **Catalogue.** ERCOT's Settlement Points List and Electrical Buses Mapping (NP4-160-SG, report 10008, re-posted
  weekly, read only when a new one is posted) gives each resource node its substation, voltage and settlement load
  zone. **Neither ERCOT nor SPP publishes coordinates or counties for these nodes**, so `latitude`, `longitude` and
  `county` stay NULL. They are never geocoded by guessing.
- **Terms.** The nodal datasets come from the same sites under the same terms as each source's hub data, and
  `power/registry.py` (`NODAL_VERDICTS`) records the same verdict: ERCOT public (raw public data), SPP internal
  (commercial use needs SPP's written authorization). Every nodal read is admin-only regardless.
- **ERCOT's three-downloads rule.** The forward job runs the nodal datasets after the hub datasets in the same
  process. The NP6-905 and NP4-190 documents the hub run just read are reused from memory, not downloaded again.

**Runs.** `python -m power nodal backfill|forward|status|cheap`.
- The backfill reads every day not yet read, from `--since` (default: the last 366 days) or the source's earliest
  day, whichever is later. One request per day (ERCOT real-time: one per 15-minute file).
- Each day is recorded raw-first in `power_fetches` (URL + SHA-256; the bodies are not kept) and committed on its
  own, so an interrupted run resumes and a re-run stores nothing twice.
- A day is read once no answer for it failed. An ERCOT real-time day missing a 15-minute file is read as it is
  once it is a day old.
- A re-read replaces a day's summary only when it changed and covers at least as many intervals.
- The `power_forward` job runs `nodal.run("forward")` after the hubs (`POWER_NODAL=false` turns it off).

**Ranking.** `GET /v1/ops/power/nodes/cheap` (admin only) and `power.nodal.cheap_nodes(window_days, market)`:
- per node over a window (default 90 days, at most 365, ending at the latest stored day): the share of intervals
  below $0 and below $20, the hours below each, mean, min, max, and the days covered;
- `median` is the median of the node's daily medians, because the exact median of every interval would need the
  interval prices;
- ranked by the share below $20, then the share below $0, then the lower mean;
- filters: `market` (ercot, spp), `series` (rt, da), `zone`, `node_type`, `min_days` (default 7), `until`, `limit`.

**Size, measured 2026-11-15.** A summary row takes 100 bytes with its primary key (68 bytes of heap), measured on
200,000 rows. A year of both markets and both series is about 2.0 M rows, about 200 MB:

| Dataset | Rows a year | Size a year |
|---|---|---|
| ERCOT real-time (~1,131 nodes) | 413 k | 41 MB |
| ERCOT day-ahead (~1,135 nodes) | 414 k | 41 MB |
| SPP real-time (~1,626 locations) | 594 k | 59 MB |
| SPP day-ahead (~1,626 locations) | 594 k | 59 MB |
| `power_fetches` (ERCOT real-time: one row per 15-minute file) | 36 k | 21 MB |

- The catalogue is under 1 MB.
- The first backfill (SPP 12 months, ERCOT the 6 / 29 days MIS lists) is about 1.23 M rows, about 125 MB.
- Leaving out the two day-ahead datasets would halve the total.

Measured run times from a laptop:
- an SPP real-time day file (39-51 MB) takes about 32 seconds, so 12 months take about 3.3 hours;
- an SPP day-ahead day takes about 3 seconds;
- an ERCOT real-time day (96 files at the 2.5-second pace) takes about 4.2 minutes.

## Cross-analysis (derived, internal)

**Mapping regions to markets.** Built on `context/geo.py` (unchanged). A provider region gets a state only where
the cloud documents the place, or where the region string names the place. `power/geo.py` then adds one step, and
nothing is guessed:
- **zone:** the place is documented below the state, either by a cloud region table (AWS us-east-1 is Northern
  Virginia, which is PJM Dominion; AWS us-west-1 is Northern California, which is CAISO NP15; GCP us-west2 is Los
  Angeles, which is SP15) or by "US, <city>, <ST>" for a city wholly inside one pricing area;
- **state_hub:** only the state is known, and the ISO prices a hub for that state (MISO's MINN.HUB, MS.HUB, ...);
- **market_area_hub:** only the state is known, and the state's load is in one ISO. The ISO's reference hub is used,
  labelled as such. CAISO and NYISO have none, so a state-only California or New York region stays unmapped;
- **unmapped:** a city alone ("Dallas" is also in Georgia), a multi-region listing, or a split state.

The day-ahead hub price is used. Real-time prices are noisier, and the analysis is daily.

**Power cost per GPU-hour** = GPU board power (TDP, `tdp_w` in `hardware.py`; a GPU with no published TDP is
skipped) / 1000 × **PUE** × the day's mean day-ahead price ($/MWh) / 1000.
- PUE (power usage effectiveness) is the facility overhead on IT power. It is assumed to be **1.3**
  (`POWER_PUE`), a typical figure for current colocation and cloud data centres. Hyperscalers report about 1.1 to
  1.2, and older sites 1.5 or more.
- Example: an H100 SXM at 700 W × 1.3 × $50/MWh = $0.0455 per GPU-hour.
- This is the wholesale energy cost of running the GPU at full board power. It is **not** a provider's cost:
  providers buy power on tariffs and contracts, they pay for delivery, capacity and the servers' CPUs, and idle
  GPUs draw less.

**Spread** = the day's median listed price in that region (public on-demand listings, each as of the end of the
day) − the power cost. `power_share` = power cost / median price.

**Lead and lag.** For each GPU and price series, OpenGrid computes the Pearson correlation of day-on-day changes:
the power price on day *t* against the GPU's median price (and its in-stock share) on day *t + lag*, for lags −7 to
+7 days, over the last 180 days.
- At least 20 pairs are required; with fewer, the result is null.
- Changes are taken in levels, not logs, because power prices go negative.
- `spike_days` counts the days whose power price exceeded 2 × its trailing 30-day median.
- Natural gas (Henry Hub spot, carried over weekends) enters as a slow input, at lags −14 to +30 days: against the
  power price itself, and against GPU prices.

**Correlation, never cause.** A lagged co-movement over a few months of daily data does not say why a price moved.
OpenGrid reports these figures as correlations and never as explanations.

**Shifts.** For each major shift (methodology/shifts.md), `/v1/ops/power/shifts` lists the power-price spikes
inside the shift's own related-items window, each labelled by how it relates (the shift's GPU's regions, the
shift provider's regions, or market-wide).
- The join is made at read time. `market_shifts` and `market_shift_items` are untouched, so shift results and the
  public related items are exactly what they were.
- The heading is the shifts page's own: related, not necessarily causal.

**Tier of derived rows.** A derived row is internal unless every input is public. The GPU side uses public listings
only. The power side takes its series' tier. ISO prices are internal, so today every derived row is internal.

| Endpoint (admin only) | What |
|---|---|
| `/v1/ops/power/sources` | the registry: tier, reason, terms URL, signup, configured |
| `/v1/ops/power/status` | per dataset: series, series-days, depth, bytes; table sizes |
| `/v1/ops/power/series` | the catalogue (every tier); `?key=` day rows, `intervals=true` for the values |
| `/v1/ops/power/map` | provider regions with listings, and the market each maps to |
| `/v1/ops/power/cross/regional` | power cost per GPU-hour, spread and power share per region and day |
| `/v1/ops/power/cross/leadlag` | the latest window's lagged correlations |
| `/v1/ops/power/shifts` | power spikes around major shifts (related, not causal) |
| `/v1/ops/power/nodes/cheap` | ERCOT / SPP nodes ranked by the share of intervals below $20/MWh (stranded power) |

## Physical sites

The prices above say what power costs. Siting a small datacenter on spare power also needs the physical side:
where the power plants are, how much each one actually runs, which ones are retiring, and what new generation is
queued to connect nearby. A large plant that rarely runs, or one with a planned retirement, marks a grid
connection that has room. Code: `power/sites/`. Tables: `power_generators`, `power_generator_history`,
`power_plant_generation`, `power_queue_projects` (migration 0045). These are snapshots, not time series, so they
have their own registry (`power/sites/registry.py`) with the same terms rule as `power/registry.py`.

### Sources and tiers

| Source | What OpenGrid reads | Tier | Why (terms, checked 2026-11-15) |
|---|---|---|---|
| **EIA-860M** via API v2 `electricity/operating-generator-capacity` | Every operable generator, every month from 2008-01: plant, operator, state, county, latitude and longitude, balancing authority, technology, energy source, prime mover, nameplate, summer and winter MW, status, operating month, planned retirement month | **public** | U.S.-government work, public domain ([copyright and reuse](https://www.eia.gov/about/copyrights_reuse.php)) |
| **EIA-860M** monthly workbook ([eia860m](https://www.eia.gov/electricity/data/eia860m/)) | The newest file only: its Operating, Planned, Retired and Canceled sheets. Planned generators (with their planned month and construction status) and exact retirement months exist only here | **public** | as above |
| **EIA-923** via API v2 `electricity/facility-fuel` | Annual net generation per plant (fuel ALL, prime mover ALL), from 2008 | **public** | as above |
| **Berkeley Lab "Queued Up"** ([data page](https://eta-publications.lbl.gov/publications/us-interconnection-queue-data-0)) | Every interconnection request in the US queues (the 7 ISOs and about 50 utilities): state, county, type, MW, queue, proposed, online and withdrawn dates, status. One workbook a year, every edition kept | 2026 edition **public**; 2025 edition internal | The 2026 edition is "licensed CC BY 4.0 ... attribute it to Lawrence Berkeley National Laboratory and GridTracker". The 2025 edition's page states no licence, so it stays internal. emp.lbl.gov answers automated clients with a Cloudflare challenge, which is not bypassed: the files are read from eta-publications.lbl.gov |
| **ERCOT GIS report** (PG7-200-ER, public MIS) | Monthly: every generation project with a full interconnection study requested (county, fuel, technology, MW, projected commercial date, study phase), from 2018-12 | **public** | ERCOT terms section 5 allow redistributing raw public data in compilations. Each document is downloaded once |

**Large loads.** ERCOT publishes no project-level large-load queue. Large loads appear only as zone and total
figures in PDF slides (the Monthly Operational Overview, LLWG and TAC updates), so nothing is read. Queued Up
covers generation and storage only. The tables and the county totals keep generation and load apart (`kind`), so
a load source can be added later, but today every queued row is generation.

### How a generator's state and history are kept

- `power_generators` holds one row per (plant, generator): its latest state, the inventory month of its last change
  (`period`) and which publication said so (`via`: `api` or `file`).
- A month is applied whole or not at all (a missing page would look like generators that left). Months are applied
  oldest first.
- For the same month the workbook outranks the API. The two publications spell shared generators identically
  (checked on 2026-11-15), so a generator does not flip between them.
- `power_generator_history` is change-only: a row when a generator first appears, when its status, nameplate,
  summer capacity, planned operation month, planned retirement month or retirement month changes, and when it
  leaves. A generator stored as operable that an API month no longer lists has left the operable inventory: status
  `RE`, retirement month unknown until the workbook gives it. A generator missing from every sheet of a workbook
  becomes status `XX` (group `removed`).
- Re-applying the same answer stores nothing. EIA-923 years are upserted where the value changed, and each change
  is counted (`revisions`). The two newest complete years are re-read every run, because EIA revises them.

### Capacity factor and idle MW

For plant *p* and year *Y*:

- **capacity factor** = net generation in *Y* (EIA-923) / (sum over the plant's non-storage generators of nameplate
  MW x the hours of *Y* each was in service).
- A generator is in service from its operating month. If it is retired, it is in service until the month before
  its retirement month. A generator that left the inventory at an unknown month is not counted. A currently operable
  generator is taken as in service for the whole year (EIA's monthly status before 2008 is not read).
- Storage (batteries, pumped hydro, flywheels, compressed air) is left out of the denominator, because it consumes
  more than it makes and would make a plant look idle. Net generation includes the storage's losses, so a plant
  with a lot of storage can read slightly low.
- The factor is not clamped. A value above 1 means the inventory's nameplate understates the plant.
- **Idle MW** = operable non-storage nameplate x (1 - capacity factor, clamped to 0..1): the MW that sat unused on
  average over the year.
- Example (EIA-923 2025 and the September 2026 EIA-860M): Limestone, Texas, 7,523,395 MWh / ((893 + 956.8) MW x
  8,760 h) = 0.464, so 991 idle MW.
- The default year is the newest one with generation for at least half as many plants as the year before.

### Queued MW per county

- Only `active` requests count as queued. Withdrawn, suspended, operational and unknown requests are kept but never
  counted.
- Totals are per source and edition. Queued Up already includes ERCOT's queue, so the two sources are never added.
- MW is the request's capacity. A hybrid request adds its parts (Queued Up `mw_1 + mw_2 + mw_3`). ERCOT reports
  repowering as a net change, so some ERCOT values are zero or negative. They are kept as published.
- A Queued Up request has the id `<entity>|<queue id>`. Requests without a queue id, or with a repeated one, get
  `#2`, `#3` in file order, so those ids are not stable across editions.

### Raw first, depth and size

- Every request is a `power_fetches` row (dataset `sites.*`) with its URL and SHA-256. No body is kept: EIA, LBNL
  and ERCOT keep every file online, and the hash verifies a re-fetch.
- Depth: the EIA-860M API from 2008-01; EIA-923 from 2008 (the route has 2001); every Queued Up edition listed in
  `power/sites/lbnl.py`; every GIS report ERCOT still lists (94 months, 2018-12 onward, measured 2026-11-15).
- Measured bytes per row (heap and indexes, on the full real files): generator 314, history 185, queue project 312.
- Estimated size after the first run: about 45 k generators (14 MB), 100 k history rows (20 MB), 250 k plant-years
  (20 MB), 170 k queue rows (55 MB). About 110 MB in all.

### Running it

- `python -m power.sites sync [--source eia860m,eia923,lbnl_queues,ercot_gis] [--since YYYY-MM] [--dry-run]`:
  - fetches what is not stored yet, so the first run is the backfill and every later run a refresh;
  - is resumable: an interrupted run continues from the last month, year or edition stored;
  - runs the sources side by side, one request at a time within a source (EIA one a second, LBNL one every 5
    seconds, ERCOT one every 2.5 seconds). EIA-860M's two parts and EIA-923 share one EIA queue.
- The first run asks about 1,350 API pages for EIA-860M (225 months of about 6 pages of about 5 MB of JSON). That
  is about 6 GB received over 1 to 2 hours. It also asks about 55 pages for EIA-923, 2 Queued Up workbooks (30 MB)
  and 94 GIS reports (about 55 MB).
- `--since 2015-01` starts the EIA-860M history later and halves that.
- `python -m power.sites status` prints rows and bytes per table, generators per status, and queue editions.
- The `power_sites` job runs `sync` weekly when `POWER_SITES_ENABLED=true`. EIA-860M and the GIS report are
  monthly, so a weekly look picks each one up within a week. The EIA sources also need `EIA_API_KEY`.
- A new Queued Up edition is one line in `power/sites/lbnl.py` plus its verdict in `power/sites/registry.py`, added
  after reading that edition's licence.

| Endpoint (admin only) | What |
|---|---|
| `/v1/ops/power/plants` | plants with capacity factor, idle MW and planned retirements; `?state ba technology status county year sort=idle\|retirement\|nameplate\|cf limit` |
| `/v1/ops/power/queues` | queue projects (latest edition per source unless `?edition`); `?source state county iso type status kind`; plus MW queued per county, generation and load apart |
| `/v1/ops/power/sites/status` | site tables: rows, bytes, editions, fetches, and the site registry |

Python, for the site-ranking work (plain dicts, `power/sites/reads.py`): `plants(...)`,
`plants_near(lat, lon, km)`, `plants_in_county(state, county)`, `queue_projects(...)`,
`queued_mw_by_county(...)`. The [site ranking](power-siting.md) is built on them.
