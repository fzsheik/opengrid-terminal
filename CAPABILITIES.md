# OpenGrid Terminal — Capabilities & Features

**OpenGrid Terminal is a market-intelligence and execution terminal for GPU compute.**
It watches 32 GPU clouds, turns their prices into one trustworthy market, explains what is moving
and why, and routes a workload to the best provider through one OpenGrid API key — with every
decision audited and every dollar reconciled.

> Think: Bloomberg Terminal + CoinMarketCap + Kayak + OpenRouter + smart order routing, for GPU
> infrastructure.

This document describes everything the system does today, how it does it, and — just as
important — what it deliberately does not claim.

---

## Contents

1. [At a glance](#1-at-a-glance)
2. [Architecture](#2-architecture)
3. [Market data layer](#3-market-data-layer)
4. [Market intelligence](#4-market-intelligence) (incl. 4.2a settlement-grade fixings)
5. [News & external signals](#5-news--external-signals)
6. [Execution layer: routing & provisioning](#6-execution-layer-routing--provisioning)
   - [Training jobs (OpenGrid Jobs)](#6a-training-jobs-opengrid-jobs)
   - [Reserved compute: price-lock requests](#6b-reserved-compute-price-lock-requests-reservations-methodologyreservationsmd)
   - [Workload intelligence, CLI, MCP and assistant](#6c-workload-intelligence-cli-mcp-and-assistant-opengrid-20)
7. [Reconciliation, billing & cost accounting](#7-reconciliation-billing--cost-accounting)
8. [Routing quality, reliability & economics](#8-routing-quality-reliability--economics)
9. [Design partners, onboarding & product analytics](#9-design-partners-onboarding--product-analytics)
10. [Alerts & Discord channels](#10-alerts--discord-channels)
11. [Accounts, API keys & security](#11-accounts-api-keys--security)
12. [Web terminal (UI)](#12-web-terminal-ui)
13. [Text terminal (TUI)](#13-text-terminal-tui)
14. [Public API](#14-public-api)
15. [Operations: deployment, jobs, configuration](#15-operations-deployment-jobs-configuration)
16. [Testing](#16-testing)
17. [Data honesty principles](#17-data-honesty-principles)
18. [Current status & known limits](#18-current-status--known-limits)
19. [Methodology documents](#19-methodology-documents)

---

## 1. At a glance

| | |
|---|---|
| GPU clouds monitored | **32** (AWS, Google Cloud, Microsoft Azure, Oracle Cloud, IBM Cloud, Lambda, RunPod, CoreWeave, Together AI, Vast.ai, Salad, Hyperstack, Hyperbolic, Nebius, DigitalOcean, Vultr, Lium, Massed Compute, Verda, Voltage Park, Jarvislabs, Hot Aisle, Crusoe, Denvr, Latitude.sh, Boost Run, Horizon Compute, Phyntec, Scaleway, Akamai/Linode, OVHcloud, Koyeb) |
| Canonical GPU models | **91**, variants never merged (H100 SXM ≠ H100 PCIe ≠ H100 NVL) |
| OpenGrid indices defined | **251** (per-GPU, family composites, overall compute index, regional, spot, provider-class) |
| News & signal sources | **44** verified feeds (hyperscalers, neoclouds, NVIDIA/AMD, US Federal Register, trade press) |
| API operations | **203** (188 under `/v1`, 61 of them admin-only), plus 3 runner-callback routes outside the public schema |
| Web pages | **37** routes: five-item navigation (GPUs, Find Compute, My Jobs, Markets, Developers), light/dark themes, keyboard command bar |
| Database tables | **71** (Postgres, migrated by Alembic, 20 migrations) |
| Background jobs | **17** (polling, rollups, indices, settlement fixings, events, news, reconciliation, tracking, training-job sync, reservation expiry, alerts, feeds…) |
| Provisioning adapters | **9 providers** (Lambda, RunPod, Hyperstack, DigitalOcean, Crusoe/Denvr/Latitude via Shadeform, Vast, Verda) |
| Discord channels | **4** (ops, deployments, market, news) with OpenGrid-branded embeds |
| Training jobs | **1** allowlisted template (PyTorch MNIST) end to end: preview → approval → GPU → model artifacts → automatic termination |
| Settlement fixings | Hourly + daily TWAP fixings for **6** settlement indices, immutable, hash-chained, publicly verifiable |
| Workload intelligence | GPU memory estimator, project-aware recommendations (`POST /v1/recommend`), local project analyzer, `opengrid` CLI, MCP server (11 tools), grounded assistant |
| Automated tests | **58** Python test files + **9** JavaScript test files + **4** CLI/MCP test files: unit, integration, end-to-end, concurrency, chaos, money-loss invariants, red team |

---

## 2. Architecture

```
                    ┌──────────────────────── 32 GPU clouds ───────────────────────┐
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

   index_levels ─► index_fixings (final hours only) ─► hash-chained ledger ─► /v1/fixings + verify
```

**Stack:** Python 3.13 · FastAPI · SQLAlchemy 2 · Alembic · PostgreSQL · httpx · Textual (TUI) ·
vanilla JavaScript (no build step) · deployed on Railway as a single web process that also runs
the poller and background jobs.

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
Every provider polls on its own schedule (15 minutes for most, hourly for AWS); a slow or failing
provider never holds up the others.

| Provider | Class | How OpenGrid reads prices |
|---|---|---|
| AWS | hyperscaler | public pricing file |
| Google Cloud | hyperscaler | public page (us-central1; attachable T4/V100 are GPU-only and kept out of market statistics) |
| Microsoft Azure | hyperscaler | public API (Retail Prices, eastus + eastus2) |
| Oracle Cloud | hyperscaler | public pricing file (global list prices) |
| IBM Cloud | general cloud | public API (Global Catalog; on-demand and spot) |
| Lambda | neocloud | authenticated API |
| RunPod | neocloud | authenticated API |
| Hyperstack | neocloud | authenticated API |
| DigitalOcean | general cloud | authenticated API |
| Salad | decentralized | authenticated API |
| Vast.ai | marketplace | public API |
| Hyperbolic | marketplace | public API |
| Lium | decentralized | public API |
| Verda (ex-DataCrunch) | neocloud | public API |
| Voltage Park | neocloud | public API |
| Nebius | neocloud | public page |
| Massed Compute | neocloud | public page |
| CoreWeave | neocloud | public page (on-demand and spot kept separate) |
| Together AI (GPU Clusters) | neocloud | public page (per GPU-hour; preemptible = spot) |
| Jarvislabs | neocloud | public page |
| Hot Aisle (AMD MI300X) | neocloud | public page |
| Vultr | general cloud | public API |
| Akamai/Linode | general cloud | public API |
| OVHcloud | general cloud | public API (the two USD catalogues, US and World) |
| Koyeb | general cloud | public API (serverless GPU instances) |
| Scaleway | general cloud | aggregator (via Shadeform; its own catalogue is EUR-only) |
| Crusoe · Denvr · Latitude.sh · Boost Run · Horizon Compute · Phyntec | neocloud | aggregator (via Shadeform) |

Candidates checked and **not** added (2026-10-13): Paperspace (its own page contradicts the aggregator),
Thunder Compute (terms forbid automated access), TensorWave ("starting at" floors only), Fluidstack,
Nscale and Sesterce (no public prices), Genesis Cloud (unreachable). Also declined: Alibaba Cloud (price API needs an
AccessKey; robots.txt disallows the page's data files), TensorDock (key-only API, "from" floors), Cudo Compute (public
on-demand closed 2026-03-31), SF Compute (login; robots disallows /api), Akash (estimates from stale bids, variants mixed),
io.net and Oblivus (terms forbid scraping), Prime Intellect (auth; an aggregator), Clore.ai (key; per day), Nosana
(token-priced), Exoscale (no GPU model in the keyless feed), Civo and Gcore (key required), Seeweb (EUR only), and
Shadeform's Excess Supply and IMWT (unidentifiable or reselling other clouds). A provider is added only when it
publishes on-demand prices that can be read without an account and its terms allow it.

### 3.2 Normalization
- Every listing becomes a standard `ComputeListing`: provider, SKU, GPU, count, region, country,
  price per GPU-hour and per instance-hour, market type (on-demand/spot), tier, interruptibility,
  availability, capacity, vCPU, RAM, storage, observed time.
- **Canonical GPU naming** (`canonical.py`) is hand-maintained: 91 models, named
  `<VENDOR> <MODEL> <VRAM> [<VARIANT>]`. Unknown names go to an **unmapped queue** instead of being
  guessed. Materially different variants are never merged.
- **GPU families** (`families.py`): H100, H200, B200, A100, Blackwell, Hopper… group variants for
  browsing (`/gpu/h100`) but are never priced as one product.
- **Field provenance** (`mapping.py`): for every provider, how each field is obtained (raw,
  derived, constant, lookup, absent) and the provider's quirks.
- **Regions** (`regions.py`): provider region strings mapped deterministically to US, Canada,
  Europe, UK, APAC, Middle East, LATAM, Africa — unknown stays unknown.

### 3.3 Raw storage & retention
- Every fetch is recorded (`raw_snapshots`: time, provider, endpoint, request, status, latency,
  error, body hash). Identical response bodies are stored **once** (`raw_payloads`, SHA-256).
- Retention (configurable): raw response bodies **30 days**, fetch metadata and news fetch logs
  **90 days**. The latest complete fetch round per endpoint is always kept.
- Normalized listings, price/supply history, rollups, indices, events, articles and all
  billing/audit records are **never trimmed**.

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

---

## 4. Market intelligence

### 4.1 Hourly rollup
`market_hourly` samples every listing's state at the top of each hour with the exact market rules
(on-demand, not interruptible, not floor prices, not sold out, not stale), per GPU × provider ×
region. Everything historical reads from it, so 90-day statistics don't replay raw history.

### 4.2 OpenGrid indices (251 defined)
- **Per-GPU indices** (e.g. *OpenGrid H100 80GB SXM5 Index*) in $/GPU-hour.
- **Family composites** (H100 / H200 / A100 class) and the **OpenGrid GPU Compute Index**, in
  base-100 points with fixed, documented weights.
- **Regional**, **spot** (interruptible, labelled) and **provider-class** (neocloud, hyperscaler,
  marketplace…) indices.
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

### 4.4 Market structure
- **Dispersion** per GPU: low / median / high, absolute and % spread, IQR, standard deviation,
  coefficient of variation, providers, available listings.
- **Market Efficiency / Fragmentation score** (0–100) with transparent components.
- **Provider relative value**: average premium/discount to market, by GPU, % of time cheapest,
  % of time top-3, rank distribution, availability frequency, listing lifetime, price volatility,
  GPU coverage, regional coverage, feed health — each gated by minimum sample sizes. Generated
  facts like *"RunPod has been the cheapest H100 provider 37% of the last 30 days"*.
- **Hardware metadata** for all 91 GPUs (architecture, VRAM, memory bandwidth, dense
  FP16/BF16/FP8, interconnect, form factor, TDP, workload class, sources).
- **Price per capability** (labelled theoretical): $/GB-VRAM-hour, $/PFLOP-hour (BF16, FP8),
  $/TB/s-hour.
- **Alternatives**: similar VRAM, same architecture, training/inference-oriented, similar
  price-performance — never called "equivalent".
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
| `provider_added_gpu` / `provider_removed_gpu` | real catalogue changes — never coverage artefacts |
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
  cheapest provider — each with a score, explanation and observed/inferred label.

---

## 5. News & external signals

- **44 verified sources** via RSS/Atom/JSON APIs (no scraping where a feed exists): AWS, Google
  Cloud, Azure, Meta, Lambda, CoreWeave, RunPod, Together, Voltage Park, Hyperstack, DigitalOcean,
  Vultr, Scaleway, NVIDIA, AMD, the **US Federal Register** (BIS export controls, FERC data-center
  rules), DatacenterDynamics, The Register, HPCwire, Tom's Hardware, SemiAnalysis, The
  Information, and more. Sources that couldn't be verified are listed as disabled with the reason.
- **Raw first** (re-classifiable), conditional GET (ETag / Last-Modified), per-source health.
- **Deduplication**: canonical URLs, plus story clustering across sources (one story, N sources).
- **Classification** (deterministic, re-runnable): providers (with aliases), GPUs ("H100" → the
  family; "H100 SXM" → that variant), regions, topics (price change, capacity, outage, GPU launch,
  funding, capex, export controls, power, contracts, futures, neocloud expansion…).
- **Relevance score** 0–100 with stored components (source tier, entities, topics, recency).
- Feeds that stamp every item with one date are detected; those dates are never treated as
  publish times.
- **Timeline**: price moves, availability changes, market events and news on one axis, and
  *"Related news and events around this move (not necessarily causal)"* — OpenGrid never claims
  news caused a move.

---

## 6. Execution layer: routing & provisioning

The goal: *"I need 8 H100s in the US under $2.50/hr"* → OpenGrid understands the market, ranks
providers, explains the choice, quotes, gets approval, provisions, tracks, terminates and
reconciles.

### 6.1 Execution control plane
- **Modes**: `DISABLED` · `PREVIEW_ONLY` (default) · `SUPERVISED` (every launch approved by an
  admin) · `LIVE` (validated, live-enabled providers launch within cost guards).
- **Env ceiling**: unless `ROUTING_LIVE_PROVISIONING=true`, the effective mode can never exceed
  PREVIEW_ONLY — whatever the database says.
- **Per-provider flags**: adapter status (simulated → validated only by a complete, evidenced
  validation cycle), supervised-enabled, live-enabled.
- **Kill switches**: global *STOP ALL LIVE PROVISIONING* and per-provider kill/unkill — they stop
  new launches without stopping monitoring, reconciliation or termination. Every change needs a
  reason and is logged.

### 6.2 Provider capability registry
| Provider | API supports | OpenGrid implements | Status |
|---|---|---|---|
| Lambda | full lifecycle (no stop) | L3 | simulated → **first validation target** |
| RunPod | full lifecycle | L3 | simulated |
| Hyperstack | full lifecycle | L3 | simulated |
| DigitalOcean | full lifecycle | L3 | simulated |
| Crusoe · Denvr · Latitude (Shadeform) | full lifecycle | L3 | simulated |
| Vast.ai | full lifecycle | L3 | simulated |
| Verda | full lifecycle | L3 | simulated |
| AWS · Nebius · Massed Compute · Voltage Park · Hyperbolic · Lium · Salad | varies | L0 (market data) | no adapter |

Each adapter declares a capability matrix with evidence: quote, live availability, launch, SSH key
injection, startup script, status, stop, terminate, region selection, GPU-count selection, price
known before launch, billing unit, minimum commitment, interruptible, name/tag at launch, list
instances, idempotency token, stopped-time billing, forced account SSH keys, and risks.

### 6.3 Best execution scoring
Transparent weighted scoring — never just "cheapest wins". Factors (each 0–1, with raw value and
data kind): price vs market median, availability now, region match, data freshness, availability
persistence, price stability, integration level. **Reliability and performance appear with weight
0 and "no data — not used"** until real execution data exists.

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
  that may inject account keys (RunPod, Vast, unknown) require an explicit admin override. The
  approval screen shows *"Customer supplied key: SHA256:… · OpenGrid operator access: NONE"*.
- **Pinned credentials**: each deployment is bound to the credential (and its fingerprint) that
  launched it; a different key can never manage — or "confirm termination" of — the instance.
  Customer (BYO) credentials fail closed if they can't be decrypted.

### 6.6 Provider validation
A provider moves from *simulated* to *validated* only through a real, supervised validation run:
1 GPU, ≤ $3/hour, ≤ 30 minutes, one at a time, with every precondition checked (kill-switch drills
in the last 7 days, healthy reconciliation and tracker jobs, a delivered ops test alert). The full
cycle — launch, observed running, found by name, terminated with two-signal confirmation, cost
reconciled — must have evidence before the provider is marked validated.

## 6a. Training jobs (OpenGrid Jobs)

One real workload end to end: **"Train Your First AI Model — PyTorch MNIST"** (allowlisted template
`pytorch-mnist`; no arbitrary commands, code or dataset URLs). Runbook: `docs/FIRST_TRAINING_JOB.md`.

- **Workload-aware selection** (`workloads/`): the template declares requirements (NVIDIA CUDA, ≥ 8 GB VRAM,
  sm 7.0–9.0 for the pinned torch 2.4.1 / CUDA 12.1 build, 1 GPU, on-demand only). `gpu: auto` ranks every
  eligible GPU with the existing scorer and shows two views: **market best** (best observed listing) and
  **execution-ready** (what OpenGrid can really run now), with alternatives, exclusions and exact blockers.
- **Jobs** (`jobs/`, `api/jobs.py`, migration `0017_jobs`): a job is the workload and its outcome; the deployment
  is the cloud resource. Submit (Idempotency-Key) reuses the existing route → quote → **supervised approval**
  (`POST /v1/route/{rr}/approve`) → launch → terminate → reconcile path. An explicit job state machine
  (created … training … reconciled, plus every failure / uncertain state) with outcome and resource
  disposition kept separate; `job_sync` every 30 s rebuilds state after restarts.
- **Runner** (`runner/`): a cloud-init bootstrap (≈ 197 KB) installs hash-locked torch 2.4.1+cu121, checks
  nvidia-smi and CUDA, trains a small CNN, uploads `model.pt`, `config.json`, `metrics.json`, `env.json`,
  `manifest.json` (sha256-verified) and calls back with a per-job HMAC-derived token (never stored).
  OpenGrid, not the instance, terminates the GPU.
- **Results**: website `/jobs`, `/jobs/new`, `/jobs/{id}` (lifecycle + deployment strips, live loss / accuracy,
  logs, 12 evidence-backed verification checks, artifacts download, four cost numbers); API logs / metrics /
  artifacts; Discord `#opengrid-deployments`. Artifacts live in Postgres (optional S3 backend).
- **Sandbox**: `sandbox: true` runs a fully SIMULATED lifecycle (no provider, no spend), labelled everywhere.
- **Status**: built and tested against a fake Lambda API and a real local GPU; **no real Lambda job has run
  yet**. A real job needs the Lambda validation run, then `supervised_enabled` on Lambda.

## 6b. Reserved compute: price-lock requests (`reservations/`, methodology/reservations.md)

Customers ask for a locked price ("8× H100 SXM5, 5,376 GPU-hours, starting Oct 20–27, ≤ $2.25/GPU-h") at
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
  written) → tiers (lower cost / balanced / higher performance by theoretical specs) and three picks
  (cheapest advertised, best for the workload, best OpenGrid can run). Runtime is never invented: cost is
  scenarios with stated assumptions and listed unknown costs. "Executable" needs the readiness checks and a
  supported workload (today only the MNIST template). `performance_observations` table (0020) is ready for
  measured benchmarks; no data yet.
- **Local analyzer** (`cli/src/opengrid_cli/analyze`): allowlisted files only, `.gitignore`, symlink and size
  limits, never reads secrets/weights/datasets, never executes code, redacts secret-looking values, flags
  prompt-injection text; only the structured requirements ever leave the machine. Dry-run mode.
- **CLI** (`cli/`, `uv tool install ./cli`): doctor, gpu search/show/compare/market, providers, analyze,
  estimate, recommend, route preview, jobs list/status/logs/cancel, demo train (sandbox only), mcp; `--json`.
  No command can provision.
- **MCP server** (`opengrid mcp`, stdio; optional loopback HTTP): 11 tools, read-only except a non-provisioning
  route preview and a confirm-gated job cancel. Verified with MCP Inspector 2.10.1 (`--strict`), Claude Code
  and Codex registration.
- **Assistant** (`assistant/`, `POST /v1/assistant`, `/assistant`, panel on Find Compute): deterministic intents
  by default; optional vendor-neutral LLM (off unless configured) whose replies are post-checked so every
  number comes from tool results; no mutation tools.

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
  (buyer fee %, flat per GPU-hour, spread, subscription, data API — nothing hardcoded), credits,
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
  latency p50/p95, unexpected terminations, API error rate, termination success, quote accuracy —
  **with sample sizes; no score below 10 launches**. Validation runs are reported separately.
- **Economic value**: per deployment market median vs selected price vs savings — only when a
  valid comparison exists (same GPU, on-demand, ≥ 3 providers); totals by GPU, provider and
  strategy.

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
| `#opengrid-ops` | `OPS_ALERT_WEBHOOK_URL` | **Urgent, money at risk**: orphans, termination failures, past-deadline deployments, unknown states > 10 min, provider API down with active deployments, overspend > 20%, active resources without billing, unowned provider resources, kill switches |
| `#opengrid-deployments` | `DEPLOYMENTS_WEBHOOK_URL` | Route awaiting approval, approved, launching, running, terminating, terminated, failures |
| `#opengrid-market` | `MARKET_WEBHOOK_URL` | Notable and major market events |
| `#opengrid-news` | `NEWS_WEBHOOK_URL` | Stories with relevance ≥ 50, one post per story |

- Rich **Discord embeds** in OpenGrid green (`#91C61D`) with the OpenGrid logo, structured fields,
  links back to the terminal and timestamps; @-mentions disabled. Slack webhooks receive plain text.
- Cost-exposure alerts carry deployment, provider, account, **estimated $/hour exposure**, time in
  state and a suggested action; they re-escalate every 30 minutes and resolve only on evidence.
- Feeds post each item exactly once (even across restarts), never flood history into a new
  channel, and retry failed sends in order.
- All webhook delivery is SSRF-safe (single DNS resolution, pinned IP, private ranges blocked, no
  redirects) and HMAC-signed.

### 10.2 Watchlists & user alerts
Watchlists of GPUs, providers, GPU+provider, regions and indices with live values. Alert rules such
as *H100 below $2/hr*, *B200 becomes available*, *Lambda H100 drops 10%*, *US availability falls
20%*, *H100 Index hits a new 30-day low* — edge-triggered with cooldowns, never firing on unknown
data. Delivery: in-app feed and signed webhooks (email not implemented).

---

## 11. Accounts, API keys & security

- **One OpenGrid account, one API key**: `opg_live_…` keys (256-bit), stored only as an HMAC with a
  server-side pepper; shown once; scoped (`data:read`, `route:preview`, `route:execute`,
  `deployments:read/write`, `watchlists`, `billing:read`, `keys:read`, `account:manage`, `admin`);
  revocable with cascade to child keys; expiry; last-used tracking; usage logging.
- **Rate limits** per key **and** per account by request class (read / write / execute);
  shutdowns (terminate/stop) are never throttled like launches.
- **BYO provider credentials** encrypted at rest (Fernet / MultiFernet with key rotation), never
  returned by any API, never usable by another tenant.
- **Security controls** (independently re-audited by attack):
  CSRF protection on every state-changing request · safe URL schemes everywhere links are rendered
  · CSP limited to OpenGrid's own scripts · X-Frame-Options / nosniff / Referrer-Policy · login
  throttling · DNS-rebinding protection · SSRF-safe webhooks · production detection that can't be
  spoofed · secrets redacted from all logs · admin API requires platform-admin authority · tenant
  isolation on every resource.
- **Public pages** (optional, `PUBLIC_PAGES=true`) expose only read-only market data; anything that
  spends money or reads private data still requires a key.

---

## 12. Web terminal (UI)

A dense, dark, data-first market terminal — no hero sections, no decorative dashboards.

| Area | Pages |
|---|---|
| Market | Overview · Indices · Index detail · GPUs · GPU detail · GPU family · Providers · Provider detail · Compare · Explorer · Heatmaps |
| Signals | Opportunities · Events · News (feed + timeline) |
| Execute | Jobs · New training job · Job detail · Reserve (price-lock requests) · Request detail · Route (order ticket, approval ticket) · Deployments · Deployment detail · Onboarding · Watchlists · API keys |
| Reference | API docs (with try-it) · Methodology · Ops (data quality) |
| Admin | Execution control (modes, kill switches, flags, orphans, resources, validation) · First-route checklist · Partners · Value (economics, quality, reliability, funnel) |

- **GPU pages** feel like stock pages: index level, low/median/high, spread, providers,
  availability, changes, volatility, percentiles, robust price charts with event/news markers,
  availability history, provider ranking, regional pricing, best available now, dispersion,
  price distribution, price per capability, hardware specs, alternatives, events, news, listings.
- **Keyboard command bar**: `/` to search, `g h100` open a market, `p lambda` provider,
  `c h100 h200` compare, `r h100 8 us` route preview, `w h100` watch, `j` jobs, `j new` new training
  job, `e` events, `n` news, `o` opportunities, `x` explorer, `?` help.
- **Index detail pages** for the settlement indices carry a **Settlement fixings** panel (latest daily TWAP
  and hourly fixings in UTC, status, surveillance flags, evidence, a Verify button).
- Shareable URLs for every view, CSV export on every table, honest "insufficient data" states,
  data-kind badges (observed / inferred / estimated / transaction) and freshness badges.
- **SEO** renderers for GPU, family, provider and compare pages with real numbers, structured data
  and a sitemap (active when `PUBLIC_PAGES` is on).

---

## 13. Text terminal (TUI)

`python terminal.py` — a Textual app with a command bar:
`listings`, `avail`, `history`, `mapping`, `indices`, `idx <id>`, `ctx <gpu>`, `mkt <gpu>`,
`best <gpu> [count] [region] [mode]`, `preview <gpu> <count> [region] [max]` (never provisions),
`ev`, `opp`, `news`, `prov <name>`, `ops`, `caps`, `fam <family>`, `help`.

---

## 14. Public API

All `/v1` responses use one envelope: `{"data": …, "meta": {"as_of", "kind", "methodology", …}}`.
Authentication: `Authorization: Bearer opg_…` (or the operator session). Highlights:

| Domain | Endpoints |
|---|---|
| GPUs & markets | `/v1/gpus`, `/v1/gpus/{gpu}`, `/v1/markets/{gpu}`, `/v1/markets/{gpu}/regions`, `/v1/markets/{gpu}/context`, `/v1/families`, `/v1/hardware`, `/v1/regions`, `/v1/compare` |
| History & indices | `/v1/history/{gpu}`, `/v1/indices`, `/v1/indices/{id}`, `/v1/indices/{id}/history`, `/v1/indices/{id}/fixings` |
| Settlement fixings | `/v1/fixings`, `/v1/fixings/{id}`, `…/verify`, `/v1/fixings/chain/verify`, `/v1/fixings/restatements`, `/v1/fixings/methodology` |
| Structure | `/v1/spreads`, `/v1/heatmaps/{kind}`, `/v1/providers`, `/v1/providers/{p}` |
| Signals | `/v1/overview`, `/v1/events`, `/v1/opportunities`, `/v1/tape`, `/v1/news`, `/v1/timeline` |
| Trust | `/v1/trust/listings`, `/v1/trust/providers`, `/v1/capabilities` |
| Routing | `/v1/best/{gpu}`, `/v1/route/preview`, `/v1/route`, `/v1/route/{id}`, `/v1/quotes/{id}` |
| Deployments | `/v1/deployments`, `/v1/deployments/{id}`, `…/terminate`, `…/stop`, `…/feedback` |
| Training jobs | `/v1/jobs/templates`, `/v1/jobs/readiness`, `/v1/jobs/preview`, `/v1/jobs`, `/v1/jobs/{id}`, `…/logs`, `…/metrics`, `…/artifacts`, `…/artifacts/{name}`, `…/cancel`; runner callbacks (job token only) |
| Reservations | `/v1/reservations/reference`, `/v1/reservations/requests`, `/v1/reservations`, `/v1/reservations/{id}`, `…/accept`, `…/withdraw` |
| Accounts & billing | `/v1/me`, `/v1/keys`, `/v1/scopes`, `/v1/usage`, `/v1/credentials`, `/v1/billing/*` |
| Watchlists | `/v1/watchlists`, `/v1/alerts`, `/v1/alerts/firings` |
| Analytics | `/v1/quality`, `/v1/reliability`, `/v1/economics`, `/v1/onboarding`, `/v1/partners/me` |
| Admin (61) | execution control, kill switches, validation, reconciliation, orphans, resources, channels, ops, metrics, traces, checklist, partners, funnel, accounts, fee policies, reservation offers, fixing restatements, methodology change notices |

Full interactive reference: `/api` in the terminal, or `/docs` (OpenAPI).

---

## 15. Operations: deployment, jobs, configuration

### 15.1 Deployment
Railway, one service + Postgres, built from `pyproject.toml` / `uv.lock`, started by
`railway.toml` with **exactly one worker** (the poller lives in the web process). Migrations run
automatically at startup. A push to GitHub does not trigger a Railway build on its own: deploy with
`railway up --service opengrid-terminal --detach`. The app **refuses to start in production** without `APP_PASSWORD` and
`API_KEY_PEPPER`.

### 15.2 Background jobs
| Job | Every | Purpose |
|---|---|---|
| provider pollers | 15 min (AWS 1h) | fetch, store raw, normalize, quality-screen |
| `market_hourly` | 10 min | hourly rollup → indices, provider value, events hooks |
| `index_fixings` | 10 min | settlement fixings for hours that can no longer be revised; daily TWAP |
| `market_events` | 15 min | event detection |
| `news` | 1 min | poll due news sources |
| `reconcile` | 2 min | provider vs OpenGrid state, orphans, deadlines |
| `routing_tracker` | 1 min | deployment status, uptime, usage metering |
| `job_sync` | 30 s | training jobs: deployment state + runner callbacks → job state; terminate on completion, failure, timeout |
| `reservation_expiry` | 5 min | expire stale price-lock offers and requests |
| `channel_feeds` | 1 min | Discord deployments / market / news posts |
| `alerts` | 3 min | watchlist alert evaluation |
| `quality_schema_watch` | 2 min | provider response schema changes |
| `raw_storage` | 1 min | raw payload deduplication and retention |
| `route_outcomes`, `product_events` | 5 min | routing quality and product analytics |
| `db_latency` | 1 min | database latency metric |
| `api_usage_flush`, `api_usage_retention` | 10 s / daily | API usage logging |

### 15.3 Key configuration
| Variable | Purpose |
|---|---|
| `DATABASE_URL`, `APP_USER`, `APP_PASSWORD` | database and site login |
| `API_KEY_PEPPER`, `CREDENTIALS_ENCRYPTION_KEY` | key hashing and credential encryption — never change once set |
| `ENVIRONMENT=production`, `TRUST_PROXY_HEADERS=true` | production behaviour behind Railway's proxy |
| `SALAD_*`, `HYPERSTACK_API_KEY`, `LAMBDA_API_KEY`, `RUNPOD_API_KEY`, `DIGITALOCEAN_API_KEY` | provider market data (and routing where supported) |
| `SHADEFORM_API_KEY`, `VAST_API_KEY`, `VERDA_CLIENT_ID/SECRET` | optional additional routing credentials |
| `ROUTING_LIVE_PROVISIONING` | env ceiling for real launches (default off) |
| `ROUTING_LAUNCH_DEFAULTS` | per-provider launch defaults, e.g. the validation SSH key name |
| `OPS_ALERT_WEBHOOK_URL`, `OPS_ALERT_WEBHOOK_SECRET` | ops alerts (+ signing secret used by all channels) |
| `DEPLOYMENTS_WEBHOOK_URL`, `MARKET_WEBHOOK_URL`, `NEWS_WEBHOOK_URL` | channel feeds |
| `PUBLIC_PAGES` | open read-only market pages to anonymous visitors |
| `RAW_PAYLOAD_RETENTION_DAYS`, `RAW_SNAPSHOT_RETENTION_DAYS` | raw storage retention (payloads referenced by settlement fixings are always kept) |
| `JOB_CALLBACK_BASE_URL` | where GPU runners call back (default `PUBLIC_BASE_URL`; https required) |
| `JOB_ARTIFACT_STORE`, `JOB_ARTIFACT_MAX_MB`, `JOB_ARTIFACT_S3_*` | training artifacts: Postgres (default) or S3-compatible |
| `JOB_SYNC_INTERVAL_SECONDS`, `JOB_BOOTSTRAP_TIMEOUT_MINUTES`, `JOB_TOKEN_GRACE_MINUTES` | training-job timing |

`deploy/make_env.py` builds the Railway variables file and generates the secrets.

---

## 16. Testing

55 Python test files and 6 JavaScript test files (plain scripts, scratch Postgres databases, no real
provider calls):
- unit and integration tests for every analytics module, API and page;
- **routing benchmark**: 50 deterministic decision scenarios;
- **chaos suite**: 22 failure scenarios (timeouts, 5xx, crashes mid-launch, restarts, expired
  credentials, concurrent workers…);
- **money-loss invariants**: 28 scenarios proving OpenGrid never loses track of a resource that
  can cost money — every case ends either proven terminated and billed, or flagged as possibly
  billing with escalating alerts;
- **concurrency proofs**: simultaneous approvals across threads and separate processes;
- **security re-audit** and **red-team** regression suites;
- **training jobs**: GPU selection, the job state machine, API, security (token, quotas, injection), the
  runner and bootstrap, and an **end-to-end simulation** (real reporter + real CPU training over HTTP
  against a fake Lambda);
- **settlement fixings**: finality, TWAP, hash-chain tamper detection, the immutability trigger,
  restatements, evidence retention;
- **reservations**: state machine, supply-basis enforcement, tenant isolation, nothing launched or billed;
- **provider page parsers**: the newer scraped price pages parsed from saved real copies (CoreWeave, Together AI,
  Jarvislabs, Hot Aisle, Boost Run, Vultr, Nebius), plus one parser test file each for Google Cloud, Azure,
  Oracle, IBM, the general clouds (Linode, OVHcloud, Scaleway, Koyeb) and Shadeform's Horizon / Phyntec;
- JavaScript tests for the terminal's core, charts and execution UI.

---

## 17. Data honesty principles

1. Never fabricate market data — no number without coverage; "insufficient data + reason" instead.
2. Never guess GPU mappings; never merge materially different variants.
3. Label every number: **observed**, **inferred**, **estimated** or **transaction**.
4. Never mix list price, observed market price, quote and execution price.
5. Never claim provisioning support, a completed route, or reliability that isn't real.
6. Never claim news caused a market move.
7. Every published statistic links to its methodology.

---

## 18. Current status & known limits

- **Market intelligence**: live in production; window statistics (7d/30d/90d) fill in as history
  accumulates.
- **Execution**: built, hardened, and independently reviewed; **all adapters are still
  simulated**. Training jobs (§6a) are built and tested but have not run on a real GPU provider yet. The next step is one supervised **Lambda validation run** (≈ $1 realistic, ≤ $1.85
  worst case with the operator abort rule). Customer routes follow only after validation.
- **Settlement fixings** (§4.2a) and **price-lock requests** (§6b): built and tested; fixing history starts at
  deployment. **No futures, swaps or trading of any kind**: see docs/FUTURES_REGULATORY_PATH.md for the regulated
  paths (index licensing to an exchange is the recommended one).
- Not implemented (deliberately): real payment processing, email alerts, AWS/Nebius/Massed
  Compute provisioning (need IAM/partner access), workload-performance benchmarks.
- Known limits: rate limits and login throttling are per process; rotating a provider key during
  live deployments strands them as `credentials_unavailable` (alerted, never marked terminated);
  Lambda's API exposes no termination timestamp, so billing end is an observed estimate.

---

## 19. Methodology documents

Every number OpenGrid publishes traces to a document in `methodology/` (also served at
`/methodology` in the terminal):

`data-kinds` · `indices` · `historical-context` · `dispersion` · `provider-value` · `hardware` ·
`families` · `events` · `opportunities` · `news` · `data-quality` · `data-trust` · `routing` ·
`best-execution` · `execution-safety` · `reconciliation` · `provider-capabilities` ·
`first-live-route` · `routing-quality` · `reliability` · `economics` · `billing` · `alerts` ·
`security` · `jobs` · `workloads` · `runner` · `settlement` · `reservations`
