# Updates

One entry per shipped update, newest first. Every entry says what shipped, what was planned but did not ship (and why),
what is left, and what is next. Add an entry in the same commit as the change.

---

## 2026-11-06 (fourth release) — Power markets, natural gas and power stocks (internal), beside GPU prices, never mixed

### What shipped
- **New `power/` package and `power_*` tables** (migration `0035_power_markets`, additive only):
  - `power_series`, the catalogue;
  - `power_days`, one row per series and market day, with the day's values as `real[]` (24 hourly or 288
    five-minute values, DST days 23/25 or 276/300) and the day's mean, min and max;
  - `power_fetches`, every request with its URL, SHA-256 and byte count, plus the bytes where they are small;
  - two derived tables, `power_gpu_region_daily` and `power_leadlag`.
  Power data is never written into listings, rollups, indices, fixings or archived prices, and never votes in a
  GPU statistic.
- **Sources** (`power/sources/`; terms verdicts in each module, read by `power/registry.py`):
  - **EIA-930** (public, public domain): hourly demand, net generation, interchange, the day-ahead forecast and
    generation by fuel per balancing authority. Available from 2019-01.
  - **CAISO OASIS** (internal: its terms do not clearly permit redistribution): day-ahead hourly and real-time
    5-minute LMP at NP15, SP15, ZP26 and the four DLAPs. OASIS serves only a rolling ~39 months (from 2023-08).
  - **EIA natural gas** (internal by choice: Henry Hub spot, NYMEX C1-C4 until EIA stopped them on 2024-04-05,
    weekly storage, the monthly electric-power price by state). Documented in `methodology/internal/natural-gas.md`.
  - **NYISO** (internal; no signup): zonal day-ahead hourly and real-time 5-minute LBMP, 15 zones, from 2021-01.
  - **SPP** (internal; no signup): SPPNORTH_HUB and SPPSOUTH_HUB, day-ahead hourly and real-time 5-minute, from
    2021-01.
  - **ERCOT** (PUBLIC: its Terms of Use §5 permit redistributing raw public data in compilations and analyses; no
    signup, MIS public reports): day-ahead hourly and real-time 15-minute settlement point prices, 6 hubs and 8 load
    zones, from 2011-01.
  - **PJM** (internal; free Data Miner 2 key) and **ISO-NE** (internal; free ISO Express account): hub and zone
    LMPs, off until their keys are set. They are built from the documented formats and not verified live yet.
  - **MISO** (built, off): its site terms forbid automated access, so its public files must not be enabled. Its
    Data Exchange API (subscription key) is the allowed route and is not implemented.
- **Runner:** `python -m power backfill|forward|status|sources`.
  - Idempotent and resumable: stored days are skipped; "no data" and not-yet-published days are recorded.
  - Polite: one request at a time per source, paced.
  - Streams: one chunk in memory, nothing written to disk.
  - The `power_forward` job runs every 12 h behind `POWER_ENABLED`.
- **Cross-analysis** (`power/cross.py`, internal):
  - provider regions are mapped to ISO zones or hubs only where the place is documented (`power/geo.py`);
  - power cost per GPU-hour = TDP × PUE (1.3) × the day-ahead hub price;
  - the spread to the median listed price;
  - lagged correlations of GPU price and in-stock share against power, and against Henry Hub;
  - power spikes around major shifts, joined at read time, so `market_shift_items` is untouched.
  Correlation, never cause.
- **API:**
  - admin-only `/v1/ops/power/{sources,status,series,map,cross/regional,cross/leadlag,shifts}`;
  - no public route. Power is for OpenGrid's routing and research; the EIA-930 series are public domain and could
    be added later through the Data API's versioned contract.
  - `tests/test_visibility.py` now seeds internal power, gas and derived values and checks every public route.
- **Power stocks (internal equities):** 20 tickers were added to `context/equities.TICKERS`:
  - generators: CEG, VST, TLN, NRG;
  - datacenter-region utilities: D, AEP, SO, EXC, NEE, DUK, ETR, XEL;
  - nuclear and fuel: OKLO, SMR, CCJ;
  - grid and datacenter power equipment: GEV, ETN, PWR, BE;
  - XLU.
  The hourly `context_equities` job backfills each from 2016 (or its listing) on its first run.
- **Storage:** measured 250 bytes per hourly series-day and 1,417 per 5-minute day. Projected about 1.2 GB for the
  backfill plus 3 years with room for the other ISOs (about 0.9 GB without), against the 3 GB budget.
  `methodology/power-markets.md` has the table.

### Not shipped
- The CAISO history before 2023-08: OASIS does not serve it.
- MISO through its Data Exchange API (subscription key): not implemented.
- PJM and ISO-NE are off until the free accounts in `methodology/power-markets.md` exist and their keys are set.

### Deploy notes
- **Migration:** `0035_power_markets`, after `0034_api_key_version`. It was re-chained at the merge with Data
  API v1; it was written against `0029_news_history`.
- **Variables:**
  - `POWER_ENABLED=true` turns on the `power_forward` job (default off).
  - `POWER_SOURCES` is optional: a comma list, empty for the defaults (caiso, eia, eia_gas, nyiso, spp, ercot).
  - `POWER_PUE` defaults to 1.3.
  - `EIA_API_KEY` is already set.
  - ERCOT, PJM and ISO-NE: `ERCOT_API_SUBSCRIPTION_KEY`, `ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD`,
    `PJM_API_KEY`, `ISONE_USERNAME`, `ISONE_PASSWORD` (unset means off).
- **Backfill on the production server, AFTER the deploy, detached** (a deploy restarts the container and kills
  one-off jobs; re-running resumes where it stopped). In this order, smallest first:
  1. `nohup .venv/bin/python -m power backfill --source eia_gas > /tmp/power-gas.log 2>&1 &` (no `--since`: the
     gas history from 1993 to 1997; minutes, about 9 MB)
  2. `nohup .venv/bin/python -m power backfill --source caiso > /tmp/power-caiso.log 2>&1 &` (OASIS's ~39 months;
     about 440 requests at 1 per 5 s, so about 40 min; about 25 MB)
  3. `nohup .venv/bin/python -m power backfill --source eia --since 2021-01 > /tmp/power-eia.log 2>&1 &` (about
     8,500 paged requests at 1 a second plus response time, so about 4-5 h; about 600 MB)
  4. Then `.venv/bin/python -m power forward` once (it fills the overlap and builds the cross-analysis) and
     `.venv/bin/python -m power status`. Set `POWER_ENABLED=true` after that.
  Then the same for `nyiso`, `spp` and `ercot` (`--since 2021-01`; ERCOT from 2011 with no `--since`). SPP's
  real-time files are large, about 39 GB downloaded in total, and are streamed and discarded.
  Steps 1-3 may run in parallel (different sources). `--dry-run` prints the plan first.
- Full suite before push (`tests/run_all.py`): 136 of 136 files pass. `test_contract.py` compared the export date with the local day; it now uses the UTC day (it failed on main every evening after 19:00 US Eastern).

---

## 2026-11-06 (third release): public customer docs at docs.tryopengrid.com

### What shipped
- A separate, public docs site in `docs-site/` (Next.js + Fumadocs, Mintlify-style layout), on Vercel project
  `opengrid-docs`, domain `docs.tryopengrid.com`. Pages: introduction, quickstart (curl / Python / JavaScript),
  authentication, versioning, responses, errors (one anchor per code), pagination, rate limits, caching, ids, data
  rules, point in time, bulk exports, three guides, for-agents, changelog, terms.
