# Routing and execution

OpenGrid routes a workload to one provider listing chosen by a transparent ranking, quotes it, and
provisions only where an adapter exists and live provisioning is switched on; every decision is audited.

Version `routing-1.0`. Code: `routing/` (engine, adapters, deployments, audit, transactions, tracker).
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
neither, hourly cost only). `deadline_hours` is not otherwise used: there is no performance data to
estimate completion time.

## Route — `POST /v1/route` (scope `route:execute`)

For each candidate in rank order (at most `routing_max_attempts` provision attempts):

1. skip if OpenGrid cannot provision there (level < 2);
2. resolve credentials (`accounts.credentials.resolve`: BYO first, then OpenGrid-managed); skip if none;
3. live availability check through the adapter (public endpoints where the provider has them);
   skip if unavailable;
4. **quote**: the provider's live catalogue price when the check read one (`basis:
   "live_provider_api"`), else the observed listing price (`basis: "observed_listing"`); skip if over
   `max_price_per_gpu_hour`;
5. **if `settings.routing_live_provisioning` is false: stop.** Return `status: "not_provisioned"`,
   reason "live provisioning disabled in this environment", with the decision and quote. **No
   deployment record is created.**
6. check the launch spec has what that provider needs (e.g. an SSH key name for Lambda, an image for
   RunPod/Vast, a region-bound environment for Hyperstack); skip with the missing fields if not;
7. provision. On failure (capacity, auth, invalid request, provider error) the attempt is recorded and
   the next candidate is tried (**failover**). On a **timeout or an unreadable success response** the
   provider may have created the instance anyway, so OpenGrid does **not** fail over (that could buy two
   instances): the deployment is marked `failed` with `needs_reconciliation`.

Statuses returned: `provisioned`, `not_provisioned`, `failed`, `no_candidates`. "Provisioned" means the
provider accepted the launch and returned an instance id; the deployment then tracks whether it runs.

Real provisioning requires all of: `ROUTING_LIVE_PROVISIONING=true` in that environment, a key with
`route:execute`, credentials for the provider, a complete launch spec, and a quote within the caller's
maximum.

## Deployments

`GET /v1/deployments`, `GET /v1/deployments/{id}` (refreshes status from the provider),
`POST /v1/deployments/{id}/terminate`, `POST /v1/deployments/{id}/stop` (only where the adapter
implements stop; note several providers keep billing a stopped instance), `POST
/v1/deployments/{id}/outcome` (the caller's report of workload completion — OpenGrid cannot observe it).
A key only ever sees its own account's deployments.

Statuses: `routing → provisioning → running → (stopped) → terminating → terminated`, or `failed`.
Every transition is a `deployment_events` row. The `routing_tracker` job polls live deployments every
60 s. **Uptime** is OpenGrid-observed (summed between status checks that saw it running; accurate to the
polling interval, not the provider's billing clock). An **interruption** is a running deployment leaving
`running` without OpenGrid having requested it (provider termination, preemption, failure).

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
