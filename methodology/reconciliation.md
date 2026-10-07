# Reconciliation, metering and cost reconciliation

How OpenGrid makes its deployment records agree with what actually exists (and bills) at each provider,
how usage is metered while a deployment runs, and how each finished deployment's cost is reconciled.
Code: `routing/reconcile.py`, `routing/tracker.py`, `routing/transactions.py`, `billing/usage.py`.
Tables: `reconciliation_runs`, `orphan_resources`, `deployment_watch`, `usage_slices` (migration 0011).

## Principles

- **Uncertainty is never collapsed into failure.** A launch whose outcome is unknown (timeout, 5xx,
  connection reset, unparseable answer) stays `provider_timeout` / `launch_unknown` until the provider's own
  list proves what happened. Nothing is ever retried or failed over while it is unresolved.
- **Termination needs two signals.** `terminated` is recorded only when the provider says so twice: the
  instance list omits it (or lists it as ended) AND a status read says not_found/terminated, or two
  consecutive not_found status reads at least `not_found_confirm_seconds` (60 s) apart. One 404 proves nothing.
- **Pinned credentials only.** Every status, list, stop and terminate uses exactly the credential the
  deployment was launched with. If it becomes unusable the deployment goes to `credentials_unavailable`
  (state kept, alert, retried every pass) and is never marked terminated.
- **Only provably-ours instances are touched automatically.** OpenGrid names every instance
  `og-<deployment id>` (name and, where supported, a tag/label). Anything not named `og-*` is ignored entirely.

## The reconciliation pass (job `reconcile`, every `reconcile_interval_seconds`, default 120 s)

For each provider with an adapter, for each credential that live or recently ended deployments are pinned
to (plus OpenGrid's managed credential when configured): `list_instances()` (all pages, or an error, never
a partial list), then:

| Situation | Action |
|---|---|
| `provider_timeout` / `launch_unknown` / `provisioning` without an instance id (worker crashed mid-call) | Find an instance named `og-<dep>` in the list, then via `find_instance(name)`. Found: **adopt** (link the instance id, move to the state the provider reports, evidence + alert). Two live matches: `orphan_suspected` + `duplicate_launch` orphans (never pick one). Nothing found: wait; only after `provisioning_timeout_minutes` (15) and with BOTH the list and `find_instance` showing absence -> `provision_failed`. A failed list proves nothing. |
| running / stopped / degraded / provisioning, instance absent from the list | Status read. not_found/terminated -> `terminated` (provider_terminated) with both signals as evidence; alive -> recorded as `list_inconsistent`, nothing changes. |
| `terminating` / `termination_failed` | Gone by two signals -> `terminated`. Still present -> re-issue terminate with backoff `termination_retry_base_seconds x 2^n` (cap 1 h). After `termination_retry_max` (5) attempts -> `termination_failed` + alert; retries continue (it is still billing). |
| `terminate_deadline_at` passed on a live deployment | terminate (actor system, `termination_reason = max_runtime_exceeded`) + alert. A deployment whose instance id is not yet known is terminated as soon as reconciliation adopts it. |
| accepted but not running after the provisioning timeout | alert (`boot_slow`); after 4x the timeout terminate (`boot_timeout`; never-running time is not billed). |
| pinned credential unusable | `credentials_unavailable`, alert (at most hourly), retried each pass; restored automatically when it works again. |

### Orphans (`orphan_resources`)

| Kind | Meaning | Automatic action |
|---|---|---|
| `og_no_deployment` | an `og-*` instance with no OpenGrid deployment on that provider | none: alert, operator decides |
| `deployment_ended_alive` | the deployment ended in OpenGrid (terminated / provision_failed / provider_rejected) but its instance is alive | auto-terminate ONLY if the instance is listed under the very credential the deployment was pinned to (`provably_ours`); a rejected launch that created an instance is re-opened (`orphan_suspected`, linked) and terminated through the state machine |
| `duplicate_launch` | a second live `og-<dep>` instance for a deployment | none: alert, operator decides |