- API reference: one page per customer endpoint (67), grouped by tag, generated from the app by
  `python -m api.docs_openapi` into `docs-site/openapi.json`. Internal, website-only and write-side routes
  (`/v1/events/track`, `/v1/news/refresh`, `/v1/news/reclassify`) are left out. Response fields come from the
  frozen contract snapshot; the estimate body from the limits in `api/cost.py`.
- Playground, deliberately limited: only for key holders. The reader pastes their own key, the browser calls
  `data.tryopengrid.com` directly, nothing goes through the docs site, there is no shared or demo key. The docs
  CSP allows requests only to itself and the Data API.
- For agents: `/llms.txt`, `/llms-full.txt`, `.md` on any page URL, `Accept: text/markdown`, `/openapi.json`,
  copy-as-markdown and open-in-ChatGPT / Claude / Cursor on every page.
- App: CORS on the Data API host for the docs origin only (no credentials); error `docs_url` now points to
  `docs.tryopengrid.com/errors#<code>`. `tests/test_docs_site.py` checks the generated files are current, only
  customer routes are published, every route and link named in the docs exists, every error code has an anchor,
  and the CORS rules.
- Keys: the docs say to contact `hello@tryopengrid.com`; there is no self-serve sign-up.

### What did not ship
- Self-serve keys and a sandbox / anonymous playground (by decision: the docs are public, so nothing runs without
  a key).
- Field-level response schemas for 11 routes without a contract snapshot; their pages say so.

### Known issues
- The playground keeps the pasted key in the browser's localStorage (Fumadocs default), on that reader's machine only.
- The playground works only after the app release (CORS) is deployed and the docs DNS record exists.

### Deploy notes
- Docs: `cd docs-site && vercel deploy --prod`. DNS: Cloudflare `A docs 76.76.21.21`, DNS only (not proxied).
- App: no migration; needs a `railway up` for CORS and the new `docs_url`. Rollback: the previous deployment.
- After changing routes, rerun `python -m api.docs_openapi` and commit the output (the test fails otherwise).

### Next
- Account-creation test on the real address, customer login, self-serve keys.

---

## 2026-11-06 (second release): the website runs on the Data API; first-UI routes retired

### What shipped
- The Data API gained what the first UI's routes gave the website, so nothing is lost: `/v1/markets?trend=true`
  (24h `change`, `trend` series, `low_provider` / `high_provider`), `/v1/markets/{gpu}/history/providers` (each
  provider's price line plus the market low / median / high), `/v1/listings?previous=true` (`price_change`: the
  price before the newest recorded change). All additive; the frozen contract snapshot lost nothing.
- Every website page now reads only `/v1` and `/internal`. Retired: `/market`, `/market/detail`, `/listings`,
  `/changes`, `/history`, `/reference-prices`, and the first UI (`/classic` redirects to `/gpus`; `classic.html`
  and its test removed). `/polling` and `/mapping` moved to `/internal/polling` and `/internal/mapping`.
- Tests prove the replacements match the old routes (trend, change, providers, series, previous price).

### What did not ship
- The website's client-side field adapters (`OG.legacyGpu`, `OG.legacyHistory`, flattening `price_change` in three
  pages) stay: they sit in the browser, customers never see them; removing them means editing every chart.
- Python SDK, customer sign-up / login, stored export files.

### Known issues
- The disk of the development machine filled during a parallel test run and stopped its local Postgres; tests ran
  fine once space was freed. Production is unaffected.
- Baseten has failed every poll since 2026-11-03 (a scraped page); unrelated to this release.

### Deploy notes
- No migration. Rollback target: deployment 29daeac7 (previous release).

### Next
- Customer-facing docs, an account-creation test on the real address, customer login.

---

## 2026-11-06: OpenGrid Data API v1 (contract, new routes, API address)

### What shipped
- The market intelligence API follows one contract, written in `docs/API_CONTRACT.md`: one error format
  (`{error: {type, code, message, param, request_id, docs_url}}`), `request_id` and `api_version` in every `meta`,
  dated versions (`OpenGrid-Version` header, pinned per API key in the new `api_keys.api_version` column, migration
  0034), cursor paging on every list, frozen vendor-prefixed GPU ids (`nvidia-h100-80gb-sxm5`, `gpu_ids.py`) with
  `gpu_name` beside them, `provider_name` beside provider ids, exact decimal strings for fixings, ETag + 304 +
  `Cache-Control`, `?as_of=` on `/v1/listings`, bulk export `/v1/exports`, the schema at `/v1/openapi.json`.
- New route layout (old paths removed, no outside clients): `/v1/gpus` (reference), `/v1/markets`,
  `/v1/markets/{gpu}` (+ `/history`, `/history/daily|archived|dispersion`, `/regions`), `/v1/listings` (+ `/history`),
  `/v1/costs/*`, `/v1/fx/rates` (+ `/history`), `/v1/reliability/*`, `/v1/events/shifts`, `/v1/macro/*`.
  UI-only reads moved to `/internal/*` (overview, tape, opportunities, heatmaps, compare, uptime, trust, live).
  Launch reliability is `/v1/execution/reliability` (key only).
- `DATA_API_HOST` (set to data.tryopengrid.com after this deploys): that host serves Data API routes only and needs
  an API key. Unset, nothing changes.
- Every DB connection is set to UTC (a non-UTC server shifted dates by a day). Tests: one runner
  (`tests/run_all.py`), GitHub Actions on every push, 132 test files; contract shape snapshot
  (`tests/contracts/2026-11-03.json`); website smoke compare (`tests/smoke/compare.sh`).

### What did not ship
- Python SDK package (planned, own repo). Customer sign-up / login (keys are still issued by the operator on
  `/admin/partners`). Stored daily export files (exports are built per request). Rounding of published fixings
  (full stored value is published; a rounding rule is a settlement methodology decision).

### Known issues
- A few website pages translate new field names back to the old ones (`OG.legacyHistory`, `gpu`/`index_id` shim in
  `web/core.js`); cleanup before the next contract change.
- `/v1/listings?as_of=` and the listings export scan `listing_observations`; untested at production size.
- Whole-suite runs can hit a 10 minute timeout on `test_money_invariants` when the machine is busy (81 s alone).

### Deploy notes
- Migration 0034 only adds a column (and sets existing keys to 2026-11-03); old code ignores it, so a rollback is safe.
- Rollback target: deployment 8862412c (2026-11-02).

### Next
- Set `DATA_API_HOST`, test account creation end to end, write the customer-facing docs, then customer login.

---

## 2026-11-02 (fifth release) — Linode capacity: Retry-After on a 200 is not a request to wait

### What shipped
- `providers.base.rate_limit_wait` reads `Retry-After` only on a refusal (HTTP 429 or 503). Linode sends
  "Retry-After: 60" on every 200 response; that is its rate-limit window, not a request to wait. Read as a wait,
  each poll's first call (`/v4/linode/types`, prices) blocked the next one (`/v4/regions/availability`,
  capacity) for 60 s. That is longer than the 15 s inline wait, so the capacity call was skipped as throttled on
  every poll from 11:09 UTC on 2026-11-02, and the `capacity_silent` alert fired. Remaining-quota headers
  (`X-RateLimit-Remaining: 0` with a reset) still pause a provider on any status.

### Deploy notes
- No migrations and no new variables. Deployed after the running archive import finished. Linode capacity was
  silent from 11:09 UTC until this deploy.
- Full suite before push: 127 test files, all passing except the known intermittent `test_jobs_e2e`.

---

