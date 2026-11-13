# Adapter contract audit (2026-11-13)

Every call each execution adapter makes was compared with the provider's current official API reference on
2026-11-13, without any authenticated or billable call. Concrete, documented mismatches were fixed in the
adapter and its tests. Where the docs are ambiguous, behaviour was left unchanged and the question is listed
under **Watch during the live launch**. The exact request each adapter now sends for quote, launch, status,
terminate and list is pinned in `tests/test_adapter_contracts.py`. If a live call disagrees with that file,
the provider changed something, or the docs were wrong.

Status per call: **matches** = same method, path, auth, field names and types, and response fields read ·
**fixed** = changed in this audit · **open** = docs ambiguous, behaviour unchanged.

Every adapter is still `SIMULATED`. Nothing here changes validation, adapter status or a safety rule: one
provision call per deployment, write-ahead attempts, no failover on an ambiguous outcome, capacity only from
documented codes, cost guards and quote revalidation are untouched.

## Lambda Cloud

Docs: <https://cloud.lambda.com/api/v1/openapi.json> (v1.10.0; byte-identical at cloud.lambda.ai),
<https://docs.lambda.com/api/cloud>, <https://docs.lambda.com/public-cloud/firewalls/>.

| Call | Status |
|---|---|
| Host | **fixed**: the spec's `servers` now list `cloud.lambda.com` as "Production server". `cloud.lambda.ai`, the host we used, is "Secondary production server (deprecated)". `BASE_URL` is now `https://cloud.lambda.com/api/v1`. |
| Auth `Authorization: Bearer` | matches (basic auth is kept only "for backward compatibility") |
| `GET /instance-types` | matches (`data{type: {instance_type{price_cents_per_hour, specs.gpus}, regions_with_capacity_available[{name}]}}`) |
| `POST /ssh-keys`, `GET /ssh-keys`, `DELETE /ssh-keys/{id}` | matches (name 1–64; `public_key` always sent, since omitting it generates a key pair) |
| `POST /instance-operations/launch` | matches. Required fields are only `region_name`, `instance_type_name` and `ssh_key_names` (exactly one). Tag key `opengrid` fits `^[a-z][a-z0-9-:]+$`. `user_data` is plain text, ≤ 1 MB |
| `GET /instances/{id}` | matches. The status enum is exactly booting / active / unhealthy / terminating / terminated / preempted, and `first_healthy` is still the only timestamp |
| `POST /instance-operations/terminate` | matches (`data.terminated_instances[]`; 404 `global/object-does-not-exist`) |
| `GET /instances` | matches: no `page_size` means every instance in one response, oldest first |
| Errors | matches. 429 is `global/rate-limited` with `Retry-After`, and a new 403 is `global/account-inactive` / `global/invalid-address`. Both are rejected via the HTTP status |

**Watch during the live launch**
- `GET /instances` is summarised as "List running instances", and the spec does not say whether terminated or preempted instances are listed. After terminate, termination is confirmed by `GET /instances/{id}` and by absence from the list. Note whether the terminated instance still appears in the list.
- The rate limit is 1 request/s, and launch is limited to 1 per 12 s. Do not click Approve twice.
- Out of scope but noted: the market-data poller (`providers/lambda_labs.py`) and probe target still use `cloud.lambdalabs.com` ("Tertiary ... deprecated").

**Account setup:** an API key; an account in good standing (no `account-inactive`, a valid billing address, a payment method). Default firewall: "Lambda allows only incoming ICMP traffic or TCP traffic on port 22 (SSH)", so SSH works with no rule change. The per-deployment SSH key is registered by the adapter.

## RunPod (REST v2)

Docs: <https://api.runpod.io/v2/openapi.json> (2.0.0), <https://docs.runpod.io/api-reference-v2/migrate-from-v1>,
<https://rest.runpod.io/v1/openapi.json>, <https://docs.runpod.io/pods/configuration/use-ssh>,
<https://docs.runpod.io/sdks/graphql/manage-pods>, <https://docs.runpod.io/release-notes>.

**fixed (whole adapter):** "REST API v1 is deprecated and will be retired on November 15, 2026" (migration
guide), and GraphQL "will be retired in early 2027". Every call the adapter makes moved to v2:

