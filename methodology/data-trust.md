# Data trust: source, freshness, availability basis, confidence

Every listing can carry a **trust block** (`GET /v1/trust/listings`, and
`quality.trust.listing_trust(row)` for other modules). It is built from observable facts
only. OpenGrid does not invent a reliability score: the confidence label is a fixed rule
over the facts below, and the reasons are returned with it. The listing's own fields are
**observed**; the trust block is **inferred** by the stated rules on this page.

## Fields

| Field | Meaning |
|---|---|
| `source` | The host OpenGrid reads for this provider (`provider_meta.py`). |
| `source_type` | `direct_api` (the provider's own API, keyed or public), `public_page` (a pricing page we read), `aggregator` (another service's listing of the provider, e.g. Shadeform), `pricing_file` (a published machine-readable price file, e.g. AWS), or `unknown`. `source_detail` keeps the finer label. |
| `last_fetched` | The provider's last successful raw fetch (any endpoint). |
| `last_seen` | When this listing was last present in a fetch (`compute_listings.observed_at`, the provider fetch time). |
| `last_changed` | The listing's last recorded change in price or supply (newest `listing_observations` row). Observations are change-only, so this can be much older than `last_seen`. |
| `age_seconds` | now − `last_seen`. |
| `freshness` | `fresh`: age ≤ 1.5 × the provider's polling interval (one poll plus jitter). `aging`: older, but within `market.stale_after()` = max(2.5 × interval, 10 min). `stale`: beyond it — the market view already treats the listing as gone. |
| `availability_basis` | From `mapping.py`'s field map for `available`: `explicit` when it is copied from a provider field (RAW); `inferred` when it follows from a stated rule (DERIVED, CONSTANT — e.g. Salad's `capacity > 0`, Vast's "a listed offer is rentable"); `unknown` when the provider does not expose it (ABSENT) or this listing's `available` is null. |
| `held` | Pending quarantine holds on this listing (field, rule, since). See data-quality. |
| `flags` | Open listing-level quality incidents (e.g. `instance_price_mismatch`). |
| `provider_status` | The provider's health status (below). |
| `confidence` | `high`, `medium` or `low`, with `confidence_reasons`. |

## Confidence (exact rule)

- **low** if any of: freshness is `stale`; a pending quarantine hold on the listing; an
  open listing-level quality flag; the provider's status is `down`.
- **high** if all of: freshness `fresh`; `source_type` is `direct_api`; availability basis
  `explicit` or `inferred`; provider status `healthy`; no holds and no flags.
- **medium** otherwise (for example a public page, aggregator or pricing-file source; an
  aging listing; unknown availability; a degraded provider).

Confidence describes how directly and how recently OpenGrid observed the listing. It is
not a claim about the provider's uptime, capacity or service quality.

## Provider health

`GET /v1/ops/providers` (admin; includes error text) and `GET /v1/trust/providers`
(data:read; no error text or endpoint paths). Per provider, from `raw_snapshots`,
`compute_listings` and the quality tables:

- last successful fetch; last failure (time, endpoint, status, error — admin only);
- 24 h: fetches, failures, failure rate, latency p50 / p95 (`duration_ms` of every
  response in the window, ok or not);
- consecutive failures = failed responses recorded after the last ok one;
- listings now (seen within the stale window) and total rows; listings 24 h ago = the
  count produced by the screened poll nearest before 24 h ago (within one stale window),
  or null with "insufficient history";
- pending quarantine count; schema changes in 24 h and 7 d; open incidents by kind.

Status:

- **down**: no successful fetch ever, or none within `market.stale_after(provider)`.
- **degraded**: ≥ 2 consecutive failed responses, or > 20% of responses failed in 24 h,
  or an open `normalizer_error`, `empty_response` or `quality_layer_error` incident.
- **healthy**: otherwise.

A held price does not by itself degrade a provider; it lowers the confidence of that
listing only.
