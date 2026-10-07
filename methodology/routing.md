# Routing and execution

OpenGrid routes a workload to one provider listing chosen by a transparent ranking, quotes it, and
provisions only where an adapter exists and live provisioning is switched on; every decision is audited.

Version `routing-1.1` (execution core, migration 0010). Code: `routing/` (engine, control, quotes, guards, idempotency, adapters, deployments, credentials, audit, transactions, tracker).
Ranking: see [best-execution](/methodology/best-execution).

## Region: preferred vs strict (`strict_region`)

`region` names a region group (`US`, `Canada`, `Europe`, `UK`, `APAC`, `Middle East`, `LATAM`, `Africa`;
from `regions.region_groups`, never guessed). What happens to each listing depends on `strict_region`
(request body of `POST /v1/route/preview` and `POST /v1/route`; query parameter of `GET /v1/best/{gpu}`):

| listing location | `strict_region: false` (default) | `strict_region: true` |
|---|---|---|
| in the group | candidate, `region_match` 1.0 | candidate, `region_match` 1.0 |
| unknown (no region / country we can map) | candidate, `region_match` 0.5, flagged "not confirmed" | **excluded**, code `region_unconfirmed`, reason "region not confirmed as <X>" |
| known to be elsewhere | excluded, code `wrong_region` | excluded, code `wrong_region` |

A listing that spans several groups (e.g. a provider-wide price offered in the US and Europe) counts as
in each of them. Use strict mode when data residency or latency makes "probably in the US" not good
enough; the default keeps unknown-location listings visible, ranked below confirmed ones on region.
`strict_region` without `region` is rejected (422). The response echoes `strict_region`.

## GPU families (`allow_variants`)

`gpu` is normally one canonical GPU (`h100-80gb-sxm5`). A family name (`h100`, `blackwell`; see
[families](/methodology/families)) is rejected with 422, `detail.code = "family_needs_variant"`, and
the list of variants to choose from — because the variants are different products. With
`allow_variants: true` the request is routed across the family's listed variants: each variant is
ranked on its **own** market (its price factor is relative to its own variant median), the candidates
are merged (CHEAPEST: by price; other modes: by score) and each carries `variant` / `variant_slug`.
There is no family-wide market price: `market` is the selected candidate's own variant market, and
`by_variant[]` lists every variant's market and candidate counts. A deployment records the variant
actually chosen, never the family.

## Integration levels

| Level | Meaning |
|---|---|
| 0 | Market data only: OpenGrid reads prices; cannot check, quote live or launch. |
| 1 | Availability check: a live, per-request stock check. |
| 2 | Provisioning: OpenGrid can launch an instance through the API. |
| 3 | Full lifecycle: launch, inspect status, terminate (stop where the API has it). |

`GET /v1/capabilities` reports, per provider, three separate claims:

- `level_supported_by_provider_api` — what the provider's public API documents;
- `level_implemented` — what OpenGrid's adapter does, read from the adapter code itself
  (`routing.adapters.level()`), so the registry cannot claim more than is implemented;
- `verified_live` — **false for every provider**: no adapter has yet been run against a real provider
  account. Adapters are tested against mocked HTTP built from each provider's documented
  request/response shapes (`tests/test_routing_adapters.py`). `docs_checked` says how the API facts
  were established (docs fetched, found by search, or recalled).

Plus `credential_requirement` (OpenGrid-managed key / BYO / commercial agreement), the settings that
hold OpenGrid-managed credentials and whether they are configured, docs URL and notes.

Crusoe, Denvr and Latitude.sh are reached **through Shadeform** (`via: "shadeform"`): one Shadeform key,
Shadeform is the counterparty and bills, and the price is Shadeform's listing of that cloud.

## Preview — `POST /v1/route/preview` (scope `route:preview`)

Validates the request, resolves the GPU, ranks, and returns the selected candidate, alternatives,
multi-instance alternatives, exclusions, the market median, a **quote** and savings vs the median, and
whether OpenGrid can provision the selected candidate (if not, the best provisionable alternative).
Preview never calls a provider: its quote has `basis: "observed_listing"` — the observed listing price
× GPUs × hours (`duration_hours`, else `deadline_hours` read as "cost if it runs the full window"; with
neither, hourly cost only). For the best provisionable candidate it also persists a quote record
(`quote_record`: `quote_id`, `expires_at`, `price_source: "observed"`) that `POST /v1/route` can launch after a
live re-validation. `deadline_hours` is not otherwise used: there is no performance data to estimate
completion time.

## Route — `POST /v1/route` (scope `route:execute`, `Idempotency-Key` required)

The full safety model (modes, quotes, idempotency, state machine, cost guards, credentials, SSH keys) is in
[execution-safety](/methodology/execution-safety). In order:

1. the account must be active (suspension blocks new routes; never stop/terminate);
2. the effective execution mode (env ceiling `ROUTING_LIVE_PROVISIONING` × the operator's mode). In
   `DISABLED`/`PREVIEW_ONLY` the first provisionable candidate is live-checked and quoted and the route returns
   `status: "not_provisioned"` ("live provisioning disabled in this environment" when the env flag is off);
   **no deployment is created**;
3. for each provisionable candidate in rank order: `control.launch_permission` (an unvalidated adapter never
   launches customer compute), credentials (BYO first; an unusable BYO credential fails closed), launch spec
   and SSH-key policy;
4. a live availability check for at most `ROUTE_LIVE_CHECK_CANDIDATES` (3) candidates, each provider call
   bounded by `PROVIDER_CALL_TIMEOUT_SECONDS` (20); skip if unavailable or over `max_price_per_gpu_hour`;
5. a persisted **quote** (`price_source: live_check` when the provider API priced it), then the **cost guards**;
6. a deployment `created → quoted →` either `pending_approval` (SUPERVISED provider, or any limit violated:
   HTTP 202 with `approval` = provider, region, GPU, price, cost, fees, quote expiry) or `approved` (LIVE
   provider, no violations) → the single provision call.

Outcomes of the provision call: `accepted` → `provisioning` (route `status: "provisioned"`); a definitive
rejection → `provision_failed` / `provider_rejected`, and only then failover to the next candidate as a NEW
deployment (at most `ROUTING_MAX_ATTEMPTS` provision calls, default 1 = none); anything ambiguous →
`provider_timeout` / `launch_unknown` (HTTP 202) and **no failover**: reconciliation resolves it by instance
name `og-<deployment_id>` first.

`quote_id` in the body launches exactly that quote (no reroute) after a live re-validation; a price move over
`QUOTE_PRICE_TOLERANCE` (2%) or an expired quote is refused (409) with a new quote. `max_runtime_minutes` sets an
auto-terminate deadline. Approval: `POST /v1/route/{id}/approve` (admin, `quote_id`, optional
`override_limits` + `reason`, Idempotency-Key); `POST /v1/route/{id}/reject` (admin, reason).

## Deployments

`GET /v1/deployments` (`?status=live|uncertain|<state>`, paginated), `GET /v1/deployments/{id}`
(`refresh=true` asks the provider; default false), `POST /v1/deployments/{id}/terminate` and `/stop`
(Idempotency-Key required; allowed for suspended accounts and in every execution mode), `POST
/v1/deployments/{id}/outcome` (the caller's report of workload completion — OpenGrid cannot observe it).
A key only ever sees its own account's deployments. Admin: `GET /v1/admin/deployments?state=live`,
`POST /v1/admin/deployments/{id}/terminate`.

States: see the transition table in [execution-safety](/methodology/execution-safety). Every transition is a
`deployment_events` row with actor, reason and evidence. Terminate moves to `terminating` and asks the
provider; `terminated` is recorded only on provider evidence (status says terminated, or two `not_found` reads
≥ 60 s apart, or the reconciler's instance list). Status, stop and terminate use the credential **pinned at
launch**. The `routing_tracker` job polls live deployments. **Uptime** is OpenGrid-observed (summed between
status checks that saw it running). An **interruption** is a running deployment leaving `running` without
OpenGrid having requested it.

## Prices — four concepts, four fields

| Field | Concept | Source |
|---|---|---|
| `observed_market_price` | observed market price | the stored listing price the ranking used |
| `list_price` | list price | the provider catalogue price read on the live check |
| `quote` (+ `quote_basis`) | quote | the price OpenGrid quoted for this route |
| `execution_price` | execution price | what the provider reports for the running instance; null until known |

They are stored in separate columns and never merged.

## Audit and transaction records

- `route_requests`: every preview and route (account, key, request with env values redacted, mode,
  status, result summary).
- `routing_decisions`: every candidate considered with prices, availability and factor values, every
  exclusion with its reason, the weights, the selected listing and its observed price, the market
  snapshot used (median/low, as-of times, the selected listing's `observed_at`), methodology version.
- `deployments`, `deployment_events`, `provision_attempts` (provider, latency, ok, error kind).
- `execution_records` (kind *transaction*): quoted, actual and observed prices, provision success and
  latency, attempts, uptime, interruptions, termination reason, workload completion when reported, and
  at termination the provider cost = price × GPUs × instance lifetime (`cost_basis: "actual"` when the
  provider reported an execution price, else `"quote"`). This is an estimate; the provider's invoice is
  authoritative and invoice reconciliation is not implemented.

On termination OpenGrid calls `billing.usage.record_usage(...)` (kind `byo` when the deployment used
the account's own provider key, else `compute`). It is idempotent per period and retried by the
tracker until it succeeds.

These records are the only data reliability/performance scoring will ever use; until enough exist,
those factors stay at weight 0.

## Provider-specific data

Provider responses are translated into canonical fields by each adapter. Provider-specific fields (host
ids, ask ids, raw status words, launch request bodies) are kept in `deployments.provider_metadata` for
debugging and are never returned by the public API.