| Call | Before (v1 / GraphQL) | Now (v2) |
|---|---|---|
| Quote / stock | GraphQL `gpuTypes.lowestPrice{uninterruptablePrice, stockStatus}` (pod total) | `GET /v2/catalog/gpus?include=AVAILABILITY&product=POD&count=N&cloud=SECURE`. `price.secure` is per **single GPU**; `availability` is NONE / LOW / MEDIUM / HIGH |
| Launch | `POST /pods {imageName, gpuTypeIds, gpuCount, cloudType, computeType, interruptible, volumeInGb, containerDiskInGb, env}` | `POST /v2/pods {name, image, gpu{id, count}, cloud, ports:["22/tcp"], disk?, env{PUBLIC_KEY,…}, mounts.persistent{size ≥ 10, path:/workspace}?}`. The body is strict (`unevaluatedProperties: false`), so the v1 names would get 422 |
| SSH key | env `SSH_PUBLIC_KEY` | env `PUBLIC_KEY` (the v2 convention). `ports` must include `22/tcp` because v2 has no default ports. `startSsh` is never sent, since it would inject the account's keys |
| Status | `GET /pods/{id}` `desiredStatus` (a target state) | `GET /v2/pods/{id}` `status` PROVISIONING / STARTING / RUNNING / EXITED / ERROR / TERMINATED, which is the actual state. ERROR is tracked as error, `cost` is USD/h, the IP is `ssh.direct.host` |
| Stop | `POST /pods/{id}/stop` | `POST /v2/pods/{id}/action {"action":"stop"}` |
| Terminate | `DELETE /pods/{id}` | `DELETE /v2/pods/{id}` → 204; a 404 is now documented (already_gone) |
| List / find | `GET /pods?name=` (bare array) | `GET /v2/pods?limit=1000&cursor=` → `{pods, pagination{nextCursor, hasNextPage}}`. There is no name filter, so matching happens client-side over every page, and a list that is not complete is an error |
| Cost | `GET /billing/pods?…&grouping=podId` `[{amount}]` | `GET /v2/billing/pods?podId&startTime&endTime&bucketSize=hour` → `records[].totalAmount` |
| Errors | 201/400 only | RFC 9457 `{title, status, detail, errors[]}`. 400 = a rule violation **or** "could not be placed" (no capacity code), so it is a rejection but never labelled capacity. 402 = balance (rejected, quota). 422 = contract. 429 has `Retry-After`. A 202 QueuedPod is never adopted as a pod (we never send `queue`) |

`forces_account_ssh_key` moved from YES to **UNKNOWN**. The gate is unchanged: anything but NO still holds customer launches for an admin override.

**Watch during the live launch**
- **SSH:** with `env.PUBLIC_KEY` set and `startSsh` omitted, confirm (a) that sshd starts in the RunPod official image, (b) that `ssh.direct` gets a host and port, and (c) that `~/.ssh/authorized_keys` holds **only** the per-deployment key and no account key. Only (c) can move `forces_account_ssh_key` to NO.
- The quote is `price.secure × count`. Compare it with the created pod's `cost`.
- A 400 on create carries only a human-readable `detail`. Record its text if it happens: it is the only signal for capacity.
- Not documented: whether pod ids created through v1 work on v2. None exist, but check that no older v1 pod is on the account.
- Not documented: how fast the billing records for a short pod appear. `reported_cost` reports "no records yet" until they do.
- Out of scope: the market-data poller still reads GraphQL stock/prices, which retires in early 2027.

**Account setup:** an API key with pod read/write and billing read scopes (403 otherwise); at least one hour of credit (402 otherwise); a container image that runs sshd from `PUBLIC_KEY` (RunPod official images do).

## Hyperstack

Docs: <https://docs.hyperstack.cloud/openapi/hyperstack.json> (server `https://infrahub-api.nexgencloud.com/v1`),
docs.hyperstack.cloud `api-reference/{introduction,pagination,rate-limits,errors}`, `billing/{states-and-billing,pricebook}`,
`virtual-machines/{manage-vm-state,initialization-configuration,images}`, `network/firewalls`, `resource-management/environments`.

| Call | Status |
|---|---|
| Auth header `api_key: <key>` | matches |
| `GET /core/flavors?region=` | matches (`data[].flavors[]{name, region_name, gpu, gpu_count, stock_available}`) |
| `GET /pricebook` | matches (a bare array; `value` per GPU-hour; a string or number is accepted) |
| `POST /core/keypairs` | matches. **fixed**: the response's `keypair.fingerprint` is now kept |
| `GET /core/keypairs` | **fixed**: now paged with `page` / `pageSize`, like the VM list (the docs do not say what happens without them) |
| `DELETE /core/keypair/{id}` | matches (the singular path is correct) |
| `POST /core/virtual-machines` | matches: the required fields are name ≤ 50, environment_name, flavor_name, key_name and count, plus one boot source (`image_name`). `security_rules` are as documented (tcp needs port_range_min / max) |
| `GET /core/virtual-machines/{id}` | matches (`instance{…}`, `floating_ip`, `environment.region`, `flavor.gpu_count`). **fixed**: `STOPPING` (documented) is now mapped to stopping, where before it was unknown |
| `GET /core/virtual-machines?page&pageSize&search` | **fixed**: `count` is "Number of items returned in the current page", not a total. Treating it as a total ended paging after the first full page of 100, so VMs past 100 were missed. Paging now stops only on a short or empty page |
| `DELETE /core/virtual-machines/{id}` | matches (400 "being created" → retried) |
| Billing history | matches (`start_date` / `end_date` are `YYYY-MM-DDTHH:MM:SS`; "returns 500 intermittently") |

