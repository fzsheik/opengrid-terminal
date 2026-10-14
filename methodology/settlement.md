# OpenGrid settlement fixings: methodology

Settlement version **S1.0** (index methodology [1.0](/methodology/indices)). Code:
`analytics/settlement.py` (schedule, frozen recompute math, flags, ledger, verification),
`store/settlement.py` + migration `0018_settlement` (tables and the append-only trigger),
`api/settlement.py` (API). Tests: `tests/test_settlement.py`.

A **fixing** is an official, immutable value of an OpenGrid index for one point in time, fit to
be the reference price a contract or a venue could cash-settle against. OpenGrid itself builds
no derivative, trades nothing and moves no money: fixings are published reference data only.

Fixings are **observed market prices** (advertised list prices as OpenGrid normalizes them),
never quotes, never execution prices. See the gaps in §12: list prices are not transactions.

## 1. Why fixings exist next to the index

The [index](/methodology/indices) (`index_levels`) is a display series. It is recomputed after
every rollup with a 3-hour overlap, and rebuilt in full when its methodology changes. That is
right for a chart and wrong for settlement, where a price must be:

| Property | How a fixing provides it |
|---|---|
| **Fixed** | taken only after the hour can no longer be revised (§3) |
| **Immutable** | one append-only ledger row; a Postgres trigger rejects UPDATE, DELETE, TRUNCATE (§8) |
| **Reproducible** | stores every input; the value is recomputed from those inputs alone by frozen S1 math (§9) |
| **Auditable** | sha256 of the inputs, a hash chain over the whole ledger, raw evidence kept forever (§8, §10) |

## 2. Settlement indices

`SETTLEMENT_INDICES` in code: the per-GPU on-demand **benchmark** indices and the composite.

| Index | Unit |
|---|---|
| `h100-80gb-sxm5`, `h200-141gb-sxm5`, `b200-180gb-sxm`, `a100-80gb-sxm4`, `l40s-48gb` | USD / GPU-hour |
| `gpu-compute` (OpenGrid GPU Compute Index) | points (base 100) |

Spot, regional, provider-class and family indices are not settlement indices (thinner, and spot
capacity is interruptible). `GET /v1/indices/{id}/fixings` says `settlement_index: false` for them.

### 2.1 What these fixings measure, and how they differ from other GPU rental indices

An OpenGrid per-GPU fixing is the **advertised on-demand list price across all sellers OpenGrid
observes**:
- hyperscalers, neoclouds, general clouds and marketplaces;
- one vote per provider, its lowest eligible listing;
- chain-linked (methodology/indices.md).

It is **not** a measure of negotiated, reserved or actually transacted rental prices.

Other published GPU rental indices measure different quantities:
- The Silicon Data H100 and B200 Rental Indices, referenced by the CME Compute futures announced on
  2026-08-11, are described by CME as "neocloud indices measuring hourly on-demand rental GPU costs".
- The Ornn Compute Price Index (OCPI), referenced by the ICE futures announced on 2026-05-19, is described
  by ICE and Ornn as built "only from printed transactions".

The levels therefore differ, and a difference is **not** an error in either index. On 2026-10-13 the
OpenGrid H100 80GB SXM5 hourly fixing for 12:00 UTC was **$3.44 / GPU-hour**. The Silicon Data and
Ornn H100 figures shown on their public sites in the same days were about **$2.53** and **$2.65**
(as reported on those sites; OpenGrid does not reproduce or verify them).

What OpenGrid's own data says about the gap, as of 2026-10-13:
- **It is not driven by hyperscalers.** Restricted to neoclouds, the H100 SXM index was *higher*:
  $3.82 from 4 neocloud constituents (Lambda $4.19, Verda $4.09, RunPod $3.99, Massed Compute $3.14).
  The cheapest H100 supply OpenGrid sees sits on marketplaces: Vast $1.79 included, and Lium $1.30
  excluded as an outlier at that hour.
- **Coverage is the main limit.** OpenGrid currently observes only a handful of neocloud H100 sellers
  with public on-demand prices. A survey with wider neocloud coverage, or one that includes non-public
  rates, can sit well below list.
- **Coverage was widened on 2026-10-13.** Six public sources were added: CoreWeave, Together AI,
  Jarvislabs, Hot Aisle, Boost Run (via Shadeform) and Vultr. That takes the H100 SXM neocloud
  constituents from 4 to 7. The new list prices (CoreWeave $6.155, Together $3.99, Jarvislabs $3.49
  per GPU-hour) do **not** pull the list-price level down. Wider list-price coverage makes the index
  more representative of advertised prices; it does not turn it into a transaction index.
  Chain-linking means a provider joining never moves the level by itself (methodology/indices.md §4).
  Providers are added only where they publish on-demand prices readable without an account and their
  terms allow it (CAPABILITIES.md §3.1 lists the candidates that were declined).
