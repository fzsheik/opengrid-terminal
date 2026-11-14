# Batches: many independent jobs, one budget, one approval, one status

A **batch** runs many independent jobs at once (a hyperparameter sweep, evaluation shards, batch work), spread across
the cheapest suitable GPU clouds, under one budget, one approval and one status. Each shard is an **ordinary job**
(methodology/jobs.md): it goes through the same validation, workload-aware selection, quote, route, supervised
approval, cost guards, idempotency, single provision call per deployment and termination / reconciliation as a job
you submit by hand. A batch adds no way to provision anything.

Code: `jobs/batches.py`, `api/batches.py`, `store/batches.py` (migration `0036_batches`), the hooks in
`jobs/orchestrator.submit(..., batch=)` and `jobs/relaunch.py`. Web: `/batches`, `/batch/<id>`. CLI: `opengrid batch
preview | list | status | cancel | sandbox`. MCP: `opengrid_preview_batch`, `opengrid_batch_status`.

**What a batch is not.** It is not one distributed training run (the shards never talk to each other; there is no
multi-node job), and it is not inference load-balancing (nothing routes requests to the shards). It is N separate
jobs that OpenGrid places, pays for and reports on together.

## 1. The request

`POST /v1/batches/preview` and `POST /v1/batches` take the job request's shared fields plus the batch's own:

| field | meaning |
|---|---|
| `template`, `job_type`, `config`, `resources`, `routing.strategy` / `weights`, `limits.max_price_per_gpu_hour_usd`, `limits.max_runtime_minutes`, `ssh_public_key`, `sandbox` | shared by every shard, exactly as in a job |
| `shards: [{config}]` **or** `sweep: {grid: {field: [values]}, max_shards?}` | the per-shard parameters: an explicit list, or the cartesian product of the grid (keys sorted, values in the order given) |
| `limits.max_total_cost_usd` | optional cap per shard attempt (default: the template maximum, within the budget) |
| `budget.max_total_usd` | REQUIRED: the ceiling over ALL shards and ALL relaunch attempts |
| `placement` | `max_share_per_provider` (default 0.5), `max_shards_per_provider`, `include_providers` / `exclude_providers`, `include_regions` / `exclude_regions` (a raw region or a region group), `allow_interruptible` (default false; the template decides: `pytorch-mnist` refuses spot) |
| `concurrency.max_running` | shards running at once (default 4); the rest queue |
| `policy` | `on_failure`: `continue` (default) or `stop_on_first_failure`; `max_attempts` 1-3 per shard (automatic relaunch, section 6) |
| `metric` | `{name: test_accuracy, goal: max}`: how the best result is chosen |

Caps: at most `BATCH_MAX_SHARDS` shards (default 64). A grid is sized BEFORE it is expanded, so a huge grid is
refused (`too_many_shards`, with the count) without being materialised. Every shard's job request is validated like a
job; an error names the shard (`field: shards[3].config.learning_rate`). Codes: `no_shards`, `shards_and_sweep`,
`invalid_sweep`, `too_many_shards`, `invalid_placement`, `invalid_budget`, `invalid_concurrency`, `invalid_policy`,
`invalid_metric`, `invalid_name`, plus every job validation code.

## 2. The placement plan

One workload-aware selection (`workloads.select`, every candidate kept, no provider call) ranks every compatible
listing by the request's strategy: CHEAPEST orders by price; the other strategies use the best-execution score
(methodology/best-execution.md: price, availability now, availability history, price stability, freshness,
integration level, and **reliability** once OpenGrid has enough launches of its own). For a real batch only
execution-ready candidates count; a sandbox batch uses the market candidates (for information).

* **Diversification cap.** At most `max_shards_per_provider` shards per provider, default `floor(max_share_per_provider
  x N)` (50%), at least 1. One cloud's outage cannot take out more than that. Providers are filled in rank order up to
  the cap; if the eligible providers cannot hold every shard, the plan says which shards cannot be placed and a real
  submission is refused (`placement_infeasible`): raise the share (1.0 allows everything on one provider), add
  providers or relax the lists.
* **Listed capacity.** When a listing reports its capacity (instances, or GPUs / shard GPUs), no more shards than
  that are placed on it; unknown capacity is not a limit (and is shown as unknown).
* **Interleaving.** Shards are assigned providers alternately in rank order, so the first wave (and every later wave
  of the queue) is spread too.
* **Per shard:** provider, listing, GPU, region, observed price, `est_max_total_usd` (price x billed runtime ceiling),
  `est_typical_total_usd` (the template's expected runtime for that shard's config), and its **reservation**
  (section 3).
* **The alternative.** `all_on_one` = every shard on the rank-1 provider (what N separate jobs would each pick, its
  capacity ignored), and `single_provider` for each provider. `savings_vs_all_on_one_usd` is positive when the spread
  plan is cheaper and negative when spreading costs more (`diversification_premium_usd`): under CHEAPEST, not
  depending on one cloud usually has a price, and the plan says how much.

All plan numbers are estimates (`kind: estimated`) from observed listing prices, never quotes: every shard is quoted
and re-validated live at its own submission.

## 3. Money: the batch budget