**Watch during the live launch**
- **Port randomization:** `enable_port_randomization` defaults to **true** when `assign_floating_ip` is true, and the docs do not say what it does to SSH on port 22. If SSH to `floating_ip:22` fails, look for a mapped port before suspecting the security rule.
- `user_data` "is Base64 encoded when it is submitted", but the docs' example sends raw text, so it is unclear who encodes it. Startup scripts stay refused.
- The status enum is not in the schema (docs pages only), and `STOPPED` is not documented. Note any status that maps to unknown.
- A create 409 `already_exist` (a duplicate keypair or VM name) is classified unknown, never retried blindly.

**Account setup:** an environment in the target region (`POST /core/environments`; the name must be unique across the org), set as `ROUTING_LAUNCH_DEFAULTS.hyperstack.environments{"CANADA-1": "<env>"}`. Image names are region-scoped and must be exact, for example `Ubuntu Server 22.04 LTS R550 CUDA 12.4 with Docker`. The test fixture's "Ubuntu Server 22.04 LTS" is not a documented name; list them with `GET /core/images?region=`. A prepaid balance covering 1 h of the VM. Keypairs are per environment; the adapter registers one per deployment.

## DigitalOcean

Docs: github.com/digitalocean/openapi `specification/` (DigitalOcean-public.v2.yaml, `resources/droplets`, `resources/sizes`, `resources/ssh_keys`, `shared/`),
<https://docs.digitalocean.com/products/droplets/details/pricing/>, `…/details/limits/`, `…/getting-started/recommended-gpu-setup/`, `…/details/features/`.

| Call | Status |
|---|---|
| Auth `Authorization: Bearer` | matches |
| `GET /v2/sizes?per_page=200` | matches (per_page max 200; `slug, price_hourly, available, regions, gpu_info.count`) |
| `POST/GET/DELETE /v2/account/keys` | matches (201 `ssh_key{id}`; 204 on delete) |
| `POST /v2/droplets` | matches (name pattern, `ssh_keys` int ids, `tags`, `user_data` ≤ 64 KiB; 202 `droplet{id, status: new}`) |
| `GET /v2/droplets/{id}` | matches (status new / active / off / archive; `networks.v4[type=public]`) |
| `DELETE /v2/droplets/{id}` | matches (204; 404 `not_found`) |
| `GET /v2/droplets` (list) | **fixed**: "By default, only non-GPU Droplets are returned. To list only GPU Droplets, set the `type` query parameter to `gpus`." `list_instances` sent no `type`, so it could never see a GPU droplet, and orphan detection and terminate confirmation would have read "absent". It now sends `type=gpus`. |
| `GET /v2/droplets?tag_name=` (find) | open. `tag_name` "Can not be combined with `name` or `type`", and the docs do not say whether a tag-filtered list includes GPU droplets. The adapter keeps `tag_name` alone, and the base adapter already re-checks an empty answer against the full (`type=gpus`) list, so find is correct either way |

**Watch during the live launch**
- Does `GET /v2/droplets?tag_name=og-…` return the GPU droplet? Either answer works; record it.
- A 422 on create has no documented text for no-capacity. Record the message.
- `GET /v2/account/keys` is limited to 10 per 60 s. Key cleanup lists keys, so avoid repeated manual reconcile runs.

**Account setup:** GPU Droplets enabled on the team; the droplet limit (`droplet_limit`) ≥ 1, with at most 10 concurrent creates. A token with `droplet:create/read/delete`, `ssh_key:create/read/delete`, `sizes:read`, `regions:read`, `tag:create/read` and `image:read`. Image per size in `ROUTING_LAUNCH_DEFAULTS.digitalocean.images`: `gpu-h100x1-base` for every single-GPU size, `gpu-h100x8-base` for 8-GPU sizes, `gpu-amd-base` for AMD.

## Shadeform (Crusoe · Denvr · Latitude)

