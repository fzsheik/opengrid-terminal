# Data quality: screening, quarantine, schema watch

OpenGrid would rather hold a suspicious number back than publish it. Every poll's
normalized listings pass through a screen (`quality.screen`, called from
`normalize.refresh`) **before** anything reaches current state (`compute_listings`) or
history (`listing_observations`). Raw snapshots are never modified by any of this.

## What the screen compares

Each new listing is compared with its **applied** state — the row currently in
`compute_listings` — not with the provider's last raw reading. So while a value is held,
every new reading keeps being compared with the last good one. Price rules only run when
the price differs from the applied one (or the listing is new): an unchanged value has
nothing new to hold.

## Rules

| Rule | Threshold | Action | Auto-accept when persistent |
|---|---|---|---|
| `price_jump` | new / previous per-GPU price ≥ 5× or ≤ 0.2× | hold price | only if the new price is plausible (within floor and ceiling) |
| `price_nonpositive` | per-GPU or instance price ≤ 0 | hold price | never |
| `price_below_floor` | per-GPU price below $0.50 for H100/H200/B200/B300/GB200/GB300/GH200/MI300X/MI325X/MI355X, $0.20 for A100 | hold price | never |
| `price_above_ceiling` | per-GPU price above $60/GPU-h | hold price | never |
| `gpu_count_invalid` | gpu_count missing or ≤ 0 | hold gpu_count (a new listing is withheld) | never |
| `gpu_count_changed` | gpu_count differs for the same listing_id | hold gpu_count | yes |
| `region_disappeared` | listing had a region, now has none | hold region + country | yes |
| `capacity_spike` | capacity ≥ 10× previous and ≥ +50 units (previous ≥ 1) | hold capacity | yes |
| `vram_conflict` | VRAM in the raw name differs from the canonical name's VRAM by > 10% (one unambiguous figure required) | hold the mapping: canonical name withheld (None), never guessed | never |
| `instance_price_mismatch` | instance price vs per-GPU price × gpu_count differ by > 10% | flag (incident, lowers trust) | — |
| `duplicate_explosion` | listings this poll > 3× the median of the last 10 polls and ≥ +20 | hold back NEW listing ids; existing ones update | yes |
| `listing_collapse` | listings < 20% of the norm (norm ≥ 10) | flag | — |
| `empty_response` | normalizer returned 0 listings when the norm or previous poll had ≥ 5 | incident; nothing saved; previous state kept | — |
| `normalizer_error` | the provider's normalizer raised | incident; nothing saved for that provider; other providers continue | — |
| `schema_change` | a new JSON shape for a (provider, endpoint) | incident (see below) | — |
| `quality_layer_error` | the quality layer itself failed | incident; listings pass through unscreened (fail open) | — |

The floors and ceiling are deliberately far outside any real listing (the cheapest real
H100 seen is ~$1.30/GPU-h, the dearest listing ~$18/GPU-h). A provider-level norm needs
at least 3 recorded polls; until then the count rules do not fire.

Why some rules only flag: `instance_price_mismatch` has no "last good value" — the
inconsistency is between two fields of the same reading, so holding would only blank the
price. It is recorded as an open incident and the listing's trust confidence becomes low
until the inconsistency goes away (the incident then resolves itself).

## Quarantine lifecycle

Table `quality_quarantine`: provider, listing_id (`*` for provider-level), field, rule,
previous_value, new_value, detail (all rules that fired, and the latest listing as
reported), first_seen, last_seen, seen_count, status, resolved_by, resolved_at, note.
At most one `pending` row per (provider, listing_id, field).

| Status | Meaning |
|---|---|
| `pending` | Held. The listing is saved with the field's last good value (None if it never had one). |
| `auto_accepted` | Seen in **K = 4** consecutive sightings spanning at least **max(30 min, 0.9 × 3 × polling interval)** (45 min for a 15-minute provider) and every rule on that field allows auto-accept. Applied by that same poll; `resolved_by = system:auto`. |
| `accepted` | An operator accepted it (`POST /v1/ops/quarantine/{id}/accept`). Applied immediately: compute_listings is updated and, if a tracked value moved, an observation is written at the last time the provider reported the value. |
| `rejected` | An operator rejected it. While the provider keeps sending that same value it stays held (the decision is remembered per field). |
| `superseded` | The provider went back to a normal value, or sent a different one, before anyone resolved it. Never applied. |

