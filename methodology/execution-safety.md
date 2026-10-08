# Execution safety

How OpenGrid makes sure a route launches **at most one** paid instance, only when it is allowed to, only at
an approved price, and never loses track of it. Code: `routing/control.py`, `routing/quotes.py`,
`routing/idempotency.py`, `routing/guards.py`, `routing/deployments.py`, `routing/engine.py`,
`routing/credentials.py`, `routing/validation.py` (gate). Tables: migrations `0010_execution`, `0014_limits`.

## 1. Control plane: may we launch at all?

Two switches; the stricter wins.

| Switch | Where | Meaning |
|---|---|---|
| `ROUTING_LIVE_PROVISIONING` | env (deploy-level) | `false`: the effective mode is capped at `PREVIEW_ONLY`, whatever the database says |
| execution mode | `execution_controls` (admin API, reason required, logged) | `DISABLED` · `PREVIEW_ONLY` (default) · `SUPERVISED` · `LIVE` |

Per provider (`provider_execution_flags`; defaults: everything off): `adapter_status` `simulated` |
`validated`, `supervised_enabled`, `live_enabled`, `killed` (+ reason, who, when).

`launch_permission(provider, purpose)`:

| Situation | customer launch | validation launch |
|---|---|---|
| mode `DISABLED` or `PREVIEW_ONLY`, or provider killed | never | never |
| adapter `simulated` | **never** | allowed (SUPERVISED/LIVE mode, admin approval, validation caps) |
| `validated` + mode `LIVE` + `live_enabled` | `LIVE`: no per-launch approval (guards, quote re-validation, idempotency still apply) | admin approval |
| `validated` + `supervised_enabled` | `SUPERVISED`: an admin approves every launch | admin approval |
| `validated`, nothing enabled | never | admin approval |

`adapter_status` becomes `validated` only via `control.mark_validated()`, called after a complete,
provider-confirmed validation cycle (launched → observed running → termination confirmed → cost reconciled).
It never enables launches by itself. The admin API can only *demote* to `simulated`.

**Kill switches.** `POST /v1/admin/execution/kill` sets the mode to `DISABLED`; `…/providers/{p}/kill` blocks
one provider. Both stop NEW launches only. Status polling, reconciliation, stop and terminate are never gated
by the mode, the provider flags or account suspension.

## 2. Quotes

