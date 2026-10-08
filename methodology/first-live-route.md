# First live route checklist

Before OpenGrid routes the first real design-partner workload on a provider, every required item on this
checklist must be green. The checklist is computed live from the running system, never ticked by hand.

API: `GET /v1/admin/checklist?provider=<p>[&route_request_id=rr_…][&account_id=…][&probe=false]` (admin).
Code: `routing/checklist.py`. Each item is **green**, **red** or **unknown** and carries the evidence it
was judged on, plus a `fix` when it is not green. The overall status is green only when every required
item is green. Unknown is not green: if the system cannot prove an item, it is not ready.

| # | item | green when | how to make it green |
|---|---|---|---|
| 1 | `api_key_pepper` | `API_KEY_PEPPER` is set (at least 32 characters); unset means a pepper derived from `DATABASE_URL` (dev only) | set a long random secret; it must never change afterwards |
| 2 | `credentials_encryption_key` | `CREDENTIALS_ENCRYPTION_KEY` parses as a real Fernet key (not a passphrase, not unset) | `Fernet.generate_key()`; rotate stored secrets with `admin.py rotate-credentials` |
| 3 | `migrations_at_head` | the database's Alembic revision equals the code's head | restart (startup migrates) or `alembic upgrade head` |
| 4 | `provider_credential` | an OpenGrid-managed credential for the provider exists **and** a read-only call (`list_instances()`) succeeds now | set the provider key; check its permissions. `probe=false` skips the call (unknown) |
| 5 | `ssh_key` | an SSH public key (or registered key) is configured for validation deployments in `ROUTING_LAUNCH_DEFAULTS[provider]` | add `ssh_public_key` |
| 6 | `launch_params` | every launch field the adapter requires (`REQUIRED_LAUNCH`, e.g. image, environment) is in the launch defaults | add the missing keys |
| 7 | `spend_limits` | the account has an `account_limits` row, or the global default limits apply | set account limits (admin) |
| 8 | `execution_mode` | `ROUTING_LIVE_PROVISIONING=true`, effective mode **SUPERVISED** (not LIVE: the first route is supervised), the provider's `supervised_enabled` is on and it is not killed | set the mode / provider flag through the admin control API, with a reason |
| 9 | `provider_validated` | the provider's adapter status is `validated`: a recorded validation cycle (launched → observed running → terminated with provider confirmation → cost reconciled) | run a validation deployment |
| 10 | `kill_switch_tested` | `execution_control_log` shows a kill (global or this provider) followed by an un-kill within the last 7 days | kill, confirm a preview still works and a launch is refused, restore |
| 11 | `reconciliation_running` | the reconciliation job ran within 2× its interval without error, and the latest `reconciliation_runs` row is recent and `ok` | jobs must run in this process (`OPENGRID_NO_JOBS` unset); fix the provider errors the run shows |
| 12 | `termination_tested` | a validation deployment on this provider reached `terminated` with provider confirmation evidence recorded on the event | terminate the validation deployment; the reconciler confirms it gone |
| 13 | `logging_active` | the request-id middleware is installed and logs are JSON lines | `LOG_JSON=true` (the default when deployed) |
| 14 | `alerts_active` | an outbound ops alert channel is configured for orphans and termination failures | configure the ops alert webhook. Until then this item is unknown; the alerts surface only in the admin API and in `EXECUTION_ALERT` error log lines |
| 15 | `quote_approved` | for the given `route_request_id`: a quote exists, a deployment of that route was approved against it, and the quote is unexpired (or was consumed by that deployment) | the operator approves the quote before it expires (default 5 minutes); an expired quote is re-quoted and re-approved |

## Order of operations for the first route

1. Make items 1–3 green (deploy configuration).
2. Configure the provider (items 4–6) and limits (7).
3. Set mode SUPERVISED. A validation launch needs NO provider flag (`supervised_enabled` is for customer
   launches): only the validation gate below. Run one validation deployment end to end — launch, observe
   running, terminate, cost reconciliation — with the exact procedure in the next section. This turns 9 and 12
   green.
4. Drill the kill switch (10). Confirm the reconciler is healthy (11), logs are JSON (13), and alerts
   reach a human (14).
5. Enable `supervised_enabled` for customer launches (8). The partner previews, OpenGrid quotes, and the
   operator approves the unexpired quote (15). Then launch.
