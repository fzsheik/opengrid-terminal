# Provider reliability

Provider reliability is measured only from deployments OpenGrid itself launched, and no score is shown
until a provider has at least `RELIABILITY_MIN_SAMPLES` (default 10) real launches.

Kind: **transaction**. Code: `routing/reliability.py`. API: `GET /v1/reliability` (customer view),
`GET /v1/admin/reliability` (adds validation deployments and in-process provider-call counters).

Nothing here uses market data, provider status pages, marketing claims, or synthetic data. A provider
OpenGrid has never launched on has no reliability data, and says so.

## Metrics (per provider, each with its sample size n)

| metric | definition | n |
|---|---|---|
| successful launches | share of launches the provider reported **running** | launches |
| failed launches | counts by kind: **rejected** (the provider definitively created nothing: `provision_failed`, `provider_rejected`, a capacity / auth / validation / quota error), **timeout** (`provider_timeout`, a timed-out call), **unknown** (`launch_unknown`, `orphan_suspected`, an ambiguous error such as a 5xx). A launch that later ran is a success | launches |
| provisioning latency | first provision call → first `running` event, p50 / p95 | runs |
| time to capacity | approval (else creation) → first `running` event, p50 / p95 | runs |
| unexpected terminations | share of runs that ended without OpenGrid asking (interruption, terminated or failed by the provider) | runs |
| API error rate | failed provision calls / provision calls (attempt rows) | calls |
| termination success | terminations requested that reached a provider-confirmed `terminated` (vs `termination_failed`) | terminations requested |
| quote accuracy | mean absolute difference between execution price and quote, as a share of the quote, and the share within the quote tolerance | runs with both prices |

A *launch* is a deployment with at least one provision call that has reached a verdict; in-flight
launches are not counted either way.

## Sample-size gate

Every metric reports `n`. Below `RELIABILITY_MIN_SAMPLES` its value is withheld and its status reads
`insufficient sample (n=…, need 10)`. One failure (or three successes) is never a reliability verdict.

## Score

    score = 100 × launch success share × (1 − unexpected termination share) × termination success share

The score exists only when the provider has at least the minimum number of launches. Each of the second
and third factors is used only when its own n clears the minimum; skipped factors are listed in
`factors_skipped_insufficient_sample`. The score is a summary of the metrics above, nothing more.

## Validation deployments

Validation deployments (`purpose = validation`) are OpenGrid testing its own adapter (one instance, a
small cost cap, auto-terminate). They are excluded from the customer view, because their workload is not
representative, and shown separately in the admin view.

## In-process provider-call counters

The admin view also shows provider API calls counted from the structured provider-call log since the
last restart (by provider, operation, outcome). They describe recent API health and are not part of the
score.
