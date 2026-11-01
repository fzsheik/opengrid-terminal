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