6. Watch the deployment through `GET /v1/admin/trace/{deployment_id}`. After termination, ask the partner
   for feedback (`POST /v1/deployments/{id}/feedback`).

## The supervised Lambda validation run (exact procedure)

Verified against the code (`api/routing.py`, `api/reconcile.py`, `routing/engine.py`, `routing/validation.py`)
by the final red-team review. One instance, one GPU, at most $3.00/h total (checked against the LIVE price at
start AND at approval), auto-terminated 30 minutes after launch.

**Before you start (deploy configuration, one OpenGrid process, `--workers 1`, jobs on):**
`ROUTING_LIVE_PROVISIONING=true`, `LAMBDA_API_KEY=<key>`,
`ROUTING_LAUNCH_DEFAULTS={"lambda": {"ssh_public_key": "ssh-ed25519 AAAA… ops@opengrid"}}` (a PUBLIC key; it
is registered as `og-<deployment>` and deleted after termination), `OPS_ALERT_WEBHOOK_URL` +
`OPS_ALERT_WEBHOOK_SECRET`, `APP_PASSWORD`, `API_KEY_PEPPER`, `CREDENTIALS_ENCRYPTION_KEY`.
`VALIDATION_ALLOWED_PROVIDERS` defaults to `["lambda"]`. `OPENGRID_NO_JOBS` must be unset. No other OpenGrid
environment (staging, a laptop, a restored DB copy) may use the same `LAMBDA_API_KEY` during the run. Do not
redeploy during the run. Keep the Lambda console open.

