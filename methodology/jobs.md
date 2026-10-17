# Jobs: one training workload, end to end

A **job** is the customer's workload and its outcome; a **deployment** is the cloud resource it runs on. A job
references the existing execution layer (`route_request_id`, `deployment_id`, `quote_id`) and never duplicates
it: the quote, the supervised approval, the cost guards, the single provision call, status tracking,
termination and cost reconciliation are the same code every route uses (methodology/execution-safety.md,
methodology/reconciliation.md). Both states are always shown: a job is never "completed" because an instance
started, and a failed job never implies the GPU stopped billing.

Code: `jobs/` (states, orchestrator, sync, tokens, artifacts, ephemeral_ssh, models), `api/jobs.py`,
`store/jobs.py` (migration `0017_jobs`). Selection: methodology/workloads.md. What runs on the GPU:
methodology/runner.md.

## 1. Lifecycle

1. `POST /v1/jobs/preview` validates the request against the allowlisted template (`workloads.spec`) and runs
   the workload-aware selection (`workloads.select`): `market_best` (best observed listing regardless of
   provider validation) and `execution_ready` (best candidate OpenGrid can actually run now), alternatives,
   exclusions with reasons, blockers, and a cost estimate (kind `estimated`). `submission` says what a submit
   would do now: `pending_approval`, `simulated` (sandbox) or `refused` with the exact code and blockers.
2. `POST /v1/jobs` (Idempotency-Key required) repeats validation and selection, then:
   - **sandbox: true** → a SIMULATED run (section 6). No provider is called.
   - otherwise an `execution_ready` candidate is REQUIRED; without one the request is refused (HTTP 422,
     `execution_disabled` / `provider_unvalidated` / `provider_killed` / `credentials_unavailable` /
     `no_suitable_gpu` / `no_eligible_provider` / `budget_exceeded`) and no job is created. Nothing is faked.
   - the job row is created (`created` → `validating`); if the request has no `ssh_public_key` an
     **ephemeral** ed25519 key is generated and its private half discarded immediately (section 5);
   - OpenGrid persists a preview route request and a quote for **exactly** the selected listing (observed
     price), then calls the EXISTING `engine.route` with that `quote_id`, purpose `customer`, launch
     `{ssh_public_key, startup_script: "opengrid-job:<job_id>"}`, `max_runtime_minutes` = the job's runtime
     ceiling and `max_price_per_gpu_hour` = min(the request's cap, `max_total_cost_usd` / (GPUs × billed
     hours)). The route re-validates the quote live (price within `quote_price_tolerance`, still available),
     checks cost guards and creates the deployment: SUPERVISED → `pending_approval` (HTTP 202).
     The budget therefore binds through the existing max-price cap: an approval can never launch at a price
     whose ceiling-length cost exceeds `max_total_cost_usd`.
3. Approval is the EXISTING `POST /v1/route/{route_request_id}/approve` (scope admin, Idempotency-Key). The
   job's `approval` block names it; jobs add no second approval path.
4. At provision time `routing/deployments.launch` (under the deployment row lock, after the control / cost-guard
   / operator-key gates and before the quote is consumed) replaces the placeholder with
   `runner.bootstrap.render(job_id, token, ...)` (section 4). If it cannot render, the launch is refused
   (`job_bootstrap_unavailable`, deployment back to `pending_approval`): a placeholder never reaches a provider.
5. The runner reports phases, logs, metrics, GPU samples, its result, artifacts, and `complete`.
6. On `complete` (any outcome), a training failure, OOM, an artifact failure, a bootstrap timeout, a runner
   that went silent (`runner_lost`) or a cancel,
   job_sync calls the EXISTING `deployments.terminate` (actor system, idempotent); the tracker / reconciler
   confirm termination with provider evidence; cost reconciliation (`routing/transactions.py`) sets
   `reconciled_at`; the job becomes `reconciled`.

## 2. State machine (`jobs/states.py`)