`budget.max_total_usd` covers every shard and every relaunch. It is enforced with a **ledger** (`batch_attempts`),
with the same Postgres-lock discipline as the account cost guards (routing/guards.py):

1. Before ANY submission (a shard's first job, a re-try after a refusal, or a relaunch), OpenGrid opens a transaction,
   takes `pg_advisory_xact_lock(LOCK_BATCH, hashtext(batch_id))`, computes what is already **committed**, refuses if
   `committed + reservation > max_total_usd` (`batch_budget_exhausted`), writes the reservation and commits. Only then
   is the job submitted. Concurrent passes, API calls and relaunch workers serialise on the lock: the budget can never
   be overspent by a race (tested with threads).
2. **committed** = per reservation: its `reserved_usd` while it has no job, or its job's deployment is not reconciled
   yet (the max with the metered cost); the reconciled actual cost (max of transaction, provider-reported and metered)
   once reconciled; 0 when it provably created nothing (released before a job existed, refused before any deployment,
   rejected / provision_failed with no instance); 0 for a sandbox job.
3. **reservation** = `min(per-shard limit, est_max_total x (1 + quote_price_tolerance))`, rounded UP to the cent. It
   becomes the shard job's `limits.max_total_cost_usd`, so the existing max-price cap binds the launch
   (`orchestrator._route` sets `max_price_per_gpu_hour <= max_total_cost / (GPUs x billed hours)` and `engine.approve`
   refuses `over_max_price`): price x billed runtime can never exceed the reservation, even after a price move
   accepted within the tolerance.
4. At submission the plan's reservations must fit the budget (`batch_budget_exceeded` otherwise); what is left is the
   **relaunch headroom**. A queued shard that does not fit waits while earlier attempts may still settle below their
   reservations, and is refused once nothing is pending.
5. The account cost guards (max hourly cost, max active deployments, max GPUs, monthly spend) still apply to every
   shard on top, atomically at approval and at provisioning, unchanged. A `max_running` above what they allow is
   flagged in the plan.

## 4. Approval (SUPERVISED)

One approval for the batch: `POST /v1/batches/{id}/approve` (scope `admin`, Idempotency-Key; body `{quotes?:
{shard_index: quote_id}, reason?}`).

* It records, per shard, the **terms** it covers: the exact current quote (provider + price) of every shard waiting in
  `pending_approval`, and the planned provider + price of every queued shard. With `quotes`, only the listed waiting
  shards are approved and each must still be on that exact quote (`quote_mismatch` otherwise).
* Every waiting shard is then approved through the EXISTING `engine.approve`, which re-validates each one
  individually (fresh live check, price within `quote_price_tolerance`, unexpired quote, launch spec, guards). A price
  that moved past the tolerance is re-quoted by the engine and that shard goes to **`needs_reapproval`** with the new
  quote: it is never launched silently.
* A shard submitted LATER (the queue) or re-quoted on expiry is approved by the batch pass under the same approval
  ONLY when its quote is on the approved provider at no more than the approved price x (1 + tolerance). Anything
  else (a higher price, another provider) waits in `needs_reapproval` for the next batch approval. A relaunch,
  resume, OOM escalation or move of a shard is a new decision, not a queue submission: it always waits in
  `needs_reapproval` (`fresh_approval_required`) for the next batch approval, whatever its price. The approver is re-verified each time (the operator, or a key that still exists, is not revoked or
  expired, and is still admin + platform_admin).
* LIVE providers launch without per-launch approval exactly as a plain job does. A sandbox batch needs none
  (`approval_not_required`).

## 5. The queue, failures, cancel

* **Queue.** At most `concurrency.max_running` shards are busy at once (submitting, waiting for a relaunch, or with a
  job that is not finished: a job that is still terminating holds its slot, because it may still bill). The
  `batch_advance` background pass (every `BATCH_ADVANCE_INTERVAL_SECONDS`, 30 s, with a durable heartbeat) follows
  each shard's job, then fills free slots in shard order, then applies the batch approval to what it just submitted.
  Each queued shard is re-placed at its turn: its planned provider while the price is within the tolerance of the
  plan, else the best candidate under the cap (counted over the other unfinished shards). The pass submits as the
  batch's original owner, re-verified like a relaunch (a revoked / expired key, a lost `route:execute` or a suspended
  account refuses the remaining queued shards with that code). `BATCH_QUEUE_ENABLED=false` stops new submissions from
  the queue (status, approval and cancel still work).
* **Refusals.** A refused shard submission (`price_changed`, `gpu_unavailable`, `quote_expired`,
  `no_eligible_provider`, `budget_exceeded`, no candidate under the cap) goes back to the queue and is tried again on
  a later pass, at most 3 times; any other refusal is final (`refused`).
* **Failure policy.** A training failure (`training_failed`, `out_of_memory`, `bootstrap_*`, ...) is reported on its
  shard, never retried. `stop_on_first_failure`: the batch moves to `stopping`, queued shards are `skipped` and every
  shard job without an outcome is cancelled through the EXISTING jobs cancel path (before launch: reject; after:
  terminate); outcome `stopped`.
