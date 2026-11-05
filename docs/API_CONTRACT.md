# OpenGrid Data API: contract

Decided 2026-11-03. This is the source of truth for the public market intelligence API. Code, tests and the
/api reference follow this file; a change to anything here is a contract change (see Versioning).

The routing / workloads API is a separate product (OpenGrid Compute API) and is out of scope here.

---

## 1. Products and hosts

| Product | Host | What |
|---|---|---|
| OpenGrid Data API | `data.tryopengrid.com/v1` | Market intelligence: prices, indices, fixings, capacity, costs, reliability, events, news |
| OpenGrid Compute API | `compute.tryopengrid.com/v1` | Routing, jobs, reservations (not covered here) |
| Website | `tryopengrid.com` | Behind site login; reads `tryopengrid.com/internal/*`, never the public hosts |

- One account and one key work for both APIs; scopes decide what a key reaches (`data:read` for this API).
- At first both hosts are served by the same app, routed by hostname. Either can move to its own service later
  without a URL change.

## 2. Access and license

- Every `/v1` request needs an API key (`Authorization: Bearer opg_live_...`). Nothing is anonymous.
- Enterprise only for now. Each key carries a `plan`; the error code `plan_limit` is reserved for a future cheaper
  tier. Nothing else for tiers is built.
- Licence: internal use only. No republishing or reselling. Stated in the enterprise contract and terms of use.
- All data is real time (as fresh as polling); every row says how old it is.

## 3. Versioning

- `/v1` stays in the URL forever. `/v2` is reserved for a full redesign and should never be needed.
- Shape is chosen by a dated version, `OpenGrid-Version: 2026-11-03`. Resolution order:
  1. the request header, 2. the version pinned to the key at creation, 3. latest.
  Unknown or retired version: 400 that lists the supported dates.
- Every response echoes the version used: `OpenGrid-Version` header and `meta.api_version`.
- The code builds only the latest shape. Each breaking change adds one file in `api/versions/` with a `downgrade`
  step that turns the new shape back into the previous one, plus a changelog entry.
- Additive (no new version): new endpoint, new field, new optional parameter, new enum value.
- Breaking (new dated version): removing or renaming a field, changing a type, unit or meaning, a new required
  parameter, a changed default, a changed error code.
- Retired versions get at least 12 months, with 6 months' notice (`Deprecation` and `Sunset` headers, email).

## 4. Response shape

Success:
```json
{"data": ..., "meta": {"as_of": "2026-11-03T14:00:00Z", "api_version": "2026-11-03", "request_id": "req_...",
                       "next_cursor": null}}
```

Error (every `/v1` error, including auth and rate limits):
```json
{"error": {"type": "invalid_request", "code": "unknown_gpu", "message": "...", "param": "gpu",
           "request_id": "req_...", "docs_url": "https://tryopengrid.com/api/errors#unknown_gpu"}}
```
- `code` values are part of the contract, like fields.
- Every response carries `request_id` (also in the `X-Request-Id` header and the logs).
- Rate-limit headers on every response (`X-RateLimit-Limit`, `-Remaining`, `-Reset`).

## 5. Data rules

- **Numbers** are returned exactly as stored, never rounded. Computed statistics (medians, spreads, ratios) are cleaned of binary noise to 12 significant digits (`2.4215999999999998` -> `2.4216`).
- **GPU fields**: every `gpu` field is the public id, with `gpu_name` (the display name) beside it, on every Data API response (applied once in `api.contract.public_shape`).
- **Numbers (fixings)**: fixings use fixed decimals with round half to even,
  stated in the methodology, and are sent as decimal strings.
- **Prices**: `price_per_gpu_hour` in USD on every price row. Beside it: original price, currency, unit and
  `gpu_count`. If the GPU count is unknown, `price_per_gpu_hour` is `null`; it is never guessed.
