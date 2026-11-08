# Power markets

OpenGrid reads US wholesale electricity prices and grid data. The prices are hub-level and zone-level, never
nodal. The grid data is hourly demand, generation and interchange per balancing authority. Each source stores the
data in its own tables (`power_*`), and the data is analysed beside GPU prices and capacity without being mixed
into them. Power data is never written into listings, rollups, indices, fixings or archived prices. It never votes
in a GPU statistic, and nothing here says that a power price moved a GPU price.

Kind: **observed** (the series), **derived** (the cross-analysis). Code: `power/`. There is no public power route: every read is admin-only, under `/v1/ops/power/`
(sources, status, series, map, cross/regional, cross/leadlag, shifts).

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

- **Hub and zone series, never nodal.** Nodal data runs to terabytes. A hub or zone price is what a region's
  wholesale power costs.
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