- **List vs transacted.** Advertised list prices are an upper reference. Real rentals, especially
  multi-node and committed ones, are often agreed below list. A transaction-based OpenGrid index,
  built from reconciled routed executions, is planned once there is enough paid volume. It will
  be published *alongside* this one, never merged into it.

Anyone settling against an OpenGrid fixing should do so knowing it is a list-price benchmark.
Contract terms that reference it should say "OpenGrid advertised on-demand list price index", not
"GPU rental price".

## 3. Schedule and finality

**Hourly fixing for hour H** (fixing_time = H, the top of a UTC hour). Taken only when

    H <= min(newest market_hourly hour, newest index_levels hour, now floored to the hour) - FINALITY_LAG
    FINALITY_LAG = rollup OVERLAP (3 h) + SAFETY_MARGIN (2 h) = 5 h

Why: the rollup and the index refresh rewrite every hour at or after (their newest hour - 3 h).
Hour H is out of reach of every later incremental refresh once the frontier is at least H + 4 h.
The 2-hour margin puts the fixing one hour beyond that strict bound, absorbing a refresh that is
running while the fixing job reads, observations that are normalized late (a slow poll or
normalize cycle) and clock skew between the job and the database. The frontier is read from the
stored tables, not the wall clock alone, so a stalled rollup delays fixings instead of fixing a
half-built hour. A methodology rebuild of `index_levels` can still change *display* levels; it
never touches a fixing (§11).

**Daily fixing for UTC day D** ("daily settlement", fixing_time = D+1 00:00 UTC). Taken once the
hourly fixing of D 23:00 exists, i.e. about 05:00-05:10 UTC on D+1 with the job running every
10 minutes.

The job `index_fixings` (every 600 s, durable heartbeat `execution_controls 'job:index_fixings'`)
is idempotent: one original fixing per (index, type, time) is enforced by a unique index and by
a check under an advisory lock, so re-running it never adds a duplicate. After downtime it
catches up (at most 48 hours per run, then continues on the next run). Hours are fixed in order
and never skipped: an hour with no data gets a `not_published` row with its reason.

Settlement fixings start when the job first runs (from the start of that UTC day); OpenGrid does
not back-fill fixings for earlier history.

## 4. The values

**Hourly, per-GPU index.** The published chain-linked index level at H (methodology/indices §2-4):
one vote per provider, feed-down and outlier exclusions, median below 5 constituents else 20%
trimmed mean, chained on the constituents common with the previous published hour. The fixing
stores the inputs that determine it:

- every candidate constituent: provider, vote price, region, status (`included`,
  `excluded_feed_down`, `excluded_outlier`, `excluded_correction`), and its evidence: the listing
  observation that set the price (listing id, observed_at, price) and the ids, endpoints, fetch
  times and payload sha256 of the provider's raw snapshots that recorded it (the snapshot at the
  observation time and the provider's last successful round at or before H);
- the chain link state: the previous published hour, its level, its chain segment and its
  constituents' prices;
- the parameters (MIN_PROVIDERS 3, TRIM_FROM 5, TRIM 0.2, OUTLIER_RATIO 3, settlement minimum 4);
- the `index_levels` row it was taken from, as a cross-check.

The value is recomputed from these inputs: screening (outliers recomputed; feed-down and
corrections are input facts), statistic, then `value = link_level × stat(common now) /
stat(common then)` (or the cross-sectional value on a base/rebased hour). At creation the
recomputed value must equal the published index level; if it does not, the fixing is
`not_published` with a severe `recompute_mismatch` flag and an ops alert, never a silent value.

**Hourly, `gpu-compute`.** The published composite level: each component's level and chain
segment, weights, link state; recomputed as chained weighted relatives (indices §5). Each
included component records its own hourly fixing id and whether it matches.

**Daily: TWAP.** The arithmetic mean of the day's 24 hourly fixings (00:00 … 23:00 UTC),
using each hour's **effective** fixing (the latest restatement if any):

    eligible = hours with status final|restated, a value, no severe flag,
               in the chain segment of the day's last such hour
    daily    = (1 / n) × Σ value_h over eligible hours,  if n >= MIN_DAILY_HOURS (18 of 24)