Every launch references a quote (`quotes`, id `q_…`): provider, listing, offer snapshot, GPU and count, region
and region group, `observed_price_per_gpu_hour` (the market observation), `quote_price_per_gpu_hour` (the live
check's price when there was one: `price_source = live_check`, else `observed`), estimated hourly and total
cost, itemized fees from the fee policy in force (billing), `taxes = null` ("unknown: taxes not computed"),
billing unit and minimum commitment from the adapter's capability matrix, `expires_at`
(`QUOTE_TTL_SECONDS`, default 300) and status `active | consumed | expired | superseded`.

At launch the quote is **re-validated** with a fresh live check through the adapter: expired → refused;
listing unavailable → refused; price moved more than `QUOTE_PRICE_TOLERANCE` (default 2%, either way) →
refused with a **new quote** for re-approval (the old one is superseded). A quote is consumed by exactly one
deployment (conditional update inside the launch transaction). Because a move inside the tolerance may be UP,
every price cap — the request's `max_price_per_gpu_hour` (approval and `POST /v1/route` with `quote_id`), the
account cost guards (`guards.gate`, at approval and again at → provisioning) and the validation $3.00/h cap —
is checked against the higher of the quote and the re-validated live price
(`provider_metadata.revalidated_price_per_gpu_hour`), never the stale quote alone.

## 3. Idempotency

`Idempotency-Key` is required on `POST /v1/route`, `POST /v1/route/{id}/approve`,
`POST /v1/deployments/{id}/terminate` and `/stop` (428 without it). Keys are unique per
(principal, operation, key) by a database constraint; the claim is a single `INSERT … ON CONFLICT DO NOTHING`.

| Repeat with… | Result |
|---|---|
| same key, same body, first finished | the stored response (same HTTP code), header `Idempotent-Replayed: true` |
| same key, different body | 422 `idempotency_key_reused` |
| same key while the first is running | 409 `idempotency_in_progress`, `Retry-After` |
| same key after a crash (or `in_progress` older than 10 min) | re-run (safe: see the launch token) — except `POST /v1/route` when a deployment was created for the principal since the key was claimed: 409 `idempotency_outcome_unknown` naming those deployments (re-running would create a second instance); use a new key |

Independently of keys, each deployment has a **launch token**: the provision call happens only after a row
lock, a check that the deployment is `approved` with no token, a fresh `launch_permission()` check (a kill switch pressed during approval wins: back to `pending_approval`), setting the token and committing — so at most
one provision call per deployment ever, including double approvals from a refreshed browser tab.

## 4. Deployment state machine

`transition()` is the only writer of `status`; every transition is a `deployment_events` row with actor
(`user | admin | system | reconciler`), reason and evidence. Ambiguity is never collapsed into failure.

| From | Allowed to |
|---|---|
| created | quoted, quote_failed, rejected |
| quoted | pending_approval, approved, quote_expired, quote_failed, rejected |
| pending_approval | approved, rejected, quote_expired, quote_failed |
| quote_expired | pending_approval, rejected, quote_failed |
| approved | provisioning, pending_approval (re-quote), rejected, provision_failed, quote_expired |
| provisioning | running, degraded, stopping, stopped, terminating, terminated, provision_failed, provider_rejected, provider_timeout, launch_unknown, orphan_suspected, credentials_unavailable |
| provider_timeout | launch_unknown, provisioning, running, degraded, stopped, terminating, terminated, provision_failed, orphan_suspected, credentials_unavailable |
| launch_unknown | provisioning, running, degraded, stopped, terminating, terminated, provision_failed, orphan_suspected, credentials_unavailable |
| running | degraded, stopping, stopped, terminating, terminated, orphan_suspected, credentials_unavailable |
| degraded | running, stopping, stopped, terminating, terminated, orphan_suspected, credentials_unavailable |
| stopping | stopped, running, degraded, terminating, terminated, credentials_unavailable, orphan_suspected |
| stopped | running, degraded, stopping, terminating, terminated, credentials_unavailable, orphan_suspected |
| terminating | terminated, termination_failed, orphan_suspected, credentials_unavailable |
| termination_failed | terminating, terminated, orphan_suspected, credentials_unavailable |
| orphan_suspected | running, degraded, stopped, terminating, terminated, credentials_unavailable |
| credentials_unavailable | provisioning, running, degraded, stopping, stopped, terminating, terminated, termination_failed, launch_unknown, orphan_suspected |
| provision_failed, provider_rejected | orphan_suspected (contrary evidence only) |
| quote_failed, rejected, **terminated** | — (absorbing) |

Meaning of the outcome states:

- `provision_failed` — the provider definitively created nothing (capacity / validation error, no instance).
- `provider_rejected` — refused for an account reason (auth, quota, rate limit).
- `provider_timeout` / `launch_unknown` — no usable answer (timeout, 5xx, connection reset after send,
  unparseable 2xx). The instance may exist. **No failover, no retry** until reconciliation resolves it by the
  instance name `og-<deployment_id>` (`adapter.find_instance`).
- `degraded` — the provider reports an error on an instance that may still exist (still tracked and billed).
- `credentials_unavailable` — the credential pinned at launch can no longer be used; state kept, operator alerted.

Rules: `running` only after the provider reports running. `terminated` only with provider evidence — a status
read saying terminated, or two consecutive `not_found` reads ≥ 60 s apart (reconciliation adds the instance
list). Stale or out-of-order reads (older than the last recorded check, or taken before terminate was requested)
are recorded and ignored.

The **write-ahead attempt**: before the provider call a `provision_attempts` row (deployment, provider, listing,
launch token, instance name, pinned credential ref, request summary; `outcome = provisioning`) is committed.
A crash between the provider accepting and OpenGrid recording it leaves that row; reconciliation finds the
instance by name.

**Failover** (LIVE only): only after a definitive rejection, as a NEW deployment with a new quote, at most
`ROUTING_MAX_ATTEMPTS` provision calls per route (default **1**: no failover). SUPERVISED launches never fail
over: the admin approved one provider at one price.

## 5. Cost guards, concurrency and runtime ceilings

`account_limits` (NULL = the settings default): max price per GPU-hour (default off), max hourly cost ($50 —
the account's concurrent burn: quote × GPUs of every active deployment + this one), max total cost (off), max
concurrent GPUs (8), max active deployments (2), provider and region allowlists, monthly spend ($2000; finished
actual cost + every active deployment's cost projected to its auto-terminate deadline + this launch's maximum
exposure, quote × GPUs × its runtime ceiling), `max_runtime_minutes`, `default_runtime_minutes`.
"Active" includes `approved` and the uncertain states (an unresolved `launch_unknown` may be billing).

**Atomic enforcement (multi-process safe).** The guards are checked when the quote is issued (informational)
and then enforced INSIDE the transaction that makes the state change, twice:

1. `pending_approval → approved` (admin approval, or a LIVE auto-approval in `deployments.create`), and
2. `approved → provisioning` (`deployments.launch`, the moment the provision call is committed to).

Each takes `pg_advisory_xact_lock(account)` — plus a global lock (one active validation deployment) and a
per-provider lock for validation launches, always in that order — counts active/uncertain deployments, GPUs,
hourly burn and the monthly projection in the same transaction, then refuses (`limits_exceeded`; the
deployment goes back to / stays `pending_approval` with `limit_violations`) or commits the state change. The
lock is released at commit, and only then is the provider called: no DB transaction is ever open across a
provider call. Postgres locks only — no Python lock is relied on for correctness. Proven by tests/test_limits.py:
8 threads approving 8 routes of one account with `max_active_deployments = 1` → exactly 1 provisioning and 7
`limits_exceeded` (same for `max_gpus`, hourly and monthly spend, one-active-validation, LIVE routes, and two
separate OS processes).

A violation never launches; an admin may approve with `override_limits: true` **and a reason** (recorded on
the deployment and in the control log). The override covers exactly the violation codes recorded at approval:
a new violation found at the provisioning gate still refuses.

**Runtime ceilings — never unlimited.** `effective = min(RUNTIME_HARD_MAX_MINUTES (1440), account
max_runtime_minutes, request max_runtime_minutes | account default_runtime_minutes | RUNTIME_DEFAULT_MINUTES
(60))`; validation ≤ `VALIDATION_MAX_RUNTIME_MINUTES` (30). A request above the effective maximum is clamped
with a note, never exceeded. Persisted on the deployment: `effective_max_runtime_minutes` (NOT NULL, CHECK > 0,
no default), `runtime_ceiling_source` (`request | account | system_default | system_hard_max | validation_cap`;
`legacy_backfill` for rows that predate migration 0014, which got `created_at + 60 min`). The route/quote
ticket shows the ceiling and `auto_terminate_at_if_launched_now` (and warns when `duration_hours` is longer);
approval sets `terminate_deadline_at = approval time + ceiling` and returns it; launch re-states it as launch
time + ceiling. Reconciliation terminates at the deadline.

Validation launches are additionally capped and **not overridable**: one 1-GPU instance at a time, ≤
`VALIDATION_MAX_PRICE_PER_HOUR` ($3.00/h total), runtime ≤ 30 min — and gated (section 7).

## 6. Credentials and SSH keys

Credentials are resolved for the adapter's `CREDENTIAL_PROVIDER` (Crusoe/Denvr/Latitude → Shadeform, so a
native Crusoe key is never sent to Shadeform): the account's active BYO credential, else OpenGrid-managed. A BYO
credential that does not decrypt **fails closed** (no fallback to OpenGrid's key). The credential is **pinned**
on the deployment at launch (`credential_source`, `credential_ref` = `byo:<id>` | `platform:<provider>`); status,
stop, terminate and reconciliation use exactly it. Adding, replacing or revoking a BYO key never changes which
provider account OpenGrid asks about an existing instance; a revoked pinned credential → `credentials_unavailable`
(never "terminated").

SSH — operator access is impossible by default on customer machines:

- Only the customer's explicitly supplied **public** key is installed (`launch.ssh_public_key`). It is
  validated (`ssh-ed25519`, `ecdsa-sha2-nistp256/384/521`, `ssh-rsa` ≥ 2048 bits; one line; no
  authorized_keys options or `command=` prefix; structure checked), and identified by its SHA-256 fingerprint,
  persisted as `deployments.ssh_key_fingerprint` and the only thing ever logged. Anything that looks like a
  private key, in any launch field, is discarded and refused (`ssh_private_key_rejected`) without being echoed,
  stored or logged.
- The adapter registers it for that deployment only, under `og-<deployment_id>` (`SSH_KEY_REGISTRATION =
  per_deployment`); two customers' launches can never carry each other's key. Key-name references are refused
  on OpenGrid-managed accounts (allowed with BYO credentials: the account is the customer's).
- The operator's default key (`routing_launch_defaults[...].ssh_key` / `.ssh_public_key`) is used ONLY for
  `purpose = validation` (`operator_access = validation_operator_key`), and is re-checked at the provisioning
  moment: a customer launch carrying it is refused.
- A provider whose adapter does not declare `CAPABILITIES.forces_account_ssh_key == "NO"` (YES or UNKNOWN /
  undeclared) may install the provider account's own keys: a customer launch on OpenGrid's account is held
  (`operator_access = blocked:provider_forced_account_key`, never auto-approved) until an admin approves with
  `allow_provider_account_keys: true` and a reason (recorded as
  `provider_forced_account_key:override_by:<who>` and in the control log), or rejects it.
- Quote/approval tickets and the deployment view expose `ssh_access: {customer_key_fingerprint: "SHA256:…",
  operator_access: "NONE" | …}`.

Lifecycle timestamps on every deployment: `requested_termination_at` (set whenever termination is requested,
including while the launch outcome is unresolved), `provider_created_at`, `provider_running_at`,
`provider_terminated_at`, `billable_start`, `billable_end`, `billable_basis` (filled by reconciliation /
billing).

## 7. Flows

- **SUPERVISED** — `POST /v1/route` → live checks (top `ROUTE_LIVE_CHECK_CANDIDATES`, per-call timeout
  `PROVIDER_CALL_TIMEOUT_SECONDS`), quote, guards → deployment `pending_approval`, HTTP 202 with the exact
  provider, region, GPU, price, estimated cost, fees and quote expiry. `POST /v1/route/{id}/approve`
  (admin; `quote_id`; optional `override_limits` + `reason`; Idempotency-Key) → re-validate → guards →
  `approved` → the one provision call. `POST /v1/route/{id}/reject` (admin, reason).
- **LIVE** — the same, but a validated, live-enabled provider launches without per-launch approval. A
  `quote_id` from preview may be passed to launch exactly that quote after re-validation.
- **Validation** — `POST /v1/admin/execution/validation {provider, listing_id?, reason}` (Idempotency-Key
  required) → a `purpose = validation` route, always pending approval, validation caps, operator default key
  allowed. **The validation launch gate** (`routing/validation.preconditions`, also
  `GET /v1/admin/execution/validation/preconditions?provider=`) is checked at start AND again at approval;
  every failed condition is returned with its reason (409 `validation_preconditions_failed`): provider in
  `VALIDATION_ALLOWED_PROVIDERS` (default `["lambda"]`); exactly 1 GPU; the cheapest acceptable on-demand
  1-GPU instance ≤ $3.00/h total; runtime ≤ 30 min with a hard deadline; no other active/uncertain validation
  deployment (advisory-locked again at approval); mode SUPERVISED and an admin; global kill switch functional
  (not engaged + a kill and un-kill within `VALIDATION_DRILL_WINDOW_DAYS`, 7); the provider's kill switch
  functional (not killed + a drill in the window); `reconcile` and `routing_tracker` jobs healthy (durable
  heartbeats in `execution_controls`: a success within 2× the interval, no consecutive failures) and the last
  reconciliation pass reconciled that provider without error; ops alerts healthy (`alerts.ops.channel_configured()`
  and a delivered test alert in the window: `POST /v1/admin/ops/test-alert`).
- **Terminate / stop** — idempotent, allowed for suspended accounts and in every mode. Terminate moves to
  `terminating` and calls the provider; the tracker/reconciler confirms `terminated`. A provider refusal →
  `termination_failed` (+ alert). Before launch, terminate cancels (→ `rejected`, no provider call).
  Admin: `GET /v1/admin/deployments?state=live`, `POST /v1/admin/deployments/{id}/terminate`.

Every provider call is logged on `opengrid.provider` with route_request_id, deployment_id, provider, op,
outcome/status, latency; never credentials, bodies or env values.

## Pinned credential fingerprint

A deployment pins *where* its credential comes from (`platform:<provider>` or a BYO credential row) and, from
launch, a one-way fingerprint of the exact secret used (`provider_metadata.credential_fingerprint`). Status,
terminate, reconciliation and SSH-key cleanup refuse to act with any other secret: a different key can belong
to a different provider account, where the instance is invisible, and an empty list plus a 404 from the wrong
account would otherwise read as proof of termination while the real instance keeps billing.

Consequence for operations: **do not rotate a provider key while OpenGrid has live deployments on it.** If you
must, the affected deployments move to `credentials_unavailable` with an ops alert (never `terminated`).
Restore the old key, or terminate the instances in the provider console and let reconciliation confirm. A
"re-pin to the new key" action does not exist yet. Deployments launched before fingerprints existed are not
checked.