**Authentication.** Use the operator login (HTTP Basic `APP_USER:APP_PASSWORD`). Every POST must carry
`X-OpenGrid-Request: 1` (CSRF guard) and, with a body, `Content-Type: application/json`. Every money-moving
POST below carries an `Idempotency-Key` (a fresh UUID per intent; reuse it ONLY to retry the same request after
a network error — it then replays instead of acting twice; a different body with the same key is a 422).
(A platform-admin API key `Authorization: Bearer opg_…` can approve and force-terminate, but `GET
/v1/deployments/{id}` 404s for it on the operator's validation deployment: use the operator login.)

```sh
B=https://<host>; A=(-u "opengrid:$APP_PASSWORD" -H 'X-OpenGrid-Request: 1' -H 'Content-Type: application/json')
```

| # | Step | API call | UI (`/admin/execution` unless noted) |
|---|---|---|---|
| 1 | Mode SUPERVISED | `curl "${A[@]}" -X POST $B/v1/admin/execution/mode -d '{"mode":"SUPERVISED","reason":"lambda validation"}'` → `data.effective_mode == "SUPERVISED"` (else the env ceiling is off) | Execution mode → SUPERVISED → reason |
| 2 | Global kill drill | `curl "${A[@]}" -X POST $B/v1/admin/execution/kill -d '{"reason":"drill before lambda validation"}'` then step 1 again (`"reason":"drill done"`) | STOP ALL LIVE PROVISIONING → type STOP + reason; then Execution mode → SUPERVISED |
| 3 | Provider kill drill | `curl "${A[@]}" -X POST $B/v1/admin/execution/providers/lambda/kill -d '{"reason":"drill"}'` then `…/providers/lambda/unkill -d '{"reason":"drill done"}'` | Providers → Lambda → Kill, then Unkill (reason each) |
| 4 | Ops test alert | `curl "${A[@]}" -X POST $B/v1/admin/ops/test-alert -d '{"reason":"validation precondition"}'` → `data.delivered == true`, and confirm a human received it | Validation preconditions → Send test ops alert |
| 5 | Gate green | `curl "${A[@]}" "$B/v1/admin/execution/validation/preconditions?provider=lambda"` → `data.ok == true` (all 12 checks; the reconcile job starts 90 s after boot and must have run within 240 s, the tracker within 120 s) | Validation preconditions → Re-check until every dot is green |
| 6 | Create the validation route (does NOT launch) | `curl "${A[@]}" -X POST $B/v1/admin/validation/start -H "Idempotency-Key: $(uuidgen)" -d '{"provider":"lambda"}'` → 202; record `data.route_request_id`, `data.deployment_id`, `data.quote_id`, `data.listing` (price ≤ $3.00/h). The instance will be named `og-<deployment_id>`. (Equivalent: `POST /v1/admin/execution/validation` with `{"provider":"lambda","listing_id":"<1-GPU on-demand listing>","reason":"…"}`; without `listing_id` it takes the cheapest listing of ANY size and the gate may refuse it.) | Validate provider… → Create validation route |
| 7 | Approve (within 5 min: the quote TTL) | `curl "${A[@]}" -X POST $B/v1/route/<rr>/approve -H "Idempotency-Key: $(uuidgen)" -d '{"quote_id":"<q>","reason":"supervised lambda validation"}'` → 200 `data.status == "provisioned"`, `data.launch.outcome == "accepted"`, `data.launch.instance_id`, `data.auto_termination.terminate_deadline_at`. 409 `quote_expired` / `quote_invalid` → nothing launched; approve `detail.new_quote.quote_id` with a NEW Idempotency-Key. 409 `validation_preconditions_failed` / 422 `over_max_price` → nothing launched. 202 (outcome unknown) → do NOT approve again; reconciliation resolves it by name | Approve on ticket → (on `/route?rr=…`) Approve & launch… → type `lambda` → Approve & launch |
| 8 | Watch it run | `curl "${A[@]}" "$B/v1/deployments/<dep>?refresh=true"` until `data.status == "running"`; then `curl "${A[@]}" $B/v1/admin/validation/<dep>` until `data.steps.find_instance.ok` and `data.steps.list_instances.ok` are true (the tracker checks within ~60 s of running). Cross-check the Lambda console: exactly one `og-<dep>` instance, key `og-<dep>` | `/deployments/<dep>`: state, auto-terminate countdown, Validation evidence |
| 9 | Terminate (or let the deadline do it) | `curl "${A[@]}" -X POST $B/v1/deployments/<dep>/terminate -H "Idempotency-Key: $(uuidgen)"` → 202 `data.terminate.outcome == "accepted"`. Admin alternative: `POST /v1/admin/deployments/<dep>/terminate -d '{"reason":"…"}'` | `/deployments/<dep>` → Terminate… → tick the acknowledgement → Terminate |
| 10 | Confirmation + cost | optional `curl "${A[@]}" -X POST "$B/v1/admin/reconcile/run?provider=lambda"`; repeat until `GET /v1/deployments/<dep>` shows `status == "terminated"` and `reconciled_at` is set | – |
| 11 | Mark validated | `curl "${A[@]}" $B/v1/admin/validation/<dep>` → `data.missing == []`, then `curl "${A[@]}" -X POST $B/v1/admin/validation/<dep>/mark -H "Idempotency-Key: $(uuidgen)"` → `data.validated == true` | Validation → click the deployment → Re-check & mark validated |
| 12 | Leftovers | `GET /v1/admin/resources?type=ssh_key` (no leftover), `GET /v1/admin/orphans?status=open` (empty), `GET /v1/admin/exposures` (nothing for `<dep>`); Lambda console: no `og-*` instance, no `og-*` key; compare the Lambda invoice line with `data.reconciliation` | Execution: orphans / resources panels |

**Abort rule (the human bound on exposure).** If OpenGrid is unreachable for more than 5 minutes after the
approval, if the approval answered 202 (outcome unknown) and no instance is adopted within 15 minutes, or if the
deployment is not `terminated` by `terminate_deadline_at` + 5 minutes: terminate `og-<deployment_id>` in the
Lambda console, delete the `og-<deployment_id>` SSH key there, then (once OpenGrid answers)
`POST /v1/admin/execution/kill`. OpenGrid reconciles on recovery (two provider signals → `terminated`, billed to
the first confirmed-gone observation, key cleanup).

**Maximum exposure** (code caps: ≤ $3.00/h; deadline = launch + 30 min; deadline enforced by the first
reconciliation pass after it, every 120 s, the deadline pass first; a failed/unknown terminate re-issued every
pass; Lambda bills per minute from first health check to the terminate request): ≤ 33 billed minutes ≈ **$1.65**
when OpenGrid stays up or restarts within the 30 minutes; with a crash right after Lambda accepted the launch
and an outage of R > 30 minutes, the first pass after restart (90 s after boot) adopts and terminates it:
≈ $3.00/h × (R + ~2.5 min); with the abort rule above ≤ ~37 minutes ≈ **$1.85**.