`transition()` is the only writer of `jobs.status`; every change writes `job_events` (from, to, actor
`user|admin|system|runner|reconciler`, reason, evidence). Moving to the current state is a no-op.

| state | meaning | next |
|---|---|---|
| created → validating | request accepted and validated | quoted, validation_failed, no_eligible_provider, initializing (sandbox) |
| quoted | quote for the selected listing persisted | pending_approval, approved, provisioning, approval_rejected, quote_expired, no_eligible_provider, provider_timeout, provisioning_uncertain |
| pending_approval | deployment waits for an admin | approved, provisioning, approval_rejected, quote_expired, … |
| quote_expired | the quote expired before approval (a fresh quote is issued for re-approval) | pending_approval, approved, provisioning, approval_rejected |
| approved | admin approved; launch next | provisioning, pending_approval, quote_expired, provider_timeout, provisioning_uncertain, cancel_requested |
| provisioning | the provision call was made | initializing, provider_timeout, provisioning_uncertain, no_eligible_provider, bootstrap_failed, cancel/terminate states |
| provider_timeout / provisioning_uncertain | the launch outcome is unknown: the instance may exist; reconciliation resolves it by name, never a second launch | provisioning, initializing, terminate states |
| initializing | the provider reports running; bootstrap in progress | training, saving_artifacts, training_completed, failure states (incl. runner_lost), terminate states |
| training → saving_artifacts | runner phases (forward only: a late or out-of-order phase never moves a job back) | training_completed, failure states (incl. runner_lost) |
| training_completed | `complete(succeeded)` with the runner's result AND every required artifact persisted and sha256-verified AND real GPU metrics | terminating (+ terminate states) |
| bootstrap_failed, training_failed, out_of_memory, training_timed_out, artifact_upload_failed | the workload failed; outcome `failed`; the GPU is still terminated | terminating, termination_confirmed, termination_failed, orphan_suspected, credentials_unavailable, cancel_requested |
| runner_lost | the runner reported, then went silent past `JOB_RUNNER_SILENCE_MINUTES` before completing (section 3); outcome `failed`, `failure.code` `runner_lost` (or `gpu_interrupted` when the provider stopped the machine unasked); the GPU is terminated | as the row above |
| cancel_requested → cancelling | customer cancel after launch | terminating, termination_confirmed, termination_failed, … |
| terminating | terminate requested, awaiting provider confirmation | termination_confirmed, termination_failed, orphan_suspected, credentials_unavailable |
| termination_failed, orphan_suspected, credentials_unavailable | possibly still billing; the operator is alerted by the existing reconciliation | terminating, termination_confirmed, … |
| termination_confirmed → reconciliation_pending → reconciled | provider-confirmed gone; cost reconciled | (reconciled is final) |
| validation_failed, no_eligible_provider, approval_rejected | final; nothing was ever created | n/a |

Sandbox jobs finish at `training_completed` (or the failure state) with outcome `simulated`.
**Outcome** (`succeeded | failed | cancelled | simulated`) is separate from the state and set once.
**Disposition** (`resource.disposition`): `not_launched_yet`, `none` (nothing was ever created),
`possibly_billing` (any deployment state in which an instance may exist), `terminated_confirmed`,
`simulated_none`. A cancel before launch uses the existing reject (`approval_rejected`, outcome `cancelled`).

Deployment → job mapping (job_sync): created/quoted/pending_approval → pending_approval; quote_expired →
quote_expired; approved → approved; rejected → approval_rejected; provision_failed → no_eligible_provider
(`gpu_unavailable`), provider_rejected → no_eligible_provider (`provider_rejected`); provisioning →
provisioning; provider_timeout → provider_timeout; launch_unknown → provisioning_uncertain; running with no
runner phase yet → initializing; terminating / termination_failed / orphan_suspected / credentials_unavailable
→ the same; terminated → termination_confirmed → reconciliation_pending → reconciled (`deployments.reconciled_at`).

## 3. job_sync (`jobs/sync.py`)