## 2026-11-02 (fourth release) — News: placeholder 1970 dates are not publish times

### What shipped
- `news.parse.parse_time` returns None for stamps before 1995. TrendForce's feed sends "Thu, 01 Jan 1970 08:00:00
  +0800" for some items, and those were stored as published on 1970-01-01. They are now undated items, with the
  time inferred from when they were read (the archive capture time for backfilled items), like any other undated
  item.
- Production repair, run on 2026-11-02 before this deploy: the 60 stored items dated before 1995 (all TrendForce,
  all backfilled) were set to their capture time and marked inferred. The earliest news item is now 2008-02-07.

### Deploy notes
- No migrations and no new variables. Deployed after the running history imports finished, because a deploy
  restarts the container and stops one-off jobs.

---

## 2026-11-02 (third release) — Fewer unmapped GPUs, none guessed

### What shipped
**Fewer unmapped GPUs, none guessed.**
- Nine real GPU models are added to the catalogue, each from NVIDIA's own datasheet or product page: A30, GB200 (one
  GPU of the superchip), Quadro RTX 8000, RTX PRO 4000 Blackwell SFF, V100 32GB SXM2 and PCIe, V100S, and K80 and
  M60 (one GPU of the dual-GPU board, as clouds count them).
- Provider names are mapped only where they state the variant. Thunder Compute's H100 is mapped to SXM because
  Thunder's own site says so.
- Names whose board, memory or edition no provider source states stay unmapped: Oracle A100/H100, Gcore's VM H100,
  Sakura, RTX PRO 6000 editions, Vast's pooled "A100 SXM4", and others. The reason for each is now shown in the
  unmapped queue (`mapping.UNMAPPED_REASONS`).
- Unmapped provider GPU names: 192 -> 169. The K80 and M60 aliases also map the AWS price-list archive's p2 and g3
  rows. A re-import fills the GPU on archived rows stored unmapped. GRID K520, Gaudi 3 and MI350X stay unmapped until
  a vendor spec is read.

### Deploy notes
- No migrations and no new variables.
- The history one-offs listed for the second release run after this deploy, so the archive imports use the new
  mappings. Re-running `python -m backfill aws` fills the GPU on stored p2 and g3 rows.
- Full suite before push: 127 test files, all passing except the known intermittent `test_jobs_e2e`.

---

## 2026-11-02 (second release) — Archived page history back to 2017, news history, explained price shifts, internal equities

### What shipped
- **Archived page history.** Archived pricing pages are parsed into `archived` points (never in statistics):
  - Azure: Linux VM page from 2017 to 2024, and the 2019–21 design.
  - Lambda: 2020–2025.
  - CoreWeave: 2021–2024, three designs plus the a-la-carte page.
  - RunPod: 2022–2024.
  - Verda / DataCrunch: 2020–2025.
  - Paperspace: 2017–2026.
  - Exoscale: from 2021.
  - Hugging Face Inference Endpoints: from 2022, tier `resold:<vendor>`.
  - CUDO: 2024–25.
  - Akash: from July 2025.
  - Denvr: 2025.

  `--since` / `--until` take YYYY, YYYY-MM or YYYY-MM-DD. A re-import fills the GPU on rows that were stored
  unmapped, and never changes a mapped one.
- **News history.** Past news items are recovered from feed archives, Wayback captures of changelogs, SEC EDGAR and
  GDELT, with archive provenance on each item (`news_items.backfilled`, `provenance`).
- **Major shifts.** Large moves are detected in observed and archived history: market medians, indices, provider
  repricings and capacity episodes. Each shift stores its related news items and events, ranked by time distance and
  relevance. They are served at `GET /v1/shifts` and shown on the GPU page and the news timeline under a "related,
  not causal" heading. The new `market_shifts` job keeps them current.
- **Equities (internal only).** Alpaca market data (never the trading API) supplies daily bars from 2016 and hourly
  bars for 5 years for GPU-relevant tickers, stored raw first with split and dividend refetches. They are read only
  at the admin-only `/v1/ops/context/equities` and never appear on public pages, APIs, indices or exports. The
  `context_equities` job runs hourly when both keys are set.
- **Build.** `git` is now in the production image (`RAILPACK_DEPLOY_APT_PACKAGES=git`), so `python -m backfill
  datasets` works in production. It failed in the previous release with "No such file or directory: 'git'".

### Planned but not shipped, and why
- GPU-name cleanup for unmapped rows (`data/unmapped-cleanup`) was still in progress at merge time. It ships in the
  next release.

### Deploy notes
- Migrations: `0033_equities` -> `0029_news_history`. The news migration was re-chained after `0033_equities` at
  merge; it was written against `0026_fx`.
- New variables: `ALPACA_DATA_KEY_ID`, `ALPACA_DATA_SECRET_KEY` (market-data keys) and
  `RAILPACK_DEPLOY_APT_PACKAGES=git`.
- One-offs after deploy, in this order:
  1. `python -m backfill datasets`
  2. `python -m context equities backfill`
  3. `python -m news.history run`
  4. `python -m backfill import --since 2016-01` (the first run downloads archives one at a time)
  5. `python -m analytics.shifts run` (after the history imports, so shifts see them)
  6. `python -m backfill coverage`
- Full suite before push: 126 test files (115 Python, 11 web), all passing except `test_jobs_e2e` ("no usage slice metered"), which fails the same way on the previous release. `test_pages` and `test_developer_docs` read the local dev database, so they were run against a freshly migrated one; a migration from empty reaches the single head `0029_news_history`.

---

## 2026-11-02 — Data release: public/internal tiers, freshness, full cost, 7 providers, AWS history to 2015, Lium live feed

### What shipped

**Public and internal data tiers.** Every listing, capacity row and archived price now carries a `visibility`
(migration `0027_visibility`; `public` by default, CHECK on listings). The one registry of internal sources is
`provider_meta.INTERNAL_SOURCES`, each with its reason (DeepInfra's container-API prices; the AWS Spot Advisor;
Fireworks AI as a whole provider, below); every
other source is public, and `enforce()` fails closed. Every public statistic (market view, rollups, indices, fixings,
spreads, capacity, archived history, legacy JSON, trust) reads the public tier only. Routing, job placement and keyed
recommendations may still use internal rows to choose and order candidates, but a non-admin caller never sees an
internal value: the candidate shows the public list price of the same product or no price (`price_basis: "quote"`,
"available via OpenGrid"), and derived numbers (quotes, savings, comparisons, cost estimates, the stored audit
record) are rewritten. The operator and platform-admin keys see everything, labelled. `tests/test_visibility.py`
seeds unmistakable internal rows and calls every public GET route enumerated from the app (223 requests over 127
routes) plus the public POSTs; it fails on any internal name, price or count. Method: `/methodology/data-visibility`.