Why a TWAP and not the last print: a single hourly print can be moved by one provider changing
one price in one hour; to move a 24-hour average by the same amount it must hold the change for
the whole day, in public, while every other constituent and the surveillance below watch. That
is the standard reason administrators average over a window (IOSCO Principle 6/7). Hours from
before a chain rebase are excluded because levels in different segments are not comparable.

## 5. Data sufficiency

Stricter than the display index:

| Rule | Display index | Settlement fixing |
|---|---|---|
| per-GPU constituents | 3 | **4** (`SETTLEMENT_MIN_CONSTITUENTS`) |
| composite components | 2 and 50% of weight | **3** and 50% of weight |
| daily eligible hours | n/a | **18 of 24** |

An hour below the settlement minimum is fixed as `not_published` with the reason
("settlement data sufficiency: 3 constituent(s) included, 4 required").

## 6. Surveillance flags

Recorded on the fixing (`flags`: code, severity, detail) and shown on the index page.

| Code | Where | Severity | Rule |
|---|---|---|---|
| `concentration` | hourly | warn ≥ 50%, severe = 100% | one constituent's share of the statistic's weight (median: the middle one or two; trimmed mean: 1/core). With 4 constituents the two middle votes carry 50% each |
| `price_jump` | hourly | warn | an included vote moved > 25% since the previous published hour |
| `new_constituent` | hourly | info | an included vote was not a constituent in the previous published hour (chain-linking keeps it from moving the level) |
| `feed_outage` | hourly | warn; severe ≥ 50% of candidates | candidate feeds failing that hour |
| `low_constituents` | hourly | severe | below the settlement minimum |
| `thin_link` | hourly | warn | the hour's move is measured on one constituent |
| `rebased` | hourly | warn | the chain was rebased this hour |
| `evidence_untraceable` | hourly | warn | no stored raw snapshot was found for an included vote |
| `component_not_fixed` | hourly (composite) | warn | an included component has no matching settlement fixing this hour |
| `recompute_mismatch` | hourly | severe | settlement recompute ≠ published index level |
| `pre_fix_jump` | daily | warn | a `price_jump` in the last 3 hourly fixings of the day |
| `pre_fix_influential_jump` | daily | **severe** | that jump came from a constituent carrying ≥ 50% of the statistic |
| `pre_fix_new_listing` | daily | warn | a constituent entered within 6 h of the fix and carries weight |
| `rebased_in_day` | daily | warn | only the last segment's hours are averaged |
| `methodology_changed_in_day` | daily | severe | the day's hourly fixings span two methodology versions |
| `severe_hours_excluded`, `feed_outage_hours`, `hours_not_fixed` | daily | info | context |

Severe hourly flags exclude that hour from the TWAP. A severe daily flag makes the daily
fixing `not_published` ("surveillance hold"). Every daily `not_published` or severe daily flag
raises an ops alert (`alerts/ops.py`, kinds `fixing_not_published`, `fixing_severe_flags`);
hourly ones alert at most once per index per UTC day.

## 7. Fallback waterfall (daily)

1. TWAP over the eligible hours if there are at least 18 of them and no severe daily flag.
2. Otherwise the daily fixing is **not published**, with the reason.

There is no step 3. A value is never carried forward from a previous day, interpolated or
estimated. A contract referencing a fixing should define its own fallback for a
`not_published` day (for example "the next published daily fixing" or "postponement").

## 8. The ledger: immutability and the hash chain

`index_fixings` columns: `seq` (ledger order), `fixing_id`, `index_id`, `fixing_type`
(hourly|daily), `fixing_time`, `status`, `value` (null unless published), `reason`,
`methodology_version`, `settlement_version`, `inputs`, `input_hash`, `flags`, `restates`,
`actor`, `prev_hash`, `row_hash`, `created_at`.

| Status | Meaning |
|---|---|
| `final` | the original official fixing, published |
| `not_published` | the original fixing slot, not publishable; `reason` says why; `value` null |
| `restated` | a correction (§9); `restates` = the original fixing id |
| `provisional` | API only: the current index level for an hour that is not final yet. Not a fixing, not in the ledger |

- `input_hash = sha256(canonical JSON of inputs)`; canonical JSON = sorted keys, no whitespace,
  UTF-8, no NaN.
- `row_hash = sha256(canonical JSON of every column except seq, including prev_hash)`;
  `prev_hash` = the previous row's `row_hash` (64 zeros for the first). Editing any row breaks
  its own hash; deleting or inserting a row breaks the next row's link.
- A Postgres trigger (`og_append_only`) rejects every UPDATE and DELETE on `index_fixings` and
  `fixing_evidence`, and TRUNCATE. The application has no update or delete path at all.