"Same value" allows 1% slack for prices (a marketplace median's wobble) and 25% for
listing counts. A sighting is a poll with a newer fetch time; re-normalizing the same raw
data never counts as another sighting.

Accepted and auto-accepted values are remembered too: a value an operator accepted below
the floor does not re-open on the next poll.

### Freshness while a value is held — not lying about "seen" or "gone"

- A listing whose **price** is held is still marked seen (its `observed_at` moves) for at
  most **grace = max(1 h, 5 × polling interval)** after the suspicious value first
  appeared — long enough for a persistent plausible move to auto-accept.
- After the grace window, or once the value is rejected, the listing is **withheld from the
  save**: its `observed_at` stops moving, so it ages out through `market.stale_after()`
  exactly like any listing we cannot confirm, and drops out of the market view. We never
  present an old price as current indefinitely.
- Nothing ever writes a fabricated "sold out" or "gone" row. Availability is not touched
  by a price hold. An empty or failed response saves nothing, so current listings simply
  age (their freshness label says so) rather than being marked gone.
- History (`listing_observations`) only ever contains applied values. The held period
  shows the last good price; an accepted value enters history when it was applied.

## Malformed responses

`normalize.normalize_all` catches a normalizer exception per provider (before, one bad
provider aborted the whole refresh): nothing is saved for that provider, its previous
state is kept, and a `normalizer_error` incident is recorded. Zero listings when the
provider normally has some is an `empty_response` incident; again nothing is saved. Both
resolve automatically on the next good poll.

## Source schema changes

The `quality_schema_watch` job (every 2 min) reads `raw_snapshots` newer than its cursor
(read-only) and fingerprints each ok JSON payload per (provider, endpoint):

- the shape is the set of `path:type` strings; list elements are merged (`path[]`), so an
  optional key present in any element is part of the shape;
- dicts with > 40 keys, or whose keys all look like data (digits, spaces, dots), are maps:
  keys collapse to `*` (AWS keys by instance name, Hyperstack stocks by `8x`);
- bounded: ≤ 500 elements per list and ≤ 50,000 nodes visited, ≤ 2,000 paths stored;
- a value that is only ever null is `path:null` and compatible with any type.

The first shape of an endpoint is a baseline. A shape never seen before for that endpoint
records a `schema_change` incident listing added / removed / re-typed paths (major if
anything was removed or re-typed, notable for additions only). Returning to a shape seen
before is not a new change, so an endpoint alternating between two known shapes does not
alert on every poll. On first start the watcher baselines each endpoint from its newest
snapshot and then follows new rows, rather than replaying history.

## Ops endpoints (admin scope)

`/v1/ops/summary`, `/v1/ops/providers`, `/v1/ops/incidents`, `/v1/ops/quarantine`,
`POST /v1/ops/quarantine/{id}/accept|reject`, `/v1/ops/schema-changes`, `/v1/ops/stale`,
`/v1/ops/unmapped`, `/v1/ops/jobs`. The summary also probes other domains' tables
(`routing_decisions`, `deployments`, `news_sources`, `news_fetch_log`) with `to_regclass`
and reports them as unavailable until they exist; failures there are counted from
error / status / ok columns only when such columns exist, and the rule used is returned.

## Scale notes

- `normalize._latest_raw` used to stream every ok snapshot (with payload) a provider ever
  produced on each poll — a full per-provider scan that grows forever. It now finds the
  provider's endpoints with a recursive skip-scan over `ix_raw_provider_endpoint_time`,
  each endpoint's newest ok fetch with a `LIMIT 1`, and loads only rows within 120 s of
  it. Same rows, same order (tested for equivalence). On 200k synthetic rows: ~6 s → ~9 ms.
- New indexes (migration 0005): `raw_snapshots (provider, ok, fetched_at)` for provider
  health (last ok / failure, 24 h failure rate and latency, consecutive failures) and the
  first-ok-fetch-per-provider lookups; `compute_listings (provider, observed_at)` for
  stale scans and live counts. `listing_observations` already has `(observed_at)` and
  `(provider, listing_id, observed_at)`; per-listing "last changed" uses the latter via a
  `LATERAL … LIMIT 1`.
- Provider health is ~10 indexed queries per provider and cached 30 s; at 50 providers
  that is well under a second per refresh of the cache.
- **TimescaleDB is not needed yet.** History reads longer than ~48 h go through the
  `market_hourly` rollup; `listing_observations` is change-only (a row per price or
  supply change, not per poll), so even 50 providers × thousands of listings stay in the
  tens of millions of rows per year, comfortably served by B-tree indexes. Revisit when
  raw ingest or per-poll sampling makes a single table exceed a few hundred million rows,
  or when retention needs chunk-level drops.
- **Raw payload retention.** `raw_snapshots` is the largest table (whole JSON bodies) and
  is the one that needs a policy. Options, in order of preference: (1) keep every row's
  metadata but move payloads older than N days to object storage (S3/R2) as compressed
  daily NDJSON per provider, keyed by snapshot id and sha256, leaving `payload` NULL with
  an archive pointer; (2) since `sha256` is stored, deduplicate identical bodies (many
  polls return the same body) by keeping one payload per (provider, endpoint, sha256);
  (3) declarative range partitioning of `raw_snapshots` by month so old months can be
  detached and archived whole. Re-normalizing old history then reads from the archive.
  None of these is implemented: no raw data is deleted today.