**Freshness: when a change happened, and how old each feed is.** Price history gains `prev_observed_at` (the last
fetch that still showed the old value), `provider_changed_at` and `changed_at_basis` (`exact` | `date_only` |
`lower_bound` | `poll_window`; migration `0028_changed_at`). Provider times come from AWS spot `timestamp` and Nebius
`effectiveTime` (exact), Azure `effectiveStartDate` (date only), GCP `effectiveTime` (lower bound) and, new in this
release, the Lium stream's frame `ts` (exact). AWS spot is now read as a change log (DescribeSpotPriceHistory from
the last successful poll), so every change between polls is recorded with its own time. Polling is tiered: a price
and a capacity cadence per provider, a request budget (token bucket) about half of each documented limit, and
rate-limit headers and 429s stored as `throttled` (not failures, not silence). Fast: Lambda, Hyperstack, RunPod 2
min, Vast 5 min; medium: Salad, DigitalOcean, Linode 5 min, AWS and Nebius spot every 5 min, everyone else 15 min;
slow: Azure, GCP, Oracle, IBM, OVHcloud, E2E, Mithril 6 h, pages, Vultr and Exoscale 1 h. `GET /v1/freshness`
(public, like `/v1/capacity`) gives per provider and stream the cadence, data age, p95 age over 24 h, detection lag
over 7 days and throttled answers. Vast and Lium now keep the cost, network and interconnect fields they already
received (Vast `storage_cost`, `inet_up/down_cost`, `dph_base`, `min_bid`, NVLink/PCIe bandwidth; Lium measured
Mbps, NVLink/P2P, InfiniBand/RoCE ports). Method: `/methodology/freshness`.

**True cost of a job (`/cost`).** A `cost_components` job (daily) stores the machine-readable cost sources raw in
`cost_snapshots` (migration `0030_cost_components`): AWS Data Transfer and S3 offer files, Azure Retail Prices
(bandwidth, storage), Oracle's price list, Verda volume types; Hyperstack, Vultr and each Vast offer are read from
provider raw already stored, and a hand table with source URL and verification date covers the other top providers.
`POST /v1/cost/estimate` (public) ranks providers on compute + egress + storage for a job (presets R0-R3 or custom);
an unknown component is named and makes the total a lower bound ("≥ $X"), never $0, and a lower bound never outranks
a complete total. `GET /v1/cost/{components,billing,sources,presets}`. The `/cost` page sits in the GPUs group.
Nothing statistical reads cost data. Method: `/methodology/full-cost`.