A background job (`job_sync`, every `JOB_SYNC_INTERVAL_SECONDS` = 30 s, with a durable heartbeat) plus hooks
after every runner callback and on `GET /v1/jobs/{id}` (database only, no provider call). Each pass locks the
job row (`SELECT … FOR UPDATE`), reads the deployment and the runner ledger, and moves the job along the
shortest legal path, so a restart (or a missed approval / boot while no worker ran) is rebuilt from the
database in one pass. Enforcement: runner complete / any workload failure / cancel → terminate; running with
**no runner callback** for `JOB_BOOTSTRAP_TIMEOUT_MINUTES` (20) → `bootstrap_failed` (`bootstrap_timeout`) +
terminate; past the deployment's `terminate_deadline_at` while the workload runs → `training_timed_out` (the
deadline's auto-terminate itself stays the reconciler's). Runner tokens are revoked when the job reaches
`termination_confirmed` or finishes.

**Runner liveness.** The runner sends a `gpu` sample every ~15 s while the trainer runs (and retries with
backoff capped at 60 s), so a live job reports far more often than `JOB_RUNNER_SILENCE_MINUTES` (10). A job in
initializing / training / saving_artifacts that has reported at least once, has not posted `complete`, and has
sent nothing (no runner event, no artifact upload) for that long is judged by what the deployment shows:

| deployment | job |
|---|---|
| running | `runner_lost` (`failure.code` `runner_lost`, reason "the GPU machine stopped reporting for N minutes") + the existing terminate, exactly once |
| stopping / stopped without OpenGrid asking (an interruption) | `runner_lost` with `failure.code` `gpu_interrupted` + terminate |
| degraded, termination_failed, orphan_suspected, credentials_unavailable, provider_timeout, launch_unknown, or stopped on request | unchanged: the uncertain state stays visible; one `runner silent` note per silence |
| terminating / terminated | the deployment mapping above decides (no runner_lost) |

While still initializing, the limit is the longer of `JOB_RUNNER_SILENCE_MINUTES` and
`JOB_BOOTSTRAP_TIMEOUT_MINUTES` (the pinned PyTorch install sends nothing for minutes). A job that never reported
is the bootstrap case (`bootstrap_failed` / `bootstrap_timeout`). `0` turns the check off. Precedence on one
pass: the runner's own error / complete, then the deadline (`training_timed_out`), then the deployment's end,
then silence.

**Interruptions.** When the deployment ends before the runner completed and it was an interruption (it left
`running` without OpenGrid asking: `deployment_events.detail.interruption`, `deployments.interruptions`), the
job's `failure.code` is `gpu_interrupted`; any other early end stays `terminated_before_completion`. The code
is written once, so jobs recorded before this rule keep theirs.

## 4. Runner authentication and callbacks