Docs: <https://api.shadeform.ai/v1/openapi.json> (OpenAPI 3.1, linked from docs.shadeform.ai/llms.txt; the
old `docs.shadeform.ai/openapi.yaml` returns 404), docs.shadeform.ai `api-reference/{instances/*,sshkeys/*,authentication}`,
`getting-started/{concepts,faq,quickstart}`, `guides/{sshkeys,startupscript,spotinstances}`; the public catalogue `GET /v1/instances/types`.

| Call | Status |
|---|---|
| Auth `X-API-KEY` | matches |
| `GET /instances/types?cloud=&shade_instance_type=` | matches (`hourly_price` cents; filter `rental_type == on_demand`) |
| `POST /sshkeys/add` | matches (`{id}`) |
| `GET /sshkeys` | **fixed**: `ssh_keys` is `array \| null`, and null (no keys) used to raise a parse error |
| `POST /sshkeys/{id}/delete` | matches (409 for the default key or a key in use; deleted only after confirmed termination) |
| `POST /instances/create` | matches: required cloud / name / region / shade_instance_type; `envs` is top-level `[{name, value}]`; the response is `{id, status}`. The cloud strings `crusoe`, `denvr` and `latitude` are confirmed in the live catalogue. **fixed**: `NAME_MAX = 50` ("up to 50 characters"), and more than 20 `envs` is refused ("Up to 20") |
| `GET /instances/{id}/info` | matches. **fixed**: `cost_estimate` is documented as "a decimal dollar string", so the "unit UNVERIFIED" caveat was removed |
| `POST /instances/{id}/delete` | **fixed**: "An instance cannot be deleted while it is `creating`" (409). This used to be a non-retryable failure (→ `termination_failed`); it is now a retryable `still_creating` failure, as for Hyperstack, re-issued by reconciliation |
| `GET /instances` | **fixed**: `instances` is `array \| null`, and null used to raise a parse error. No pagination unless `limit` / `offset` is sent (matches) |
| Errors | the docstring was corrected: 400 (plain text), 404, 409 `{error, error_code}` and 500 are documented, with no capacity code and no 429 |

**Watch during the live launch**
- Is the account's default ("Shadeform Managed") key **also** installed when `ssh_key_id` is given? Check `authorized_keys`: it decides `forces_account_ssh_key`, which is still UNKNOWN.
- `ssh_user` is `shadeform`, and `ssh_port` is "Usually 22; some clouds use another port". The adapter does not read `ssh_port`, so use the info response to connect.
- `os` must be one of the instance type's `os_options`, for example crusoe `ubuntu22.04_cuda12.8_shade_os`, latitude GPU `ubuntu24.04_cuda12.4_shade_os`, denvr H100 `ubuntu22.04_cuda12.4_shade_os`. The adapter passes `image` through unchecked.
- If create is refused because the type is unavailable, record the status and body (no code is documented).
- Bare-metal Latitude types can take a long time to become active (Denvr Gaudi2 lists `max_boot_in_sec` 20820).

**Account setup:** a funded wallet (minimum $20 top-up); an API key (at most 10 per workspace); a real region taken from the live catalogue (e.g. crusoe `culpeper-usa-1`).

## Vast.ai

Docs: <https://docs.vast.ai/api-reference/openapi.yaml>, docs.vast.ai `api-reference/{search/search-offers,instances/create-instance,instances/show-instance,instances/show-instances,instances/manage-instance,instances/destroy-instance,instances/attach-ssh-key,billing/show-charges,rate-limits-and-errors}`,
<https://docs.vast.ai/guides/instances/connect/ssh>; secondary: the official CLI (github.com/vast-ai/vast-cli `vastai/api/*.py`, `vastai/SKILL.md`).

| Call | Status |
|---|---|
| Auth `Authorization: Bearer` | matches |
| Search offers | **fixed**: the spec documents only `POST /api/v0/bundles` with the query as a JSON body, which is also what the CLI sends. The adapter now POSTs it when it has a key. Without a key it keeps the legacy public `GET ?q=`, which our market poller uses daily |
| `PUT /api/v0/asks/{id}/` (create) | matches (`client_id: me`, image, disk, label, `runtype: ssh`, onstart ≤ 4048; `{success, new_contract}`) |
| Create errors | **fixed**: the documented 404 message is `"error 404/3603: no_such_ask …"`. The adapter required the message to *start* with `no_such_ask`, so it labelled this a validation rejection instead of capacity. It now matches `no_such_ask` anywhere in a 404 `msg` (still a definite rejection; only `error_kind` changes) |
| `GET /api/v0/instances/{id}/` | matches (`{instances: {...}}`; null = not on the account). **fixed**: `exited` "will never reach `running` — destroy and retry" (spec quirks) is now tracked as error, where before it was stopped, unless `intended_status` is `stopped` (after an OpenGrid stop) |
| `GET /api/v1/instances?limit=25&after_token=` | matches (`instances`, `next_token`, `total_instances`) |
| `PUT /api/v0/instances/{id}/ {state: stopped}` | matches |
| `DELETE /api/v0/instances/{id}/` | matches (`{success, msg}`; 404 `not_found`) |
| `POST /api/v0/instances/{id}/ssh/` | matches. **fixed**: a 200 with `success: false` is now reported on the accepted launch, where before it was silent |
| `GET /api/v0/charges` | matches (`select_filters` day / type; `results[].source = instance-<id>`) |