**Thirteen providers (47 -> 60).** Seven without keys: Paperspace (via Shadeform's catalogue, Shadeform's resale price, Paperspace
list + $0.04/GPU-h), Akash Network (one weighted bid summary per GPU, bids up to 31 days old), Hydra Host (bare-metal
inventory, cents converted), CUDO Compute (VMs and bare metal), GPU.ai, Exoscale (22 listings; GPU table from
Exoscale's pricing page) and Hugging Face Inference Endpoints (38 listings). GPU.ai and Hugging Face resell other
clouds, so their listings carry `provider_tier` `resold:<vendor>` and, by a new shared rule, are stored and shown but
never enter the market view, rollups, indices or routing validation. Six of them add capacity. Four more from
public price pages, polled hourly:
- **Gcore** (bare metal and Virtual GPU, EUR at the ECB rate; 8 listings). Public on the site and API, but its
  robots.txt sets `Content-Signal: ai-input=no`, so its rows are kept out of everything the OpenGrid Assistant is
  grounded on (and so any LLM phrasing) and out of every MCP tool answer, local `opengrid mcp` and hosted `/mcp`
  (`provider_meta.AI_INPUT_EXCLUDED`, mirrored in the CLI). Aggregates over all providers are unchanged.
- **Fireworks AI** (on-demand deployments per GPU-hour): **internal only**. Its Terms of Service clause (e) forbids
  copying or distributing any part of the Service, so the whole provider is registered in `INTERNAL_SOURCES`: its
  listings are stored internal, routing and keyed recommendations may use them, and no public page, API answer or
  provider list names it. 60 clouds are monitored, 59 published.
- **IONOS Cloud** (H200 PCIe VMs from IONOS CLOUD Inc.'s USD price list) and **Hetzner** (GEX dedicated GPU servers
  at the hourly rate Hetzner prints, capped at the monthly price, EUR at the ECB rate; monthly and setup kept raw).
- **Yotta Shakti Cloud** (on-demand H100 SXM and L40S VMs, INR; GST treatment not stated, kept as printed) and
  **Sakura Internet** (Koukaryoku DOK GPU containers incl. the 8x H100 box, JPY), both hourly. Sakura's prices
  include consumption tax at an unstated rate, so its listings carry `provider_tier` `tax_inclusive` and, like
  `resold:*`, are stored and shown but never vote in market statistics, rollups, indices or routing's market
  reference (`market._ELIGIBLE`; routing explains them as `tax_inclusive_price`). Its H100s are unmapped today (form
  factor not stated), so the rule is preventive.

**Currency cross rates.** `fx.py` now parses every EUR->X quote in the ECB feed (stored bodies re-parsed, so all 29
currencies share the history) and converts any of them to USD through the euro: X->USD = (EUR->USD) / (EUR->X), both
legs from the same ECB date, the same 5-day fail-closed rule, 8 decimals, source `ecb_cross`, legs kept; a currency
below 0.0005 USD (IDR) is refused rather than stored imprecisely. `GET /v1/fx/cross`, `/v1/fx/rates?quote=`, and
`fx_legs` on `/v1/fx/listings`. No migration.

**Archived history from official sources, back to 2015.** The AWS price-list archive now reads all 123 versions from
2015-12 (the pre-2018 files lack columns that used to empty them; 2017-11 gives p3.2xlarge $3.06/h), keeps older
types unmapped (p2, g2, g3, p3dn) and stores reserved 1y/3y at effective hourly price, never as on-demand. Azure
"effective since" anchors: one point per GPU size, region and market at the current price's `effectiveStartDate`
(ND96asr_v4 $27.197/h since 2021-03-01). Open datasets with licences: Full Stack Deep Learning's cloud-gpus.csv
history (MIT, from 2022-08) and Huang (2026) Vast monthly transacted medians (CC BY 4.0, own market `transacted`);
a dataset point that repeats a primary source becomes corroboration on the primary row, never a second point.
Every archived point records `basis`, `licence`, `attribution` and the visibility tier (migration
`0031_backfill_sources`). A daily `gpuhunt_snapshot` job keeps dstack's expiring catalogues raw in
`catalog_snapshots`. `python -m backfill coverage` reports how far back each provider and GPU reaches.

**Lium live feed.** `live/lium_stream.py` consumes Lium's public executor WebSocket (no key): change-only
`stream_events` (migration `0031_stream_events`) with the provider's timestamp and sequence, at most one write a
minute plus the whole book as one raw snapshot in the poller's shape, then Lium is re-normalized, so Lium listings
and capacity follow a change within about a minute instead of fifteen. Sequence gaps and reconnects resnapshot and
diff; a stale book is never written. The REST poller keeps running as the fallback, and the two never normalize Lium
at the same time (`live.locks`). Live price changes reach price history with `changed_at_basis` `exact`. `GET
/v1/live/status` and `/v1/live/events` are operator-only. Method: `/methodology/live-feeds`.

**Market context: power prices and AI capex.** EIA state electricity prices (industrial and commercial, monthly,
public domain) and SEC EDGAR XBRL series (NVIDIA and AMD total and Data Center revenue; capex of Microsoft,
Alphabet, Amazon, Meta, Oracle, CoreWeave, NVIDIA, AMD), raw first in `context_snapshots`, parsed into
`context_series` with publication date, first-seen date and revisions (migration `0032_context_series`). `GET
/v1/context/series` and `GET /v1/context/power` (US listings by state beside the state's electricity price), both
public. Context, never cause: nothing statistical reads them. Each source is off until configured. Method:
`/methodology/market-context`.

**Integration fixes found while merging.** The full-cost estimator read internal listings and returned their
prices on the public `/v1/cost/estimate`, and `/v1/context/power` counted internal listings and named their
providers (both fixed: public tier only; both in the leak test, mutation-checked). `/v1/freshness` never names
a wholly internal provider. Migrations chained linearly. The Vast full-cost reader is tested against an offer
exactly as the Vast poller now stores it.

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| Gcore in market aggregates seen by the assistant | Only Gcore's own rows are withheld from AI input; a market median or provider count that includes it is not recomputed. |
| ENTSO-E and US wholesale (LMP) power prices | No clear reuse licence; the power-exchange and market-operator terms restrict redistribution. |
| SkyPilot catalogue history | `skypilot-catalog` has no licence file. The loader is ready and refuses to run until `SKYPILOT_CATALOG_ENABLED` and `SKYPILOT_CATALOG_LICENCE` name the licence SkyPilot confirm. |
| SF Compute order book | Needs an account token; not ingested. Raw + depth series + transaction index design in `/methodology/live-feeds`. |
| gpuhunt catalogues as prices | Kept raw only: the bucket states no licence for the data. |
| Hostkey / LeaderGPU | `invapi.hostkey.com/robots.txt` disallows every agent. io.net and CloudRift re-checked and stay declined. |
| Exact stream times for Lium stock-only changes | As for every provider, a provider time is attached only when the price moved; stock-only changes keep `poll_window`. |
| GCP networking and storage from the Billing Catalog | Under service ids OpenGrid does not read yet; the hand table is used. |

### Known issues
- Verda publishes no egress prices: its egress is always unknown (a lower-bound total).
- Vast's storage and egress read as unknown until the first Vast poll after deploy (older snapshots lack the fields).
- Paperspace is Shadeform's resale price, not Paperspace's own; Akash is a bid summary, not a list price. Both are
  documented in their module docstrings and provider notes.
- AWS reserved archive rows are stored but not served: the archived API offers `on_demand` and `spot` only.
- `tests/test_developer_docs.py` and `tests/test_pages.py` read the database in `.env`; they fail on a dev database
  that is not migrated to head (they pass on one that is).
- `test_jobs_e2e` fails here and on main ("no usage slice metered").

### Deploy notes
- Migrations run at startup, in order: `0027_visibility` -> `0028_changed_at` -> `0030_cost_components` ->
  `0031_backfill_sources` -> `0031_stream_events` -> `0032_context_series` (one head).
- **Write pause.** `0028_changed_at` builds the partial index `ix_raw_throttled_time` on `raw_snapshots` with a plain
  `CREATE INDEX`, which blocks writes to `raw_snapshots` while it scans the table. Pollers' saves wait for it; expect
  a short gap in fetches during the migration.
- One-off after deploy: `python -m changed_at backfill` (idempotent; fills provider times for the last 30 days of raw
  payloads and brackets for 90 days; `--dry-run` first to see counts).
- Backfills to run in production, in this order (so dataset points corroborate primary rows instead of duplicating
  them). Counts measured on an empty table on 2026-11-01; rows already stored are skipped (idempotent):
  1. `python -m backfill aws --since 2015-12-01`: 123 versions, 3,272 on-demand rows and 34,589 reserved rows
     (17,205 `reserved_1y`, 17,384 `reserved_3y`), 2015-12 to 2026-10. Downloads take about an hour cold.
  2. `python -m backfill azure-since`: about 1,388 points (704 on-demand, 684 spot), earliest 2020-03-01; one request
     per size, 6.5 s apart. The count follows Azure's current API.
  3. `python -m backfill datasets`: FSDL 41 revisions -> 569 row versions, 538 stored and 31 corroborations; Huang
     185 monthly points (Vast, 2022-10 to 2026-07).
  Then `python -m backfill coverage` to check.
- New jobs: `cost_components` (daily), `gpuhunt_snapshot` (daily, `GPUHUNT_SNAPSHOT_ENABLED`, default true),
  `lium_stream` (long-lived supervisor), `context_series` (daily; registered only when `EIA_API_KEY` or
  `SEC_USER_AGENT` is set). `cost_components` and `lium_stream` are off when the poller is off;
  `gpuhunt_snapshot` runs whenever jobs run.
- New variables: `LIUM_STREAM_ENABLED` (default true), `GPUHUNT_SNAPSHOT_ENABLED` (default true),
  `SKYPILOT_CATALOG_ENABLED` (default false) and `SKYPILOT_CATALOG_LICENCE` (unset); leave the SkyPilot ones off.
  `EIA_API_KEY` (free registration at eia.gov/opendata) and `SEC_USER_AGENT` ("Company Name contact@domain", SEC
  fair access): market context stays off until each is set; `/v1/context/*` has no values until then.
- Expected effects: schema-change incidents for Vast and Lium on their first polls (fields added to stored raw; Lium
  also from the stream's first snapshot); a quarantine `duplicate_explosion` hold for any provider whose listing
  count jumps against its norm (new listing ids are withheld until released on /ops); the seven new providers have no
  norm yet, so they are not held. AWS spot polls every 5 minutes with the key.
- Full suite before push: 122 test files (111 Python, 11 web); all pass on the final run. `test_jobs_e2e` is
  intermittent ("no usage slice metered" on earlier runs, here and on main). Two test-only fixes for time-dependent false results: `test_visibility`'s price marker matched a public
  volatility near the internal price on some days, and `test_settlement`'s trigger check could pick a fixing with
  no evidence just after 00:00 UTC (also failing on main then).

---

## 2026-11-01 (fourth release) — AWS spot prices, GCP in 34 regions; Vast and Hot Aisle capacity fixes

### What shipped

**AWS spot prices and placement scores (read-only IAM key).** IAM user `opengrid-market-ro` can only read EC2 market
data (launch is denied; verified). Requests are signed with a small built-in SigV4 signer (no AWS SDK). Spot prices
for every GPU instance type in six regions (us-east-1, us-east-2, us-west-2, eu-west-2, eu-north-1, ap-northeast-1)
become spot listings per Availability Zone (`p5.48xlarge:us-east-1d:spot`), interruptible, per GPU-hour: 669 listings
on 1 Nov, e.g. H100 spot $2.58-4.67 vs $6.88 on-demand. Spot placement scores (1-10) for H100, H200, B200, B300 and A100
instances refresh every 2 hours and show as capacity tiers per region (1-3 low, 4-6 medium, 7-10 high;
methodology/capacity). Capacity Block offerings are fetched daily and stored raw; the new account is not yet allowed
to describe them, which is recorded quietly. Spot Advisor interruption bands stay off public pages and out of routing.

**Google Cloud beyond Iowa (read-only service account).** Service account `opengrid-market-ro` in a dedicated
OpenGrid project (Compute Viewer + calendar-mode advice; creating VMs is denied). The poller reads Compute
Engine's machine and accelerator types per zone every 6 hours and lists a GPU machine type in a region only when a
zone offers the exact proven shape and Google prices every part on demand there: 28 Iowa listings -> 369 in 34
regions. Iowa ids and prices are unchanged except the retired P100 (Google no longer offers it). All 436 per-region
G2/G4 prices on Google's pricing page are reproduced exactly. GCP commitment rows in `/v1/reserved` follow the same
regions (22 -> 258). Calendar-mode advice is stored raw; this project gets "service not available", so no GCP
capacity rows yet. Method: `/methodology/gcp-regions`.

**Hotfixes shipped earlier the same day.**
- Vast (bcb46b8): production capacity went silent at 02:11 UTC because the keyed census (2 searches x every GPU model,
  every 15-minute poll) exhausted Vast's 20,000-rows-a-day search quota. The census now runs on a 16,000-row daily
  budget (1h-12h between runs) and waits out Vast's retry_after on refusal. Providers can declare a slower capacity
  cadence (`capacity_interval_seconds`); the stale flag and capacity-silence alert use it (Vast: 36h / 24h).
- Hot Aisle (4c3dd00, bcb46b8): the available-VMs endpoint answers null or [] when nothing is free; both now read as
  every size sold out instead of a failed fetch with no rows.

### Deploy notes
- New Railway variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `GCP_SERVICE_ACCOUNT_JSON`,
  `GCP_PROJECT_ID`. `deploy/make_env.py` does not copy the GCP ones; they are set by hand.
- AWS adds about 670 spot listings and a vote in the spot indices.
- Full suite before push: 98 of 99 test files pass (Python and web); `test_jobs_e2e` fails here and on main.

---

## 2026-11-01 (third release) — Reserved prices page, ECB currency conversion, Hot Aisle and Crusoe stock

### What shipped

**Reserved prices, in their own section.** A new `/reserved` page (GPUs group) and `GET /v1/reserved` /
`GET /v1/reserved/summary` (public like `/v1/capacity`) show reserved and committed GPU prices, computed on read from
raw responses already stored (no new polling): Azure (1/3/5 years), GCP (1/3-year GPU-only commitments, Iowa),
Jarvislabs (1/3/6 months, 1/2 years), Latitude (month/year plans, USD only), Together (3-30 to 181+ days) and Verda
(its 2-25% ladder applied to on-demand, marked derived). Each row has term, effective $/GPU-hour, whole-term price
where given, the matching on-demand price and the saving (Azure 8x H100: $7.87 1y, $5.40 3y, $4.92 5y vs $12.29
on-demand). None of it enters listings, indices, spreads or statistics; tests prove those are identical with reserved
data stored. Methodology: `/methodology/reserved-prices`.

**Currency conversion at the ECB euro reference rate.** Each ECB response is stored raw; EUR->USD is parsed from it
(migration `0026_fx`). A price uses the rate already published (16:00 Frankfurt) when it was fetched, so weekends and
holidays carry the last rate; if the rate is more than 5 days old nothing is converted (fail closed). Converted
listings stay USD for every statistic and carry provenance (original currency and amount, rate, rate date) in
`/listings`, `/history` and `/v1/fx`. **Scaleway** is the first converted feed: from 1 Nov 2026 its prices are
Scaleway's own EUR list prices, not Shadeform's resale listing, counted once. Existing products keep their listing ids,
so the 1 Nov step of -3% to -7% (H100 $3.30 -> $3.20, L4 $0.95 -> $0.88, B300 $9.01 -> $8.38/GPU) is a source change,
not a market move. Scaleway's H100-SXM 2/4/8 are listed for the first time. Methodology: `/methodology/fx`.

**Hot Aisle stock from its own API** (`HOTAISLE_API_KEY` + `HOTAISLE_TEAM`, the team handle). VM `Quantity` per type
becomes count capacity joined to page skus; prices stay from hotaisle.xyz/pricing because the API lists only in-stock
sizes (its price is cross-checked each poll and never published; it matched the page on 1 Nov). Open item: Hot Aisle's
AUP bans "harvesting or scraping" for account holders, so get written OK for reading the price page.

**Crusoe stock from Crusoe itself** (`CRUSOE_ACCESS_KEY_ID` + `CRUSOE_SECRET_KEY`). HMAC-signed read-only GETs on
`api.cloud.crusoe.ai/v1` (`/capacities`, `/locations`, the VM type catalogue). Capacity is yes/no per type and location
(`quantity` is only ever 0/1). Without the keys, or when the API is over 6 hours behind, capacity falls back to
Shadeform's yes/no; never both. Prices still come from Shadeform (Crusoe's API publishes none).

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| OVHcloud EUR catalogues | They price the same products as the World USD catalogue already read; needs a rule for which list counts. |
| GCP regions beyond Iowa, AWS spot listings | Built separately on the new read-only GCP service account and AWS IAM user; next release. |

### Deploy notes
- New Railway variables: `HOTAISLE_API_KEY`, `HOTAISLE_TEAM`, `CRUSOE_ACCESS_KEY_ID`, `CRUSOE_SECRET_KEY`.
- Migration `0026_fx` runs at startup. The first FX run also loads the ECB 90-day file.
- Full suite before push: 95 of 96 test files pass (Python and web); `test_jobs_e2e` fails here and on main.

---

## 2026-11-01 (second release) — Nebius back, silence alerts, 24 capacity providers, four raw sources

### What shipped

**Nebius prices are back.** The page scraper broke because Nebius added a "Preemptible, GPU-hour" column to its
on-demand GPU table and the scraper rejected any unexpected column. It now stores that column raw and still reads only
the on-demand price, so the 7 on-demand listings return with the same ids. Spot prices now come from Nebius's public,
no-account spot-price API: 12 listings, one per GPU and region (e.g. H100 $0.79 in eu-north1). The two sources fail
independently; a spot response with no prices is a failure, never an empty market. On-demand is still a scraped page.

**Alert on silence** (to-do 1 of 2026-10-30). A `silence_alerts` job (every 10 min) sends one ops alert when a polled
provider has had no successful fetch for max(3h, 6 polls), one when its current listings drop to 0 after having some,
and one when its capacity feed stops while prices still arrive; each sends one "recovered" alert and never repeats while
open. State is in `ops_alert_state`, so restarts don't re-send. Unpolled (no key), killed and `SILENCE_ALERT_MUTED`
providers never alert. Settings: `SILENCE_ALERT_MIN_HOURS` / `_POLLS` / `_EMPTY_GRACE_MINUTES`, `SILENCE_ALERTS_ENABLED`.

**Capacity now covers 24 providers**, adding Clore.ai (free of total GPUs per model; free = no on-demand rental),
Thunder Compute (in stock per instance size, `/v2/status`) and Salad (free nodes per GPU class at each of four
priorities; cumulative estimates, never added up). Salad's `is_high_demand` is kept in raw_value only: Salad does not
define it. All from data we already fetch with our own keys.

**Capacity hotfix (shipped earlier today, 68ea227).** When a provider changes capacity source, current rows from the
source it no longer uses are removed (Massed Compute's 166 old Shadeform rows beside its 81 API rows).

**Four sources stored raw** (no listings, capacity or statistics yet; see methodology/data-kinds.md):
- Scaleway's own EUR catalogue (incl. H100-SXM types Shadeform lacks). Listings stay on Shadeform's USD prices because
  OpenGrid converts no currencies; needs an FX policy first.
- AWS Spot Instance Advisor: interruption band and saving per GPU instance type and region (feed last updated by AWS on
  2026-03-20). AWS's site terms restrict data gathering on aws.amazon.com; decide before showing it publicly.
- OVHcloud bare-metal GPU stock (World and US APIs); those servers are not listings, so no capacity rows.
- Together reserved rates (3-30 / 31-90 / 91-180 / 181+ days), kept apart like Azure's reserved prices.

**Fixes.** Verda `.CC.AMD` / `.CC.INTEL` types are tagged confidential. "RTX PRO 6000" maps to the Server Edition for
Verda and Nebius only (their own pages/docs say so); the generic name stays unmapped for other providers. The missing
`tests/fixtures_backfill/runpod_2026-10.html` is restored from Internet Archive capture 20261019070258.

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| Scaleway prices from its own catalogue | No currency conversion in OpenGrid; EUR listings would mix into USD statistics. Needs an FX rule (e.g. ECB daily rate). |
| Showing AWS spot advice, OVH stock, reserved prices | Raw only until the data model / terms decisions are made. |
| Oracle RTX PRO 6000 edition | Oracle's launch blog (says Server Edition) could not be read; left unmapped. |

### Deploy notes
- Full suite before push: 81 of 82 test files pass (`test_jobs_e2e` fails here and on main).
- No new variables. Expect one silence alert per provider that is already broken, shortly after startup.

---

## 2026-11-01 — 47 providers merged; Verda, Massed Compute and DeepInfra keys; archived price history

### What shipped

**Merged into main:** the `history/archived-backfill` branch (15 more GPU clouds, 32 -> 47, incl. Thunder Compute and
Clore.ai; archived price history from public archives, kind `archived`, never in statistics) and the Azure reserved
prices entry below. Merge notes:
- Both branches added a `0025` migration off `0024_probes`. `0025_archived_prices` now runs after `0025_capacity`,
  so there is one head. It runs automatically at startup.
- The backfill read archived Jarvislabs and GCP pages through the live page scrapers, which main replaced with APIs.
  The old scrapers are kept unchanged in `backfill/retired/` (never polled live) for archived captures only.

**Verda capacity from its own API** (needs `VERDA_CLIENT_ID` + `VERDA_CLIENT_SECRET`, Cloud API credentials). Yes/no
per instance type, location (FIN-01/02/03) and market from `GET /v1/instance-availability`, asked separately for
on-demand and spot; the capacity sku is the listing id, so every row joins to a price. Example at ship time: 8x H100
on-demand only at FIN-02; 8x B200 spot at FIN-03 but no on-demand anywhere. Prices stay public and work without the
credentials. The commitment ladder (2/3/4/8/25%), the 56-entry cluster catalogue and cluster availability are stored
raw; none becomes a listing yet. The OAuth secret and token are never stored. The credentials only enable GETs in the
poller; launching on Verda still needs every execution gate (validation allowlist, admin-approved validation, per-
provider flags, per-launch admin approval).

**Massed Compute moved to its official API** (needs `MASSEDCOMPUTE_API_KEY`, a read-only token from
vm.massedcompute.com/settings/api). Prices and capacity come from `GET https://vm.massedcompute.com/api/v1/gpu-inventory`
(`Authorization: Bearer`). The earlier 401 came from `api.massedcompute.com`, which the docs say not to use. Capacity
is now a count (`capacity_available` instances per product, in-stock regions in `raw_value`), replacing Shadeform's
yes/no; Shadeform is not read for Massed Compute when the key is set. All 74 page listings carry over unchanged and 7
spot products are added. Without the key, or if the API fails, the public /pricing page is still read.

**DeepInfra stock from its container API** (needs `DEEPINFRA_API_KEY`). Prices still come from the public
/gpu-instances page. The key adds DeepInfra's in-stock flag per GPU configuration as yes/no capacity, joined by sku.
The API's own hourly prices are stored raw but never published: on 2026-11-01 it priced a B200 at $3.69/GPU-hr
against $4.89 on the public page, which may be specific to our account, and DeepInfra's terms (s.17) treat non-public
pricing as confidential.

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| Denvr API | Key not created yet. |
| Publishing DeepInfra API prices | Possibly account-specific and confidential (see above). Ask DeepInfra first. |

### Known issues at ship time
- Full suite before push: 76 of 78 test files pass. `test_jobs_e2e` fails here and on main before this merge.
  chaos, events, limits, mcp_remote and money_invariants failed on main in a parallel run and pass here; several
  suites are date/timezone dependent, and tests that share the dev database interfere when run concurrently.
- `test_backfill_lambda_runpod` needs `tests/fixtures_backfill/runpod_2026-10.html`, which was never committed.
- Verda: `.CC.AMD` / `.CC.INTEL` types are not tagged confidential (check is `endswith(".CC")`), and
  `RTX PRO 6000 96GB` has no canonical GPU name.

### Deploy notes
- New Railway variables: `VERDA_CLIENT_ID`, `VERDA_CLIENT_SECRET`, `MASSEDCOMPUTE_API_KEY`, `DEEPINFRA_API_KEY`,
  `THUNDER_API_KEY`, `CLORE_API_KEY`. Pushed to main before the variables were changed.

---

## 2026-10-31 — Azure reserved (1 / 3 / 5-year) prices, stored raw

### What shipped

**Azure reservations captured from now on.** The Azure provider makes a second filtered GET per region on the
same public Retail Prices API, with `priceType eq 'Reservation'`. There is no new auth and no new host.
- The filter is applied server-side: one region, Virtual Machines, `Standard_NC` / `ND` / `NV` sizes only, no
  Windows. That is about 100 rows a region (1, 3 and 5-year terms) on one page. `NextPageLink` is followed, up to
  the same 3-page cap as consumption, and more than that fails the call.
- A poll is now 4 requests (2 regions x consumption and reservation). Azure allows about 10 a minute. Raw bodies
  are deduplicated, so an unchanged price list adds no new payload.
- Stored raw like the GCP commitments and Jarvislabs' `reserved_pricing`: its own endpoint,
  `/api/retail/prices/reservation#<region>`, with payload `{region, price_type: "Reservation", items}`. The items
  are the consumption fields plus `reservationTerm`, verbatim.
- Fails closed like the consumption call: a missing field, a foreign region or type, or no known GPU size fails
  that snapshot only. Consumption prices are never affected.

**Effective $/GPU-hour, as a pure function.** `providers/azure.py: reservation_price(row)` returns the term,
region, VM size, GPU model (raw and canonical), GPU count, whole-term price, term hours and $/GPU-hour.
`retailPrice` is the whole-term price of the whole VM, even though the row says "1 Hour":
`retailPrice / (8,760 x years) / GPUs`. GPU counts come from the same `GPU_SIZES` table as the listings, so a size
not in the table (GB200, fractional A10 / RTX PRO, `ND96is_H100_v5`) is stored but never converted.
`reserved_prices(by_endpoint)` reads the current ones back out of stored snapshots.
- At ship time, eastus `Standard_ND96isr_H100_v5` (8 x H100) cost $12.29 per GPU-hour on demand. Reserved, it was
  $7.87 for 1 year ($551,221), $5.40 for 3 years ($1,134,310) and $4.92 for 5 years ($1,722,566).

**Kept out of every statistic.** `normalize()` now reads the consumption endpoints by exact label, and it skips any
payload tagged with another price type. Tests prove there are still 22 listings, only `on_demand` / `spot`
segments, and unchanged index votes (H100 SXM still $12.29) with reservation snapshots stored.

**Methodology:** `/methodology/data-kinds` has a new section, "Reserved and committed list prices".

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| Reserved prices as listings, or a read-only API / page | There is no safe non-statistical price kind in the listing model: `market_type` feeds index segments. It still needs the reserved-price data model decision. |
| More Azure regions | Same two regions as the on-demand listings, so the reserved and on-demand prices stay comparable. |
| Azure savings plans | A different commitment: $/hour spent across VM sizes, not a reservation of one size. Not read. |

### Next
1. The reserved-price data model, then one read-only reserved view across Azure, GCP, Jarvislabs and Latitude.

---

## 2026-10-30 — Capacity tab, provider APIs instead of scraping, sign-in page

### What shipped

**Capacity (new data product).** Each provider's own stock signal, stored raw first, then normalised into one shape:
in stock yes/no (always), a tier (low / medium / high) when the provider gives one, and counts when it gives them.
Current state plus a change-only history.
- 19 providers report capacity:
  - **Free + total GPU counts:** Lium, Vast (new).
  - **Free counts:** Hyperstack (machines per size), Jarvislabs, Voltage Park, Latitude (new, servers per location).
  - **Tiers:** RunPod, Koyeb, Scaleway.
  - **Yes/no:** Lambda, DigitalOcean, Linode, Crusoe, Denvr, Boost Run, Horizon, Phyntec, Massed Compute, Hyperbolic.
- API: `GET /v1/capacity` and `GET /v1/capacity/history` (public, documented on /api).
- Page: `/capacity` (GPUs group in the nav). Methodology: `/methodology/capacity`.
- Database: migration `0025_capacity` (tables `capacity_current`, `capacity_changes`).
- Capacity runs after the price refresh and fails open: a capacity error never touches prices.

**Vast capacity census** (needs `VAST_API_KEY`). Free and total GPUs per GPU model, from Vast's own fields.
Free GPUs come from the `gpu_ids` in rentable offers. Each machine's size comes from `num_gpus` / `gpu_frac`.
Example at ship time: H100 SXM had 8 of 101 GPUs free.

**Latitude capacity from its own API** (needs `LATITUDE_API_KEY`). Stock level and in-stock server count per
location. This replaces Shadeform's yes/no. Latitude prices still come through Shadeform.

**Scraping replaced by official APIs:**
- **Jarvislabs** (`misc/server_meta`, needs `JARVIS_API_KEY`). Prices per region and workload, plus spot prices
  and live free-GPU counts. Reserved prices are stored raw.
- **GCP** (Cloud Billing Catalog API, needs `GCP_API_KEY`). VM prices are assembled from Google's GPU, vCPU, RAM
  and SSD SKUs. Each of the 24 machine shapes is proven against Google's published Iowa prices. Spot, commitment
  and Calendar Mode prices are stored raw.

**Sign-in page.** A styled `/login` form with a signed session cookie that lasts 7 days, plus `/logout`, replacing
the browser's password popup. Basic auth still works for scripts. Changing `APP_PASSWORD` signs everyone out.

**Also:**
- `deploy/restore.sh` can prompt for the Postgres values instead of needing a full URL.
- `docs/PROVIDER_API_SURVEY.md`: what each of the 32 providers exposes (capacity, reserved pricing, capacity blocks).
- The /api reference now lists the capacity, uptime and probe endpoints. They were public but undocumented.

### Planned but not shipped, and why

| Item | Why not |
|---|---|
| Nebius capacity (tier plus count per region and machine size) | Works with a personal login. The read-only service account the server needs was not created: the step that creates the account and its key file was blocked for approval. Commands are ready. |
| Massed Compute counts (`capacity_available`) | The key in `.env` gets a 401. Massed Compute issues API tokens by email (techadmin@massedcompute.com). |
| GCP regions beyond Iowa, and GCP capacity (`advice capacity`) | Both need a GCP service account. The API key only reads the price catalogue. |
| Crusoe counts (`/capacities` returns `quantity`) | Needs a Crusoe account. |
| Reserved / committed prices as listings | Already stored raw for Jarvislabs, GCP and Latitude. It needs a data model decision first (term, upfront, per-GPU-month vs hour). |
| A shared tier standard across providers | Waiting on a decision. Nebius publishes the only written tier definitions so far, which makes it a good template. |
| A "market vs account" flag on counts | Needed before Nebius goes live: its counts are capped at our quota (8 GPUs), so they must show as "8+". Not built yet. |
| Latitude prices from Latitude's own API | Out of scope for this update; still via Shadeform. |
| Akash (public free + total GPU counts) | Not one of our 32 providers; waiting on a decision to add it. |

### Known issues at ship time
- 6 test suites fail on this commit and on the commit before it, so they predate this work: chaos, jobs_e2e,
  limits, reconcile, stats, structure. Most failures show New York times, which suggests they depend on the
  timezone or the date. Not investigated yet.
- At startup the analytics rollup can log a duplicate-key error on `structure_provider_daily` when two refreshes
  run at once. It is logged and skipped, and prices and capacity are unaffected. Not fixed yet.
- Most counts cover the whole market. Exceptions: Nebius's counts will be capped at our quota, and Crusoe's may be.
  See the methodology page.
- Found after shipping:
  - **Jarvislabs had no prices from Oct 25 to Oct 30.** The old pricing-page scraper broke around midday on
    Oct 25 and failed every poll until today's switch to the API. The gap cannot be backfilled: the failed polls
    stored errors, and the API has no history.
  - **Nebius has had no prices since Oct 19** (the last good fetch was Oct 19 15:55 UTC). Every poll of its
    pricing page has failed since, and it has 0 listings now.
  - Neither break raised an alert. The health view marked them "degraded" / "down", but nothing notifies anyone.
- Railway's `railway.toml` config format stops working on 2026-12-01. Run `railway config migrate` before then.

### To do: treat every non-API source as likely to break
Any price or capacity feed that isn't read from an official API or CLI should be assumed to fail eventually
and silently, whether it's a scraped page or a third party's listing. Jarvislabs and Nebius both broke this way.
1. **Alert on silence.** Raise an ops alert when any provider has had no successful poll for more than a few
   hours, and again when its listings drop to 0. Highest priority: it would have caught both breaks within hours.
2. **Label the source on every row.** Put each listing's and capacity row's source type in the API and on the
   page: official API / CLI, published pricing file, scraped page, or via an aggregator. Then readers can weigh
   scraped data accordingly.
3. **Move every scraped provider to an official source, or flag it as fragile.**
   - **Scraped pages:** Nebius (down since Oct 19; has an API, needs the service account), CoreWeave, Together,
     Hot Aisle, Massed Compute (the API needs the emailed token).
   - **Via Shadeform (secondhand):** Crusoe, Latitude prices, Denvr, Boost Run, Horizon, Phyntec, Scaleway
     prices. These are not broken today, but a third party's listing can lag or be wrong.
   - **Pricing files:** AWS, Oracle. Official, but parsed from files, so a format change breaks them.
4. **Fail loudly on shape changes.** A page that parses to zero rows should count as a failure, never as a
   provider with nothing for sale. Most scrapers already do this; check that all of them do.

### Next
1. Alerts on silence (to-do 1), then Nebius prices back via its API (service account), plus Nebius capacity
   with the market vs account flag.
2. Fix the 6 date-dependent test suites so a red test means a real problem again.
3. Reserved pricing data model, then turn on the stored reserved and commitment prices.
4. Accounts to request: Massed Compute token, GCP service account, Crusoe, Oracle, AWS.
5. Agree the tier standard.

### Deploy notes
- Production needs these keys in Railway (web service `opengrid-terminal`): `JARVIS_API_KEY`, `GCP_API_KEY`,
  `VAST_API_KEY`, `LATITUDE_API_KEY`. Without the first two, Jarvislabs and GCP drop out of production, because
  their scrapers are gone.
- `0025_capacity` runs automatically at startup. It was checked on a clean database: up from 0024, down, and up
  again.

---

## 2026-10-23 — Uptime and probes tabs, roadmap

- **Shipped:**
  - Provider uptime from UptimeProject, with a 90-day backfill.
  - OpenGrid's own probes, which store raw results and tell regional outages apart from global ones. Probe agents
    run in EU-West and Southeast Asia.
  - `docs/ROADMAP.md`.
- **Incident:** a Railway variable change rebuilt production from an out-of-date GitHub `main`, which caused about
  10 minutes of 502 errors. Rule since then: push to `main` before any deploy or variable change.