- **Timestamps**: ISO 8601, UTC, with `Z`.
- **Missing values**: the field is always present, `null` when unknown.
- **Names**: `snake_case`.
- **Enums**: lowercase strings. Clients must accept values they have not seen.
- **Corrections**: never silent. A corrected value carries `revised_at` and a reason; fixings use restatements.
- **Pagination**: cursors on every list. Pass `cursor` back until `meta.next_cursor` is `null`. `limit` caps a page.
- **Time series**: always `<thing>/history`; `window` (or `from` / `to` on change feeds), `interval`, and a cursor where rows can be many.

## 6. IDs

Stored once, frozen forever, never computed from a display name. Old or short names are accepted as input aliases;
responses always return the ID.

| Thing | Format | Example |
|---|---|---|
| GPU | vendor-model-memory-variant | `nvidia-h100-80gb-sxm5`, `amd-mi300x-192gb`, `google-tpu-v5e` |
| Provider | name | `lambda`, `runpod` |
| Index | **unchanged from today** | `h100-80gb-sxm5`, `h100-80gb-sxm5.spot` |

Index IDs keep their current form because they are stored in `index_levels` and the hash-chained fixings ledger;
renaming them would break `/fixings/chain/verify`. An index points at its GPU (`gpu: nvidia-h100-80gb-sxm5`).

Path parameters are named after the thing (`{gpu}`, `{index}`), never `{x_id}`.

## 7. Routes

```
REFERENCE
  GET /v1/gpus                          GET /v1/gpus/{gpu}            spec + hardware + family
  GET /v1/families                      GET /v1/families/{family}
  GET /v1/providers                     GET /v1/providers/{provider}
  GET /v1/regions

PRICES
  GET /v1/listings                      every current price row (?gpu&provider&region&market_type)   NEW
  GET /v1/listings/history              past rows (?from&to&cursor)                                    NEW
  GET /v1/markets                       one summary row per GPU (absorbs /spreads)
  GET /v1/markets/{gpu}                 current market (absorbs /markets/{gpu}/context)
  GET /v1/markets/{gpu}/history         absorbs /history/{gpu}, /archived, /extended, /dispersion
  GET /v1/markets/{gpu}/regions
  GET /v1/reserved                      GET /v1/reserved/summary

INDICES AND FIXINGS
  GET /v1/indices                       GET /v1/indices/{index}
  GET /v1/indices/{index}/history       GET /v1/indices/{index}/fixings
  GET /v1/fixings                       GET /v1/fixings/{fixing}      GET /v1/fixings/{fixing}/verify
  GET /v1/fixings/chain/verify          GET /v1/fixings/restatements

CAPACITY        GET /v1/capacity              GET /v1/capacity/history
COSTS           GET /v1/costs/components      GET /v1/costs/billing     GET /v1/costs/sources
                GET /v1/costs/presets         POST /v1/costs/estimate   (read-only computation)
FX              GET /v1/fx/rates (current, incl. cross rates)           GET /v1/fx/rates/history

RELIABILITY     GET /v1/reliability/providers                  one row per provider: status, availability,
                GET /v1/reliability/providers/{provider}       latency, data freshness
                GET /v1/reliability/providers/{provider}/history
                GET /v1/reliability/incidents                  provider status-page incidents + our measured dips
                GET /v1/reliability/probes                     raw results from our probe agents

EVENTS          GET /v1/events (shifts are type=shift)        GET /v1/events/{event}
NEWS            GET /v1/news   GET /v1/news/{item}   GET /v1/news/sources   GET /v1/news/topics
                GET /v1/timeline (?around=)
MACRO           GET /v1/macro/series          GET /v1/macro/series/{series}/history
METHODOLOGY     GET /v1/methodology           GET /v1/methodology/{name}
                (absorbs /events/types, /shifts/definition, /fixings/methodology)
```

**Reliability sources.** OpenGrid's own probe agents (`probes/`) are the source of everything under
`/v1/reliability`. UptimeProject (`uptime/`, third party, CC BY 4.0) is internal only: gap filler, backfill and a
cross-check for us, shown on internal pages with attribution, never served by this API. Every reliability point
carries a `source` field so a second source can be added later without a breaking change.

