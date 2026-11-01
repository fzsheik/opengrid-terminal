# Freshness: when a change happened, and how late OpenGrid learned of it

Every price and stock change OpenGrid records says when OpenGrid saw it (`observed_at`, the fetch time) and, as well
as the source allows, when it actually happened: the provider's own effective time when it publishes one, and
otherwise the window between two fetches.

Kind: **observed** (fetch times and the providers' own timestamps). Code: `changed_at.py`, `normalize.py`
(`record_observations`), the provider modules in `providers/`.

## When a change happened

A history row (`listing_observations`) is written only when a fetch shows a new value. Each row carries:

| Field | Meaning |
|---|---|
| `observed_at` | The fetch that first showed the new value: when OpenGrid knew. Never replaced by a provider time. |
| `prev_observed_at` | The last fetch that still showed the old value. The change happened in (`prev_observed_at`, `observed_at`]. Empty on a listing's first row. |
| `provider_changed_at` | The provider's own effective time for the new price, when it publishes one. |
| `changed_at_basis` | How good that time is: `exact`, `date_only`, `lower_bound` (from the provider) or `poll_window` (no provider time; only the bracket is known). |

Which providers publish a time:

| Provider | Field | Basis | Meaning |
|---|---|---|---|
| AWS (spot) | `SpotPrice.timestamp` (DescribeSpotPriceHistory) | exact | When AWS set the price. |
| Nebius (spot) | `effectiveTime` (billing spot-price API) | exact | When the spot price took effect. |
| Azure | `effectiveStartDate` (Retail Prices API) | date_only | The date the retail price took effect, stored as midnight UTC. |
| GCP | `pricingInfo.effectiveTime` (Cloud Billing Catalog) | lower_bound | The price was in force by then. Google reports the time within the queried range, so the real change may be earlier. A VM price is assembled from several SKUs (GPU, cores, memory, local SSD); its time is the newest of theirs. |
| Everyone else | none | poll_window | Only the bracket. |

Rules:
- A provider time is attached only when the **price** moved, or on a listing's first row. A row written because only
  stock moved gets `poll_window`: the provider's price time says nothing about stock.
- A provider time more than 5 minutes after `observed_at` is a scheduled change, not the price in force. It is
  dropped (`poll_window`) and logged, never trusted silently.
- The best single time for a change is `provider_changed_at` when present, else `observed_at`.
- Detection lag, `observed_at - provider_changed_at`, is how late OpenGrid learned of a change. It is measured only
  for `exact` rows that have a bracket and whose provider time falls inside it.

**AWS spot is read as a change log.** Each poll asks DescribeSpotPriceHistory for the window since that region's
previous successful poll (StartTime = its EndTime, at most 24 hours back; a fresh process resumes from the last
stored poll, else looks back one hour). AWS returns the price in force at the start plus every change inside the
window, each with its exact time, so no change between polls is lost, whatever the poll interval. The listing shows
the newest price; every earlier change in the window gets its own history row with AWS's time as
`provider_changed_at`. OpenGrid learned of all of them at that fetch, so their `observed_at` is the fetch time (one
microsecond apart, in AWS's order) and they share the bracket (previous poll, this poll]. A change already recorded
(at or before the last row's own time) is never written twice.

Rows written before this field existed are filled by a backfill that re-reads the stored raw responses (no new
fetching): `python -m changed_at backfill [--provider P] [--since DATE] [--dry-run]`. It writes only empty fields, so
it can be run again safely. Raw bodies are kept 30 days and fetch records 90 days, so provider times can be
recovered for the last 30 days and brackets for the last 90; older rows get `poll_window` with no bracket.

## How often each source is read

Each provider has a **price cadence** and a **capacity cadence** (`PollingPolicy` in `providers/base.py`), because
stock moves in minutes and list prices move by announcement. The poll loop runs at the shorter of the two, or faster
for a timestamped sub-feed (AWS and Nebius spot). Each poll is told which cadences are due; a provider whose price
and capacity endpoints are separate reads each on its own clock, and one that returns both in one call reads both
on the faster one. Cadences never go faster than a documented limit allows, and the request budget spends at most
about half of it.

| Tier | Providers | Price | Capacity | Documented limit |
|---|---|---|---|---|
| fast | Lambda | 2 min | with price | 1 request/s (budget: 1 per 2 s) |
| fast | Hyperstack | 2 min | with price | 500/min per IP (budget 250/min) |
| fast | RunPod | 2 min | with price | REST v2 60/min per user (budget 30/min) |
| fast | Verda | 6 min | 2 min (with credentials) | 500/min per project, 60/min per endpoint (budget 30/min) |
| fast | Vast | 5 min (public search, ~18 calls at 1/s) | keyed census on its 16,000-row daily budget, 1-12 h apart | 1/s; 20,000 keyed rows/day |
| medium | AWS | price file hourly; spot change log every 5 min (with key) | placement scores every 2 h | EC2 Describe* bucket 100, refill 20/s per region |
| medium | Nebius | price page hourly; spot every 5 min | none | none documented (public) |
| medium | Salad, DigitalOcean, Linode | 5 min | with price | 240/min, 250/min, 200/min (budget about half) |
| medium | Akash, Cudo Compute, GPU.ai, Hugging Face, Hydra Host, Paperspace (via Shadeform) | 15 min | with price | undocumented (public, no key) |
| medium | everyone else not listed | 15 min (unchanged) | with price | undocumented: unchanged |
| slow | Azure, GCP, Oracle, IBM, OVHcloud, E2E, Mithril | 6 h | none | Azure ~10/min (budget 5/min) |
| slow | CoreWeave, Together, Arkane, VESSL, Civo, Atlantic.Net, Modal, Replicate, Baseten, Beam (pages), Vultr, Exoscale | 1 h | none | site terms |

Lium stays on its 15-minute REST poll for now; its WebSocket feed is a later step.

**Rate limits.** Every call spends a token from the provider's budget (`rate_limit`, a token bucket) and reads the
provider's rate-limit headers: `Retry-After`; `RateLimit-*`, `X-RateLimit-*` and `ratelimit-*` remaining/reset;
the structured `RateLimit: remaining=0, reset=N`; Azure's `x-ms-ratelimit-remaining-*`. When the provider asks for
a wait, calls wait inline up to 15 s; beyond that the call is not made this poll and the loop sleeps until the wait
is over. A 429 that names no wait backs off 2 s, doubling on each consecutive 429.

**Throttling is not failure.** A 429 (and a call held back for the provider's limit, and EC2's
`RequestLimitExceeded`) is stored with `throttled = true`. It counts in `throttled_24h`, never in `failures_24h`,
consecutive failures, the ops error list or the poll-failure counts, and it counts as hearing from the provider for
the silence alert: being paced is not the source going silent. The capacity stale flag (3 capacity cadences) and the
capacity-silence alert (at least 2 capacity cadences) follow each provider's capacity cadence.

## The published measure: GET /v1/freshness

Public with public pages on, like `/v1/capacity`; keys need `data:read`. One row per provider and stream (`price`,
`capacity`; a provider with no capacity data has no capacity row). Filters: `provider`, `stream`.

| Field | Meaning |
|---|---|
| `tier`, `target_interval_seconds` | The provider's polling tier and this stream's cadence (table above). |
| `sla_seconds` | 2 x the target interval: the promise is that the p95 age over 24 h stays within it. |
| `last_successful_fetch_at` (price) | The newest successful raw fetch of any of the provider's endpoints. |
| `last_refresh_at` (capacity) | The newest capacity observation stored. |
| `newest_data_at`, `age_seconds` | The newest stored observation of the stream, and its age now. |
| `age_p50_seconds_24h`, `age_p95_seconds_24h`, `samples_24h` | Data age sampled once a minute over 24 h: at each minute, the minute minus the newest data stored by then (logged after every poll in `freshness_log`). Minutes before the log starts are not sampled, so a new deployment shows fewer than 1,440 samples. |
| `status` | `meeting` (p95 within the SLA), `breaching`, or `unknown` (no samples yet). |
| `provider_timestamps` (price) | What the provider's own change times are worth: `exact`, `date_only`, `lower_bound` or `none`. |
| `detection_lag_p50_seconds_7d`, `..._p95_...`, `detection_lag_changes_7d` (price) | How late OpenGrid learned of a provider-timed change: `observed_at - provider_changed_at` over the last 7 days of `exact` rows whose provider time falls in their bracket. Null where the provider gives no exact time. |
| `throttled_24h` (price) | Answers in 24 h that were rate-limit refusals (not failures). |

The age is the age of the provider's newest stored observation, so one fast feed (AWS spot) keeps a provider's price
age low while a slower one (its hourly price file) ages; the per-listing trust block still says how old each
listing is (`/methodology/data-trust`).