- The current chain head (`ledger_head` in `GET /v1/fixings`) can be recorded by anyone: the
  chain can only be extended, so a different head hash at a seq you recorded proves a rewrite.

## 9. Restatement policy

A published fixing is corrected only by appending a new row with status `restated`,
`restates` = the original fixing id, the corrected inputs, the reason and the actor. The original
row stays exactly as it was; the API marks it `superseded_by`, and `effective=true` history
returns the latest correction for each slot.

- **Who:** an admin only (`admin` scope: the operator, or a key the operator flagged
  `platform_admin`). `POST /v1/admin/fixings/{fixing_id}/restate` with a reason (10+ characters).
- **What can be corrected:** staff never type a value. They can (a) exclude a constituent with a
  reason (e.g. a provider confirms an erroneous list price), (b) re-include one excluded by a
  correction, or (c) re-derive the inputs from the stored data (e.g. after a rollup defect is
  fixed). The value is then recomputed from the corrected inputs by the same frozen math. A daily
  fixing is restated only by re-deriving it from the effective hourly fixings of that day.
- **Window:** hourly fixings within **24 hours** of publication; daily fixings within **2
  business days** (Mon-Fri, UTC) of publication. After that a fixing is final for good; a
  later-discovered error is documented in a notice, not by restating.
- **Scope of a correction:** a restatement corrects that one fixing. Later hourly fixings are not
  recomputed; a daily fixing that used a corrected hour is restated separately (re-derive).
- **Disclosure:** every restatement raises an ops alert (`fixing_restated`) and is listed publicly
  at `GET /v1/fixings/restatements` with the correction details.

## 10. Evidence retention

Raw provider responses are normally kept 30 days (bodies) and 90 days (fetch metadata)
(`storage.py`). Every raw snapshot referenced by a fixing is recorded in `fixing_evidence`
(append-only) and is **exempt from retention**: neither the snapshot row nor its body is ever
deleted. So every fixing stays traceable to the provider bytes it was computed from; `GET
/v1/fixings/{id}` lists each referenced snapshot and whether it (and its body) is retained.

## 11. Governance and methodology control

- **Frozen per fixing.** Each fixing stores the index `methodology_version` and the
  `settlement_version` it was taken under. The recompute math used for verification (S1) is a
  separate, frozen copy in `analytics/settlement.py`, so a later change to the live index code
  cannot change how an old fixing verifies.
- **Change notices.** A methodology change needs a new version string and a recorded notice
  (`methodology_notices`: scope, version, effective_from, notice_days, summary, actor,
  published_at; `POST /v1/admin/methodology/notices`; public at `GET /v1/fixings/methodology`).
  `effective_from` must be at least `notice_days` after publication. The initial versions (1.0,
  S1.0) were recorded automatically when fixings began.
- **Enforced.** A fixing is published only under versions that have a notice in effect at its
  fixing time; otherwise it is `not_published` ("governance: … has no recorded change notice").
- **Past fixings are never rewritten.** `index_levels` may be rebuilt for display under a new
  methodology; fixings are not touched, and the daily fixing of a day whose hours span two
  versions is held (`methodology_changed_in_day`).
- **Oversight function.** Today: the OpenGrid operator, through the admin API, every action of
  which is logged in the ledger (restatements) or the notices table. There is **no independent
  oversight committee yet** (see §12).
- **Conflicts of interest.** OpenGrid also routes compute and earns fees, so it has an interest in
  where prices are. Controls in place:
  - the index is computed mechanically from observed public list prices; no judgment, no
    submissions;
  - OpenGrid's own routing quotes, execution prices and fees never enter any index or fixing
    (the index reads only `market_hourly`, built from provider listings);
  - staff cannot edit inputs or values: the ledger is append-only at the database level and a
    correction can only exclude/re-include a recorded constituent or re-derive from stored data,
    with a reason, inside the window, publicly listed;
  - every fixing is reproducible by anyone from its published inputs and evidence.

## 12. IOSCO Principles for Financial Benchmarks (2013): self-assessment