**Removed from the public API** (move to `/internal/*` for the website, or stay internal):
`/overview`, `/tape`, `/opportunities`, `/heatmaps/*`, `/compare`, `/live/*`, `/trust/*`, `/ops/*`, `/admin/*`,
`/uptime/*`, `/probes/*` (except as above), `/freshness`, `/shifts/*`, `/spreads`, `/history/*`, `/fx`,
`/fx/cross`, `/fx/listings` (provenance moves onto `/listings` rows), `/news/families`, `/timeline/around`,
`/context/power` (undecided: macro series or internal), `POST /events/track`, `POST /news/refresh`,
`POST /news/reclassify`, `POST /probes/ingest`.

## 8. Database changes

- GPU ids live in `gpu_ids.py` (canonical name -> id), committed and append-only, not in a table: canonical
  names are code (`canonical.py`), so a table would need syncing. Vendor, memory and form factor already come
  from `hardware.py`. Old short slugs and full names stay accepted as input aliases (`api.common.resolve_gpu`).
  No existing rows are rewritten: `canonical_gpu_name` stays the internal key in every table, and the API
  translates at the edge. `tests/test_gpu_ids.py` enforces the rules.
- Provider ids are already the stored provider strings (`lambda`, `runpod`).
- `api_keys`: add `api_version` and `plan`.
- History tables: add `(timestamp, id)` indexes where cursor pagination needs them.
- No tables are dropped. `uptime/` keeps polling for internal use.

## 9. Promises

- Uptime: 99.5% in contracts, 99.9% internal target (raise after the web / worker split, ROADMAP section 2).
- Status page for the API itself.
- Corrections are visible (section 5).
- Deprecation: 12 months minimum (section 3).

## 10. Hosting, website and how this is built

- **One repo.** Website, Data API, Compute API and workers deploy from this repo as separate Railway services
  (same code, a role setting decides routes and jobs). Stages: (1) one service answering all hosts, routed by
  hostname (`DATA_API_HOST`); (2) web / worker split, exactly one poller; (3) per-product services, a CDN in front
  of `data.`, a Postgres read replica when load needs it.
- **The website uses the same Data API** (same handlers, same shapes) on its own domain with the site login;
  only UI-only views (overview, tape, heatmaps, ...) are `/internal/*`. Its pages send their own
  `OpenGrid-Version` and upgrade on purpose. No API key is ever put in the browser.
- **The data host** answers Data API routes only and only with an API key (`api/contract.py`).
- **Sandbox.** All of this is built on the `api-v1` branch; `main` (production) is frozen until the API is done,
  then gets one merge. A Railway `staging` environment deploying `api-v1` is the place to click through it.
- **Price polling is untouched** (pollers call providers outbound). Watch: the probe agents call
  `/v1/probes/targets` and `/v1/probes/ingest`, so those keep working until both agents are re-uploaded; at the
  worker split, exactly one poller runs and every provider key is copied to the worker service.

## 11. Testing

- `tests/run_all.py` runs every test; GitHub Actions runs it on every push (`.github/workflows/tests.yml`).
- `tests/smoke/compare.sh [ref]` loads all 28 website pages in headless Chrome on this checkout and on `ref`
  (default `origin/main`), each on a seeded scratch database, and prints only differences: new JS errors,
  failed requests, changed price / row counts. Run it after every area moves (needs Chrome; not in CI).
- `tests/test_contract.py` sweeps every Data API route (no 500s, version header, meta, error shape) and covers
  the version header, key pin and data host.
- Still to come, in order: old-vs-new equivalence for every moved route (before any old route is deleted);
  invariants on the numbers (price > 0, min <= median <= max, per-GPU x count = node price, every ID resolves,
  index level = recomputed level, no internal field in a response); contract tests once the shape is frozen
  (saved samples per version, schema diff); staging smoke test, load test, and production checks (source
  cross-check, our probes probing our own API, alerts).

## 12. Build order

1. Error shape, `request_id`, version header (empty `api/versions/` chain), key-only `/v1`, website on
   `/internal/*`, `data.` host routing.