Orphans not seen in a later listing become `gone` (or `terminated` after an auto-terminate). Operator
actions: `reconcile.resolve_orphan(id, action, by)` with `terminate` (provider call with the listing
credential), `ignore`, `adopt` (link to its still-unresolved deployment).

Every pass writes a `reconciliation_runs` row: per-provider summary (credentials, listed, list errors) and
every finding. Alerts go to `alerts.ops.alert` (incident on /ops + signed ops webhook) for orphan_detected,
termination_failed, launch_unknown (unresolved after the timeout), credentials_unavailable,
reconciliation_failed and deadline_terminate; deduplicated per deployment/instance (6 h).

## Status polling and metering (job `routing_tracker`, every `tracker_interval_seconds`, default 60 s)

Each live deployment with an instance id is read with its pinned credential and applied through the core's
state machine (`deployments.observe` -> `transition`). Usage is metered incrementally into **hour slices**
(`usage_slices`, one per deployment per UTC hour, unique on (deployment, period_start)):

- billed at the GPU rate: `running`, `degraded`, `stopping`, `terminating`, `termination_failed`,
  `orphan_suspected`, `credentials_unavailable` — but only after the instance was first observed running;
- `degraded` (a provider error state) is not billed where the provider documents no charge (Hyperstack);
- `stopped`: per the adapter's `CAPABILITIES.stopped_billing` — `full` bills at the GPU rate (DigitalOcean,
  Hyperstack, Verda), `storage_only` records the seconds at $0 GPU time (RunPod, Vast; storage itself is not
  metered by OpenGrid and appears in cost reconciliation); unknown is treated as `full`. Each slice records
  the rule and its evidence;
- time before first running (provisioning, unknown launches) is recorded as unbilled seconds;
- while live, only CLOSED hours up to the last SUCCESSFUL provider observation are written; after
  termination the final partial hour is written;
- the end time is the provider-confirmed termination time when the provider gives one (Shadeform
  `deleted_at`, Lambda/others when reported), else the first observation of `terminated`; the final slice is
  flagged `end_estimated`.

Each billable slice produces exactly one billing usage record (`billing.usage.record_usage_slice` ->
`record_usage` with the slice's billable GPU-hours), so invoices see each hour in its own month (an hour
never crosses a month boundary). Price: the provider-reported execution price when known, else the quote.
Deployments billed before incremental metering kept their single legacy usage record and are not re-metered.

## Cost reconciliation (`transactions.reconcile_cost`, after confirmed termination)

Stored in `deployments.reconciliation` (plus `provider_reported_cost`, `reconciled_at`). Every figure is
separate; none is merged into another:

| Field | Definition | Data kind |
|---|---|---|
| `quote` | per GPU-hour and total, from the consumed quote | quote |
| `expected_cost` | quote price x metered billable GPU-hours | estimated |
| `provider_reported_cost` | `adapter.reported_cost()` where the provider exposes billing (RunPod `/billing/pods`, Hyperstack billing history, Vast charges, Shadeform `cost_estimate` [unit unverified]); else null + reason. Re-asked hourly for 48 h while provider billing lags. | provider-reported |
| `opengrid_transaction_cost` | sum of the deployment's usage records (provider cost passed through); OpenGrid fee lines reported next to it | transaction |
| `quote_error` | transaction cost - expected cost (USD and %) | derived |
| `effective_hourly_rate` | per GPU-hour, from transaction cost and (separately) provider cost | derived |
| `billing_rounding` | what the provider's billing unit adds over exact metering (per minute: ceil to 60 s) | derived |
| `unexpected_fees` | provider-reported - transaction cost - rounding, when > $0.005; null without a provider cost | derived |

## Functions for the admin API / frontend

`reconcile.run_once(provider=None)`, `reconcile.orphans(status=None)`, `reconcile.resolve_orphan(id, action, by)`,
`reconcile.runs(limit)`, `reconcile.last_run()`, `reconcile.watch(deployment_id)`, `tracker.track()`,
`tracker.meter(deployment_id)`, `billing.usage.slices_for(deployment_id)`, `transactions.reconcile_cost(id)`.
