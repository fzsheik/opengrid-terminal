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
- **Time series**: always `<thing>/history` with `from`, `to`, `interval`, `cursor`.

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

**Lists** page by cursor: `limit` (max 1000) and `cursor`; `meta.next_cursor` (null on the last page), `meta.total`.

