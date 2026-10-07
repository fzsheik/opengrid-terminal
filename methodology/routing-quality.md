# Routing quality

Every route request is scored after the fact on two things: what the decision chose and what actually
happened when OpenGrid executed it, using only OpenGrid's own transaction records.

Code: `routing/quality.py`, table `route_outcomes` (store/metrics.py), job `route_outcomes` (every
`METRICS_RECOMPUTE_SECONDS`, default 300 s). API: `GET /v1/quality` (your account), `GET /v1/admin/quality`,
`POST /v1/admin/quality/recompute`, `GET /v1/admin/metrics`, `GET /v1/admin/trace/{id}`.

## Per route

One row per route request, derived from `route_requests`, `routing_decisions`, `deployments`,
`deployment_events`, `provision_attempts`, `quotes` and `usage_slices` / `usage_records`.

| field | definition | kind |
|---|---|---|
| winner / runner-up | rank 1 and rank 2 candidates of the routing decision | inferred |
| candidates | the top 10 (provider, listing, observed price, score) of all candidates | inferred |
| market median | from the decision's market snapshot: the median of each provider's lowest current eligible price (canonical GPU, on-demand, not interruptible, not stale, not sold out) at decision time | observed |
| cheapest valid option | the lowest-priced candidate that passed every eligibility rule | observed |
| strategy | the routing mode (CHEAPEST, BALANCED, ...) | |
| expected savings | (median − winner observed price) / median, at decision time | inferred |
| launched | at least one provision call was made | transaction |
| provisioned | the provider reported the instance **running** (a `deployment_events` row to `running`). An accepted launch alone is not provisioned | transaction |
| provisioning latency | first provision call → first `running` event | transaction |
| execution price | the price the provider reports for the instance | transaction |
| realized savings | (median − execution price) / median, only for deployments that ran | transaction |
| quote error | (execution price − quote) / quote | transaction |
| interrupted | ran, then ended without OpenGrid asking (interruption count > 0, or terminated/failed by the provider) | transaction |
| uptime, GPU-hours | uptime as observed; GPU-hours from metered usage slices (running seconds × GPUs; stopped time excluded), else billing usage records, else uptime × GPUs | transaction |

A savings number exists only with a **valid comparison** (see [economics](/methodology/economics)):
same canonical GPU (a family route has no single median), median from at least 3 providers at decision
time. Otherwise `comparison_reason` says why, and the savings fields are null.

When a route fails over, each provider attempt is its own deployment; the route is represented by the
deployment that ran, else the most recent one.

## Aggregates

Each aggregate carries its sample size `n`; with no data it says "unavailable: no data".

- **routing success rate** = route requests whose deployment the provider reported running / all
  non-preview route requests (no candidates, refused quotes, limits, rejections and provider failures all
  count against it).
- **provisioning success rate** = deployments reported running / deployments with a provision call.
- **savings vs market median**: mean and median of expected and realized savings over valid comparisons.
- **savings vs your previous provider** = (the design partner's stated normal price − execution price) /
  normal price. The normal price is what the partner told us (partner-reported, not observed) and is used
  only when given.
- **provider failure rate** = launches that never reported running / launches, per provider.
- **provisioning latency**: mean, median, p50, p95.
- **quote accuracy**: mean |quote error| and the share within `QUOTE_PRICE_TOLERANCE` (default 2%).

Validation deployments (`purpose = validation`, OpenGrid testing its own adapters) are excluded unless
`include_validation=true` (admin).

## Recompute

The job upserts by `route_request_id`, so recomputing never duplicates. A row is `final` (skipped by
later runs) when it is a preview, a route with no deployment older than a day, or a deployment in a
terminal state that was cost-reconciled more than a day ago. `POST /v1/admin/quality/recompute?full=true`
recomputes everything.

## Metrics and traces

`GET /v1/admin/metrics` combines in-process counters (API requests by route template and status, API
latency p50/p95, provider calls by provider / operation / outcome from the structured provider-call log,
DB latency from a `SELECT 1` every 60 s; all since the last restart) with counts from the database over
a window (route previews, launches, approvals, terminations, termination failures, reconciliation runs and
failures, orphan detections, provider polling failures from `raw_snapshots`, news ingestion failures from
`news_fetch_log`), job health (running, overdue, failing), and deployment lifecycle durations
(created → approved → provisioning → running → terminated, p50/p95, from `deployment_events`).

`GET /v1/admin/trace/{route_request_id | deployment_id}` assembles the full chain from the database in time
order: route request → decision (weights, candidates) → quotes → deployment → approval → provision
attempts → state changes → termination → reconciliation → usage slices / billing → alerts → feedback.

Every log line carries `request_id` (from or returned as `X-Request-ID`), and where known
`route_request_id`, `deployment_id`, `provider`, `account_id`; JSON lines when deployed (`LOG_JSON`);
secrets (authorization headers, API keys, tokens, passwords, Fernet keys, provider key shapes, the values
of secret settings) are redacted on every handler.