2. `gpus` and `gpu_aliases` tables, stored IDs, ID translation at the edge.
3. Route layout (section 7), with the website pages updated in the same change.
4. Cursor pagination on every list.
5. Caching: `ETag`, `Cache-Control`.
6. As-of queries (`?as_of=`) and visible price corrections.
7. Bulk export (daily snapshot files per dataset).
8. Contract tests: saved samples per version, schema diff against the saved OpenAPI.
9. OpenAPI-generated reference, then a Python SDK and CLI with the version built in.

No outside client uses the API yet (only our own UI), so steps 1 to 4 need no aliases or deprecation period.

## 13. Resource shapes (filled in as each area moves)

**GPU** (`/v1/gpus`, `/v1/gpus/{gpu}`): reference data only, no prices.
```json
{"id": "nvidia-h100-80gb-sxm5", "name": "NVIDIA H100 80GB SXM5", "families": ["h100", "hopper"],
 "vendor": "NVIDIA", "architecture": "Hopper", "generation": "Hopper", "released": 2022, "segment": "datacenter",
 "datacenter": true, "vram_gb": 80, "memory_type": "HBM3", "memory_bandwidth_tbps": 3.35, "fp32_tflops": 67,
 "fp16_tflops_dense": 989.5, "bf16_tflops_dense": 989.5, "fp8_tflops_dense": 1979, "fp8_supported": true,
 "interconnect": "PCIe Gen5", "nvlink": "NVLink 4, 900 GB/s", "form_factor": "SXM", "tdp_w": 700,
 "workload_class": "training", "notes": "...", "sources": ["https://..."]}
```
Hardware field names are the vendor-spec names already in use (`vram_gb`, `bf16_tflops_dense`, ...): explicit,
so not renamed. A family slug on a GPU route is a 404 naming `/v1/families/{family}`.

**Market row** (`/v1/markets`): one per GPU with a live listing.
```json
{"gpu": "nvidia-h100-80gb-sxm5", "gpu_name": "NVIDIA H100 80GB SXM5",
 "price": {"low": 1.6168, "median": 2.4216, "high": 2.9269, "currency": "USD", "unit": "gpu_hour"},
 "providers": 4, "listings": {"live": 5, "priced": 4, "available": 4, "sold_out": 1, "availability_unknown": 0},
 "spread": {"abs": 1.3101, "pct_of_low": 0.810304, "pct_of_median": 0.541006},
 "score": {"efficiency": 69.1, "fragmentation": 30.9, "label": "moderately fragmented", "confidence": "low",
           "reason": null}}
```
`change_24h` per GPU joins this row when the history area moves (additive).

**Market detail** (`/v1/markets/{gpu}`): the market row's fields plus `stats`, full `score`, `by_provider` (each
provider's lowest price, rank, premium, `change_24h`), `capability`, `alternatives`, `context` (where today's
price sits in its 30 / 90 day history).

**Naming rules** (every area follows them):

| Concept | Field |
|---|---|
| Price across providers | `price: {low, median, high, currency, unit}` |
| One provider's / listing's price | `price_per_gpu_hour` |
| GPU | `gpu` (id) + `gpu_name` |
| Provider | `provider` (id) + `provider_name` (both added by `public_shape`) |
| Counts | `providers`, `gpus` are numbers; lists are `provider_ids`, `gpu_ids` |
| Listing counts | `listings: {live, priced, available, sold_out, availability_unknown}` (the ones that apply) |
| Old `slug` fields | removed: the id replaces them (website links compute their own) |

**Family** (`/v1/families`, `/v1/families/{family}`): `id` is the slug (`h100`, `hopper`); `member_families` are
family ids. Each variant: `gpu`, `gpu_name`, `price`, `low_provider`, `providers`, `listings {available, priced}`,
`reason`, `index {id, level, published, change_24h}` (detail only), `links {gpu, market, regions, history}`.
`cheapest_variant_now`: `gpu`, `price_per_gpu_hour`, `provider`, `label` (a cross-variant fact, not a family price).

