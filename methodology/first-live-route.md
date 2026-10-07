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
3. Set mode SUPERVISED and enable the provider for validation. Run one validation deployment end to end:
   launch, observe running, terminate, cost reconciliation. This turns 9 and 12 green.
4. Drill the kill switch (10). Confirm the reconciler is healthy (11), logs are JSON (13), and alerts
   reach a human (14).
5. Enable `supervised_enabled` for customer launches (8). The partner previews, OpenGrid quotes, and the
   operator approves the unexpired quote (15). Then launch.
6. Watch the deployment through `GET /v1/admin/trace/{deployment_id}`. After termination, ask the partner
   for feedback (`POST /v1/deployments/{id}/feedback`).