`token = HMAC-SHA256(API_KEY_PEPPER, "job:" + job_id + ":" + nonce)`; header `Authorization: Bearer
ogj_<job_id>.<token>`. `job_tokens` stores only the nonce, `expires_at` (= the deployment deadline + 15 min,
`JOB_TOKEN_GRACE_MINUTES`), `revoked_at`, `last_seq`. The token exists only in the rendered bootstrap (handed to
the provider; the adapter's stored request is redacted) and on the GPU. The job keeps `bootstrap_sha256` and
`bootstrap_template_version`; the script is never stored or logged. Verification: shape → the URL's job id
equals the token's → a token row of that job matches (constant-time HMAC compare) → not revoked → not expired
→ job not finished (409, which the reporter treats as terminal). Accepting a call extends nothing. The runner
routes never accept an API key or the site login; a runner token is not an API key anywhere else.

| route | body | rules |
|---|---|---|
| `POST /v1/jobs/{id}/runner/events` | `{"events": [{"seq", "type": phase|log|metric|gpu|result|error, "at", "data"}]}` | ≤ 1 MB, ≤ 500 events; one `job_runner_events` row per (job, seq): duplicates ignored, out-of-order accepted; log lines truncated at 4 KB; metric values finite numbers only |
| `PUT /v1/jobs/{id}/runner/artifacts/{name}` | raw bytes, `X-Content-SHA256` (or `X-Artifact-SHA256`) | name `^[a-z0-9][a-z0-9._-]{0,63}$`; ≤ `JOB_ARTIFACT_MAX_MB` (64); sha256 verified; same bytes again = idempotent, different bytes = 409. `ckpt-eNNNN.pt` = a checkpoint (section 8a): ≤ `JOB_CHECKPOINT_MAX_MB` (32), latest-N retention |
| `GET /v1/jobs/{id}/runner/resume` | none | only for a job created with `resume_from`: the pinned checkpoint, re-verified (same account, still stored, same sha256), `X-Content-SHA256`; otherwise 404 `resume_not_requested` |
| `POST /v1/jobs/{id}/runner/complete` | `{"outcome": succeeded|failed|oom|timeout, "summary": {...}}` | once (a second complete is a no-op); `summary.reason` maps failures (`cuda_unavailable` → bootstrap_failed / `cuda_init_failed`, `artifact_upload_failed`, …) |

Success is never recorded on the runner's word alone: `succeeded` needs the runner's `result` event (or the
summary's result), every template artifact (`model.pt`, `config.json`, `metrics.json`, `env.json`) stored with
a verified sha256 that also matches the runner's manifest, and real loss/accuracy metrics trained on a CUDA
device. Otherwise the job fails with `result_missing`, `artifact_upload_failed` or `metrics_missing`.
Checkpoints never count toward this: they are not template artifacts, `artifacts.persisted()` ignores them,
and a stored checkpoint does not set `weights_saved`.

## 5. SSH access

A customer key is validated (`routing.credentials.parse_public_key`) and registered per deployment by the
adapter; only its fingerprint is stored. Without one, OpenGrid generates an ed25519 pair, keeps the public key
and fingerprint, and discards the private key in the same function: nobody, OpenGrid included, can log in.

## 6. Sandbox (SIMULATED)

`sandbox: true` never calls a provider. It produces the same kinds of events through the same ingestion code
with `source = 'simulated'` (placeholder metric values, a `simulated-report.json` artifact), every one of the
12 checks is `simulated` (never `pass`), the outcome is `simulated`, the disposition `simulated_none`, and every
response carries `simulated: true` (`meta.kind = simulated`). Checkpoints are simulated as `checkpoint_saved`
phase events only (`stored: false`): only the runner may store checkpoint bytes, so a sandbox job has no
checkpoint and cannot be resumed from (`resume_checkpoint_not_found`). A sandbox job MAY resume from a real,
verified checkpoint of the same account: it emits a simulated `resumed` phase and simulates the remaining
epochs, without downloading or loading anything.

## 7. The 12 verification checks (`jobs/models.py`)

Each check has `status` (`pending | pass | fail | simulated`), `source` (provider / runner / opengrid /
simulated), `evidence` and `at`:
instance created (provider instance id), runtime initialized (first runner callback), torch installed
(`torch_installed` versions), GPU detected (nvidia-smi / torch device name), CUDA available
(`torch.cuda.is_available()` and the trainer's device; CPU = fail), training ran (epoch metrics), params updated
(`param_l2_change` > 0), real loss/accuracy (runner metrics with a CUDA batch device), weights saved (`model.pt`
stored), artifacts persisted (every template artifact stored, sha256 verified), GPU terminated (terminate
requested and accepted by the provider), termination verified (the deployment's provider-evidenced
`terminated` event).

## 8. Artifacts

`jobs/artifacts.py`: an `ArtifactStore` with `postgres` (default: bytes in `job_artifacts`, on the app's
Postgres volume) and `s3` (S3-compatible: `JOB_ARTIFACT_S3_ENDPOINT/BUCKET/REGION/ACCESS_KEY_ID/
SECRET_ACCESS_KEY`; server-side upload only, the GPU never gets storage credentials; needs boto3; enabled only
when configured, otherwise `artifact_storage_unavailable`, never a silent fallback). Downloads
(`GET /v1/jobs/{id}/artifacts/{name}`, scope deployments:read) are tenant-scoped, `Content-Disposition:
attachment`, `application/octet-stream`, with `X-Artifact-SHA256`; the stored sha256 is re-checked on read.
Every artifact carries `kind`: `output` or `checkpoint` (plus `epoch`).

## 8a. Checkpoints and resume

**What.** The `pytorch-mnist` trainer saves model + Adam optimizer state + epoch + torch/data-loader RNG state
every `config.checkpoint_every_epochs` epochs (template field, 0-10, default 1; 0 = never) before the last one,
as `ckpt-eNNNN.pt` (methodology/runner.md section 5a). The reporter uploads the newest one through the ordinary
artifacts PUT while training continues.

**Rules (`jobs/artifacts.py`).** A name matching `^ckpt-e[0-9]{4}\.pt$` is a checkpoint: sha256-verified like
every artifact, ≤ `JOB_CHECKPOINT_MAX_MB` (default 32; the MNIST state is ~14 MB), and only from the runner
(`checkpoint_source_invalid` otherwise). **Latest wins**: after a checkpoint is stored, the job keeps only its
newest `JOB_CHECKPOINT_KEEP` (default 2, clamped 1-4) by epoch and deletes the rest; an upload older than every
kept one is answered `superseded: true` and not stored. A checkpoint that an unfinished job is pinned to (its
`resume_from`) is never pruned. Checkpoints still count toward the per-job limits (32 artifacts,
4 × `JOB_ARTIFACT_MAX_MB` bytes), which are unchanged; retention keeps their share at ≤ keep × the checkpoint
limit. They are listed with the job's artifacts (`kind: checkpoint`) and in the job's `checkpoints` block
(`stored`, `latest`, `resumable`, `keep`, `reported_epochs`).

**Resume.** `POST /v1/jobs` (and preview) accept `resume_from: {"job_id": "job-…", "artifact": "ckpt-e0003.pt"}`
(`artifact` omitted = that job's latest checkpoint). At submission `jobs.artifacts.resolve_resume` requires: a
job of the SAME account (otherwise `resume_job_not_found`, 404, the same answer as a job that does not exist),
a stored runner checkpoint (`resume_checkpoint_not_found`, `resume_artifact_not_checkpoint`), bytes that still
match the stored sha256 (`artifact_corrupt`), and `config.epochs` > the checkpoint's epoch
(`resume_nothing_to_do`). It pins `{job_id, artifact, epoch, sha256, size}` into the new job's spec. At launch
the bootstrap gets the pinned sha256 + epoch; stage 2 downloads the checkpoint with the new job's own runner
token (`GET /v1/jobs/{new}/runner/resume`, re-checked again server-side), refuses bytes whose sha256 differs,
and starts the trainer with `--resume`. The trainer re-verifies the sha256, loads the file weights-only (no
pickled code), checks template/format/epoch, restores the state and continues at epoch + 1 with a `resumed`
phase `{from_epoch, next_epoch}`. Nothing is public: the source job's artifacts stay readable only by its
account (and the operator); the new job's token reaches exactly the pinned checkpoint and nothing else, and
stops working when that job ends. The job's `resume` block shows `{job_id, artifact, epoch, sha256,
confirmed, confirmed_from_epoch}` ("Resumed from epoch N of job X" on the job page once `confirmed`).

**Relaunch interface.** `jobs.artifacts.latest_checkpoint(job_id, verify=False) -> {job_id, artifact, epoch,
size, sha256, created_at} | None` answers "is there a resumable checkpoint"; a new submission (same account)
with `resume_from = {"job_id": job_id, "artifact": <artifact>}` continues from it on any provider.
`jobs.artifacts.resolve_resume(account_id, ref, epochs=)` is the same validation without creating a job.

## 8b. Automatic relaunch (`jobs/relaunch.py`)

**Off by default, twice.** Nothing is ever relaunched unless BOTH the deployment setting
`JOB_AUTO_RELAUNCH_ENABLED` (default `false`) is on AND the job asked for it:

```json
"retry": {"max_attempts": 3, "max_total_usd": 6}
```

`max_attempts` counts every attempt including the first (1-3; default 1 = never relaunched). `max_total_usd` is
REQUIRED when `max_attempts > 1`: the ceiling on the spend of ALL attempts together; it must cover the first
attempt (`>= limits.max_total_cost_usd`). Validation codes: `invalid_retry`, `retry_budget_required`. Preview
shows the policy under `submission.retry` (`applies`, `auto_relaunch_enabled`, a plain note). A sandbox job
records the policy but is never relaunched: a simulation is never interrupted, so there is nothing to simulate.

**Trigger.** Only an attempt whose `failure.code` is `gpu_interrupted` (the provider ended the machine without
OpenGrid asking) or `runner_lost` (the GPU machine stopped reporting; OpenGrid terminated it). Never
`training_failed`, `out_of_memory`, `bootstrap_failed` / `bootstrap_timeout` / `cuda_init_failed`,
`training_timed_out`, `artifact_upload_failed`, a cancel, or a validation error: those would fail again, or were
asked for.

**The core rule: never relaunch while the previous machine might still be billing.** The attempt must be
`termination_confirmed`, `reconciliation_pending` or `reconciled` AND its deployment `terminated` with the
provider's evidence. Nothing happens from `terminating`, `degraded`, `termination_failed`, `orphan_suspected`,
`credentials_unavailable`, `provider_timeout`, `provisioning_uncertain`, `launch_unknown` or any state where the
machine's existence is unproven; the job simply waits (the chain block says "waiting for the provider to
confirm the GPU is gone").

**The successor** is a NEW job (new `job_id`) with `retry_of` (the failed attempt), `root_job_id` (attempt 1) and
`attempt` (2, 3): the root's template, config, resources, strategy, price cap, runtime and retry policy, the same
owner. It goes through the EXACT submit path a person's job takes (validate, workload-aware selection, quote,
`engine.route`, cost guards, approval): in SUPERVISED mode it waits in `pending_approval` for an admin like any
other job. Routing excludes every provider that interrupted or lost an attempt of the chain
(`routing.exclude_providers`, which any request may also set); nothing eligible left ends the chain.
`resume_from` is the failed attempt's latest verified checkpoint, else the newest one anywhere in the chain;
without one the successor starts from scratch and says so.

**Money.** Before submitting, spend so far = per attempt: once reconciled, the max of the transaction cost, the
provider-reported cost and the metered usage; before that, the max of the metered usage and the attempt's
`est_max_total_usd` (an upper bound). The successor's `limits.max_total_cost_usd` becomes
`min(original, max_total_usd - spend so far)` (it can only go down), and the selection's estimate must satisfy
`spend so far + est_max_total <= max_total_usd`; otherwise the chain stops with `retry_budget_exhausted` and
nothing is quoted. The per-job and account cost guards apply on top, unchanged. Costs are compute cost (the basis
of `max_total_cost_usd`); OpenGrid's fees are reported separately, as everywhere else.

**Owner.** The relaunch submits as the job's original principal, re-verified at that moment: the API key still
exists, is not revoked (nor any key above it), not expired, still holds `route:execute`, and the account is
active. Otherwise the chain stops (`owner_key_revoked`, `owner_key_expired`, `owner_scope_missing`,
`account_suspended`, `owner_unavailable`). A customer job never runs as the operator or with more scopes than its
key; the operator's own job relaunches as the operator.

**At most one successor.** Each decision is a `job_relaunches` row keyed by the failed attempt (PRIMARY KEY: one
worker claims it), the submission runs inside the normal idempotency store with the deterministic key
`relaunch:<failed job id>` (scope `jobs:relaunch`), and `jobs.retry_of` is UNIQUE: a second successor is refused
by the database (`relaunch_exists`) before anything is quoted. A crashed pass is resumed by the next one, which
finds the existing successor instead of submitting again.

**Chain stop reasons** (`chain.stopped.code`): `max_attempts_reached`, `retry_budget_exhausted`,
`no_eligible_provider`, the owner codes above, or the refusal code of the relaunch submission itself (for
example `price_changed`). A stop is final for that attempt: a person can always submit again with `resume_from`.
An internal error is not a stop: the decision is `blocked`, retried on the next pass, and the operator gets an
ops alert `relaunch_blocked` (nothing bills: the previous machine is confirmed gone).

**Visibility.** Jobs with a retry policy or in a chain carry `chain`: `attempt`, `max_attempts`,
`max_total_usd`, `spend_so_far_usd`, `root_job_id`, `previous_job_id`, `next_job_id`, `relaunched_after` (`code`,
`provider`, `resume_from_epoch`, `from_scratch`), `relaunch` (this attempt's decision: `relaunched` | `stopped` |
`blocked` | `claimed` | `pending` | `not_relaunched` with the reason) and `stopped`. Ordinary jobs have no `chain`
key. Events: the old job gets "relaunched as job-X on <provider> resuming from epoch N", the new one is created with
"attempt N of M, relaunched after <cause> on <provider>"; both, and every chain stop, are posted to the jobs feed.
The job page shows "Attempt 2 of 3 · relaunched after the GPU on Lambda was interrupted · resumed from epoch 2"
with links both ways; My Jobs shows a chain as its newest attempt (an older attempt that may still bill is never
hidden).

**Reliability.** `routing/reliability.py` reads deployments, not jobs: the interrupted attempt's deployment is
counted once (its own interruption) and the successor is a new deployment counted like any other launch.

## 9. Errors

HTTP 4xx/5xx with `detail = {code, message, ...}`: validation (`workloads.spec` codes, 422), `execution_disabled`,
`provider_unvalidated`, `provider_killed`, `credentials_unavailable`, `no_suitable_gpu`, `no_eligible_provider`,
`budget_exceeded` (422), `price_changed`, `gpu_unavailable`, `quote_expired` (409, with a hint to preview
again), `idempotency_key_required` (428), `idempotency_key_reused` (422), `idempotency_in_progress` (409),
`artifact_storage_unavailable` (503), runner `runner_token_*` (401/403), `job_finished` (409). In-job failures
are `failure.code`: `cuda_init_failed`, `bootstrap_failed`, `bootstrap_timeout`, `training_failed`,
`out_of_memory`, `training_timed_out`, `artifact_upload_failed`, `result_missing`, `metrics_missing`,
`gpu_unavailable`, `price_changed`, `approval_rejected`, `terminated_before_completion`, `gpu_interrupted`,
`runner_lost`; a relaunch submission may also be refused with `relaunch_exists` (409); an unconfirmed
termination shows as `resource.termination_unconfirmed` with `disposition = possibly_billing`.

## 10. Endpoints

| method | path | scope |
|---|---|---|
| GET | /v1/jobs/templates | data:read |
| GET | /v1/jobs/readiness | route:preview |
| POST | /v1/jobs/preview | route:preview |
| POST | /v1/jobs (Idempotency-Key) | route:execute |
| GET | /v1/jobs, /v1/jobs/{id}, /logs?after=&limit=, /metrics, /artifacts, /artifacts/{name} | deployments:read |
| POST | /v1/jobs/{id}/cancel (Idempotency-Key) | route:execute |
| POST/PUT/GET | /v1/jobs/{id}/runner/events, /runner/artifacts/{name}, /runner/complete, /runner/resume | job token only |

Keys see their own account's jobs; the operator sees all. Discord: job transitions are posted to the
deployments channel (`alerts/channels.py`, feed `jobs`), next to the deployment's own lifecycle posts.