**Provider** (`/v1/providers`, `/v1/providers/{provider}`): `provider`, `provider_name`, `provider_class`,
`source_type`, `source`, `website`, `note`, `coverage {gpus, gpu_ids, architectures, region_groups,
listings {live, priced}}`, `relative_value`, `listing_lifetime`, `feed_health`, `facts`. Detail adds `catalog`
rows: `gpu`, `listings {live, priced, available}`, `price_per_gpu_hour` (its lowest), `rank_now`,
`market {providers, median}`, `premium_now`, `region_groups`, `window`; plus `rank_distribution`, `rank_history`,
`beats_the_market`, `expensive`.

**Region group** (`/v1/regions`): `region_group`, `providers`, `provider_ids`, `gpus`, `listings {live, priced}`.
**GPU by region** (`/v1/markets/{gpu}/regions`): `gpu`, `global {price, providers}`, `regions[]` with
`region_group`, `price`, `low_provider`, `providers`, `provider_ids`, `listings {live, available}`,
`premium_vs_global_median`, `reason`.

**Listing** (`/v1/listings`): one offer as the provider publishes it, cheapest first.
```json
{"provider": "lambda", "provider_name": "Lambda", "listing_id": "...", "sku": "...",
 "gpu": "nvidia-h100-80gb-sxm5", "gpu_name": "NVIDIA H100 80GB SXM5", "raw_gpu_name": "H100 SXM5 80GB",
 "gpu_count": 8, "region": "us-east-1", "country": "US", "region_group": "US",
 "market_type": "on_demand", "interruptible": false, "provider_tier": null,
 "price_per_gpu_hour": 2.49, "price_per_instance_hour": 19.92, "currency": "USD",
 "original": null,
 "available": true, "capacity": null, "capacity_unit": null, "vcpu": 208, "ram_gb": 1800, "storage_gb": 24000,
 "observed_at": "2026-11-03T14:00:00Z", "first_seen_at": "2026-10-01T09:00:00Z", "trust": {"...": "..."}}
```
`original` is set when the provider prices in another currency: `{currency, price_per_gpu_hour,
price_per_instance_hour, fx {rate, date, source, legs}}` (`legs` for a cross rate: EUR->USD and EUR->X).
An unmapped GPU has `gpu: null` and keeps `raw_gpu_name`.

**Listing change** (`/v1/listings/history`): `provider`, `listing_id`, `observed_at`, `gpu`, `gpu_count`, `region`,
`market_type`, `price_per_gpu_hour`, `price_per_instance_hour`, `available`, `capacity`, `capacity_unit`.
Change-only, newest first, keyset cursor (stable while new rows arrive); `from` / `to` bound the time.

**Price history** (`/v1/markets/{gpu}/history`, `?window=24h|7d|30d|90d|ytd|all&interval=1h|1d&segment=`):
`series` points `{t, low, median, high, providers}` plus the GPU's `index` line; `currency` and `unit` once per
series. Sub-resources keep one shape each: `/history/daily` (archived then observed, `kind` per day),
`/history/archived` (Internet Archive / AWS price-list captures, never in statistics), `/history/dispersion`.
Time series name aggregates `low` / `high` like everything else.

**Index** (`/v1/indices/{index}`): the definition fields once (`id`, `name`, `kind`, `segment`, `unit`, `gpu`,
`components`, `description`, ...) then the current state (`published`, `level`, `raw_level`, `hour`, `changes`,
`volatility`, ...) and `constituents_now`. Index history: `index`, `index_name`, `unit`, `window`, `interval`,
`points`. Any `index_id` field is named `index` (the id, like `gpu` / `provider`).

**Fixing** values (`value`, `raw`, in fixings, their inputs and verify results) are **exact decimal strings** of the
stored double (`"2.2494934019"`), never JSON floats: money may settle on them and float handling must not touch
them. Fixings and restatements page by cursor (`meta.next_cursor`, `meta.total`).
Decided 2026-11-05: publish the full stored value, no rounding. A rounding rule (e.g. 4 decimals, round half
to even) becomes a dated settlement methodology version once contracts settle on fixings.

