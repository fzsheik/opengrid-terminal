# Execution safety

How OpenGrid makes sure a route launches **at most one** paid instance, only when it is allowed to, only at
an approved price, and never loses track of it. Code: `routing/control.py`, `routing/quotes.py`,
`routing/idempotency.py`, `routing/guards.py`, `routing/deployments.py`, `routing/engine.py`,
`routing/credentials.py`. Tables: migration `0010_execution`.

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
deployment (conditional update inside the launch transaction).

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

## 5. Cost guards

`account_limits` (NULL = the settings default): max price per GPU-hour (default off), max hourly cost ($50),
max total cost (off), max concurrent GPUs (8), max active deployments (2), provider and region allowlists,
monthly spend ($2000; finished actual cost + estimated cost so far of active deployments + this launch).
"Active" includes uncertain states (an unresolved `launch_unknown` may be billing). Checked when the quote is
issued and again immediately before launch. A violation never launches: the route stays `pending_approval`
with `limit_violations`; an admin may approve with `override_limits: true` **and a reason** (recorded on the
deployment and in the control log).

Validation launches are additionally capped and **not overridable**: one instance at a time, ≤
`VALIDATION_MAX_PRICE_PER_HOUR` ($3.00/h total), max runtime ≤ `VALIDATION_MAX_RUNTIME_MINUTES` (30, an
auto-terminate deadline).

## 6. Credentials and SSH keys

Credentials are resolved for the adapter's `CREDENTIAL_PROVIDER` (Crusoe/Denvr/Latitude → Shadeform, so a
native Crusoe key is never sent to Shadeform): the account's active BYO credential, else OpenGrid-managed. A BYO
credential that does not decrypt **fails closed** (no fallback to OpenGrid's key). The credential is **pinned**
on the deployment at launch (`credential_source`, `credential_ref` = `byo:<id>` | `platform:<provider>`); status,
stop, terminate and reconciliation use exactly it. Adding, replacing or revoking a BYO key never changes which
provider account OpenGrid asks about an existing instance; a revoked pinned credential → `credentials_unavailable`
(never "terminated").

SSH: on OpenGrid-managed provider accounts a customer may not reference key names (they would name keys in
OpenGrid's account). The customer sends `launch.ssh_public_key`; the adapter registers it for that deployment
(`SSH_KEY_REGISTRATION`), else the provider is skipped. Key names are fine with BYO credentials. The operator's
default key (`routing_launch_defaults[...].ssh_key` or `.ssh_public_key`) is used only for validation launches; a customer launch without its own key is refused (`ssh_key` missing), never given the operator's key.

## 7. Flows

- **SUPERVISED** — `POST /v1/route` → live checks (top `ROUTE_LIVE_CHECK_CANDIDATES`, per-call timeout
  `PROVIDER_CALL_TIMEOUT_SECONDS`), quote, guards → deployment `pending_approval`, HTTP 202 with the exact
  provider, region, GPU, price, estimated cost, fees and quote expiry. `POST /v1/route/{id}/approve`
  (admin; `quote_id`; optional `override_limits` + `reason`; Idempotency-Key) → re-validate → guards →
  `approved` → the one provision call. `POST /v1/route/{id}/reject` (admin, reason).
- **LIVE** — the same, but a validated, live-enabled provider launches without per-launch approval. A
  `quote_id` from preview may be passed to launch exactly that quote after re-validation.
- **Validation** — `POST /v1/admin/execution/validation {provider, listing_id?, reason}` → a `purpose =
  validation` route, always pending approval, validation caps, operator default key allowed.
- **Terminate / stop** — idempotent, allowed for suspended accounts and in every mode. Terminate moves to
  `terminating` and calls the provider; the tracker/reconciler confirms `terminated`. A provider refusal →
  `termination_failed` (+ alert). Before launch, terminate cancels (→ `rejected`, no provider call).
  Admin: `GET /v1/admin/deployments?state=live`, `POST /v1/admin/deployments/{id}/terminate`.

Every provider call is logged on `opengrid.provider` with route_request_id, deployment_id, provider, op,
outcome/status, latency; never credentials, bodies or env values.