| # | Principle | How OpenGrid meets it today | Gap |
|---|---|---|---|
| 1 | Overall responsibility of the administrator | OpenGrid administers the indices and fixings end to end (data collection, computation, publication) | no formal statement of administrator responsibility or named accountable officer |
| 2 | Oversight of third parties | no third party computes or publishes; providers are data sources only | provider feeds are not under contract; no data-provider agreements |
| 3 | Conflicts of interest | mechanical method; routing prices never enter the index; append-only ledger; public restatements (§11) | no written conflicts policy reviewed by an independent party; OpenGrid earns routing fees |
| 4 | Control framework | finality rule, sufficiency rules, surveillance flags, DB trigger, hash chain, tests | no external review of the control framework |
| 5 | Internal oversight | operator review via ops alerts; notices table | **no independent oversight committee**; no board-level function |
| 6 | Benchmark design | chain-linked, one vote per provider, outlier screening, TWAP daily (§4) | measures **advertised list prices**, not the price compute actually clears at |
| 7 | Data sufficiency | settlement minima 4 constituents / 3 components / 18 of 24 hours; otherwise not published (§5, §7) | few constituents for some GPUs; B200 often below the minimum |
| 8 | Hierarchy of data inputs | one input type: observed public list prices | IOSCO prefers **transaction data**; OpenGrid has none from the market (its own routed transactions are deliberately excluded) |
| 9 | Transparency of determinations | every fixing publishes its constituents, statuses, flags, reason and evidence | n/a |
| 10 | Periodic review | thresholds and constants are published (`/v1/fixings/methodology`) | no scheduled periodic review process yet |
| 11 | Content of the methodology | this document and methodology/indices.md, with code and tests | n/a |
| 12 | Changes to the methodology | versioned; recorded notice with notice period; enforced at fixing time (§11) | no public consultation process before a change |
| 13 | Transition | `not_published` days are explicit; contracts should define their fallback (§7) | no published cessation / transition policy |
| 14 | Submitter code of conduct | not applicable: no submissions, prices are observed | n/a |
| 15 | Internal controls over data collection | feeds fetched by OpenGrid; schema watch, quarantine, data-quality rules (methodology/data-quality.md); feed-down exclusion | list prices can be changed by a provider at will; no attestation from providers |
| 16 | Complaints procedures | see §13 | no formal complaints register yet (handled by email) |
| 17 | Audits | the ledger is independently verifiable (§14) | **no external audit** has been performed |
| 18 | Audit trail | append-only ledger, hash chain, retained raw evidence, restatement records, notices | ops logs follow normal retention |
| 19 | Cooperation with regulators | all data and evidence can be provided on request | OpenGrid is not a registered benchmark administrator (e.g. under the EU/UK BMR) |

Summary: the computation, immutability and auditability are built; the **organisational**
principles (independent oversight, external audit, registration) and the **input** principle
(transaction data) are not met. A venue or counterparty should treat fixings as transparent,
reproducible reference data with those gaps disclosed.

## 13. Complaints

Complaints or challenges to a fixing (wrong constituent, suspected manipulation, methodology
question): email OpenGrid with the fixing id. Within the correction window a substantiated
complaint is resolved by a restatement (§9), which is public; after it, by a published notice.

## 14. How to verify a fixing yourself

    # the latest fixings of every settlement index, and the ledger head
    curl -s https://<host>/v1/fixings

    # one index's daily fixings (effective = the latest correction per day)
    curl -s "https://<host>/v1/indices/h100-80gb-sxm5/fixings?type=daily&effective=true"

    # one fixing: inputs, constituents, flags, raw-snapshot evidence
    curl -s https://<host>/v1/fixings/fx-<id>

    # recompute it server-side: value, input hash, row hash, chain links, evidence
    curl -s https://<host>/v1/fixings/fx-<id>/verify

    # the whole ledger's chain, in pages (pass next_seq as from_seq)
    curl -s "https://<host>/v1/fixings/chain/verify?limit=5000"

Independently: take `data.inputs` from `/v1/fixings/{id}`, serialize it as canonical JSON (sorted
keys, separators `,` and `:`, UTF-8) and sha256 it: it must equal `input_hash`. Recompute the
value with §4 (median below 5 constituents, else drop ⌊0.2n⌋ from each end and average; chain on
the common constituents). The row hash covers every column but `seq`; each `prev_hash` equals the
previous row's `row_hash`. Reads use the same access as `/v1/indices` (public when public pages
are on; otherwise any key with `data:read`).

## 15. Known limits

- List prices, not transactions (§12, principles 6 and 8); how this differs from other GPU rental
  indices is in §2.1.
- Evidence links a vote to the provider's raw snapshots by provider, time and price; the raw
  body is the provider's whole response, not a pre-extracted listing.
- A restatement does not cascade into later hourly fixings (§9).
- The hash chain proves the ledger was not rewritten relative to a head someone recorded; it
  cannot prove that rows were never appended and then hidden *before* anyone recorded a head.
  Recording the head externally (e.g. publishing it daily) closes that gap; not automated yet.
