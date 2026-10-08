# Provider capabilities and validation

**Every execution adapter is SIMULATED.** Each was built from the provider's published API reference and
tested only against mocked HTTP (`tests/test_routing_adapters.py`, httpx.MockTransport with the documented
request/response shapes). None has made a real authenticated call. A provider becomes `validated` only through
the recorded validation cycle below, stored in `provider_execution_flags` — never by editing code.

The full matrix (the founder's rows, each with its evidence URL) lives on each adapter as `CAPABILITIES`
(`routing/adapters/<provider>.py`) and is served by `routing/capabilities.py` (`capability(p)["matrix"]`),
together with `validation_status` (code: always SIMULATED) and `adapter_status` (DB: simulated | validated).

## Summary (docs fetched 2026-10)

| Provider | Launch errors | Name/tag at launch | List / find | Stop | Stopped billing | Billing unit | Provider cost API |
|---|---|---|---|---|---|---|---|
| Lambda (cloud.lambda.ai) | good: `error.code` (insufficient-capacity, quota-exceeded) | name + tags | GET /instances; client match | none | n/a | per minute | none |
| RunPod | weak: create documents 201/400 only | name only | GET /pods?name= | yes | storage only | per second | /billing/pods |
| Hyperstack | medium: capacity undocumented | name + labels | ?search= + paging | disabled (SHUTOFF bills all) | full | per minute | billing history per VM |
| DigitalOcean | good: {id, message}; 422 text undocumented | name (hostname chars) + tags | ?tag_name= + paging | disabled (powered-off bills) | full | per second (60 s min) | none used |
| Crusoe / Denvr / Latitude via Shadeform | poor: only 200 documented | name + tags | GET /instances (non-deleted) | none | n/a | per second | cost_estimate (unit unverified) |
| Vast.ai | good: 410 no_such_ask, 400 invalid_args | label | /api/v1/instances, next_token | yes | storage only | per second | /api/v0/charges |
| Verda | good: {code}; 503 service_unavailable = no capacity | hostname + tags | ?tag=opengrid=<name> | disabled (shutdown bills) | full | 10 min prepaid, refunded | none |

No provider offers an idempotency token, which is why the launch-outcome rules below and name-based
reconciliation exist.

## Launch outcome rules (all adapters)

- request never sent (connect error, pre-create step such as token fetch or SSH key registration) -> `rejected`
- 2xx with an instance id -> `accepted`; 2xx without one or unparseable -> `unknown`
- 4xx 400/401/402/403/404/405/410/413/415/422/429 -> `rejected` (definitively nothing created); 408/409/other -> `unknown`
- any 5xx, read timeout, reset after send -> `unknown` — except a provider-DOCUMENTED capacity code
  (Verda 503 with `{"code":"service_unavailable"}`)
- `error_kind = capacity` only from documented codes (Lambda error.code, Vast 410/no_such_ask, Verda 503 code);
  never from a substring of a body
- terminate: `accepted` (confirm by status/list) | `already_gone` (404) | `failed` (4xx; Hyperstack "being
  created" is retryable) | `unknown` (5xx, timeout)
- secrets never appear in messages, raw bodies or logs (masked keys, echoed credentials scrubbed)

Instance names are `og-<deployment id>`, `[a-z0-9-]` only; a name that would need truncating is refused.
Crusoe, Denvr and Latitude authenticate ONLY with a Shadeform credential (`CREDENTIAL_PROVIDER = "shadeform"`).

## Validation (simulated -> validated)

Code: `routing/validation.py`. Caps come from the core: one instance, total price <=
`validation_max_price_per_hour` ($3.00), runtime <= `validation_max_runtime_minutes` (30), auto-terminate
deadline, admin approval, execution mode SUPERVISED or LIVE, provider not killed.

Manual procedure (operator). The exact HTTP calls, bodies, headers and UI clicks for the first Lambda run are in
methodology/first-live-route.md ("The supervised Lambda validation run"); the Python names below are what they call.

1. Configure the provider credential (OpenGrid-managed) and `ROUTING_LAUNCH_DEFAULTS[provider]` with the
   operator SSH key (`ssh_public_key`, registered per deployment; Lambda needs no image) (and Hyperstack
   environments / DigitalOcean images).
2. Set the execution mode to SUPERVISED (and `ROUTING_LIVE_PROVISIONING=true` in that one environment).
3. `POST /v1/admin/validation/start {"provider": "lambda"}` (`validation.start_validation`) — picks the cheapest 1-GPU on-demand listing within
   the cap and creates a validation route through `engine.create_validation_route`. It does NOT launch.
4. Approve the route through the normal admin approval, `POST /v1/route/{id}/approve {"quote_id": ...}` (quote
   re-validation applies; the $3.00/h cap is checked against the re-validated live price). Keep the provider console
   open.
5. Watch: the tracker observes `running` and, for validation deployments, checks `find_instance(og-name)` and
   `list_instances()` while it runs.
6. Terminate (or let the 30-minute deadline do it). The reconciler confirms termination by two signals and
   cost reconciliation runs.
7. `POST /v1/admin/validation/{deployment_id}/mark` (`validation.validation_report(deployment_id, by=...)`). It checks: launch accepted with an instance
   id; observed running; find_instance and list_instances saw it; terminate accepted; termination confirmed by two
   signals (and a fresh list without it); cost reconciled (transaction cost, provider cost or the reason it is
   unavailable). Only if every step passes does it call `control.mark_validated(...)`. Any missing step ->
   `{"validated": false, "missing": [...]}`. Validation never enables supervised or live customer launches;
   those remain separate operator decisions.
8. Compare the provider's invoice line with `deployments.reconciliation` and note differences.

## SSH keys: forced account keys and per-deployment registration

`CAPABILITIES.forces_account_ssh_key` answers: does the provider ALWAYS install an account-level/default key on
a launch, whatever the request names? The core refuses customer launches unless it is `NO` (or an admin
override is recorded in `deployments.operator_access`). Adapters send only the key the core passed (a
reference) or the per-deployment key they register as `og-<deployment>`; never an account default key.
`SSH_KEY_REGISTRATION` is `per_deployment` | `account_only` | `none` (`results.KeyRegistration`, falsy for
`none`); `SSH_KEY_RESOURCE` says registration creates a provider-side key object that OpenGrid records and
deletes after confirmed termination (`routing/adapters/resources.py`, methodology/reconciliation.md).

| Provider | forces_account_ssh_key | Evidence (fetched 2026-10-07) | Registration / key object |
|---|---|---|---|
| Lambda | NO | launch `ssh_key_names`: "Currently, exactly one SSH key must be specified"; the Instance reports `ssh_key_names` ("The names of the SSH keys that are allowed to access the instance"); no default-key injection in the OpenAPI spec. Runtime check: the tracker alerts `unexpected_ssh_key` if an instance lists any other key | per_deployment; POST / GET / DELETE /ssh-keys |
| RunPod | YES | "Runpod will attempt to automatically inject the public SSH keys added in your account settings"; SSH_PUBLIC_KEY overrides "the default" but replacement is not stated | per-pod env, no key object |
| Vast.ai | YES | "Adding a key to your account keys only applies to new instances" (account keys go on every new instance) | per-instance attach, no key object |
| Shadeform (Crusoe / Denvr / Latitude) | UNKNOWN | the account has a default key used when none is given; whether it is also installed when one is given is not documented | per_deployment; /sshkeys/add, /sshkeys, /sshkeys/{id}/delete (unvalidated) |
| DigitalOcean | UNKNOWN | the API reference could not be re-fetched to confirm the absence of default-key injection | per_deployment; /v2/account/keys (unvalidated) |
| Hyperstack | UNKNOWN | docs page moved; not re-verified | per_deployment; /core/keypairs, /core/keypair/{id} (unvalidated) |
| Verda | UNKNOWN | the spec mentions no default-key injection; not validated | per_deployment; /v1/ssh-keys (unvalidated) |

`CAPABILITIES.billing_starts` (running | created | UNKNOWN) feeds the billable window: Lambda `running`
("Billing begins the moment you launch an instance and the instance passes health checks", docs.lambda.ai
public-cloud/billing); every other adapter UNKNOWN.
