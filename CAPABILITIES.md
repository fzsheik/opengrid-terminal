# OpenGrid Terminal: Capabilities & Features

**OpenGrid Terminal is a market-intelligence and execution terminal for GPU compute.**
It watches 60 GPU clouds, turns their prices into one trustworthy market, explains what is moving
and why, and routes a workload to the best provider through one OpenGrid API key, with every
decision audited and every dollar reconciled.

> Think: Bloomberg Terminal + CoinMarketCap + Kayak + OpenRouter + smart order routing, for GPU
> infrastructure.

This document describes everything the system does today, how it does it, and (just as
important) what it deliberately does not claim.

---

## Contents

1. [At a glance](#1-at-a-glance)
2. [Architecture](#2-architecture)
3. [Market data layer](#3-market-data-layer) (incl. capacity, live feeds, public and internal data tiers)
4. [Market intelligence](#4-market-intelligence) (incl. 4.2a settlement-grade fixings, reserved prices, full cost, RL cost)
5. [News & external signals](#5-news--external-signals)
   - [Market context, equities and power markets (beside GPU prices, never mixed)](#5a-market-context-equities-and-power-markets-beside-gpu-prices-never-mixed)
6. [Execution layer: routing & provisioning](#6-execution-layer-routing--provisioning)
   - [Training jobs (OpenGrid Jobs)](#6a-training-jobs-opengrid-jobs) (incl. relaunch, resume, OOM escalation, move, batches)
   - [Reserved compute: price-lock requests](#6b-reserved-compute-price-lock-requests-reservations-methodologyreservationsmd)
   - [Workload intelligence, CLI, MCP and assistant](#6c-workload-intelligence-cli-mcp-and-assistant-opengrid-20)
7. [Reconciliation, billing & cost accounting](#7-reconciliation-billing--cost-accounting)
8. [Routing quality, reliability & economics](#8-routing-quality-reliability--economics) (incl. provider uptime and probes)
9. [Design partners, onboarding & product analytics](#9-design-partners-onboarding--product-analytics)
10. [Alerts & Discord channels](#10-alerts--discord-channels)
11. [Accounts, API keys & security](#11-accounts-api-keys--security)
12. [Web terminal (UI)](#12-web-terminal-ui)
13. [Text terminal (TUI)](#13-text-terminal-tui)
14. [Public API](#14-public-api) (the Data API v1 contract and the Compute API)
15. [Operations: deployment, jobs, configuration](#15-operations-deployment-jobs-configuration)
16. [Testing](#16-testing)
17. [Data honesty principles](#17-data-honesty-principles)
18. [Current status & known limits](#18-current-status--known-limits)
19. [Methodology documents](#19-methodology-documents)

---

## 1. At a glance

Counts recomputed from the code on 2026-11-13.

| | |
|---|---|
| GPU clouds monitored | **60** polled, 59 published, out of **61** registered (`providers.PROVIDERS`; Alibaba Cloud is built but dormant until an AccessKey is set, section 3.1) (Fireworks AI is the only wholly internal provider: used for routing, never republished; two more sources of public providers are internal, DeepInfra's container-API prices and AWS's Spot Advisor bands, section 3.8) (AWS, Google Cloud, Microsoft Azure, Oracle Cloud, IBM Cloud, Lambda, RunPod, CoreWeave, Together AI, Vast.ai, Salad, Hyperstack, Hyperbolic, Nebius, DigitalOcean, Vultr, Lium, Massed Compute, Verda, Voltage Park, Jarvislabs, Hot Aisle, Crusoe, Denvr, Latitude.sh, Boost Run, Horizon Compute, Phyntec, Scaleway, Akamai/Linode, OVHcloud, Koyeb, Civo, Atlantic.Net, E2E Networks, Modal, Replicate, Baseten, Beam, Arkane Cloud, VESSL AI, Mithril, DeepInfra, SimplePod, Nosana, Thunder Compute, Clore.ai, Paperspace, Akash Network, Hydra Host, CUDO Compute, GPU.ai, Exoscale, Hugging Face Inference Endpoints, Gcore, Fireworks AI, IONOS Cloud, Hetzner, Yotta Shakti Cloud, Sakura Internet) |
| Canonical GPU models | **100**, variants never merged (H100 SXM ≠ H100 PCIe ≠ H100 NVL); each has a hardware spec row (100 in `hardware.py`) |
| OpenGrid indices defined | **269**: 100 per-GPU, 100 spot, 40 regional, 25 provider-class, 4 composites (H100 / H200 / A100 class and the overall GPU Compute Index) |
| News & signal sources | **81** registered, **66** enabled (41 RSS, 4 Atom, 3 US Federal Register, 16 SEC EDGAR, 2 GDELT queries); the 15 disabled ones are listed with the reason. About 68k items stored in production (67,932 on 2026-11-14), history backfill included |
| API operations | **249** in the OpenAPI schema: 227 under `/v1` (74 of them under `/v1/ops` or `/v1/admin`), 15 under `/internal` (the website's own reads) and 7 operator/debug routes at the root. Outside the schema: 9 admin-only reads (7 power, 2 equities), 5 runner-callback routes (events, instructions, artifacts, resume, complete), `/v1/openapi.json`, the hosted MCP endpoint `/mcp`, the site login (GET form, POST) / logout, `/classic` (redirect), `/robots.txt`, `/sitemap.xml`, FastAPI's `/docs`, `/redoc`, `/openapi.json` and the 45 page routes |
| Web pages | **45** routes: Home plus five destinations (Explore: GPUs, Find Compute, Markets, Developers; Your workspace: My Jobs), operator tools hidden, access indicator, light/dark themes, keyboard command bar. RL cost (`/rl-cost`) sits under GPUs; Batches (`/batches`, `/batch/{id}`) under My Jobs |
| Database tables | **109** in a freshly migrated database (108 application tables plus `alembic_version`; Postgres, migrated by Alembic: 40 migrations, head `0038_measured_performance`) |
| Background jobs | **33** registered when every optional source is configured (**30** with the defaults: no EIA / SEC / Alpaca keys, `POWER_ENABLED` off), beside the per-provider poll loops (rollups, indices, settlement fixings, events, shifts, news, reconciliation, tracking, training-job sync, relaunch and move, batch queues, reservation expiry, alerts, feeds, FX, full cost, capacity streams, uptime and probes, market context, equities, power markets…; table 15.2) |
| Capacity (stock) feeds | **32** providers report in-stock state or counts from their own APIs (section 3.6) |
| Power markets (internal) | EIA-930 grid data, EIA natural gas, CAISO, NYISO, SPP and ERCOT wholesale prices in their own `power_*` tables (about 490 MB in production on 2026-11-14); admin-only reads, never in a GPU statistic (section 5a) |
| Provisioning adapters | **9 providers** (Lambda, RunPod, Hyperstack, DigitalOcean, Crusoe/Denvr/Latitude via Shadeform, Vast, Verda) |
| Discord channels | **4** (ops, deployments, market, news) with OpenGrid-branded embeds |
| Training jobs | **1** allowlisted template (PyTorch MNIST) end to end: preview → approval → GPU → model artifacts → automatic termination. Built on top, tested against fake provider APIs only: **batches** (a sweep or shard list as one batch: placement across clouds, one budget, one approval), **resume** from a verified checkpoint, opt-in **OOM escalation** and **move** to another GPU; automatic relaunch / OOM escalation and move are off by default (`JOB_AUTO_RELAUNCH_ENABLED`, `JOB_MOVE_ENABLED`) |
| Settlement fixings | Hourly + daily TWAP fixings for **6** settlement indices, immutable, hash-chained, publicly verifiable |
| Workload intelligence | GPU memory estimator (text LLMs; recommendations also size audio, image + text and video workloads, section 6c), measured performance from completed real jobs (none yet), project-aware recommendations (`POST /v1/recommend`), local project analyzer, `opengrid` CLI, MCP server (hosted at `/mcp` with an API key: 15 tools, 2 prompts; local via uvx: 16 tools, 3 prompts), grounded assistant, RL post-training cost estimator |
| Automated tests | **136** Python test files + **14** JavaScript test files, all 150 run in parallel by `tests/run_all.py` (and by CI on every push), + **5** CLI/MCP test files: unit, integration, end-to-end, concurrency, chaos, money-loss invariants, red team, data-visibility leaks, API contract |

---

## 2. Architecture

```
                    ┌──────────────────────── 60 GPU clouds ───────────────────────┐
                    │ APIs · public price pages · pricing files · aggregator feeds │
                    └──────────────────────────────┬────────────────────────────────┘
                                                   │ independent poll loop per provider
                                                   ▼
   raw_snapshots + raw_payloads ──► normalize ──► quality screen ──► compute_listings
   (every response, deduplicated)   (per-provider)  (quarantine bad     (current state)
                                                     values)               │
                                                                           ▼
                                               listing_observations (change-only history)
                                                                           │
          ┌───────────────────────────── market_hourly rollup ─────────────┴───────┐
          ▼                    ▼                  ▼                 ▼               ▼
      indices          dispersion &        market events      heatmaps &     historical
   (index_levels)      provider value      (market_events)    comparisons    percentiles
          │                    │                  │                                │
          └──────────────┬─────┴──────────────────┴──────────────┬─────────────────┘
                         ▼                                        ▼
                 Web terminal + /v1 API                 Discord channel feeds

   Route request ─► rank (scoring) ─► quote ─► approval ─► guards (Postgres locks) ─► adapter.provision
        │                                                                                │
        └── audit log ◄── deployments state machine ◄── tracker + reconciliation ◄──────┘
                                   │
                          usage slices ─► billing ─► cost reconciliation ─► transactions

   Training job ─► workload-aware selection ─► the route path above (quote · approval · launch)
        │            cloud-init runner on the GPU ─► callbacks (logs · metrics · artifacts) ─► job_sync
        └── job state machine ◄── automatic termination + reconciliation ◄──────────────────────┘

   Batch ─► placement plan (diversification cap) ─► budget ledger (advisory lock) ─► N ordinary training jobs
        └── batch_advance: queue · failure policy · one batch approval · relaunch / resume / move inside the budget

   index_levels ─► index_fixings (final hours only) ─► hash-chained ledger ─► /v1/fixings + verify

   Beside the market, never inside it (own tables; nothing statistical reads them):
   context_snapshots ─► context_series (EIA state power prices, SEC EDGAR; equities internal) ─► /v1/macro/*
   power_fetches ─► power_days ─► power_gpu_region_daily + power_leadlag (internal) ─► /v1/ops/power/* (admin)
```

**Stack:** Python 3.13 · FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL · httpx · Textual (TUI) ·
vanilla JavaScript (no build step) · deployed on Railway as a single web process that also runs
the poller and background jobs. The public customer docs (`docs-site/`, Next.js + Fumadocs) are a separate site on
Vercel at docs.tryopengrid.com.

**Design rules that shape everything:**
- Raw first: provider responses are stored before they are interpreted, so normalization can be
  re-run over history.
- Change-only history: an observation row is written only when a price or availability changes.
- One vote per provider in every market statistic, so a provider with 40 listings can't dominate.
- "Insufficient data + reason" beats a number: every statistic that lacks coverage says why.
- Money-safety state lives in Postgres (locks, unique constraints), never in process memory.

---

## 3. Market data layer

### 3.1 Providers and sources

Each provider is one module with `fetch()` (network) and `normalize()` (pure, re-runnable).
Every provider polls on its own schedule; a slow or failing provider never holds up the others. Polling is
tiered (methodology/freshness.md): each provider has a price and a capacity cadence and a request budget (a token
bucket at about half of each documented limit), and rate-limit answers are stored as `throttled`, not as failures.
Fast: Lambda, Hyperstack, RunPod 2 min, Vast 5 min, Verda (capacity 2 min, prices 6 min); medium: Salad, DigitalOcean, Linode, GPU.ai 5 min, AWS spot every
5 min on the clock (:00:30, :05:30: AWS reprices on the hour), Nebius spot 5 min, most others 15 min; slow: GCP 1 h
(Spot), Azure, Oracle, IBM, OVHcloud, E2E, Mithril 6 h, scraped pages, Vultr and
Exoscale 1 h. Lium is also read live from its public WebSocket (section 3.7).

| Provider | Class | How OpenGrid reads prices |
|---|---|---|
| Alibaba Cloud | hyperscaler | authenticated API (ECS: GPU spot per zone and the pay-as-you-go price it is stated against); **dormant**: not polled until `ALIBABA_ACCESS_KEY_ID` + `ALIBABA_ACCESS_KEY_SECRET` are set |
| AWS | hyperscaler | public pricing files (on-demand in 15 regions; Compute Savings Plans, reserved section only); with a read-only IAM key, the EC2 API for spot prices per AZ in 15 regions and spot placement scores (six regions) (capacity tiers) |
| Google Cloud | hyperscaler | Cloud Billing Catalog API, in every region Compute Engine offers the shape (read-only service account; us-central1 without it; attachable T4/V100 are GPU-only and kept out of market statistics); on-demand and Spot (Spot proven against Google's page; H100 x1/2/4 and B200 Spot-only shapes) |
| Microsoft Azure | hyperscaler | public API (Retail Prices, eastus + eastus2) |
| Oracle Cloud | hyperscaler | public pricing file (global list prices) |
| IBM Cloud | general cloud | public API (Global Catalog; on-demand and spot) |
| Lambda | neocloud | authenticated API |
| RunPod | neocloud | authenticated API: pods from REST v2 `GET /v2/catalog/gpus` (8 GETs, cloud × size 1/2/4/8; `RUNPOD_API_KEY` with `catalog:read`); without a key, or in a round where REST fails, pods from the keyless GraphQL API; serverless pools' flex and active prices from GraphQL `gpuPools` (listed, never in market statistics) |
| Hyperstack | neocloud | authenticated API |
| Thunder Compute | neocloud | authenticated API (account key, as its terms require; live stock) |
| Jarvislabs | neocloud | authenticated API (`misc/server_meta`, `JARVIS_API_KEY`: regions, on-demand and spot prices, reserved terms, free GPUs; replaced the page scraper 2026-10-30) |
| Massed Compute | neocloud | authenticated API (`gpu-inventory`, `MASSEDCOMPUTE_API_KEY`: prices and launchable counts); the public price page without the key or when the API call fails |
| DigitalOcean | general cloud | authenticated API |
| Salad | decentralized | authenticated API |
| Vast.ai | marketplace | public API |
| Hyperbolic | marketplace | public API |
| Lium | decentralized | public API: free machines, plus the whole fleet incl. rented machines (rented stay listed, unavailable, at their current price; capacity = real utilisation) and Lium's 30-day paid-price stats (raw) |
| Nosana | decentralized | public API |
| SimplePod | marketplace | public API (open on-demand offers, Docker and VM) |
| Clore.ai | marketplace | authenticated API (USD price per server per day plus the renter fee; median and cheapest ask) |
| Verda (ex-DataCrunch) | neocloud | public API |
| Voltage Park | neocloud | public API |
| Nebius | neocloud | public page (on-demand); spot per region from its public spot-price API (no key) |
| CoreWeave | neocloud | public page (on-demand and spot kept separate) |
| Together AI (GPU Clusters) | neocloud | public page (per GPU-hour; preemptible = spot) |
| Hot Aisle (AMD MI300X) | neocloud | public page; a `HOTAISLE_API_KEY` (+ `HOTAISLE_TEAM`) adds VM stock counts from its own API; prices stay from the page |
| Vultr | general cloud | public API |
| Akamai/Linode | general cloud | public API |
| OVHcloud | general cloud | public API (the two USD catalogues, US and World) |
| Koyeb | general cloud | public API (serverless GPU instances) |
| Civo | general cloud | public page (on-demand hourly) |
| Atlantic.Net | general cloud | public page (on-demand hourly) |
| E2E Networks | general cloud | public pricing file (its USD on-demand list) |
| Replicate · Baseten | general cloud | public page (serverless, whole-instance price per second) |
| Modal · Beam | general cloud | public page (serverless GPU-only rate; CPU/RAM extra, kept out of indices) |
| Arkane Cloud · VESSL AI · DeepInfra (GPU instances) | neocloud | public page (on-demand, per GPU-hour); DeepInfra: a `DEEPINFRA_API_KEY` adds in-stock flags from its container API; prices stay from the page |
| Mithril (ex-ML Foundry) | neocloud | public page (spot, published 7-day average) |
| Scaleway | general cloud | public API (its own EUR catalogue, converted to USD at the ECB reference rate; via Shadeform until 2026-11-01) |
| Akash Network | decentralized | public API (Akash Console's bid-derived summary: allocatable-weighted average of each provider's chosen bid, up to 31 days old; free/total GPUs) |
| Hydra Host | marketplace | public API (bare-metal inventory per device, cents per hour; on-demand and interruptible; stock word per device) |
| CUDO Compute | neocloud | public API (VM and bare-metal machine types; smallest 1-GPU VM config; free GPUs / servers per data centre) |
| GPU.ai | marketplace (reseller) | public API (offerings with launchable counts; its terms say it aggregates other providers' inventory, so listings are `resold:undisclosed` and kept out of market statistics) |
| Exoscale | general cloud | public API (USD map of its price feed; GPU model, count and zones per family from its own pricing page) |
| Hugging Face Inference Endpoints | general cloud (reseller) | public API (managed endpoints on AWS/GCP/Azure; listings are `resold:<cloud>` and kept out of market statistics) |
| Gcore | general cloud | public page (GPU Cloud bare metal and Virtual GPU; EUR list prices converted to USD at the ECB reference rate; "Contact sales" servers not read; public on the site and API but kept out of the AI assistant and MCP answers: its robots.txt sets `Content-Signal: ai-input=no`) |
| Fireworks AI | general cloud | public page (on-demand dedicated deployments, USD per GPU-hour; **internal only**: its terms forbid republishing) |
| IONOS Cloud | general cloud | public page (IONOS CLOUD Inc.'s USD price list on docs.ionos.com, Markdown; Cloud GPU VM templates, H200 PCIe) |
| Hetzner | general cloud | public page (dedicated GEX GPU servers at the printed hourly rate; EUR converted to USD at the ECB reference rate; one-time setup fee not added) |
| Yotta Shakti Cloud | neocloud | public page (on-demand GPU VMs, H100 SXM and L40S; INR converted at the ECB cross rate; GST treatment not stated) |
| Sakura Internet | general cloud | public page (Koukaryoku DOK GPU containers billed per second, incl. the 8x H100 box; JPY converted at the ECB cross rate; prices tax-inclusive, marked) |
| Crusoe · Denvr · Latitude.sh · Boost Run · Horizon Compute · Phyntec · Paperspace | neocloud | aggregator (via Shadeform; Paperspace a few cents above its own list) |

Candidates checked and **not** added (2026-10-13): Paperspace (its own page contradicts the aggregator),
Thunder Compute (terms forbid anonymous automated access), TensorWave ("starting at" floors only), Fluidstack,
Nscale and Sesterce (no public prices), Genesis Cloud (unreachable). Alibaba Cloud: built 2026-11-13 on the keyed ECS API (spot per zone + pay-as-you-go), dormant until an AccessKey
is set (its public pages carry no spot price; robots.txt disallows the page's data files, which are not read). TensorDock (key-only API, "from" floors), Cudo Compute (public
on-demand closed 2026-03-31), SF Compute (login; robots disallows /api), Akash (estimates from stale bids, variants mixed),
io.net and Oblivus (terms forbid scraping), Prime Intellect (auth; an aggregator),
Exoscale (no GPU model in the keyless feed), Gcore (EUR only without a key), Seeweb (EUR only), and
Shadeform's Excess Supply and IMWT (unidentifiable or reselling other clouds). A provider is added only when it
publishes on-demand prices that can be read without an account and its terms allow it. Nosana, declined then as
token-priced, was added on 2026-10-21: its documented, keyless market API now states USD rates; Civo, declined
for its keyed API, was added from its keyless USD pricing page. Thunder Compute, declined for its terms,
was added with an account key, the only automated access its terms allow. Clore.ai, declined for its key,
was added on 2026-10-21 with an account key: its marketplace API states USD prices per server per day, and
its Terms of Service (clore.ai, effective 2026-08-19) do not restrict marketplace data (an older March 2026 copy on
docs.clore.ai limits use to internal purposes; see providers/clore.py). Also declined on 2026-10-21:
Paperspace (still contradicts Shadeform), Lightning AI and Saturn Cloud (resell AWS/GCP/Lambda/Nebius),
CloudRift, Novita, Cerebrium, fal.ai and Northflank (terms forbid automated access), Inferless (shut down),
Cirrascale (monthly only), GMI Cloud, HPC-AI and Taiga ("from" floors), Runcrate, Spheron, Qubrid, Dataoorts and
Andromeda (resellers, brokers or no stable source), Fluidstack and Nscale (still no public prices). Paperspace was added on 2026-11-01 through Shadeform's catalogue, with the gap stated: Shadeform's rate is Paperspace's own on-demand list plus $0.04/GPU-h ($0.10 on the A100s), i.e. Shadeform's resale price (providers/shadeform.py). Akash, declined for its bid-derived figures, was added on 2026-11-01 as what it is: one market-summary listing per GPU at the allocatable-weighted average of each provider's chosen bid, with the 31-day window and the cross-variant fallback documented (providers/akash.py); io.net and CloudRift stay declined (their terms forbid automated access to their services; re-checked 2026-11-01). Hydra Host was added on 2026-11-01 from its public Brokkr inventory API (prices in cents, checked against its USD category prices). CUDO Compute was added on 2026-11-01: its keyless machine-type API still states no-commitment USD prices with free capacity, and the earlier 'on-demand closed' note could not be confirmed (providers/cudo.py). GPU.ai was added on 2026-11-01 as a reseller: its offerings are stored and shown but tagged `resold:undisclosed` and never vote in the market, indices or routing validation (market.RESOLD_PREFIX). Exoscale, declined for a keyless feed with no GPU model, was added on 2026-11-01 with the family-to-GPU table from its own pricing page held in code (unknown families skipped). Hostkey/LeaderGPU stays out: its price API host (invapi.hostkey.com) answers robots.txt with Disallow: / for every agent (checked 2026-11-01). Hugging Face Inference Endpoints was added on 2026-11-01 as resold capacity: each listing names its underlying cloud (`resold:aws`, `resold:gcp`) and never votes, so AWS's and Google's GPUs are not counted twice. Hetzner was added on 2026-11-01 from its GPU server matrix (providers/hetzner.py): each GEX model at the hourly rate Hetzner prints, EUR converted at the ECB rate. IONOS Cloud was added on 2026-11-01 from IONOS CLOUD Inc.'s published USD price list (providers/ionos.py): its Cloud GPU VM templates, H200 PCIe in Frankfurt and Worcester. Fireworks AI was added on 2026-11-01 from its pricing page's on-demand deployment table, as an internal-only source: its terms allow automated access at a human rate but forbid copying or distributing the Service's content (providers/fireworks.py). Gcore, declined for its EUR-only keyless page, was added on 2026-11-01 now that EUR converts at the ECB rate (providers/gcore.py): self-serve bare-metal servers and Virtual GPU sizes from gcore.com/pricing/ai.

### 3.2 Normalization
- Every listing becomes a standard `ComputeListing`: provider, SKU, GPU, count, region, country,
  price per GPU-hour and per instance-hour, market type (on-demand/spot), tier, interruptibility,
  availability, capacity, vCPU, RAM, storage, observed time.
- **Canonical GPU naming** (`canonical.py`) is hand-maintained: 100 models, named
  `<VENDOR> <MODEL> <VRAM> [<VARIANT>]`, each with a frozen public id (`nvidia-h100-80gb-sxm5`, `gpu_ids.py`).
  Unknown names go to an **unmapped queue** instead of being guessed, each with the reason it stays unmapped
  (`mapping.UNMAPPED_REASONS`: board, memory or edition not stated by any provider source). Materially different
  variants are never merged.
- **GPU families** (`families.py`): H100, H200, B200, A100, Blackwell, Hopper… group variants for
  browsing (`/gpu/h100`) but are never priced as one product.
- **Field provenance** (`mapping.py`): for every provider, how each field is obtained (raw,
  derived, constant, lookup, absent) and the provider's quirks.
- **Regions** (`regions.py`): provider region strings mapped deterministically to US, Canada,
  Europe, UK, APAC, Middle East, LATAM, Africa. Unknown stays unknown. Google Cloud is listed only in the regions
  where Compute Engine itself offers the exact machine shape (methodology/gcp-regions.md), not wherever a SKU is
  priced.
- **Currencies** (`fx.py`, methodology/fx.md): EUR, INR, JPY and other list prices are converted to USD at the ECB
  reference rate (cross rates through the euro, both legs from the same ECB date, fail closed after 5 days);
  `GET /v1/fx/rates` and `/v1/fx/rates/history`.

### 3.3 Raw storage & retention
- Every fetch is recorded (`raw_snapshots`: time, provider, endpoint, request, status, latency,
  error, body hash). Identical response bodies are stored **once** (`raw_payloads`, SHA-256).
- Retention (configurable): raw response bodies **30 days**, fetch metadata and news fetch logs
  **90 days**. The latest complete fetch round per endpoint is always kept.
- Normalized listings, price/supply history, rollups, indices, events, articles and all
  billing/audit records are **never trimmed**.
- Other raw stores follow the same rule (store first, parse later): `cost_snapshots`, `context_snapshots`,
  `catalog_snapshots` (a daily copy of dstack's expiring gpuhunt catalogues), `stream_events` (Lium live changes),
  `power_fetches` (every power request with URL, SHA-256 and byte count; bytes kept only where small). Probe raw
  results are kept 30 days and UptimeProject probe samples 90 days; their hourly rollups are kept for good.

### 3.4 Data quality & quarantine
Suspicious values are **held, not published**. Rules include:

| Rule | Action |
|---|---|
| Price jumps ≥ 5× or ≤ 0.2× (the "$2.50 → $250" unit error) | hold price |
| Price ≤ 0, below a datacenter-GPU floor, or above $60/GPU-h | hold (never auto-accepted) |
| GPU count missing/zero or changed for the same listing | hold |
| Region disappears | hold region |
| Listing count explodes (> 3× norm) or collapses (< 20%) | hold new IDs / flag |
| VRAM in the raw name contradicts the canonical name | withhold the mapping |
| Empty response or normalizer crash where data existed | incident; previous state kept |
| JSON shape of a provider response changes | schema-change incident |

Persistent values are auto-accepted only after repeated identical observations over a time
window; operators can accept or reject from `/ops`. The quality layer **fails open**: if it errors,
ingestion continues and an incident is recorded.

### 3.5 Trust & freshness
Every listing carries a trust block: source host, source type (direct API / public page /
aggregator / pricing file), last fetched, last changed, freshness (fresh / aging / stale, relative
to that provider's own polling interval), and whether availability is **explicit, inferred or
unknown**. Providers have health: last success, failure rate, latency p50/p95, consecutive failures.

**When a change happened** (`changed_at.py`, methodology/freshness.md). Every price-history row carries
`observed_at` (the fetch that first showed it), `prev_observed_at` (the last fetch that still showed the old value),
and, where the provider publishes one, `provider_changed_at` with a `changed_at_basis`: `exact` (AWS spot, Nebius
spot, the Lium stream), `date_only` (Azure), `lower_bound` (GCP) or `poll_window` (everyone else: only the bracket is
known). AWS spot is read as a change log, so every change between polls is recorded with its own time. Per provider
and stream, the cadence, data age, p95 age over 24 h, detection lag over 7 days and throttled answers are served in
the `freshness` block of `GET /v1/reliability/providers`.

### 3.6 Capacity (`capacity.py`, methodology/capacity.md)
What each provider says it can supply now, read from its own API and stored raw first: `capacity_current` (current
state) plus change-only `capacity_changes`. **32 providers** implement it (counted from the modules that define
`normalize_capacity`, including the Shadeform-routed clouds). Shapes are kept as the provider states them: a tier
word (RunPod `High/Medium/Low`, Scaleway `available/scarce/shortage`), a count (Hyperstack deployable VMs, Lium free
GPUs) or a boolean (Linode, Verda). An unknown stays unknown, never "in stock". Capacity runs after the price refresh
and fails open: a capacity error never touches prices. `GET /v1/capacity`, `/v1/capacity/history`; page `/capacity`.

### 3.7 Live feeds (`live/`, methodology/live-feeds.md)
One source pushes changes: Lium's public executor WebSocket (no key). Changes are stored change-only in
`stream_events` with Lium's own `ts` and `seq`, written at most once a minute together with the whole book as one raw
snapshot in the poller's shape, then Lium is re-normalized, so its listings and capacity follow a change within about
a minute instead of fifteen. Sequence gaps and reconnects resnapshot and diff; a book with no frame for three minutes
is never written. The REST poller keeps running as the fallback, and the two never normalize Lium at the same time.
Operator-only status: `/internal/live/status`, `/internal/live/events`.

### 3.8 Public and internal data tiers (`provider_meta.INTERNAL_SOURCES`, methodology/data-visibility.md)
Every listing, capacity row and archived price carries a `visibility` (`public` or `internal`; migration
`0027_visibility`). Internal data is data OpenGrid may read and use but may not republish. The one registry of
internal sources, each with its reason, is `provider_meta.INTERNAL_SOURCES`:

| Provider | Internal source | Why |
|---|---|---|
| Fireworks AI | the whole provider (listings, capacity, signals) | its terms forbid copying or distributing any part of the Service |
| DeepInfra | container-API hourly prices (listings ending `:api`) | not the public price-page prices; its terms treat non-public pricing as confidential. Its public listings stay the price-page ones |
| AWS | Spot Instance Advisor interruption bands and savings | AWS's site terms restrict republishing data from aws.amazon.com |

Everything else is public, and `enforce()` fails closed: anything not exactly `public` is stored internal. Every
public statistic (market view, rollups, indices, fixings, spreads, capacity, archived history, trust) reads the public
tier only. Routing, job placement and keyed recommendations may use internal rows to choose and order candidates, but
a non-admin caller never sees an internal value: the candidate shows the public list price of the same product or no
price ("available via OpenGrid"), and derived numbers (quotes, savings, cost estimates, the stored audit record) are
rewritten. `tests/test_visibility.py` seeds unmistakable internal rows (listings, power, gas, derived values) and
calls every public route enumerated from the app; it fails on any internal name, price or count. Power and equities
have their own registries with the same rule (section 5a). Separately, Gcore's rows stay public but are kept out of
the assistant's grounding and every MCP answer (`provider_meta.AI_INPUT_EXCLUDED`: its robots.txt sets
`ai-input=no`).

---

## 4. Market intelligence

### 4.1 Hourly rollup
`market_hourly` samples every listing's state at the top of each hour with the exact market rules
(on-demand, not interruptible, not floor prices, not sold out, not stale), per GPU × provider ×
region. Everything historical reads from it, so 90-day statistics don't replay raw history.

### 4.2 OpenGrid indices (269 defined)
- **Per-GPU indices** (e.g. *OpenGrid H100 80GB SXM5 Index*) in $/GPU-hour.
- **Family composites** (H100 / H200 / A100 class) and the **OpenGrid GPU Compute Index**, in
  base-100 points with fixed, documented weights.
- **Regional**, **spot** (interruptible, labelled) and **provider-class** (neocloud, hyperscaler,
  marketplace…) indices. The 269 are 100 per-GPU, 100 spot, 40 regional, 25 provider-class and 4 composites
  (`analytics/indices.REGISTRY`); a defined index is published only when it meets the rules below.
- Methodology: one vote per provider; median below 5 constituents, 20% trimmed mean from 5;
  outliers beyond 3× / ⅓ of the median excluded; providers with failing feeds excluded; **at least
  3 providers or the index is not published (with the reason)**; **chain-linked** so a provider
  joining or leaving never moves the index.
- Each index: level, 24h / 7d / 30d / 90d / YTD / all-time change (each null with a reason when
  history doesn't cover it), historical high/low, realized volatility, constituents and coverage.

### 4.2a Settlement-grade fixings (`analytics/settlement.py`, methodology/settlement.md)
Official, immutable **fixings** for the settlement indices: the per-GPU on-demand benchmarks (H100 80GB SXM5,
H200, B200, A100 80GB SXM4, L40S) and the OpenGrid GPU Compute Index. They are built so that an exchange or an
OTC contract could cash-settle against them, measured against the IOSCO Principles for Financial Benchmarks.
- **Hourly fixings.** Hour H is fixed only once the rollup can no longer revise it (finality lag 5 h). The value
  is recomputed from the stored inputs by a frozen copy of the math (settlement version S1). If that disagrees
  with the published level, the hour is not published and an ops alert fires.
- **Daily fixing.** A **TWAP** of the day's 24 hourly fixings, referenced at 00:00 UTC. It is published only if
  at least 18 clean hours qualify; it is never carried forward and never estimated.
- **Stricter sufficiency.** At least 4 providers per GPU (the display index needs 3), and at least 3 components
  for the composite.
- **Surveillance flags.** Examples: single-provider concentration, price jumps, a new constituent, feed
  outages, a jump or new listing just before the daily fix, a methodology change within the day. Severe flags
  hold the daily fixing as `not_published`.
- **Append-only, hash-chained ledger.** Each row has `input_hash`, `prev_hash` and `row_hash`. A Postgres
  trigger blocks UPDATE, DELETE and TRUNCATE. Anyone can recompute a fixing with `GET /v1/fixings/{id}/verify`
  and check the whole chain with `GET /v1/fixings/chain/verify`.
- **Evidence retention.** Each vote is traced to the provider's raw snapshot ids and payload sha256. Referenced
  raw payloads are exempt from the storage retention deletion.
- **Restatements.** Admin only, with a reason. A correction is a new row that points at the original, which
  stays untouched. Staff never type a value: they exclude or re-include constituents and the value is
  recomputed. The correction window is 24 h for hourly fixings and 2 business days for daily ones. Every
  restatement raises an ops alert and is listed publicly (`/v1/fixings/restatements`).
- **Governance.** Every methodology version needs a recorded change notice; past fixings are never rewritten.
  Conflicts of interest are handled by these controls: OpenGrid's own routed prices and fees never enter the
  index, and inputs cannot be edited by hand.
- **Website.** A "Settlement fixings" panel on each settlement index page, with a Verify button.
- **Honest gaps.** The inputs are advertised list prices, not transactions. There is no independent oversight
  committee and no external assurance yet. Fixing history starts at deployment. The plan for closing these
  gaps, and the regulatory paths to futures, are in **docs/FUTURES_REGULATORY_PATH.md**.

### 4.3 Historical context
Current price percentile vs its own 30-day and 90-day distribution, distance from the 30-day
median and from historical low/high, and labels (very cheap → very expensive) only when the window
has enough samples. Generated sentences like *"H100 is in the 18th percentile of its 90-day range"*.

**Archived price history** (data kind `archived`, methodology/archived-history.md): prices from before OpenGrid
started recording, read by OpenGrid's own parsers from official and archived sources, raw first, each point with
its `basis`, `licence`, `attribution` and visibility tier (migration `0031_backfill_sources`):
- **Internet Archive captures** of provider price pages, one parser per page design (`python -m backfill import`):
  e.g. Azure's Linux VM page 2017-2024, Lambda 2020-2025, CoreWeave 2021-2024, RunPod 2022-2024, Verda/DataCrunch
  2020-2025, Paperspace 2017-2026, Exoscale from 2021, Hugging Face Inference Endpoints from 2022 (`resold:<vendor>`).
- **AWS's own EC2 price-list archive**: all 123 monthly versions from 2015-12 (`python -m backfill aws`); reserved
  1y / 3y rows are stored at their effective hourly price, never as on-demand, and are not served yet.
- **Azure "effective since" anchors**: one point per GPU size, region and market at the current price's
  `effectiveStartDate`.
- **Open datasets with licences**: Full Stack Deep Learning's cloud-gpus.csv history (MIT, from 2022-08) and Huang
  (2026) Vast monthly transacted medians (CC BY 4.0, stored as their own market `transacted`). A dataset point that
  repeats a primary source becomes corroboration on the primary row, never a second point.

Depth in production after the archive import: **17 providers** reach back at least 24 months, **15** at least 36
months and **7** at least 60 months (AWS, Azure, CoreWeave, Exoscale, Lambda, OVHcloud, Verda); about 30 providers
have no archive. `python -m backfill coverage` reports the depth per provider and GPU. Shown per provider at
`GET /v1/markets/{gpu}/history/archived`, each sample linked to the exact archived document. The 1Y / ALL price
chart runs one continuous line through it (archived stretch drawn lighter and labelled), via
`GET /v1/markets/{gpu}/history/daily`. Sampled only on capture days (monthly for AWS), so it misses short moves;
never used in percentiles, indices, events or routing. Public reads serve the public tier only. SkyPilot's
catalogue history stays off (`SKYPILOT_CATALOG_ENABLED`) until SkyPilot confirm a licence, and dstack's gpuhunt
catalogues are kept raw only (no licence for the data).

### 4.4 Market structure
- **Dispersion** per GPU: low / median / high, absolute and % spread, IQR, standard deviation,
  coefficient of variation, providers, available listings.
- **Market Efficiency / Fragmentation score** (0-100) with transparent components.
- **Provider relative value**: average premium/discount to market, by GPU, % of time cheapest,
  % of time top-3, rank distribution, availability frequency, listing lifetime, price volatility,
  GPU coverage, regional coverage, feed health, each gated by minimum sample sizes. Generated
  facts like *"RunPod has been the cheapest H100 provider 37% of the last 30 days"*.
- **Hardware metadata** for all 100 GPUs (architecture, VRAM, memory bandwidth, dense
  FP16/BF16/FP8, interconnect, form factor, TDP, workload class, sources).
- **Price per capability** (labelled theoretical): $/GB-VRAM-hour, $/PFLOP-hour (BF16, FP8),
  $/TB/s-hour.
- **Alternatives**: similar VRAM, same architecture, training/inference-oriented, similar
  price-performance, never called "equivalent".
- **Heatmaps**: GPU × provider (premium, availability, 24h change), GPU × region (cheapest,
  premium, availability), GPU × day (volatility, availability, change).
- **Comparisons**: GPU vs GPU, variant vs variant, provider vs provider, region vs region, with
  shareable URLs.

### 4.5 Market events engine
Structured events detected every hour, idempotent, with severities and the numbers behind them:

| Event | Fires when |
|---|---|
| `price_move` | a provider's lowest price moves ≥ 10% (marketplaces must hold 2h), or the market median moves ≥ 5% in 24h |
| `new_30d_low` / `new_30d_high` | beats the previous 30 days (requires 30 days of coherent history) |
| `new_all_time_low` / `high` | beats everything since tracking began (≥ 14 days of history) |
| `new_cheapest_provider` | the cheapest provider changes on the same provider set |
| `sold_out` / `capacity_returned` | a provider or the whole market sells out / restocks |
| `provider_added_gpu` / `provider_removed_gpu` | real catalogue changes, never coverage artefacts |
| `provider_feed_down` / `recovered` | a feed fails 3 consecutive rounds / recovers |
| `spread_anomaly` | spread exceeds its own 30-day p95 (or 300%) |
| `regional_dislocation` | a region is ≥ 20% from the global median |
| `market_capacity` | many markets sell out (or restock) within 24h |
| `coverage_started` | OpenGrid began tracking a provider (not a market event) |

### 4.6 Overview, movers & opportunities
- **Homepage payload**: index strip, market direction, top gainers/losers, most volatile, widest
  spreads, most liquid, availability gaining/losing, newly available, sold-out everywhere, recent
  provider price changes, notable events, headlines, market board, ticker tape.
- **Opportunities monitor**: unusually wide spreads, sudden price cuts, new cheap inventory,
  regional dislocations, providers far below their usual premium, temporary scarcity, sudden new
  cheapest provider, each with a score, explanation and observed/inferred label.

### 4.7 Reserved prices (`reserved.py`, methodology/reserved-prices.md)
What providers charge for a commitment term, in their own section: page `/reserved`, `GET /v1/reserved` and
`/v1/reserved/summary`. Seven providers: AWS Compute Savings Plans (1 / 3 years x No / Partial / All Upfront, 15 regions), Azure reserved instances (1 / 3 / 5 years), GCP committed use (1 / 3 years,
every region Compute Engine offers the GPU), Jarvislabs (1 / 3 / 6 months, 1 / 2 years), Latitude (month, year),
Together AI GPU Clusters (3-30 to 181+ days) and Verda's commitment ladder. **The hard rule:** none of these is a
listing. They are computed on read from raw responses already stored (under labels no on-demand normalizer reads),
write nothing, and never enter an index, spread, percentile, heatmap, routing or reliability; `tests/test_reserved.py`
proves the listings, hourly rows and every index vote are identical with and without them.

### 4.8 Full cost of a job (`cost/`, methodology/full-cost.md)
The hourly price is not the cost of a job. `POST /v1/costs/estimate` (public, read-only) ranks providers on compute +
egress + storage for a reference job (presets R0-R3 or custom); `GET /v1/costs/components`, `/v1/costs/billing`,
`/v1/costs/sources`, `/v1/costs/presets`; page `/cost` ("True cost"). Components come from price APIs and files
fetched daily into `cost_snapshots` (AWS Data Transfer and S3, Azure Retail Prices, Oracle's price list, Verda volume
types), from provider raw already stored (Hyperstack, Vultr, each Vast offer), or from a hand table with source page
and verification date (only values checked on the provider's own page are used). An unknown component is named and
makes the total a lower bound ("≥ $X"), never $0, and a lower bound never outranks a complete total. Public tier only;
nothing statistical reads cost data.

### 4.9 RL post-training cost (`cost/rl.py`, methodology/rl-cost.md)
`POST /v1/costs/rl-estimate` (public, read-only) prices an RL job as trainer + rollout GPUs + CPU sandboxes + weight
syncs, three ways: **same cluster** (the cheapest provider that hosts the trainer and every rollout GPU), **split**
(cheapest trainer + cheapest eligible rollout GPUs anywhere, the weights shipped across clouds), and the **best plan
found** over every trainer provider x rollout mixes. Inputs: trainer GPU / GPUs per node / nodes, rollout GPU (or
auto) / count / minimum VRAM / spot allowed / interruption overhead (default 1.15, an assumption), sandboxes, GB per
sync, syncs per hour, fanout, hours, checkpoint storage; presets RL1-RL3 (`GET /v1/costs/rl-presets`). Builds on
the full-cost estimator (billing increments, storage, egress tiers) and adds per-provider **ingress**
(`ingress_internet` components: free where the provider's page says so, per offer on Vast, unverified for AWS, GCP and
Oracle, unknown elsewhere). Returns line items with data kind and source, totals that are lower bounds when anything
is unknown (never $0), the saving with its bound (exact / at most / at least), a **break-even** transfer $/GB and
sync rate, a sensitivity table (overhead x syncs/hour), a rollout GPU ranking by memory bandwidth per dollar
(labelled a theoretical heuristic) and the applicable gaps in plain words. Computed in the threadpool, never on the
event loop; the listings / components read and each job's result are cached for 5 minutes. Page `/rl-cost`; MCP tool
`opengrid_rl_estimate`. **Does not:** measure tokens/s, spot reclaim rates or CPU sandbox prices (no dataset yet);
price inter-region transfer inside one provider (assumed $0 in one cluster), rollout-side storage, engineering
overhead, enterprise discounts, or compression / partial weight updates; convert a tokens-per-hour target into GPUs.

---

## 5. News & external signals

- **81 registered sources, 66 enabled** (`news/sources.py`): 41 RSS and 4 Atom feeds, 3 US Federal Register
  queries, 16 SEC EDGAR submissions sources and 2 GDELT queries; official feeds and APIs only, no HTML scraping.
  Enabled by category: 19 trade press, 16 filings, 12 hyperscaler, 12 provider, 4 vendor, 3 regulation. They include
  AWS, Google Cloud, Azure, Meta, Lambda, CoreWeave, RunPod, Together, Voltage Park, Hyperstack, DigitalOcean,
  Vultr, Scaleway, NVIDIA, AMD, the **US Federal Register** (BIS export controls, FERC data-center
  rules), DatacenterDynamics, The Register, HPCwire, Tom's Hardware, SemiAnalysis, The
  Information, and more. The 15 sources that couldn't be verified are listed as disabled with the reason.
- **About 68k items** are stored in production (67,932 on 2026-11-14), the history backfill included; the earliest is from 2008-02.
- **Raw first** (re-classifiable), conditional GET (ETag / Last-Modified), per-source health.
- **Deduplication**: canonical URLs, plus story clustering across sources (one story, N sources).
- **Classification** (deterministic, re-runnable): providers (with aliases), GPUs ("H100" → the
  family; "H100 SXM" → that variant), regions, topics (price change, capacity, outage, GPU launch,
  funding, capex, export controls, power, contracts, futures, neocloud expansion…).
- **Relevance score** 0-100 with stored components (source tier, entities, topics, recency).
- Feeds that stamp every item with one date are detected; those dates are never treated as
  publish times.
- **Timeline**: price moves, availability changes, market events and news on one axis, and
  *"Related news and events around this move (not necessarily causal)"*. OpenGrid never claims
  news caused a move.
- **SEC EDGAR, changelogs, GDELT** (2026-11-01): 16 EDGAR submissions sources (NVIDIA, AMD, Intel,
  CoreWeave, hyperscalers, Nebius, DigitalOcean, Super Micro; Form D for Lambda, Crusoe, Fluidstack),
  DigitalOcean release notes, Together changelog, Lambda status history, two GDELT queries. The GDELT GPU-cloud
  query returned nothing, live and in history, until the fix in d2fad60 (2026-11-07): GDELT rejects a quoted
  phrase as short as "H100", so all 119 history months failed. The model names are now unquoted, and the registry
  test refuses short quoted phrases in any GDELT query. GDELT is paced at one request per 5.5 s and answers 429
  ("limit requests") when pushed, so a GDELT history run is slow.
- **News history** (`news/history.py`, methodology/news-history.md): past items recovered from the feeds'
  own archive pages, Internet Archive captures of feed and index pages, EDGAR and GDELT; raw first with
  provenance, marked backfilled, deduplicated against live items (live wins), same classifier. Placeholder dates
  before 1995 (e.g. TrendForce's 1970-01-01) are never publish times.
- **Major shifts** (`analytics/shifts.py`, `GET /v1/events/shifts`, `/v1/events/shifts/{id}`,
  `/v1/events/shifts/definition`, methodology/shifts.md; job `market_shifts`, every 15 min): GPU median and index
  moves beyond their own volatility, provider-wide repricings, capacity collapses, and archived list-price
  changes before live coverage (archived points enter only where their visibility is `public`); each with stored,
  ranked related items (news, announcements, filings, OpenGrid events) under *"Related news and events around this
  shift (not necessarily causal)"*.

---

## 5a. Market context, equities and power markets (beside GPU prices, never mixed)

Numbers about the world around GPU prices. They are context, never cause: none of them feeds listings, rollups,
indices, fixings, spreads, percentiles, archived prices or routing, and nothing on OpenGrid says one of them moved a
GPU price. Each lives in its own tables, raw first.

### 5a.1 Market context (`context/`, `api/context.py`, methodology/market-context.md)
- **EIA state electricity prices**: average retail price by US state, industrial and commercial, monthly (public
  domain). **SEC EDGAR XBRL**: NVIDIA and AMD total and Data Center revenue; capital expenditure of Microsoft,
  Alphabet, Amazon, Meta, Oracle, CoreWeave, NVIDIA and AMD, quarterly, each point with the filing date that made it
  knowable and its revisions.
- Raw first in `context_snapshots`, parsed into `context_series` (migration `0032_context_series`) with
  `published_at`, `first_seen_at` and revision counts. Job `context_series` (daily), registered only when
  `EIA_API_KEY` or `SEC_USER_AGENT` is set.
- **Geo** (`context/geo.py`): a provider region gets a US state only where the cloud documents the place or the
  region string names it; nothing is guessed.
- **API** (public, Data API): `GET /v1/macro/series` (the catalogue), `/v1/macro/series/{series}`,
  `/v1/macro/series/{series}/history`, and `GET /v1/macro/power` (public US listings by state, beside that state's
  electricity price).

### 5a.2 Equities (internal only; `context/equities.py`)
Alpaca market data (never the trading API, SIP feed only): daily bars from 2016 and hourly bars for 5 years, stored
raw first (`context_snapshots`, `equity_bars`; migration `0033_equities`). **40 tickers** in `context/equities.TICKERS`
(`EQUITY_TICKERS` overrides): 20 GPU and AI names (NVDA, AMD, TSM, AVGO, MU, INTC, ARM, SMCI, DELL, CRWV, NBIS, ORCL,
MSFT, GOOGL, AMZN, META, VRT, ANET and the SMH and SOXX ETFs) and 20 power names: generators (CEG, VST, TLN, NRG),
datacenter-region utilities (D, AEP, SO, EXC, NEE, DUK, ETR, XEL), nuclear and fuel (OKLO, SMR, CCJ), grid and
datacenter power equipment (GEV, ETN, PWR, BE) and the XLU ETF. **Every equity series is internal**: read only at the
admin-only `/v1/ops/context/equities` and `/v1/ops/context/equities/moves` (equity moves beside GPU market moves for
a window), never on a public page, API, index or export. Job `context_equities` (hourly) when both Alpaca keys are set.

### 5a.3 Power markets (internal; `power/`, `api/power.py`, methodology/power-markets.md)
US wholesale electricity, grid data and natural gas, hub and zone level (never nodal).
- **Storage** (migration `0035_power_markets`): `power_series` (the catalogue); `power_days`, one row per series and
  market day with the day's values as a compact `real[]` (24 hourly or 288 five-minute slots, DST days 23/25 or
  276/300) plus the day's mean, min and max; `power_fetches`, raw first: every request (failures and "no data"
  answers too) with its URL, status, byte count and SHA-256, and the bytes only where they are small. A value the
  source did not give is NULL, never 0 and never carried over. About **490 MB** in production (2026-11-14).
- **Sources** (`power/sources/`; each module's terms verdict is read by `power/registry.py`):

  | Source | What | Tier | In production |
  |---|---|---|---|
  | EIA-930 (EIA API v2) | hourly demand, net generation, interchange, day-ahead forecast and generation by fuel per balancing authority | public (public domain) | on; region hourly and fuel mix back to 2021 |
  | EIA natural gas | Henry Hub spot and NYMEX C1-C4 futures (daily, back to 1993; EIA stopped the futures on 2024-04-05), weekly storage (back to 2010), electric-power gas price by state (monthly, back to 2002) | internal by choice | on |
  | CAISO OASIS | day-ahead hourly and real-time 5-minute LMP at NP15, SP15, ZP26 and the four DLAPs | internal (terms do not clearly permit redistribution) | on, back to 2023-08 (OASIS serves a rolling ~39 months) |
  | NYISO | zonal day-ahead and real-time LBMP, 15 zones | internal | on, back to 2021 |
  | SPP | SPPNORTH_HUB and SPPSOUTH_HUB, day-ahead and real-time | internal | on, back to 2021 |
  | ERCOT (keyless public MIS reports) | day-ahead hourly and real-time 15-minute settlement point prices, 6 hubs and 8 load zones | public (its terms permit redistributing raw public data in compilations) | on, back to 2011 |
  | PJM (Data Miner 2) | hub and zone LMPs | internal | off: needs `PJM_API_KEY` |
  | ISO-NE (web services) | hub and zone LMPs | internal | off: needs an ISO Express account (`ISONE_USERNAME`, `ISONE_PASSWORD`) |
  | MISO | hub LMPs | internal | off and must stay off: its site terms forbid automated access to its public files; its Data Exchange API (subscription key) is not implemented |

  PJM and ISO-NE are built from their documented formats and not verified live yet. ERCOT and EIA-930 are
  public-tier, but they too are served only through the admin routes below.
- **Runner** (`python -m power backfill|forward|status|sources`): idempotent and resumable (stored days are skipped,
  "no data" days recorded), one request at a time per source, paced, streamed (nothing written to disk). The
  `power_forward` job runs every 12 h when `POWER_ENABLED=true` (on in production) and re-reads a few days of
  overlap, so revisions replace a day and are counted.
- **Cross-analysis** (`power/cross.py`, derived, internal): provider regions are mapped to ISO zones or hubs only
  where the place is documented (`power/geo.py`); power cost per GPU-hour = TDP × PUE (1.3, `POWER_PUE`) × the
  day-ahead hub price, and its spread to the median listed price (`power_gpu_region_daily`); lagged correlations of
  GPU price and in-stock share against power and against Henry Hub, lags −7 to +7 days (gas −14 to +30) over 180
  days, at least 20 pairs (`power_leadlag`); power spikes around major shifts, joined at read time so
  `market_shift_items` is untouched. Correlation, never cause. The wholesale energy cost of a GPU at full board power
  is **not** a provider's cost.
- **Access**: every read is admin-only under `/v1/ops/power/*` (`sources`, `status`, `series`, `map`,
  `cross/regional`, `cross/leadlag`, `shifts`), outside the OpenAPI schema. **There is no public power route**;
  `tests/test_visibility.py` asserts none exists and seeds internal power, gas and derived values to check every
  public route.

---

## 6. Execution layer: routing & provisioning

The goal: *"I need 8 H100s in the US under $2.50/hr"* → OpenGrid understands the market, ranks
providers, explains the choice, quotes, gets approval, provisions, tracks, terminates and
reconciles.

### 6.1 Execution control plane
- **Modes**: `DISABLED` · `PREVIEW_ONLY` (default) · `SUPERVISED` (every launch approved by an
  admin) · `LIVE` (validated, live-enabled providers launch within cost guards).
- **Env ceiling**: unless `ROUTING_LIVE_PROVISIONING=true`, the effective mode can never exceed
  PREVIEW_ONLY, whatever the database says.
- **Per-provider flags**: adapter status (simulated → validated only by a complete, evidenced
  validation cycle), supervised-enabled, live-enabled.
- **Kill switches**: global *STOP ALL LIVE PROVISIONING* and per-provider kill/unkill. They stop
  new launches without stopping monitoring, reconciliation or termination. Every change needs a
  reason and is logged.

### 6.2 Provider capability registry
| Provider | API supports | OpenGrid implements | Status |
|---|---|---|---|
| Lambda | full lifecycle, no stop endpoint (host `cloud.lambda.com`) | L3 | simulated → **first validation target** |
| RunPod | full lifecycle on REST v2 (`api.runpod.io/v2`; v1 retires 2026-11-15) | L3 | simulated |
| Hyperstack | full lifecycle; stop not offered (a shut-off VM keeps billing) | L3 | simulated |
| DigitalOcean | full lifecycle; stop not offered (a powered-off GPU Droplet is still billed) | L3 | simulated |
| Crusoe · Denvr · Latitude (Shadeform) | full lifecycle, no stop endpoint | L3 | simulated |
| Vast.ai | full lifecycle | L3 | simulated |
| Verda | full lifecycle; stop not offered (shutdown keeps charging) | L3 | simulated |
| AWS · Nebius · Massed Compute · Voltage Park · Hyperbolic · Lium · Salad | full lifecycle documented (AWS, Nebius, Salad from recall) | L0 (market data) | no adapter |

Each adapter declares a capability matrix with evidence: quote, live availability, launch, SSH key
injection, startup script, status, stop, terminate, region selection, GPU-count selection, price
known before launch, billing unit, minimum commitment, interruptible, name/tag at launch, list
instances, idempotency token, stopped-time billing, forced account SSH keys, and risks. Every call each adapter makes
was compared with the provider's current API reference on 2026-11-13, without an authenticated or billable call
(methodology/adapter-contract-audit.md); the exact requests are pinned in `tests/test_adapter_contracts.py`.

### 6.3 Best execution scoring
Transparent weighted scoring, never just "cheapest wins". Factors (each 0-1, with raw value and
data kind): price vs market median, availability now, region match, data freshness, availability
persistence, price stability, integration level. **Reliability and performance appear with weight
0 and "no data, not used"** until real execution data exists.

Modes: **CHEAPEST**, **FASTEST_AVAILABLE**, **BALANCED**, **MOST_STABLE**, **USER_DEFINED**
(validated custom weights). Every result explains itself (*"9% below market median; available now;
fresh API data; 94% availability over 30 days"*) and lists exclusions with reasons. Optional
**strict region**; families allowed only with `allow_variants`.

### 6.4 Route flow
```
POST /v1/route/preview   rank + explanation + quote (never calls a provider's launch API)
POST /v1/route           route request + quote + deployment in pending_approval (SUPERVISED)
POST /v1/route/{id}/approve   admin approves the exact quote → revalidate → guards → launch
POST /v1/route/{id}/reject
POST /v1/deployments/{id}/terminate | /stop
```

### 6.5 Safety mechanisms
- **Quotes** persist observed price, quote price, estimated hourly/total cost, itemized fees,
  taxes ("not computed"), billing unit, minimum commitment, and **expire after 5 minutes**. Launch
  re-checks the live price; any move beyond 2% (or above the customer's max price) refuses and
  issues a new quote for re-approval. Every cap is checked against max(quote, live re-check).
- **Idempotency keys** (required) on route, approve, terminate, stop and orphan actions; duplicates
  replay, conflicting bodies get 422, in-progress gets 409. At most **one provision call per
  deployment, ever**.
- **Explicit state machine**: `created → quoted → pending_approval → approved → provisioning →
  running → stopping/stopped → terminating → terminated`, plus uncertain states
  (`provider_timeout`, `launch_unknown`, `degraded`, `termination_failed`, `orphan_suspected`,
  `credentials_unavailable`). Ambiguous outcomes **never fail over** to a second provider.
- **Write-ahead attempt row** committed before every provider call, so a crash mid-launch is always
  recoverable by name (`og-<deployment>`).
- **Cost guards**, enforced atomically with **Postgres advisory locks** at approval and again at
  provisioning (proven across threads and separate processes): max price per GPU-hour, max hourly
  cost, max total cost, max GPUs, max active deployments, provider allowlist, region allowlist,
  monthly spend limit. Admin overrides are explicit and recorded.
- **Runtime ceilings**: effective max runtime = min(system hard max 24h, account max, request /
  account default / system default 60 min). Never unlimited; the exact auto-termination time is
  shown before approval and enforced by reconciliation even through outages and restarts.
- **SSH access**: customer launches install **only the customer's own public key** (validated,
  fingerprinted, never logged). Operator keys are used only for validation launches. Providers
  whose account keys may be installed too (Vast: yes; every adapter but Lambda: unknown until validated) require an
  explicit admin override. The
  approval screen shows *"Customer supplied key: SHA256:… · OpenGrid operator access: NONE"*.
- **Pinned credentials**: each deployment is bound to the credential (and its fingerprint) that
  launched it; a different key can never manage (or "confirm termination" of) the instance.
  Customer (BYO) credentials fail closed if they can't be decrypted.

### 6.6 Provider validation
A provider moves from *simulated* to *validated* only through a real, supervised validation run:
1 GPU, ≤ $3/hour, ≤ 30 minutes, one at a time, with every precondition checked (kill-switch drills
in the last 7 days, healthy reconciliation and tracker jobs, a delivered ops test alert). The full
cycle (launch, observed running, found by name, terminated with two-signal confirmation, cost
reconciled) must have evidence before the provider is marked validated.

## 6a. Training jobs (OpenGrid Jobs)

One real workload end to end: **"Train Your First AI Model: PyTorch MNIST"** (allowlisted template
`pytorch-mnist`; no arbitrary commands, code or dataset URLs). Runbook: `docs/FIRST_TRAINING_JOB.md`.

- **Workload-aware selection** (`workloads/`): the template declares requirements (NVIDIA CUDA, ≥ 8 GB VRAM,
  sm 7.0-9.0 for the pinned torch 2.4.1 / CUDA 12.1 build, 1 GPU, on-demand only). `gpu: auto` ranks every
  eligible GPU with the existing scorer and shows two views: **market best** (best observed listing) and
  **execution-ready** (what OpenGrid can really run now), with alternatives, exclusions and exact blockers.
- **Jobs** (`jobs/`, `api/jobs.py`, migration `0017_jobs`): a job is the workload and its outcome; the deployment
  is the cloud resource. Submit (Idempotency-Key) reuses the existing route → quote → **supervised approval**
  (`POST /v1/route/{rr}/approve`) → launch → terminate → reconcile path. An explicit job state machine
  (created … training … reconciled, plus every failure / uncertain state) with outcome and resource
  disposition kept separate; `job_sync` every 30 s rebuilds state after restarts.
- **Automatic relaunch** (`jobs/relaunch.py`, migration `0021_job_relaunch`; OFF by default:
  `JOB_AUTO_RELAUNCH_ENABLED` and a per-job `retry: {max_attempts 1-3, max_total_usd}`): after `gpu_interrupted` /
  `runner_lost`, and only once the provider confirmed the old GPU is gone, a new attempt through the same quote /
  approval / cost-guard path, as the original owner, on another provider, resuming from the latest checkpoint,
  with all attempts within `max_total_usd`; at most one successor per attempt (`UNIQUE(retry_of)`); "Attempt 2 of
  3" on the job page and My Jobs.
- **Resume, OOM escalation, Move** (`jobs/continuation.py`, migration `0037_job_resume_move`; methodology/jobs.md
  8c-8f). **Provider-initiated stops** (a spot reclaim surfaced as `stopped` / `exited` that OpenGrid never asked
  for, confirmed by a later provider read, runner silent since) fail the job as `gpu_interrupted` at once so the
  relaunch handles them; a user / operator stop never relaunches. **Resume** (`POST /v1/jobs/{id}/resume`, owner,
  Idempotency-Key, `{provider: same|best|<id>, max_total_cost_usd, from_scratch?}`): a cancelled / interrupted job
  whose GPU is provider-confirmed gone continues as a new attempt from the chain's latest verified checkpoint; refused
  while the machine may exist or without a verified checkpoint (unless `from_scratch`). **OOM escalation** (opt-in
  `retry.on_oom: escalate`, with relaunch on): after `out_of_memory`, ONE relaunch (`max_oom_escalations` ≤ 2) on the
  next larger-VRAM GPU tier from the hardware catalog (same family first) within `retry.max_total_usd`. **Move**
  (`POST /v1/jobs/{id}/move`, owner, off unless `JOB_MOVE_ENABLED`): target selected first, a `checkpoint_now`
  instruction on the runner channel, a NEW verified checkpoint, only then the normal terminate, provider-confirmed
  gone, then the successor; no checkpoint in time, or a refusal before the terminate (requester's key revoked,
  batch stopped or out of budget) → aborted with its reason, job keeps running; a successor refused after the
  terminate → `move_failed`, and the job can then be resumed from that checkpoint; at most one open move per job. A
  read-only **move suggestion** on `GET /v1/jobs/{id}` (≥ 20% cheaper estimated remaining cost after the move
  overhead, or a degraded provider); never automatic. Every successor is an ordinary job through the one submit path
  (`UNIQUE(retry_of)`: one successor per attempt); batch shards reserve in the batch ledger first. Job page Resume… /
  Move… with the quote (provider, price, ceiling, checkpoint age) before submitting and a suggestion banner; CLI
  `opengrid jobs resume|move` (quote first, `--yes` submits); MCP shows the suggestion only (no write tool). Tested
  against fake provider APIs only.
- **Batches** (`jobs/batches.py`, `api/batches.py`, migration `0036_batches`; methodology/batches.md): many
  independent jobs (a sweep from an explicit shard list or a grid, at most `BATCH_MAX_SHARDS` = 64) as one batch with
  one budget, one approval and one status. Every shard is an ORDINARY job through `jobs.orchestrator.submit` (same
  quote, route, guards, single provision call, idempotency). A **placement plan** from the live market spreads shards
  by best-execution rank (price, availability, listed capacity, reliability where data exists) under a
  **diversification cap** (default 50% per provider) and provider / region allow + deny lists, with per-shard price
  and estimate and the total vs all on one cloud. **Budget** `max_total_usd` over all shards AND relaunches: each
  submission reserves its ceiling in a ledger under a Postgres advisory lock (no overspend under concurrency), and the
  reservation is the shard's cost limit, so the launch price cap binds it. **One approval** (`POST
  /v1/batches/{id}/approve`, admin) approves every shard's exact quote via the existing `engine.approve`; a shard
  submitted later from the queue launches under it only within the approved provider + price tolerance, else waits
  for re-approval; a relaunch, resume, OOM escalation or move of a shard always waits for a fresh batch approval
  (`fresh_approval_required`). A shard never gets a second job from one submission: a submission that errors after
  its job exists stays with that job, and a retry in flight is never judged by the refused try it replaces (a
  crashed one is requeued after 15 min). Concurrency limit
  with a queue (`batch_advance` pass, `BATCH_QUEUE_ENABLED`), `continue` / `stop_on_first_failure`, cancel through
  the jobs cancel path, interrupted shards relaunched by `jobs/relaunch.py` inside the batch budget, a SIMULATED
  sandbox mode. Web `/batches`, `/batch/<id>`; CLI `opengrid batch`; MCP `opengrid_preview_batch` /
  `opengrid_batch_status`. Not a distributed training run and not inference load-balancing: N separate jobs placed,
  paid for and reported together. Tested against fake provider APIs only; no real batch has run.
- **Runner** (`runner/`): a cloud-init bootstrap (≈ 216 KB) installs hash-locked torch 2.4.1+cu121, checks
  nvidia-smi and CUDA, trains a small CNN, uploads `model.pt`, `config.json`, `metrics.json`, `env.json`,
  `manifest.json` (sha256-verified) and calls back with a per-job HMAC-derived token (never stored).
  OpenGrid, not the instance, terminates the GPU.
- **Results**: website `/jobs`, `/jobs/new`, `/jobs/{id}` (and `/batches`, `/batch/{id}`); API logs / metrics / artifacts; Discord
  `#opengrid-deployments`. Artifacts live in Postgres (optional S3 backend).
  - **My Jobs** (`/jobs`) leads with a plain-English box ("What do you want to run?", see Assistant in 6c), then
    one card per job (Needs attention / Active / Completed) with a one-line status in plain words ("Training", "Waiting for OpenGrid
    approval", "Needs attention: the GPU may still be billing"); the full table is folded under "Show all details".
  - **New job** (`/jobs/new`) opens on three choices: the free demo (one click), the MNIST template on a real GPU
    ("Check price", then "Run it" with confirmation; when it cannot run, the root-cause reason in plain words, with
    the technical detail for admins) and "Run your own model" (Find Compute). The full form is under Advanced
    (`?advanced=1`).
  - **Job page** (`/jobs/{id}`): one status line, the cancel button and four plain questions; lifecycle and
    deployment strips, live loss / accuracy, logs, the 12 evidence-backed verification checks, artifacts and the
    four cost numbers are under Advanced.
- **Sandbox**: `sandbox: true` runs a fully SIMULATED lifecycle (no provider, no spend), labelled everywhere. Its
  one artifact, `simulated-report.json`, says in plain words what the job was (name, template, settings), how long
  the simulation took, the five steps a real run performs next to what the simulation did, placeholder metrics
  (labelled), and what a real run would take, cost at most, produce and need (an approved quote). The real-run
  timings are estimates and are labelled so.
- **Status**: built and tested against a fake Lambda API and a real local GPU; **no real Lambda job has run
  yet**. A real job needs the Lambda validation run, then `supervised_enabled` on Lambda.

## 6b. Reserved compute: price-lock requests (`reservations/`, methodology/reservations.md)

Customers ask for a locked price ("8× H100 SXM5, 5,376 GPU-hours, starting Oct 20-27, ≤ $2.25/GPU-h") at
`/reserve` or `POST /v1/reservations/requests`.
- **Indicative reference.** Each request shows the index level, the raw level, the best on-demand listings, the
  market median and the latest settlement fixing. All of it is labelled `indicative`: it is never a quote and
  never binding.
- **States.** requested → under_review → offered → accepted → fulfilled, or declined, withdrawn, expired or
  cancelled. Every transition is an event with actor and reason.
- **Supply basis required.** An operator offer must record a supply basis, i.e. the provider capacity
  commitment that backs it. The database rejects an offer price without one.
- **Revised offers.** Offers can be revised (`offer_seq`). A customer can't accept an offer that changed after
  they read it.
- **Acceptance records intent only.** Nothing is launched and nothing is billed. The order form, invoicing and
  delivery happen outside the API.
- **Not a financial contract.** This is physical compute delivered by OpenGrid: not tradable, not
  transferable, no cash settlement. The legal framing is in docs/FUTURES_REGULATORY_PATH.md, Path B.

---

## 6c. Workload intelligence, CLI, MCP and assistant (OpenGrid 2.0)

One engine serves every interface (contracts: docs/OPENGRID2_CONTRACTS.md; method: methodology/recommendations.md).
- **Estimator** (`intelligence/memory.py`, `estimate.py`): weights, gradients, optimizer states, activations
  (Korthikanti et al. 2022), KV cache, framework overhead, ZeRO sharding / offload, 20% margin; minimum vs
  recommended VRAM, ranges, confidence and every assumption listed. Model configs from published HF configs.
- **Recommender** (`intelligence/recommend.py`, `POST /v1/recommend`, `/v1/recommend/estimate`, public, read-only):
  compatible GPUs and counts → fresh on-demand listings via `rank_listings` (never `engine.preview`, no rows
  written) → tiers (lower cost / balanced / higher performance by total effective theoretical throughput) and
  picks (cheapest advertised, best for the workload, best OpenGrid can run, lowest estimated whole-job cost,
  shortest estimated runtime). `limit` caps the option list; every pick is a listed option (with a limit below the
  number of distinct picks the lower-priority ones are `null`, the reason in `picks_unavailable`).
  Multi-GPU aware (`intelligence/performance.py`): each GPU model's smallest fitting count plus one scale-up
  (2n) option, so 2 x H100 competes with 1 x H200; every option totals its GPUs (TFLOPS, VRAM, bandwidth),
  names the GPU-to-GPU link (NVLink / bridged pairs / PCIe) and applies a stated scaling assumption with a range.
  With the training dataset size (tokens x epochs) each option gets a theoretical runtime range (6/4/8 x N x T
  FLOPs, 35% utilization, range 20-50%) and a whole-job cost range at its billing unit. Never a benchmark, always
  labelled; without the dataset size runtime stays unknown and cost is scenarios. "Executable" needs the readiness checks and a
  supported workload (today only the MNIST template).
- **Measured performance** (`intelligence/measured.py`, migration 0038): every completed REAL job (never sandbox /
  simulated metrics / a mismatched GPU) is recorded as a `measured` observation keyed by workload signature; only
  consented rows (OpenGrid's own runs, or accounts that opted in) are read back, and the recommender prefers them
  over the theoretical runtime for that GPU and labels them. No data yet: until real runs
  complete, every runtime stays theoretical. Backfill: `python -m intelligence.measured backfill`.
- **Suitability, whole-job cost and explanations** (`intelligence/workload_fit.py`, `explain.py`): listing / spec
  interconnect evidence and node fabric, parallelism strategy, communication vs compute per step, an inference
  decode roofline against tokens/s and latency targets with cost per million tokens; ranking by whole-job cost
  (+ measured provider time to capacity when known), `deadline_hours`, `shortest_estimated_runtime`; per-option
  confidence with its weakest evidence and factor-by-factor deltas vs the recommended option; `multi_gpu` advice
  (when not to go multi-GPU). Review: methodology/workload-intelligence-review.md.
- **Audio, image + text and video workloads** (`intelligence/media.py`; method: methodology/recommendations.md
  "Audio, image and video workloads"):
  - **Request.** An optional `media` object (modality, task, images per request and resolution, audio seconds and
    total hours, video length / fps / resolution, generation output size / length / steps, total items), mirrored
    in the CLI contract and the /find schema. Requests without it are unchanged (24 golden payloads byte-identical).
  - **Catalog.** 29 sourced entries (Whisper tiny to large-v3-turbo, wav2vec2, Parler-TTS, Kokoro, LLaVA 1.5 / 1.6,
    Qwen2-VL 2B / 7B / 72B, Llama 3.2 Vision 11B / 90B, PaliGemma 224 / 448 / 896, CogVideoX 2B / 5B, Wan2.1 1.3B /
    14B, HunyuanVideo).
  - **Memory, by modality:**
    - VLM: KV cache sized with image (and Qwen2-VL video) tokens by each model's documented rule; Llama 3.2 Vision
      uses a cross-attention cache.
    - Whisper: decoder caches per 30 s window. wav2vec2: activations per frame. Parler-TTS: KV per codec frame.
    - Video generation: weights + latent-token activations + VAE decode, always low confidence.
  - **Runtime.** A theoretical throughput and runtime / whole-job cost range per option (audio hours, requests or
    videos per GPU-hour). Video-diffusion utilization is set to contain every publisher-reported generation time on
    record; the audio pipeline efficiency is an uncalibrated assumption. Measured observations still take
    precedence.
  - **Multi-GPU.** Independent items run as replicas, and the notes point to batch fan-out.
  - **Explanations** say what the media inputs cost, e.g. that only GPUs with at least 80 GB hold a 720p
    HunyuanVideo run on one GPU.
  - **Detection.** The local analyzer detects these models and frameworks (faster-whisper / openai-whisper /
    whisperx, transformers VLM and ASR classes and pipeline tasks, diffusers video pipelines, vLLM
    `--limit-mm-per-prompt`, config.json `model_type`). The assistant parses plain-language asks ("transcribe 10k
    hours of audio", "caption images with LLaVA", "generate 5-second videos"). /find has optional input / output
    fields shown per chosen kind.
  - **Limits.** Video understanding is modelled for Qwen2-VL only; the training memory of video and TTS models is
    a lower bound.
- **Local analyzer** (`cli/src/opengrid_cli/analyze`): allowlisted files only, `.gitignore`, symlink and size
  limits, never reads secrets/weights/datasets, never executes code, redacts secret-looking values, flags
  prompt-injection text; only the structured requirements ever leave the machine. Dry-run mode.
- **CLI** (`cli/`, `uv tool install ./cli`): version, doctor, auth login/status/logout, gpu search/show/compare/market, providers, analyze,
  estimate, recommend, route preview, jobs list/status/logs/cancel/resume/move, batch
  preview/list/status/cancel/sandbox (sandbox only), demo train (sandbox only), mcp / mcp config; `--json`. No command provisions
  directly: `jobs resume` / `jobs move` show the quote first and, with `--yes`, submit through the server's ordinary
  job path (approval, cost guards, max-price cap); there is no `jobs submit` or real `batch submit`.
- **MCP server**, one tool set (`cli/src/opengrid_cli/mcp_server.py`) in two deployments:
  - **hosted** at `<server>/mcp` (`api/mcp_remote.py`): streamable HTTP, stateless, JSON responses; connect with
    only the URL and `Authorization: Bearer opg_...` (never the site login, never anonymous, cross-site Origin
    refused). Tools call `/v1` in-process with the caller's key, so scopes, ownership, rate limits and usage
    logging are the API's own. 15 tools (no local-file access; incl. `opengrid_preview_batch`, which writes
    nothing, and `opengrid_batch_status`) and 2 prompts. `/mcp` answers 503 (the site
    still starts) if the MCP SDK cannot be imported;
  - **local** (`opengrid mcp`, stdio; optional loopback HTTP; zero-install via
    `uvx --from 'git+https://github.com/fzsheik/opengrid-terminal#subdirectory=cli' opengrid mcp`): 16 tools
    (adds the local project analyzer) and 3 prompts. `opengrid_rl_estimate` prices an RL job (section 4.9).
  Tools are read-only except a non-provisioning route preview, the free MNIST sandbox demo
  (`opengrid_start_demo`: simulated, no provider, no GPU, no spend) and a confirm-gated job cancel; no tool can
  provision paid compute or submit a real job, and results link to `/jobs/new?preset=mnist` where a person
  confirms a real run. Prompts: `cheapest_gpu`, `try_free_demo`, `size_my_project` (local). `opengrid mcp config
  <host>` prints hosted / uvx / installed snippets for Claude Code, Cursor, Codex and VS Code (keys only by
  env var or a password prompt). Verified with MCP Inspector (`--strict`, both protocol eras), Claude Code
  (hosted: Connected) and Codex registration.
- **Assistant** (`assistant/`, `POST /v1/assistant`, `/assistant`, panels on Find Compute and My Jobs): deterministic
  intents by default; optional vendor-neutral LLM (off unless configured; off in production today) whose replies
  are post-checked so every number comes from tool results; no mutation tools. Job intents (demo, real run, deploy
  your own model, my jobs, cancel) answer with **action cards**: the browser starts the free demo or cancels a job
  only when the user clicks (cancel asks for confirmation first), with the user's own login. A request to run your
  own code gets an honest "not yet" and the cheapest place to rent a GPU for it. Grounding rejects any reply that
  claims an action happened.

## 7. Reconciliation, billing & cost accounting

- **Reconciliation worker** (every 2 minutes): compares OpenGrid's deployments with each provider's
  real instance list. It adopts instances behind ambiguous launches, confirms terminations,
  enforces deadlines first on every pass, and retries failed terminations with backoff.
- **Orphan detection**: an `og-*` instance with no live deployment (or a deployment terminated
  while the instance lives) becomes an orphan record with an alert. OpenGrid auto-terminates only
  instances it can prove it launched.
- **Termination is confirmed, never assumed**: terminated requires provider evidence (terminated
  status, or two `not_found` reads ≥ 60s apart, or absence from the list).
- **SSH key lifecycle**: per-deployment provider keys are recorded before registration and deleted
  after confirmed termination; abandoned keys are detected; foreign keys are never touched.
- **Tracker** (every minute): polls live deployments, records uptime and interruptions, and meters
  **hourly usage slices**, split at month boundaries, from the provider's own timestamps where
  available (e.g. Lambda's `first_healthy`). A terminate requested mid-launch still bills the real
  runtime.
- **Billing models** (no payments processed): usage records, versioned and composable fee policies
  (buyer fee %, flat per GPU-hour, spread, subscription, data API; nothing hardcoded), credits,
  draft invoices.
- **Cost reconciliation** after termination keeps four numbers separate: **quote**, **expected
  cost**, **provider-reported cost**, **OpenGrid transaction cost**, plus quote error, effective
  hourly rate, unexpected fees and billing rounding.
- **Price concepts are never mixed**: list price · observed market price · quote · execution price.

---

## 8. Routing quality, reliability & economics

- **Routing quality** per route: winner, runner-up, all candidates, market median, cheapest valid
  option, strategy, expected vs realized savings, provisioning success and latency, actual price,
  interruptions, uptime. Aggregates: routing/provisioning success rates, savings, quote accuracy.
- **Provider reliability from real transactions only**: launches, failures by kind, provisioning
  latency p50/p95, unexpected terminations, API error rate, termination success, quote accuracy,
  **with sample sizes; no score below 10 launches**. Validation runs are reported separately. Served at
  `/v1/execution/reliability` (key only).
- **Economic value**: per deployment market median vs selected price vs savings, only when a
  valid comparison exists (same GPU, on-demand, ≥ 3 providers); totals by GPU, provider and
  strategy.

Two separate datasets measure whether a provider's public API answers. Neither feeds provider reliability, any
routing score or capacity, and neither is blended into another number:
- **Provider probes, OpenGrid's own** (`probes/`, methodology/probes.md, migration `0024_probes`): 39 targets over
  32 providers (`probes/targets.py`; 28 real API routes, 1 API host, 10 websites, each kind labelled by strength),
  one header-only GET per probe from every vantage point that reports (the web process is one, job `probe_local`
  every 30 s; `probes/agent.py` adds more via `POST /v1/probes/ingest`, scope `probes:write`). Raw results with
  DNS / TCP / TLS / first-byte timings and a classified failure reason are kept 30 days in `probe_results`; hourly
  rollups (`probe_rollup`, every 5 min) are kept for good. `GET /v1/reliability/providers`,
  `/v1/reliability/providers/{provider}`, `…/dips`, `/v1/reliability/targets`, `/v1/reliability/agents`,
  `/v1/reliability/probes`; page `/probes` ("Provider probes").
- **Provider uptime, external** (`uptime/`, methodology/uptime.md): control-plane uptime and latency measured by
  UptimeProject from five probe locations, republished under CC BY 4.0 (job `uptime`, hourly; the first poll
  backfills up to a year of daily buckets). Shown on the `/uptime` page only, read through `/internal/uptime/*`,
  never served by the Data API (third-party data). Providers UptimeProject does not measure read "not measured",
  never zero. **Status-page incidents** (the providers' own reports: Hyperstack, Nebius, Crusoe, DigitalOcean,
  Lambda and Google Cloud; job `provider_incidents`, every 10 min) are `GET /v1/reliability/incidents`, shown beside a
  dip only when their times overlap, never as proof of cause.

---

## 9. Design partners, onboarding & product analytics

- **Design partner profiles**: company, technical contact, preferred GPUs, regions, workload type,
  normal provider and price, max price, expected GPU count and duration, latency/storage/network
  needs. Operators create a partner account, profile and first key in one step.
- **Feedback after every deployment**: *Would you have chosen this provider yourself? Was the price
  better? Was setup easier? Would you route the next workload through OpenGrid? What broke?*
- **Onboarding flow**: account → API key → (optional) provider credentials → route preview →
  understand why the provider won → supervised deployment → feedback.
- **Product analytics** (no IPs, no PII, respects Do-Not-Track): page views, searches, GPU and
  provider views, comparisons, watchlists, API calls, route previews, approvals, completions.
  **Funnel**: visitor → market user → account → API key → route preview → real deployment →
  second deployment, by week.

---

## 10. Alerts & Discord channels

### 10.1 Four channels
| Channel | Variable | Posts |
|---|---|---|
| `#opengrid-ops` | `OPS_ALERT_WEBHOOK_URL` | **Urgent, money at risk**: orphans, termination failures, past-deadline deployments, unknown states > 10 min, provider API down with active deployments, overspend > 20%, active resources without billing, unowned provider resources, kill switches; **data feeds gone silent** (below) |
| `#opengrid-deployments` | `DEPLOYMENTS_WEBHOOK_URL` | Route awaiting approval, approved, launching, running, terminating, terminated, failures |
| `#opengrid-market` | `MARKET_WEBHOOK_URL` | Notable and major market events |
| `#opengrid-news` | `NEWS_WEBHOOK_URL` | Stories with relevance ≥ 50, one post per story |

- Rich **Discord embeds** in OpenGrid green (`#91C61D`) with the OpenGrid logo, structured fields,
  links back to the terminal and timestamps; @-mentions disabled. Slack webhooks receive plain text.
- Cost-exposure alerts carry deployment, provider, account, **estimated $/hour exposure**, time in
  state and a suggested action; they re-escalate every 30 minutes and resolve only on evidence.
- Feeds post each item exactly once (even across restarts), never flood history into a new
  channel, and retry failed sends in order.
- **Alert on silence** (`alerts/silence.py`, job `silence_alerts`, every 10 min): one ops alert when a polled
  provider has no successful fetch for max(3h, 6 x its polling interval), one when its current listings drop
  to 0 (it had listings), one when its capacity feed stops while prices still arrive; one "recovered" alert
  each, never repeated. State is in `ops_alert_state`, so a restart never re-sends. Providers that are not
  polled (no API key), killed, or in `SILENCE_ALERT_MUTED` never alert.
- All webhook delivery is SSRF-safe (single DNS resolution, pinned IP, private ranges blocked, no
  redirects) and HMAC-signed.

### 10.2 Watchlists & user alerts
Watchlists of GPUs, providers, GPU+provider, regions and indices with live values. Alert rules such
as *H100 below $2/hr*, *B200 becomes available*, *Lambda H100 drops 10%*, *US availability falls
20%*, *H100 Index hits a new 30-day low*: edge-triggered with cooldowns, never firing on unknown
data. Delivery: in-app feed and signed webhooks (email not implemented).

---

## 11. Accounts, API keys & security

- **One OpenGrid account, one API key**: `opg_live_…` keys (256-bit), stored only as an HMAC with a
  server-side pepper; shown once; scoped (`data:read`, `route:preview`, `probes:write`, `route:execute`,
  `deployments:read/write`, `watchlists`, `billing:read`, `keys:read`, `account:manage`, `admin`);
  revocable with cascade to child keys; expiry; last-used tracking; usage logging.
- **Rate limits** per key **and** per account by request class (read / write / execute);
  shutdowns (terminate/stop) are never throttled like launches.
- **BYO provider credentials** encrypted at rest (Fernet / MultiFernet with key rotation), never
  returned by any API, never usable by another tenant.
- **Security controls** (independently re-audited by attack):
  CSRF protection on every state-changing request · safe URL schemes everywhere links are rendered
  · CSP limited to OpenGrid's own scripts (connect-src adds only the PostHog ingest origin when POSTHOG_KEY is set) · X-Frame-Options / nosniff / Referrer-Policy · login
  throttling · DNS-rebinding protection · SSRF-safe webhooks · production detection that can't be
  spoofed · secrets redacted from all logs · admin API requires platform-admin authority · tenant
  isolation on every resource.
- **Public pages** (optional, `PUBLIC_PAGES=true`) expose only read-only market data; anything that
  spends money or reads private data still requires a key.
- **Site sign-in** (`/login`, `accounts/sitelogin.py`): a styled form (throttled, same-origin only) that sets a
  signed session cookie (7 days) for one of two logins: the operator (`APP_USER` / `APP_PASSWORD`) or the optional
  read-only viewer (`VIEWER_USER` / `VIEWER_PASSWORD`: market data and `data:read` only, no admin pages, keys or
  money paths). Basic auth still works for scripts. The return path must match an allowlist of URL-safe characters
  (no open redirect: browsers drop tabs and newlines and read `\` as `/`). The app has five server-side redirects,
  each to a constant or allowlisted same-site target (the redirect to `/login`, `/login` x2, `/logout`, `/classic` →
  `/gpus`), enforced by the security re-audit test. Sign-in-required states link to `/login`.
  With public pages on, a request without a login or key gets `401` with `WWW-Authenticate: Bearer`, so a visitor
  never sees a password popup from a background call (each page asks `/v1/me`), and a browser asking for an unknown
  page gets the app's own "Page not found" (404). With public pages off, a browser page request is redirected to
  `/login`.

---

## 12. Web terminal (UI)

A data-first market terminal that a first-time visitor can read: a dark (or light) terminal look, dense
monospaced market tables, and plain-language copy, access states and errors around them. A page-by-page
description is in docs/UI_GUIDE.md.

**Navigation and access**
- **Home**, then **Explore** (free, no account): **GPUs** (Compare, Explorer, Heatmaps, Providers, Capacity, Reserved prices, True cost, RL cost, Provider uptime, Provider probes), **Find
  Compute** (Ask the assistant), **Markets** (Indices, Opportunities, Events, News, Watchlists), **Developers**
  (API docs, API keys, Onboarding). Then **Your workspace** (needs an account): **My Jobs** (Batches, Deployments,
  Reserve, Route). Methodology at the bottom. **Operator** tools (Execution, Checklist, Partners, Value, Ops)
  appear only for the operator / an admin key.
- Pages whose data needs an account carry a lock until you are signed in. The top bar shows **"Public access"**
  (a popover says what a visitor can use, that accounts are invite-only, and offers the operator sign-in for
  staff only), **"Operator"** or **"Signed in"** (an API key or the read-only viewer login). The site logins at
  `/login` are the operator's and the viewer's; customers use API keys, and no customer sign-up is offered (none
  exists).
- Errors are a title, one plain sentence and the next step; the HTTP status and endpoint sit in a folded
  "Technical details". A missing sign-in is an access state ("accounts are invite-only"), not a failure.
  Unknown pages show "Page not found" with links.

**Shell**
- Status bar in words: "Prices updated 3 min ago" (exact local time on hover), "Prices may be out of date"
  after 10 minutes, "Can't reach OpenGrid". A ticker of live GPU prices shows only whole items, then "+N more".
- **Search / command bar** (`/`): empty, it lists the main places and example queries; every suggestion is
  clickable (commands with an argument fill the box). Type a GPU, provider or page; a question ("which gpu
  for llama 70b") goes to the assistant. Power-user commands stay: `g h100` market, `p lambda` provider,
  `c h100 h200` compare, `r h100 8 us` route preview, `w h100` watch, `j` / `j new` jobs, `e` `n` `o` `x` `i` `h`
  `m` `d` pages, `home` `gpus` `find` `markets` `developers`, `ask <question>`, `theme`; `?` lists them all.
- Design system (web/app.css): one type scale (`--fs-*`, body 14px, data tables 12.5px, no raw pixel sizes,
  enforced by a test), 32px controls (44px on touch screens), a 2px focus ring, and text colours that meet
  WCAG AA contrast in both themes (tested). Tested at 375px, 768px and 1440px; on phones the menu is a full
  list with 44px rows.

**Pages**
| Area | Pages |
|---|---|
| Home | "Compare GPU prices. Find the right compute. Run supported workloads.", a "What works today" list (including what does not work yet), three actions tagged free / simulation / needs an account, the live price board, indices, how it works |
| Market | Overview · Indices · Index detail · GPUs · GPU detail · GPU family · Providers · Provider detail · Compare · Explorer · Heatmaps · Capacity · Reserved prices · True cost (full-cost estimator) · RL cost (RL post-training estimator) · Provider uptime · Provider probes |
| Signals | Opportunities · Events · News (feed + timeline) |
| Find Compute | four guided steps (what you are doing, model or project, what matters incl. **training data** and epochs, or for inference optional image / audio / video inputs, recommendation); one recommended option with price, memory per GPU, availability, estimated runtime and whole-job cost, and whether OpenGrid can run it; up to three alternatives; the three or four answers kept apart (lowest advertised price, best fit, execution-ready, lowest cost for the whole job); per-option performance across its GPUs; everything else under Advanced details |
| Workspace | My Jobs (Needs attention / Active / Completed, simulation vs real tags, a plain-English box) · New job (example job as a free simulation, a real-GPU card with readiness, run your own model) · Job page (at a glance, next step, timeline; Resume… / Move… with the quote first, a move suggestion banner) · Batches (list) · Batch page (shards, placement, budget, approval, best result) · Route (a simple ticket; technical panel folded) · Deployments · Deployment detail · Reserve (price-lock requests) · Request detail · Watchlists |
| Developers | three paths (AI assistant over MCP, CLI, REST) with what each needs; two first successes without a key; per-client MCP setup (Claude Code, Cursor, VS Code, Codex, Claude Desktop / claude.ai), each marked tested or not; access requests via GitHub issues · API docs (curated public reference with Try it; the full schema for the operator) · API keys (one access state when signed out; presets) · Onboarding (design partners) |
| Reference | Methodology · Assistant |
| Operator | Execution control (modes, kill switches, flags, orphans, resources, validation) · First-route checklist · Partners · Value (economics, quality, reliability, funnel) · Ops (data quality) |

- **GPU pages** feel like stock pages: index level, low/median/high, spread, providers,
  availability, changes, volatility, percentiles, robust price charts with event/news markers,
  availability history, provider ranking, regional pricing, best available now, dispersion,
  price distribution, price per capability, hardware specs, alternatives, events, news, listings.
- **Index detail pages** for the settlement indices carry a **Settlement fixings** panel (latest daily TWAP
  and hourly fixings in UTC, status, surveillance flags, evidence, a Verify button).
- Shareable URLs for every view, CSV export on every table, honest "insufficient data" states,
  data-kind badges (observed / estimated / transaction / archived; `inferred` is not shown as a badge) and
  freshness badges.
- **SEO** renderers for GPU, family, provider and compare pages with real numbers, structured data
  and a sitemap (active when `PUBLIC_PAGES` is on).
- **Known UI gaps:** the New job page offers visitors a "Sign in" that is the operator / viewer site login; the
  same provider can appear twice for one GPU when it lists two regions.

---

## 13. Text terminal (TUI)

`python terminal.py` is a Textual app with a command bar:
`listings`, `avail`, `cheapest [n]`, `history`, `refs`, `mapping`, `quirks`, `unmapped`, `raw`, `summary`, `fetch`,
`normalize`, `reset`, `indices`, `idx <id>`, `ctx <gpu>`, `mkt <gpu>`, `best <gpu> [count] [region] [mode]`,
`preview <gpu> <count> [region] [max]` (never provisions), `ev`, `opp`, `news`, `prov <name>`, `ops`, `caps`,
`fam <family>`, `help`, `quit`; any other text filters the listings.

---

## 14. Public API

Two products share one app, one account and one key; scopes decide what a key reaches:
- **OpenGrid Data API v1** (market intelligence; contract in **docs/API_CONTRACT.md**, enforced at the HTTP edge by
  `api/contract.py`). Success: `{"data": …, "meta": {"as_of", "api_version", "request_id", "next_cursor", …}}`.
  Every error, whoever produced it (validation, auth, rate limits, an unhandled exception), is one shape:
  `{"error": {"type", "code", "message", "param", "request_id", "docs_url"}}`, with `docs_url` pointing at
  `docs.tryopengrid.com/errors#<code>`. Dated versions: the `OpenGrid-Version` header, else the version pinned to the
  key (`api_keys.api_version`, migration `0034`), else latest (today the only one, `2026-11-03`); an unknown value is
  a 400 and the version used is echoed. **Cursor paging on every list** (`limit`, `cursor`; follow
  `meta.next_cursor` until it is `null`), frozen vendor-prefixed GPU ids with `gpu_name` beside them, exact decimal
  strings for fixings, ETag + 304 + `Cache-Control`, `?as_of=` on `/v1/listings`, bulk export `/v1/exports`, the
  schema at `/v1/openapi.json` (key required).
- **OpenGrid Compute API** (routing, jobs, reservations, accounts, billing): same `/v1` prefix, keeps FastAPI's
  `{"detail": …}` errors for now.
- **`DATA_API_HOST`** (set in production): a request to that host reaches only Data API routes and needs
  an API key (no site login, no anonymous reads); it answers CORS only for the docs origin. Unset, nothing changes.
  With `PUBLIC_PAGES` on, the main host serves the Data API's read-only areas to anonymous visitors.
- **`/internal/*`**: the website's own reads (overview, tape, opportunities, heatmaps, compare, uptime, trust,
  polling, mapping, live). Never on the Data API host, never with a key, not part of the contract. Every website
  page reads only `/v1` and `/internal`; the first UI's routes (`/market`, `/listings`, `/history`, …) are retired
  and `/classic` redirects to `/gpus`.
- **Contract tests**: `tests/test_contract.py` sweeps every Data API GET route for the envelope, version header and
  error shape; `tests/test_contract_shapes.py` compares responses with the frozen shape snapshot
  `tests/contracts/2026-11-03.json`; `tests/test_docs_site.py` checks the generated docs are current.

Authentication: `Authorization: Bearer opg_…` (or the operator session on the main host). Routes, as registered in
the app:

| Domain | Endpoints |
|---|---|
| Reference | `/v1/gpus`, `/v1/gpus/{gpu}`, `/v1/families`, `/v1/families/{family}`, `/v1/providers`, `/v1/providers/{provider}`, `/v1/regions`, `/v1/methodology`, `/v1/methodology/{name}` |
| Markets & listings | `/v1/markets` (`?trend=true`), `/v1/markets/{gpu}`, `…/regions`, `…/history`, `…/history/daily`, `…/history/archived`, `…/history/dispersion`, `…/history/providers`, `/v1/listings` (`?as_of=`, `?previous=true`), `/v1/listings/history`, `/v1/providers/{provider}/listings/context`, `/v1/providers/{provider}/gpus/{gpu}/context` |
| Indices & fixings | `/v1/indices`, `/v1/indices/{id}`, `…/history`, `…/fixings`; `/v1/fixings`, `/v1/fixings/{id}`, `…/verify`, `/v1/fixings/chain/verify`, `/v1/fixings/restatements`, `/v1/fixings/methodology` |
| Capacity, reserved, costs, FX | `/v1/capacity`, `/v1/capacity/history`, `/v1/reserved`, `/v1/reserved/summary`, `/v1/costs/components`, `/v1/costs/billing`, `/v1/costs/sources`, `/v1/costs/presets`, `POST /v1/costs/estimate`, `/v1/costs/rl-presets`, `POST /v1/costs/rl-estimate`, `/v1/fx/rates`, `/v1/fx/rates/history` |
| Reliability | `/v1/reliability/providers`, `/v1/reliability/providers/{provider}`, `…/dips`, `/v1/reliability/targets`, `/v1/reliability/agents`, `/v1/reliability/probes`, `/v1/reliability/incidents`; probe agents: `/v1/probes/targets`, `POST /v1/probes/ingest` |
| Signals | `/v1/events`, `/v1/events/types`, `/v1/events/shifts`, `/v1/events/shifts/{id}`, `/v1/events/shifts/definition`, `/v1/news`, `/v1/news/{id}`, `/v1/news/sources`, `/v1/news/topics`, `/v1/news/families`, `/v1/timeline`, `/v1/timeline/around`; admin: `POST /v1/news/refresh`, `POST /v1/news/reclassify` |
| Market context | `/v1/macro/series`, `/v1/macro/series/{series}`, `…/history`, `/v1/macro/power` (power markets and equities: admin only, section 5a) |
| Exports | `/v1/exports`, `/v1/exports/{dataset}` |
| Website reads | `/internal/overview`, `/internal/tape`, `/internal/opportunities`, `/internal/heatmaps/{kind}`, `/internal/compare`, `/internal/trust/providers`, `/internal/polling`, `/internal/mapping`, `/internal/uptime/*` (5), `/internal/live/status`, `/internal/live/events` |
| Trust & capabilities | `/v1/capabilities`, `/v1/capabilities/{provider}`, `/v1/quality` |
| Recommend & assistant | `POST /v1/recommend`, `POST /v1/recommend/estimate`, `POST /v1/assistant` |
| Routing | `/v1/best/{gpu}`, `/v1/route/preview`, `/v1/route`, `/v1/route/{id}`, `…/approve`, `…/reject`, `/v1/quotes/{id}` |
| Deployments | `/v1/deployments`, `/v1/deployments/{id}`, `…/terminate`, `…/stop`, `…/feedback`, `…/outcome`; launch reliability `/v1/execution/reliability` |
| MCP (hosted) | `/mcp`: the MCP tool set over streamable HTTP; Bearer API key only (section 6c) |
| Training jobs | `/v1/jobs/templates`, `/v1/jobs/readiness`, `/v1/jobs/preview`, `/v1/jobs`, `/v1/jobs/{id}`, `…/logs`, `…/metrics`, `…/artifacts`, `…/artifacts/{name}`, `…/cancel`, `…/resume`, `…/resume/preview`, `…/move`, `…/move/preview`; 5 runner callbacks outside the schema (job token only: events, instructions, artifacts, resume, complete) |
| Batches | `/v1/batches/preview`, `/v1/batches` (GET list, POST submit), `/v1/batches/{id}`, `…/approve` (admin), `…/cancel` |
| Reservations | `/v1/reservations/reference`, `/v1/reservations/requests`, `/v1/reservations`, `/v1/reservations/{id}`, `…/accept`, `…/withdraw` |
| Accounts & billing | `/v1/me`, `/v1/keys` (+ `DELETE /v1/keys/{id}`), `/v1/scopes`, `/v1/usage`, `/v1/credentials` (+ `DELETE /v1/credentials/{id}`), `/v1/billing/usage`, `/v1/billing/invoices`, `/v1/billing/policy` |
| Watchlists | `/v1/watchlists`, `/v1/watchlists/{id}`, `…/items`, `…/items/{item_id}`, `/v1/alerts`, `/v1/alerts/{id}`, `…/test`, `/v1/alerts/firings` |
| Analytics | `/v1/economics`, `/v1/onboarding`, `/v1/partners/me`, `POST /v1/events/track` (anonymous product analytics) |
| Admin & ops (74 in the schema, 9 more outside it) | execution control, kill switches, validation, reconciliation, orphans, resources, channels, ops, metrics, traces, checklist, partners, funnel, accounts, credits, draft invoices, fee policies, exposures, feedback, quality and quarantine, product analytics / PostHog, usage, internal-data registry, reservation offers, fixing restatements, methodology change notices; outside the schema: `/v1/ops/power/*` (7) and `/v1/ops/context/equities` (2) |

Counts (from `main.app.openapi()` on a freshly migrated database): 249 operations, 227 under `/v1`, 74 of them under
`/v1/ops` or `/v1/admin`, 15 under `/internal`, and 7 operator/debug routes at the root (`/health`, `/fetch`,
`/normalize`, `/raw`, `/raw/summary`, `/raw/payload`, `/unmapped`). The public customer docs at
**docs.tryopengrid.com** (`docs-site/`) publish one page per customer endpoint (69), generated by
`python -m api.docs_openapi` (internal, website-only and write-side routes left out), with a key-holders-only
playground that calls the Data API host directly. Full interactive reference: `/api` in the terminal, or `/docs`
(OpenAPI).

---

## 15. Operations: deployment, jobs, configuration

### 15.1 Deployment
Railway, one service + Postgres (+ two probe-agent services), built from `pyproject.toml` / `uv.lock`. Service settings
(start command, healthcheck, replicas, domains) live in Railway infrastructure-as-code, `.railway/railway.ts` (it replaced `railway.toml`, which Railway stops reading on
2026-12-01; preview
`railway config plan`, apply `railway config apply`; variables stay unmanaged), with **exactly one worker** (the poller lives in the web process). Migrations run
automatically at startup (one head, `0038_measured_performance`). A push to GitHub does not trigger a Railway build on its
own: deploy with `railway up --service opengrid-terminal --detach`. The app **refuses to start in production**
without `APP_PASSWORD` and `API_KEY_PEPPER`. `RAILPACK_DEPLOY_APT_PACKAGES=git` puts `git` in the image (the dataset
backfill needs it). Long one-off backfills (`python -m backfill …`, `python -m news.history run`,
`python -m power backfill …`, `python -m context equities backfill`) run on the production server detached, after a
deploy, because a deploy restarts the container and stops them; each resumes where it stopped. The customer docs
deploy separately (`cd docs-site && vercel deploy --prod`).

### 15.2 Background jobs
33 jobs are registered when every optional source is configured (`jobs.JOBS`; the provider poll loops run beside
them); 30 with the defaults (`context_series`, `context_equities` and `power_forward` need keys or `POWER_ENABLED`).
`OPENGRID_NO_JOBS` registers them but starts none; with `POLLER_ENABLED` off, the jobs marked "poller" are not
registered. `news` and `gpuhunt_snapshot` are always registered and skip their work when `NEWS_ENABLED` /
`GPUHUNT_SNAPSHOT_ENABLED` is off.

| Job | Every | Purpose |
|---|---|---|
| provider pollers | tiered: 2 min to 6 h per provider and stream (section 3.1) | fetch, store raw, normalize, quality-screen, capacity |
| `market_hourly` | 10 min | hourly rollup → indices, provider value, events hooks |
| `index_fixings` | 10 min | settlement fixings for hours that can no longer be revised; daily TWAP |
| `market_events` | 15 min | event detection |
| `market_shifts` | 15 min | major shifts and their related items (never causal) |
| `news` | 1 min | poll due news sources |
| `reconcile` | 2 min | provider vs OpenGrid state, orphans, deadlines |
| `routing_tracker` | 1 min | deployment status, uptime, usage metering |
| `job_sync` | 30 s | training jobs: deployment state + runner callbacks → job state; terminate on completion, failure, timeout |
| `job_relaunch` | 1 min | automatic relaunch of an interrupted job and opt-in OOM escalation (acts only with `JOB_AUTO_RELAUNCH_ENABLED` and a per-job retry) |
| `job_move` | 30 s | drives user-requested moves: verified checkpoint → terminate → confirmed gone → successor; a refusal before the terminate aborts the move (acts only with `JOB_MOVE_ENABLED`) |
| `batch_advance` | 30 s | batches: follow each shard's job, the failure policy, the batch approval for queue shards, submit queued shards into free slots, requeue a crashed submission (`BATCH_QUEUE_ENABLED` off: nothing new is submitted; status, approval and cancel still run) |
| `reservation_expiry` | 5 min | expire stale price-lock offers and requests |
| `channel_feeds` | 1 min | Discord deployments / market / news posts |
| `silence_alerts` | 10 min | ops alert when a provider's prices, listings or capacity go silent; recovery |
| `alerts` | 3 min | watchlist alert evaluation |
| `quality_schema_watch` | 2 min | provider response schema changes |
| `raw_storage` | 1 min | raw payload deduplication and retention |
| `route_outcomes`, `product_events` | 5 min | routing quality and product analytics |
| `db_latency` | 1 min | database latency metric |
| `api_usage_flush`, `api_usage_retention` | 10 s / daily | API usage logging |
| `gpuhunt_snapshot` | daily | raw copy of dstack's expiring gpuhunt catalogues (`GPUHUNT_SNAPSHOT_ENABLED`) |
| `uptime`, `provider_incidents` | 1 h / 10 min | UptimeProject uptime and latency; providers' status-page incidents (registered only with `UPTIME_ENABLED`, default on) |
| `probe_local`, `probe_rollup` | 30 s / 5 min | OpenGrid's own probes from this process (registered only with `PROBES_LOCAL_ENABLED`, default on); hourly rollups |
| `fx_rates` | 1 h | ECB reference rates (poller) |
| `cost_components` | daily | full-cost sources into `cost_snapshots` (poller) |
| `lium_stream` | 1 min | supervisor of the Lium WebSocket consumer (poller, `LIUM_STREAM_ENABLED`) |
| `context_series` | daily | EIA state electricity prices and SEC EDGAR series (poller; only with `EIA_API_KEY` or `SEC_USER_AGENT`) |
| `context_equities` | 1 h | internal equity bars (poller; only with both Alpaca keys) |
| `power_forward` | 12 h | power markets: forward read with overlap, then the cross-analysis (poller; only with `POWER_ENABLED=true`, on in production) |

### 15.3 Key configuration
`config.py` defines 171 settings; the ones an operator sets:

| Variable | Purpose |
|---|---|
| `DATABASE_URL`, `APP_USER`, `APP_PASSWORD` | database and site login (operator) |
| `VIEWER_USER`, `VIEWER_PASSWORD` | optional read-only site login: market data only, no admin pages (`accounts/sitelogin.py`) |
| `API_KEY_PEPPER`, `CREDENTIALS_ENCRYPTION_KEY` | key hashing and credential encryption; never change once set |
| `ENVIRONMENT=production`, `TRUST_PROXY_HEADERS=true` | production behaviour behind Railway's proxy |
| `PUBLIC_BASE_URL`, `DATA_API_HOST`, `DOCS_BASE_URL` | canonical links; the Data API host name (Data API routes only, key required; unset: no such host); the customer docs origin (error `docs_url`, the only CORS origin) |
| `SALAD_*`, `HYPERSTACK_API_KEY`, `LAMBDA_API_KEY`, `RUNPOD_API_KEY`, `DIGITALOCEAN_API_KEY` | provider market data (and routing where supported) |
| `JARVIS_API_KEY`, `MASSEDCOMPUTE_API_KEY`, `THUNDER_API_KEY`, `CLORE_API_KEY`, `DEEPINFRA_API_KEY`, `HOTAISLE_API_KEY` + `HOTAISLE_TEAM`, `LATITUDE_API_KEY`, `CRUSOE_ACCESS_KEY_ID` + `CRUSOE_SECRET_KEY` | further provider prices, stock or capacity (read-only) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | read-only EC2 market calls: spot price history, spot placement scores, capacity blocks |
| `ALIBABA_ACCESS_KEY_ID`, `ALIBABA_ACCESS_KEY_SECRET` | Alibaba Cloud ECS spot and pay-as-you-go prices (read-only RAM user, international site); unset: the provider is not polled |
| `GCP_API_KEY`, `GCP_SERVICE_ACCOUNT_JSON`, `GCP_PROJECT_ID` | Cloud Billing Catalog; the read-only service account opens GCP regions beyond Iowa |
| `SHADEFORM_API_KEY`, `VAST_API_KEY`, `VERDA_CLIENT_ID/SECRET` | optional additional routing credentials |
| `POLLER_ENABLED`, `LIUM_STREAM_ENABLED`, `NEWS_ENABLED`, `SEC_USER_AGENT` | background polling, the Lium stream, news feeds; the SEC fair-access User-Agent (news EDGAR sources and market context) |
| `UPTIME_ENABLED`, `PROBES_LOCAL_ENABLED`, `PROBES_LOCATION`, `PROBES_RAW_RETENTION_DAYS` | external uptime; OpenGrid's own probes (raw kept 30 days by default) |
| `GPUHUNT_SNAPSHOT_ENABLED`, `SKYPILOT_CATALOG_ENABLED`, `SKYPILOT_CATALOG_LICENCE` | archived-history sources (SkyPilot off until its licence is confirmed) |
| `EIA_API_KEY` | EIA API v2: market-context state electricity prices, and the EIA-930 and natural-gas power sources |
| `ALPACA_DATA_KEY_ID`, `ALPACA_DATA_SECRET_KEY`, `EQUITY_TICKERS` | internal equities (market data only); ticker override |
| `POWER_ENABLED`, `POWER_SOURCES`, `POWER_PUE` | power markets: the `power_forward` job (default off; on in production); sources (empty: each module's default); facility overhead (1.3) |
| `PJM_API_KEY`, `ISONE_USERNAME`, `ISONE_PASSWORD` | PJM and ISO-NE power sources (unset: off) |
| `ERCOT_API_SUBSCRIPTION_KEY`, `ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD` | defined for ERCOT's Public API, but not read: ERCOT is read keyless from its public MIS reports |
| `ROUTING_LIVE_PROVISIONING` | env ceiling for real launches (default off) |
| `ROUTING_LAUNCH_DEFAULTS` | per-provider launch defaults, e.g. the validation SSH key name |
| `OPS_ALERT_WEBHOOK_URL`, `OPS_ALERT_WEBHOOK_SECRET` | ops alerts (+ signing secret used by all channels) |
| `DEPLOYMENTS_WEBHOOK_URL`, `MARKET_WEBHOOK_URL`, `NEWS_WEBHOOK_URL` | channel feeds |
| `SILENCE_ALERT_MIN_HOURS`, `SILENCE_ALERT_POLLS`, `SILENCE_ALERT_EMPTY_GRACE_MINUTES`, `SILENCE_ALERT_MUTED`, `SILENCE_ALERTS_ENABLED` | alert on silence (defaults 3h, 6 polls, 30 min, none, on) |
| `PUBLIC_PAGES` | open read-only market pages to anonymous visitors (default off) |
| `POSTHOG_KEY`, `POSTHOG_HOST`, `POSTHOG_PROJECT_ID`, `POSTHOG_PERSONAL_API_KEY` | optional PostHog product analytics (the CSP's connect-src adds the ingest origin only with `POSTHOG_KEY`) |
| `RAW_PAYLOAD_RETENTION_DAYS`, `RAW_SNAPSHOT_RETENTION_DAYS` | raw storage retention (payloads referenced by settlement fixings are always kept) |
| `JOB_CALLBACK_BASE_URL` | where GPU runners call back (default `PUBLIC_BASE_URL`; https required) |
| `JOB_ARTIFACT_STORE`, `JOB_ARTIFACT_MAX_MB`, `JOB_ARTIFACT_S3_*` | training artifacts: Postgres (default) or S3-compatible; 64 MB per artifact |
| `JOB_CHECKPOINT_MAX_MB`, `JOB_CHECKPOINT_KEEP` | resume checkpoints: at most 32 MB each, the newest 2 kept |
| `JOB_SYNC_INTERVAL_SECONDS`, `JOB_BOOTSTRAP_TIMEOUT_MINUTES`, `JOB_RUNNER_SILENCE_MINUTES`, `JOB_TOKEN_GRACE_MINUTES` | training-job timing |
| `JOB_AUTO_RELAUNCH_ENABLED` | automatic relaunch of interrupted jobs and opt-in OOM escalation (default off) |
| `JOB_PROVIDER_STOP_CONFIRM_SECONDS` | a provider-initiated stop counts as an interruption once a later read confirms it this long after (default 60; 0 = off) |
| `JOB_MOVE_ENABLED`, `JOB_MOVE_INTERVAL_SECONDS`, `JOB_MOVE_CHECKPOINT_TIMEOUT_MINUTES`, `JOB_MOVE_TERMINATION_TIMEOUT_MINUTES` | moving a running job (default off; 30 s pass; abort after 30 min without a new checkpoint; alert after 60 min unconfirmed) |
| `JOB_MOVE_SUGGEST_MIN_SAVINGS_PCT`, `JOB_MOVE_OVERHEAD_MINUTES` | the read-only move suggestion (default 20%, 10 min overhead) |
| `BATCH_MAX_SHARDS`, `BATCH_QUEUE_ENABLED`, `BATCH_ADVANCE_INTERVAL_SECONDS` | batches: at most 64 shards per batch; the queue (default on); the `batch_advance` pass (30 s) |
| `ASSISTANT_LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `ASSISTANT_LLM_MODEL` | optional LLM for the assistant (off unless both provider and key are set) |

`deploy/make_env.py` builds the Railway variables file and generates the secrets.

---

## 16. Testing

136 Python test files and 14 JavaScript test files in `tests/` (plain scripts, each with its own scratch
Postgres database, no real provider calls), plus 5 CLI/MCP test files in `cli/tests/`. `tests/run_all.py` runs all
150 in parallel (6 at a time by default; `JOBS`, `TIMEOUT` and name filters adjust it), and GitHub Actions
(`.github/workflows/tests.yml`) runs it on every push and pull request against a throwaway Postgres 16 whose zone is
deliberately not UTC. Beyond the per-module tests:
- unit and integration tests for every analytics module, API and page;
- **routing benchmark**: 50 deterministic decision scenarios;
- **chaos suite**: 22 failure scenarios (timeouts, 5xx, crashes mid-launch, restarts, expired
  credentials, concurrent workers…);
- **money-loss invariants**: 30 scenarios (17 test functions) proving OpenGrid never loses track of a resource that
  can cost money: every case ends either proven terminated and billed, or flagged as possibly
  billing with escalating alerts;
- **concurrency proofs**: simultaneous approvals across threads and separate processes;
- **security re-audit** and **red-team** regression suites;
- **training jobs**: GPU selection, the job state machine, API, security (token, quotas, injection), the
  runner and bootstrap, and an **end-to-end simulation** (real reporter + real CPU training over HTTP
  against a fake Lambda);
- **batches, resume and move** (`test_batches.py`, `test_job_resume_move.py`): the budget ledger under concurrent
  threads, one approval and re-approval, the queue, no second job for a shard after a submission error, a retry in
  flight never judged by its refused try, relaunch / resume / move inside the batch budget, a move never terminating
  before a new verified checkpoint, aborts and failed moves, provider-initiated stops;
- **adapter contracts** (`tests/test_adapter_contracts.py`): the exact request each adapter sends for quote, launch,
  status, terminate and list, pinned to the providers' API references (methodology/adapter-contract-audit.md);
- **workload intelligence** (`test_workload_intelligence.py`, `test_media_workloads.py`, `test_recommend.py`):
  measured-performance rules, whole-job ranking, the audio / image + text / video sizing, and 24 golden payloads
  without `media` whose estimates must stay byte-identical;
- **hosted MCP** (`tests/test_mcp_remote.py`): the real app on uvicorn with real API keys and the official MCP
  client: tool list, the sandbox demo, scopes through the caller's key, every 401/403 path, the key never
  echoed, and a thread-starvation proof (48 concurrent calls on a 6-thread pool finish; without the gate's
  admission limit they starve);
- **settlement fixings**: finality, TWAP, hash-chain tamper detection, the immutability trigger,
  restatements, evidence retention;
- **reservations**: state machine, supply-basis enforcement, tenant isolation, nothing launched or billed;
- **provider page parsers**: the newer scraped price pages parsed from saved real copies (CoreWeave, Together AI,
  Jarvislabs, Hot Aisle, Boost Run, Vultr, Nebius), plus one parser test file each for Google Cloud, Azure,
  Oracle, IBM, the general clouds (Linode, OVHcloud, Scaleway, Koyeb) and Shadeform's Horizon / Phyntec, and one
  file each for most providers added since (`test_provider_*.py`);
- **data visibility** (`tests/test_visibility.py`): seeds unmistakable internal listings, power, gas and derived
  values, calls every public route enumerated from the app, and fails on any internal name, price or count; also
  asserts no `/v1/power` route exists;
- **API contract** (`test_contract.py`, `test_contract_shapes.py` against `tests/contracts/2026-11-03.json`) and the
  customer docs (`test_docs_site.py`);
- **archived history, news history, shifts, capacity, freshness, full cost, reserved prices, FX, live feeds, uptime,
  probes, market context, equities and power** (four power files: sources, cross-analysis, NYISO / MISO, SPP / ERCOT
  / PJM / ISO-NE), each parsed from saved real responses in `tests/fixtures_*`;
- JavaScript tests for the terminal's core, shell, navigation, charts, Find Compute, jobs, batches, cost, RL cost,
  reserve, reserved, developers, assistant and execution UI.

Known flaky: `test_jobs_e2e` has failed intermittently ("no usage slice metered"), and `test_money_invariants` can
exceed the 10-minute timeout on a busy machine.

---

## 17. Data honesty principles

1. Never fabricate market data: no number without coverage; "insufficient data + reason" instead.
2. Never guess GPU mappings; never merge materially different variants.
3. Label every number by how it was obtained: **observed**, **derived**, **estimated**, **measured**, **archived** or
   **transaction** (methodology/data-kinds.md); availability is explicit, inferred or unknown.
4. Never mix list price, observed market price, quote and execution price.
5. Never claim provisioning support, a completed route, or reliability that isn't real.
6. Never claim news caused a market move.
7. Every published statistic links to its methodology.
8. Data OpenGrid may read but not republish is used internally and never exposed; a source whose terms forbid
   automated access is not read at all.
9. Context (power, gas, equities, filings) sits beside GPU prices and never mixes into them: correlation, never
   cause.

---

## 18. Current status & known limits

- **Market intelligence**: live in production; window statistics (7d/30d/90d) fill in as history
  accumulates.
- **Public pages** are OFF in production (`PUBLIC_PAGES=false`, checked 2026-11-14, a deliberate choice: the market
  data is the product): every page and `/robots.txt` / `/sitemap.xml` need the site login, so nothing on
  `terminal.tryopengrid.com` is indexed by search engines; the Data API host needs an API key. The marketing site
  `tryopengrid.com` (separate repo) is public.
- **Execution in production** (checked 2026-11-14): the env ceiling `ROUTING_LIVE_PROVISIONING=true` is set, but the
  database mode has never been changed (no `execution_controls` mode row, empty `execution_control_log`), so the
  effective mode is the default PREVIEW_ONLY; no provider has execution flags (all adapters `simulated`) and no
  deployment exists. The env ceiling therefore no longer blocks launches: an admin changing the mode to SUPERVISED,
  plus the remaining first-live-route checklist items (`routing/checklist.py`), is what stands before the first
  supervised validation launch. `JOB_AUTO_RELAUNCH_ENABLED` and `JOB_MOVE_ENABLED` are unset (off).
- **Hosted MCP**: the endpoint (`/mcp`) is live and accepts only an API key.
- **Accounts**: no self-serve sign-up, customer login or payments yet. Keys are created by the operator (`/keys`,
  `/admin/partners`, or `POST /v1/admin/accounts/{id}/keys`); the public docs tell customers to ask
  hello@tryopengrid.com. Credits are admin-issued. No Python SDK and no stored daily export files yet (exports are
  built per request).
- **Data API v1** (section 14): one contract, one version (`2026-11-03`), customer docs at
  docs.tryopengrid.com. `/v1/listings?as_of=` and the listings export scan `listing_observations` and are untested
  at production size. A few website pages still translate new field names back to old ones in the browser
  (`OG.legacyGpu`, `OG.legacyHistory`).
- **Power markets** (§5a): internal only. Every read is admin-only under `/v1/ops/power/*`; there is no public power
  route, even for the public-tier ERCOT and EIA-930 series. Missing markets: **PJM** (needs `PJM_API_KEY`), **ISO-NE**
  (needs an ISO Express account) and **MISO** (its terms forbid automated access to its public files; the Data
  Exchange API is not implemented). CAISO history before 2023-08 is not available from OASIS. A derived
  cross-analysis row is internal unless every input is public, and all of them are served to admins only.
- **Market context and equities**: EIA and EDGAR series are public at `/v1/macro/*`; all 40 equity tickers are
  internal.
- **Archived history**: about 30 of the 60 providers have no archive; AWS reserved archive rows are stored but not
  served; archived points are sampled on capture days only.
- **News**: GDELT throttles (HTTP 429 "limit requests"), so GDELT history runs are paced at one request per 5.5 s
  and are slow; the GDELT GPU-cloud query returned nothing before d2fad60.
- **Execution**: built, hardened, and independently reviewed; **all adapters are still
  simulated**. Training jobs (§6a), batches, resume, OOM escalation and move are built and tested but have not run on
a real GPU provider yet. The next step is one supervised **Lambda validation run** (≈ $1 realistic, ≤ $1.85
  worst case with the operator abort rule). Customer routes follow only after validation.
- **Settlement fixings** (§4.2a) and **price-lock requests** (§6b): built and tested; fixing history starts at
  deployment. **No futures, swaps or trading of any kind**: see docs/FUTURES_REGULATORY_PATH.md for the regulated
  paths (index licensing to an exchange is the recommended one).
- **Workload intelligence**: every runtime is theoretical until real jobs complete (no measured observation exists
  yet). Audio, image + text and video sizing is an internal accuracy improvement of recommendations: video
  generation memory is always low confidence, video understanding is modelled for Qwen2-VL only, the training
  memory of video and TTS models is a lower bound, and the audio pipeline efficiency is uncalibrated.
- **Providers**: Alibaba Cloud is built but dormant until an AccessKey is set. RunPod's market-data poller reads pods
  from REST v2 when `RUNPOD_API_KEY` is set (REST needs a key; confirmed in production on 2026-11-14: the
  production key has `catalog:read`, 9 responses, 0 failed), but still reads RunPod's GraphQL API, which RunPod
  retires in early 2027, for (a) serverless pool flex/active prices, which REST v2 does not publish, and (b) pods when
  no key is set or a REST call fails that round. When GraphQL goes, (a) stops and (b) needs the key.
- Not implemented (deliberately): real payment processing, email alerts, AWS/Nebius/Massed
  Compute provisioning (need IAM/partner access), workload-performance benchmarks (measured performance comes from
  completed real jobs only).
- Known limits: rate limits and login throttling are per process; rotating a provider key during
  live deployments strands them as `credentials_unavailable` (alerted, never marked terminated);
  Lambda's API exposes no termination timestamp, so billing end is an observed estimate. Full cost: Verda publishes
  no egress prices, so its totals are lower bounds. Gcore is withheld from AI input only row by row; market
  aggregates that include it are not recomputed for the assistant. Paperspace is Shadeform's resale price and Akash
  a bid summary, both labelled.

---

## 19. Methodology documents

Every number OpenGrid publishes traces to a document in `methodology/` (also served at
`/methodology` in the terminal and listed at `GET /v1/methodology`). The 49 documents:

- **Market data:** `data-kinds` · `data-quality` · `data-trust` · `data-visibility` · `freshness` · `capacity` ·
  `live-feeds` · `families` · `hardware` · `fx` · `gcp-regions`
- **Market intelligence:** `indices` · `settlement` · `historical-context` · `archived-history` · `dispersion` ·
  `provider-value` · `events` · `shifts` · `opportunities` · `reserved-prices` · `full-cost` · `rl-cost`
- **News and context:** `news` · `news-history` · `market-context` · `power-markets`
- **Reliability:** `uptime` · `probes` · `reliability`
- **Execution:** `routing` · `best-execution` · `execution-safety` · `reconciliation` · `provider-capabilities` · `adapter-contract-audit` ·
  `first-live-route` · `routing-quality` · `economics` · `billing`
- **Jobs, workloads and reservations:** `jobs` · `batches` · `workloads` · `runner` · `recommendations` · `workload-intelligence-review` · `reservations`
- **Operations:** `alerts` · `security`

Two internal-only methodology notes (`methodology/internal/equities.md`, `methodology/internal/natural-gas.md`) are
not served on the site.