**Watch during the live launch**
- `env`: the spec types it as a Docker-flag **string** (`"-e K=V -p 22:22"`), but the official CLI sends a **dict**. The adapter sends a dict and only when env is set. If a launch with env is refused with `invalid_args`, this is why.
- `frozen` (GPU still billed) and `rebooting` are not mapped and read as unknown.
- Rate limits are per endpoint, enforced as a minimum gap between calls, and 429 bodies are often plain text with no `Retry-After`.
- The quote always includes 32 GB of storage. A launch asking for a different `disk_gb` bills a different storage amount than quoted.
- CLI guide: "Storage charges begin at creation. GPU charges begin when status reaches `running`." Compare this with `charges`.

**Account setup:** prepaid credits; an API key. Account-level SSH keys are installed on every new instance (`forces_account_ssh_key` YES), so keep the account key list empty or expect the admin override.

## Verda

Docs: <https://api.verda.com/v1/openapi.json> (OpenAPI 3.1; changelog in `info.description`), <https://api.verda.com/v1/docs>,
<https://docs.verda.com/welcome-to-verda/pricing-and-billing/>, <https://docs.verda.com/cpu-and-gpu-instances/shutdown-hibernate-and-delete/>.

| Call | Status |
|---|---|
| `POST /v1/oauth2/token` | matches (`{grant_type, client_id, client_secret}` → `access_token`, `expires_in`) |
| `GET /v1/instance-availability` | matches (`[{location_code, availabilities[]}]`; `is_spot` defaults to "false") |
| `GET /v1/instance-types` | **fixed**: on 2026-11-12 `price_per_hour` was deprecated in favour of `location_prices[{location_code, price_per_hour}]` ("A location with its own price shows it"). The quote now uses the chosen location's price, falling back to the default |
| `POST /v1/ssh-keys`, `GET /v1/ssh-keys`, `DELETE /v1/ssh-keys {keys:[]}` | matches |
| `POST /v1/instances` | matches. Only `hostname` is schema-required; instance_type, image and location_code are required unless `instance_group_id` is set. `tags[{key ≤ 63, value ≤ 127}]` ≤ 10; 202 plain-string id; 503 `service_unavailable` = no capacity |
| `GET /v1/instances?tag=opengrid=<name>`, `GET /v1/instances/{id}` | matches (the 13-value status enum is fully handled) |
| `PUT /v1/instances {action: delete, id, volume_ids, delete_permanently}` | matches (202/207 item array; 404 single action) |
| user-agent | the docstring was corrected: the current spec no longer mentions it. It is still sent |

**Watch during the live launch**
- The token lasts 3600 s and is cached on the adapter object. It is refreshed only when a **GET** gets a 401; a create or delete is never re-sent. A delete answered 401 is a non-retryable `failed` (auth), which becomes `termination_failed`. This can only happen with an adapter object older than an hour. Watch for a 401 on the terminate.
- `PUT /v1/instances` delete answering **204** ("already in the requested state") is reported as accepted, then confirmed by status/list.
- `GET /v1/instances/{id}` documents only 200; not-found handling relies on an undocumented 404 or a `notfound` status.
- Hostname has no documented pattern or length limit (`NAME_MAX` 60 is ours).
- `contract` (LONG_TERM / PAY_AS_YOU_GO / SPOT) is not sent and its default is not stated. Confirm on the invoice that the instance billed pay-as-you-go.

**Account setup:** Cloud API credentials (console → Keys → Cloud API credentials); a prepaid balance (at zero "instances will be discontinued and your volumes (data) will be deleted"); an `image` that is an `image_type` from `GET /v1/images` (or the type's `supported_os`), e.g. `ubuntu-22.04-cuda-12.4-docker`. The rate limit is 500/min per project and 60/min per method + path (`GET /v1/instances` 120/min).
