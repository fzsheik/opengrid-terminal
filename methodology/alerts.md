# Watchlists and alerts

## Watchlists

Named sets of things an account follows. Item kinds: `gpu`, `provider`, `gpu_provider`, `region`
(a region group from `regions.REGION_GROUPS`), `index` (an `analytics.indices` id). Reading a
watchlist attaches the current **observed** lowest price for GPU items, computed with the market
rules below.

## Rules

A rule is `params` (a metric, its subject and a condition `op` / `value`), channels and a cooldown.

| metric | params | value |
|---|---|---|
| `market_low` | gpu | lowest observed on-demand price now, USD/GPU-hour |
| `market_median` | gpu | median of each provider's lowest price now |
| `provider_price` | gpu, provider | that provider's lowest price now |
| `available` | gpu, provider? | listings explicitly reported available now (default condition `> 0`) |
| `provider_price_change_pct` | gpu, provider, window_hours | % change of that provider's lowest price |
| `region_availability_change_pct` | gpu, region_group, window_hours | % change in available listings in the region group (hourly rollup) |
| `index_level` | index_id | published index level |
| `index_new_low` | index_id, window (e.g. `30d`) | 1 when the latest published level is below every earlier level in the window (default condition `== 1`) |

"Now" follows market.py exactly: eligible listings only (canonical GPU, on-demand, not interruptible,
not Vast's "cheapest" row, not "from" prices), a listing counts only until it was last seen plus a
few polls, sold-out listings are excluded from prices. Price-change windows up to 48 h replay
`listing_observations` for one provider and GPU; longer windows read `market_hourly`.
Availability is `inferred` where the provider reports capacity rather than a flag (see data-kinds).

## Unknown never fires

Every evaluation yields `true`, `false` or `unknown`. Unknown means we cannot say: no live listing,
the provider was not being recorded at the window start ("insufficient coverage"), availability not
reported, an index not published or the indices module unavailable, invalid params. An unknown
reading never fires and is never replaced by a guess.

## Edge triggering and cooldown

Rules are evaluated every 3 minutes (job `alerts`). A rule fires when its state becomes `true` and
its last **known** state was not `true`, and its cooldown (default 1 h, minimum 60 s) has passed since
it last fired. Staying true does not re-fire. `unknown` does not overwrite the last known state, so a
data gap between two true readings does not re-fire. A transition inside the cooldown is recorded but
not delivered; the rule fires again only after it goes false and back to true.
`POST /v1/alerts/{id}/test` evaluates now and reports `would_fire`, writing and delivering nothing.

## Delivery

- **In-app**: every firing is a row in `alert_firings`; `GET /v1/alerts/firings` is the feed.
- **Webhook**: JSON POST, one attempt, 5 s timeout, result recorded per firing. Signed:
  `X-OpenGrid-Timestamp: <unix>` and `X-OpenGrid-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`.
  The secret is generated per rule, shown once at creation, stored encrypted. Verify by recomputing
  over the raw body with a constant-time compare and rejecting timestamps older than 5 minutes.
  Targets must be https and must not resolve to private, loopback or link-local addresses.
- **Email**: interface only, **not implemented**. A rule may list it; its delivery status records
  `not_implemented`. No email is ever sent.

## The operator account

The site operator (basic auth / open local dev) has no API key. Watchlists and alerts made from the
web UI belong to an implicit account flagged `is_operator` (named "operator"), created on first use.
API keys act only on their own account; the operator's data is invisible to them.