**Capacity** (`/v1/capacity`, cursor paged): `provider`, `sku`, `region`, `country`, `region_group`, `gpu`,
`raw_gpu_name`, `gpu_count`, `in_stock`, `tier`, `available_count`, `total_count`, `count_unit`, `raw_value`,
`observed_at`, `changed_at`, `first_seen_at`, `stale`. `/v1/capacity/history`: changes newest first, keyset cursor.

**Reserved** (`/v1/reserved`, cursor paged; `/v1/reserved/summary`): term rows with `price_per_gpu_hour`,
`term_bucket`, `on_demand_per_gpu_hour`, `saving_pct`, ... (never in indices).

**Costs** (`/v1/costs/components`, `/billing`, `/sources`, `/presets`, `POST /v1/costs/estimate`): plural path.

**FX**: `GET /v1/fx/rates` = `{eur_usd: {rate, rate_date, state, source, ...}, to_usd: [{currency, rate, legs,
state, ...}]}` (in force now; `eur_usd.state` ok / stale / missing = conversion runs or is stopped);
`GET /v1/fx/rates/history?quote=&days=`. A listing's own conversion is on its row (`original`).

**Reliability** (`/v1/reliability/*`): one row per provider = `provider`, `probes` (our checks: status,
availability, latency, reasons; `source: opengrid_probes`), `freshness[]` (per stream: cadence, age, p95, lag;
`source: opengrid_feeds`), `feed_health` (`source: opengrid_feeds`). Detail adds `targets[]` with each target's
hourly series per vantage point. `/dips` = hours with a failed check and their measured scope;
`/v1/reliability/incidents` = providers' own status-page incidents (`source: provider_status_page`, reported, not
measured). UptimeProject data is never served here. Reliability of OpenGrid's own launches is a Compute API concern: `GET /v1/execution/reliability`, key only
(not public, not on the Data API host). The probe agents keep
`GET /v1/probes/targets` and `POST /v1/probes/ingest` (agent routes, not Data API).

**Events** (`/v1/events`, keyset cursor on (occurred_at, id)): market events, newest first; `/v1/events/types` is
the machine-readable catalogue. **Shifts** live under the same root, one shape per path: `/v1/events/shifts`
(keyset cursor on (at, id)), `/v1/events/shifts/definition`, `/v1/events/shifts/{shift}`. (Decided 2026-11-05: separate from the events feed, one shape each.)

**News** (`/v1/news`, keyset cursor on (last activity, story)): story-clustered, newest activity first.
`/v1/news/{item}`, `/v1/news/sources`, `/v1/news/topics`, `/v1/news/families` (the families the classifier
knows, tracked or not). `/v1/timeline` and `/v1/timeline/around` stay two paths (two shapes).

**Macro** (`/v1/macro/series`, `/v1/macro/series/{series}`, `/v1/macro/series/{series}/history?since=`,
`/v1/macro/power`): public-domain series (EIA, SEC EDGAR) beside prices; internal series (equities) are never in
the catalogue and their ids are 404. The old `?id=a,b` multi-series form is gone (one shape per path).
`/v1/macro/power` stays in the Data API, public (decided 2026-11-05).

**Website-only reads** (`/internal/*`): overview, tape, opportunities, heatmaps, compare, uptime (UptimeProject),
trust, live. Shaped for one screen, not part of the contract, free to change. They keep the internal shape
(full GPU names, no Data API envelope rules), are never served on the Data API host, never accept an API key, and
are readable without the site password only for the areas in `api/pages.PUBLIC_INTERNAL` (live stays
operator-only, as before).

**Timestamps** end in `Z`, in `data` and `meta`.

**Lists** (every one, enforced by the route sweep in tests/test_contract.py) page by cursor; small reference
lists use `envelope(page=(limit, cursor))`, time-ordered feeds a keyset cursor: `limit` (max 1000) and `cursor`; `meta.next_cursor` (null on the last page), `meta.total`.