* **Cancel.** `POST /v1/batches/{id}/cancel` (route:execute, Idempotency-Key): under the batch lock, so nothing is
  claimed after it commits; queued shards `cancelled`; every shard job without an outcome cancelled through the jobs
  cancel path. The batch finishes once every launched GPU is provider-confirmed terminated and reconciled.

## 6. Interrupted shards: relaunch inside the batch budget

A shard with `policy.max_attempts > 1` carries the job retry policy (`retry.max_attempts`, `retry.max_total_usd` =
per-shard limit x attempts). When its GPU is interrupted or lost, `jobs/relaunch.py` handles it exactly as for any job
(methodology/jobs.md 8b): only after the provider confirmed the old machine is gone, excluding every provider that
interrupted the chain, resuming from the latest verified checkpoint, through the normal submit / approval path, as the
original owner. The batch adds the OUTER ceiling:

* no relaunch once the batch is cancelled, stopping or finished (`batch_stopped`);
* the remaining chain budget is also capped by the batch's remaining budget; when the batch is the binding limit the
  stop code is `batch_budget_exhausted`;
* the successor follows the batch's provider allow / deny lists, not the first attempt's placement pin, and its cost
  limit is the batch's per-shard limit within what remains;
* its reservation is taken under the batch lock BEFORE anything is quoted (released if the submission is refused) and
  attached to the successor in the job-creating transaction; the successor replaces the failed job as the shard's
  current job by compare-and-set;
* in SUPERVISED mode the successor (on another provider) waits for re-approval.

Diversification is not re-applied to relaunches: a relaunch goes to the best remaining eligible provider.

## 7. Guarantees in the database

* `UNIQUE(batch_id, shard_index)`: a shard exists once.
* A queued shard is claimed (`queued -> submitting`) inside the locked reservation transaction: one claimer wins.
* `batch_attempts`: `UNIQUE(batch_id, ref)` (one reservation per submission try / per relaunch of a failed job) and
  `UNIQUE(job_id)`. A job is attached to its reservation AND made the shard's current job (compare-and-set on the job
  it replaces) in the same transaction that creates the job row (`orchestrator._create_job` -> `batches.attach`): a
  second live job for one shard is refused by the database (`batch_shard_conflict`) and rolled back.
* A submission that crashed after its claim but before its job attached (nothing was created: the attach is atomic
  with the job) is put back in the queue after 15 minutes, its reservation released; this includes a retry of a
  refused shard, whose current job is still the refused try until the new one attaches (the shard stays `submitting`
  meanwhile, never derived from the refused try). A submission that raised AFTER its job attached is never requeued:
  the shard follows that job.
* `POST /v1/batches` uses the idempotency store (same key + same body replays, a different body is 422, a crashed
  attempt that may have created a batch is never re-run).

## 8. Status

`GET /v1/batches/{id}` (deployments:read; database only, never a provider call): `status` (`active`, `stopping`,
`cancelling`, `finished`) and `outcome` (`succeeded`, `partial`, `failed`, `stopped`, `cancelled`, `simulated`);
`counts` per shard state (`queued`, `submitting`, `pending_approval`, `needs_reapproval`, `running`, `relaunching`,
`succeeded`, `failed`, `cancelled`, `skipped`, `refused`); `budget` (`max_total_usd`, `committed_usd` (an upper bound
until every deployment is reconciled), `spent_usd` (metered / reconciled so far), `remaining_usd`); `approval`
(`status`, `required`, the waiting shards with their exact quotes, the approve body); per-provider breakdown; one row
per shard with its job link, attempts, provider, price, the metric and the reason of a failure / refusal; `best` (the
succeeded shard with the best value of the batch's metric; SIMULATED values are labelled); events; the ledger; the
plan. A shard's job carries `batch: {batch_id, shard_index, ...}` (only shard jobs have this key).

## 9. Sandbox

`sandbox: true` runs every shard as a SIMULATED job (methodology/jobs.md section 6): no provider is called, nothing is
billed, no approval is needed, every response carries `simulated: true`, the budget stays at $0 committed and the
best result is labelled SIMULATED. The placement plan is still computed from the live market, so a sandbox batch shows
what a real one would cost and where it would run.

## 10. Endpoints

| method | path | scope |
|---|---|---|
| POST | /v1/batches/preview | route:preview |
| POST | /v1/batches (Idempotency-Key) | route:execute |
| GET | /v1/batches (?status=open\|active\|stopping\|cancelling\|finished), /v1/batches/{id} | deployments:read |
| POST | /v1/batches/{id}/approve (Idempotency-Key) | admin |
| POST | /v1/batches/{id}/cancel (Idempotency-Key) | route:execute |

Keys see their own account's batches (another account's batch is 404); the operator sees all. Settings:
`BATCH_MAX_SHARDS` (64), `BATCH_QUEUE_ENABLED` (true), `BATCH_ADVANCE_INTERVAL_SECONDS` (30). Errors: HTTP 4xx with
`detail = {code, message, ...}`: the codes above, `batch_not_found` (404), `batch_not_active`, `approval_not_required`
(409), `idempotency_key_required` (428), `idempotency_key_reused` (422).
