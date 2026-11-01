# Provider API survey: reserved pricing, capacity blocks, capacity and utilization

Surveyed 2026-10-29 for all 32 tracked providers. Goal: what each provider exposes beyond the on-demand price we
already ingest, and which endpoints we do not use yet.

**Evidence tags** (read these before trusting a cell):

- **[live]** called by us this session and the response seen. Strongest.
- **[spec]** present in the provider's published OpenAPI spec, not called.
- **[docs]** from a documentation page or search snippet only. Not called, may be stale.
- **[sales]** exists as a product but is quoted by a sales team, no API or price table.
- **[none]** nothing found in the sources checked. Not proof it does not exist.

Access: **public** = no key, **key** = needs an account key (we hold keys for Lambda, RunPod, Hyperstack, DigitalOcean,
Salad), **acct** = only describes the caller's own account (useless for market data unless we hold an account there).

## 0. The 32 providers in groups, and what each can give us

Groups are the registry's own provider classes (`provider_meta.py`): 4 hyperscalers, 7 general clouds, 17 neoclouds,
2 marketplaces, 2 decentralized. `[v]` = verified by a call; `[d]` = from documentation or an agent report, not called.

**Hyperscalers (4)**

| Provider | Public, no key | Free account or key | Reserved / blocks |
|---|---|---|---|
| aws | Spot Advisor feed: interruption band + savings per GPU type and region [v]; Capacity Block rate card page [d] | capacity-block offerings with future slots and price, spot placement score, spot price history [d] | Capacity Blocks; RI / Savings Plans in the Price List [d] |
| gcp | nothing new | Catalog API (free key) with `Commit1Yr` and Spot SKUs [d]; calendar-mode availability [d] | calendar-mode reservations up to 90 days; DWS flex-start rates page [d] |
| azure | **1 / 3 / 5-year reservation prices for GPU VMs [v]** | spot placement score, SKU restrictions [d] | reservations (public); capacity reservations are account-scoped |
| oracle | nothing new | compute capacity report per availability domain [d] | none found |

**General clouds (7)**

| Provider | Public, no key | Free account or key | Reserved / blocks |
|---|---|---|---|
| ibm | nothing beyond on-demand price | none | none found |
| digitalocean | reserved 12-month rates and regional GPU availability, both on static pages [d] | `/v2/sizes` `available` + `regions` (key held; not confirmed to track stock) | 12-month contracts, sales |
| vultr | availability endpoint exists but is empty for every GPU plan [v]; preemptible price | none | annual prepay, unverified |
| linode | **per region and plan availability: 429 GPU rows, 43 available in 7 regions [v]** | none | none found |
| ovhcloud | **monthly GPU prices in the public catalogue [v]** | `productAvailability`, flavor and quota routes need a key [d] | monthly billing is the commitment |
| scaleway | **stock tier per server type per zone: available / scarce / shortage [v]** | Elastic Metal and Kubernetes routes need a key | none found |
| koyeb | **availability LOW / MEDIUM per instance type and region [v]** | none | none found |

**Marketplaces (2) and decentralized (2)**

| Provider | Public, no key | Free account or key | Reserved / blocks |
|---|---|---|---|
| vast | **bid price and `min_bid` per offer [v]**; offer count and churn give a scarcity estimate | host-side routes need a key | `type=reserved` accepted but showed no discount in a sample [v] |
| hyperbolic | `enabled` flag per rental option only | none found | reserved clusters, sales |
| salad | none | **`is_high_demand` flag and prices by priority (key held) [v]** | volume discounts 50+ [d] |
| lium | **GPU counts per machine: 395 GPUs, 363 free, about 8% used; reliability score; price changes announced ahead [v]** | none | none found |

**Neoclouds (17)**

| Provider | Public, no key | Free account or key | Reserved / blocks |
|---|---|---|---|
| lambda | availability via Shadeform [d] | regions with capacity per instance type (key held, already used) | 1-Click Clusters, sales; price table on website |
| runpod | none | **stock High / Medium / Low per GPU and per data center (key held) [v]** | per-term prices unconfirmed |
| hyperstack | availability via Shadeform [d] | **GPU stock by region and model, plus pricebook discount fields (key held, already fetched) [v]** | sales |
| nebius | availability via Shadeform [d] | Capacity advisor (on-demand / preemptible / reserved level) and Capacity Blocks: real services, tenant-scoped [d] | Capacity Blocks; committed usage, sales |
| verda | **commitment ladder 2 / 3 / 4 / 8 / 25% and 56-entry cluster catalogue [v]** | `instance-availability`, `cluster-availability` need a key [v: 401] | long-term periods (public ladder) |
| voltagepark | location and preset endpoints answer but are empty now [v] | reserved GPU counts and availability per location (already in payload) | reserved infiniband clusters |
| crusoe | availability via Shadeform [d] | reservations, GPU tracking, quotas, metrics: account-scoped [d]; `/capacities` returns 401 [v] | reservations, sales |
| latitude | availability via Shadeform [d] | `stock_level` per region from `/plans` (401 without key) [v] | yearly price per plan [d] |
| denvr | availability via Shadeform [d] | none found | none found |
| boostrun | availability via Shadeform [d] | none found | none found |
| horizon | availability via Shadeform [d] | none found | none found |
| phyntec | availability via Shadeform [d] | none found | none found |
| massedcompute | `capacity_available` count per product, `regions_with_capacity_available` [live, key; read since 2026-11-01] | none found | none found |
| coreweave | none | none (utilization is a console-only Grafana) | Flex and Reserved Instances via account executive |
| together | reserved tiers on the pricing page [d] | cluster API needs a key (401) | reserved clusters 1 to 90 days+ |
| jarvislabs | none | SDK `gpu_availability()` with a key [d] | none found |
| hotaisle | none | CLI and API docs exist, content unread [d] | custom and MI355x reservations, sales |

Shadeform (`/v1/instances/types`) answered with no key and lists per-region availability booleans for these clouds
(agent report on which clouds, terms not reviewed). For Lambda, Hyperstack, Nebius, Verda and Voltage Park it can
cross-check their own feeds.

---

## 1. Summary table

| Provider | Reserved / committed pricing | Capacity blocks / scheduled capacity | Capacity / availability signal | Utilization / demand signal | What we already use |
|---|---|---|---|---|---|
| aws | RI / Savings Plans prices in the AWS Price List [docs, not called] | EC2 Capacity Blocks for ML: `DescribeCapacityBlockOfferings`, price set by supply and demand at purchase [docs, acct] | none public | none | on-demand price file |
| gcp | Cloud Billing Catalog API has `Commit1Yr` and Spot SKUs, needs a free API key [docs] | Future reservations in **calendar mode**, up to 90 days, availability check 60 days ahead [docs, acct] | none public | none | public pricing HTML page |
| azure | **1 / 3 / 5-year reservation prices for GPU VMs in the public Retail Prices API** [live] | Capacity reservations [acct] | none public | none | consumption prices only |
| oracle | none found | Capacity reservations and `compute-capacity-report` (host capacity per AD) [docs, acct] | via capacity report only [acct] | none | public price list |
| ibm | none found | none found | none found | none | catalog pricing |
| lambda | [sales]: 1-Click Clusters, 2 weeks to 1 year, price tables on the website only | none | `regions_with_capacity_available` per instance type [live, key] | none | instance types + capacity (key) |
| runpod | none in API | none | **`stockStatus` per GPU and per data center** [live, key] | stock tier (High / Medium / Low) | GPU prices only |
| vast | `type=reserved` is accepted without a key but returned the same prices as on-demand in a live sample, so a prepaid discount is not demonstrated [live]; host `discount_rate` [docs] | none | `rentable`, `rented` flags per offer (search returns rentable only) [live] | **`type=bid` returns a distinct interruptible price; `min_bid` per offer** [live, public] | offers search (on-demand) |
| salad | volume discounts 50+ GPUs [docs] | none | none | **`is_high_demand` flag + prices by priority** [live, key] | gpu-classes (uses prices) |
| hyperstack | `pricebook` has `original_value` and `discount_applied` [live, key] | none | **`/core/stocks`: GPU stock by region and model, current and upcoming** [live, key] | none | flavors, pricebook, stocks (fetched) |
| hyperbolic | reserved clusters, up to ~40% off [sales] | none | `enabled` per rental option [live] | none | rental options |
| nebius | committed usage [sales]; Capacity Blocks [docs] | **Capacity Blocks: `CapacityBlockGroupService`, `CapacityIntervalService`** (gRPC, public protobufs github.com/nebius/api; REST `GET /capacity/v1/capacity-intervals` returns 401) [spec/live] | **`ResourceAdviceService/List`: on_demand / preemptible / reserved `availability_level` (HIGH, MEDIUM, LOW, LIMIT_REACHED, UNKNOWN) + `data_state`** [spec], tenant-scoped and quota-capped | availability level + freshness | pricing web page only |
| digitalocean | 12-month GPU reserved rates on the pricing page [sales/docs] | none | `available` and `regions` per size [live, key] | none | sizes |
| lium | none found | none | `available_gpu_count` per executor [live, public] | **utilization = 1 - available / total: 395 GPUs, 363 free, about 8%**; `active` and `is_responsive` are null publicly, so idle cannot be told from unreachable [live] | executors |
| massedcompute | none found | none | `GET vm.massedcompute.com/api/v1/gpu-inventory` (Bearer, read-only token) [live, key] | none | gpu-inventory API; pricing page without the key |
| verda | **commitment ladder: 1 mo 2%, 3 mo 3%, 6 mo 4%, 1 yr 8%, 2 yr 25%** [live, public] | long-term clusters [spec] | `instance-availability`, `cluster-availability` [spec, key] | `dynamic_price` / `max_dynamic_price` / spot price [live] | instance types |
| voltagepark | `reserved_gpu_count_*` on bare-metal locations [live] | reserved infiniband clusters [live] | locations + hostnodes, `location_ids_with_availability` [live] | none | locations, presets |
| crusoe | reservations list [spec, acct] | reservations [spec, acct] | quotas [spec, acct]; `GET /capacities` exists in the spec but returns 401 [live]; public view only through Shadeform | **`reservations/gpu-tracking`** and PromQL metrics [spec, acct] | via Shadeform |
| latitude | yearly price per plan [docs] | none | `/plans` stock level per region [docs, key] | none | via Shadeform |
| denvr | none found | none | via Shadeform | none | via Shadeform |
| boostrun | none found | none | via Shadeform | none | via Shadeform |
| horizon | none found | none | via Shadeform | none | via Shadeform |
| phyntec | none found | none | via Shadeform | none | via Shadeform |
| scaleway | none found | none | **`products/servers/availability`: available / scarce / shortage per server type per zone** [live, public] | stock tier | via Shadeform only |
| coreweave | Flex Reservations and Reserved Instances via account executive [sales] | none | none | GPU utilization in Grafana, console only [docs] | pricing page |
| together | **Reserved clusters, 1 to 90 days, H100 from $3.29 vs $4.79 on-demand** [docs]; cluster create API `/v1/compute/clusters` [docs] | reserved clusters | none found ("Notify me" only) | none | pricing page |
| jarvislabs | none found | none | SDK `gpu_availability()`: free devices and price, containers vs VMs [docs, key] | none | pricing page |
| hotaisle | custom and MI355x reservations [sales] | none | none found | none | pricing page |
| vultr | annual prepay discount claimed, unverified [docs] | none | **`/v2/regions/{id}/availability`** [live, public, returned empty for GPU plans]; plan `locations` [live] | none | plans incl. preemptible price |
| linode | none found | none | **`/v4/regions/availability`: boolean per region and plan; 429 GPU rows, 43 available in 7 regions** (page_size must be 25 to 500, 2,475 rows total) [live, public] | none | types |
| ovhcloud | **`*.monthly.postpaid` GPU add-ons in the public catalogue** (e.g. a100-180 1,100 / month, h100-380 1,940 / month) [live]; hourly price not re-checked | none | `capabilities/productAvailability`, `/cloud/order/rule/availability` [spec, key] | none | public catalogs |
| koyeb | none found | none | **`/v1/catalog/usage`: availability level per instance type and region** [live, public] | LOW / MEDIUM / HIGH level | catalog instances |

## 2. Endpoints we are not using, ranked by value for effort

1. **Azure reservations** [live, public]. Same Retail Prices API we already call, filter `priceType eq 'Reservation'`.
   1, 3 and 5-year terms for ND H100 and other GPU VMs. `retailPrice` is the **whole term** price, so divide by hours
   and GPUs. Cheapest win: no new auth, no new host.
2. **RunPod stock** [live, key we hold]. Same GraphQL endpoint: `lowestPrice.stockStatus` per GPU and
   `dataCenters { gpuAvailability { gpuTypeId stockStatus } }` per data center. Introspection is disabled, so
   field names come from their docs. A regional capacity signal for the largest marketplace-style provider.
3. **Scaleway stock** [live, public]. Per server type and zone: `available`, `scarce`, `shortage`. We only see Scaleway
   through Shadeform today.
4. **Koyeb availability** [live, public]. `/v1/catalog/usage`, per instance type and region.
5. **Linode availability** [live, public]. `/v4/regions/availability`.
6. **Verda commitment ladder and cluster catalogue** [live, public]. `/v1/long-term/periods/instances` and
   `/v1/cluster-types` (56 cluster types with prices). `instance-availability` needs a key.
7. **Fields we already receive and may not be using**: Hyperstack `/core/stocks` and pricebook discount fields,
   Salad `is_high_demand` and priority prices, Lium `available_gpu_count` and `reliability_score`, Voltage Park
   `reserved_gpu_count_*` and `location_ids_with_availability`, Shadeform per-region `availability` lists (Crusoe,
   Latitude, Denvr, Boost Run, Horizon, Phyntec, Scaleway), Vultr preemptible prices. Check the normalizers before
   fetching anything new.
8. **Nebius Capacity API / advisor** [docs]. Closest thing to a published utilization signal on a major neocloud.
   Needs its endpoint path and auth confirmed; the launch post said public preview was planned for Q1 2026.
9. **Together reserved cluster prices** [docs]. Rates are on the pricing page, so our existing page parser could carry
   a reserved column.
10. **Account-scoped APIs** (AWS Capacity Blocks, GCP calendar mode, Oracle capacity reports, Crusoe reservations and GPU
    tracking). Only useful if OpenGrid holds an account there; they describe our own capacity, not the market.

## 3. Findings that change the picture

- **There is no published "utilization" endpoint** at any provider checked. What exists are proxies: stock tiers
  (RunPod, Scaleway, Koyeb, Nebius), a demand flag (Salad), and counts that let us compute it ourselves (Lium
  available vs total GPUs). A derived "tightness" measure across these is the realistic utilization signal.
- **Reserved pricing is public in only three places**: Azure's API [live], Verda's discount ladder [live], and
  Together's page [docs]. Everywhere else it is a sales conversation, so a reserved-price index would be thin and
  Azure-heavy at first.
- **Capacity Blocks are an AWS and Nebius concept** with APIs, but AWS's is account-scoped and priced at purchase time.
- **Providers whose API gives us nothing new**: Lambda (spec has 76 operations, none for reservations or utilization),
  IBM, Massed Compute, Denvr, Boost Run, Horizon, Phyntec.

## 4. Round 2: six parallel research passes (2026-10-30)

Six research agents (hyperscalers, neoclouds, marketplaces, general clouds, out-of-the-box sources, capacity-block
products) added the findings below. Claims marked **verified** were re-checked by a direct call after the agents
reported; the rest are the agents' own tags and were not re-checked.

**New public signals (no key)**
- **AWS Spot Advisor feed** `spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json` (**verified**): interruption
  band `r` 0-4 (<5%, 5-10%, 10-15%, 15-20%, >20%) and savings `s` per instance type and region; 56 GPU-class types in
  us-east-1 (e.g. p5.48xlarge `s=57 r=0`, p4d.24xlarge `s=56 r=3`). The only public hyperscaler tightness proxy found.
  AWS only; slow-moving, coarse.
- **AWS Capacity Block rate card** `aws.amazon.com/ec2/capacityblocks/pricing/` (agent, HTML with an embedded price
  table; not re-checked): per instance type and region, e.g. B300 at 129.168 / instance-hour (16.146 / GPU-hour). Page
  says prices follow supply and demand and states its next update (January 2027). A block is charged at the purchase-time
  price. Check the site's terms before indexing.
- **Shadeform `GET /v1/instances/types`** (agent, **no key needed even though the docs say a key is required**):
  per-region boolean availability for Massed Compute (60 types), Verda (44), Hyperstack (31), Lambda (23), Crusoe (18),
  Latitude (13), Scaleway (11), Denvr (9), Nebius (6), Boost Run (6), Voltage Park (4), Horizon and Phyntec (2 each).
  `rental_type` includes `spot`. Booleans only: no quantity. Terms not reviewed.
- **Azure reservations** (**verified** by the survey and by an agent): 24 rows each of 1, 3 and 5-year terms for
  `Standard_ND96isr_H100_v5`; `retailPrice` is the whole-term price.
- **OVH monthly GPU prices** (**verified**, `*.monthly.postpaid` in `order/catalog/public/cloud`); **Linode** GPU
  availability (**verified**); **Scaleway**, **Koyeb**, **Verda ladder** (see section 1).
- **Ornn OCPI** (`data.ornn.com`, agent): free, keyless, volume-weighted average of transacted H100 prices. A market
  benchmark, not per provider. **Silicon Data** (agent): H100 / B200 rental indices and forward curves, free tier with
  30 days of history. Terms not reviewed.

**Needs an account (data not public, API shape public)**
- **AWS** `DescribeCapacityBlockOfferings` (start-date slots with `UpfrontFee`: a forward-availability query),
  `GetSpotPlacementScores`, `DescribeInstanceTypeOfferings`, `DescribeSpotPriceHistory`; free account, read-only role
  probably enough (not confirmed).
- **Azure** Spot Placement Score API (POST, per SKU, region and zone) and Resource SKUs restrictions.
- **Oracle** `CreateComputeCapacityReport` (per availability domain and shape: `AVAILABLE` or `OUT_OF_HOST_CAPACITY`,
  optional count). An OCI CLI issue reports the report is sometimes wrong.
- **Google Cloud** future reservations (calendar mode, up to 90 days, GPU availability checkable 60 days ahead via
  `gcloud compute advice calendar-mode`); Cloud Billing Catalog API needs a free API key.
- **Nebius** `ResourceAdviceService/List` at `capacity-advisor.billing-cpl.api.nebius.cloud:443` and
  `CapacityBlockGroupService` at `capacity-blocks.billing-cpl.api.nebius.cloud:443`; viewer role in a tenant; bounded
  by that tenant's quota, so it shows our own launchability, not market inventory.
- **Mithril** (not one of our 32): public OpenAPI spec at `api.mithril.ai/openapi.json`; `/v2/reservation/availability`
  (slot calendar), `/v2/reservation/quote`, `/v2/pricing/current` and `/history` all need a key (401 verified).
  **SF Compute**: `/preview/v2/orderbook/depth` is documented as public preview; untested.
- **Latitude** `/plans` (`stock_level` per region; 401 verified), **Jarvislabs** SDK `gpu_availability()`.

**Dead ends**
- Crusoe `GET /capacities` looked open in the spec but returns **401** (**verified**).
- Vultr GPU availability and plan `locations` are empty for every GPU plan tested.
- Dry-run launches (AWS `--dry-run`, GCP `validateOnly`) prove permissions, not capacity.
- SkyPilot catalog and dstack `gpuhunt` publish prices and specs, not availability.
- No provider publishes a utilization endpoint; CoreWeave's is a console-only Grafana.

**Corrections to this survey made in round 2**
- OVH is not consumption-only: monthly GPU add-ons exist.
- Linode has 429 GPU rows (43 available), not 17. An agent's 91 was also a partial page.
- Vast `type=reserved` is accepted but did not show a discount in a live sample.
- Lium utilization is about 8% unfiltered; `active` / `is_responsive` are null in the public response.

## 5. Derived signal: a tightness score

No provider publishes utilization, so the realistic approach is to derive it from data we already collect. One
proposal (an agent's idea, weights are guesses to be tested against the providers that publish a native stock tier):

`T = 100 x (0.35 S + 0.25 R + 0.20 C + 0.20 D)` over a rolling 7 days per provider and GPU, where S = share of
snapshots sold out or unlisted, R = median hours out of stock (capped at 24, divided by 24), C = listing churn between
snapshots, D = price pressure (7-day change plus spread to market median, clamped 0-1). Drop missing inputs,
renormalise weights, and mark scores built on fewer than three inputs as low confidence. Native tiers that can feed S
and be used to test the weights: RunPod, Scaleway, Koyeb, Linode, Salad, Hyperstack, Lium, Vast (`min_bid` over
`dph_total`, offer churn). Needs snapshot history to be kept.

## 6. Not checked, or unreliable

- AWS Price List reserved terms, `DescribeSpotPriceHistory` and spot placement scores were not called (AWS APIs need
  credentials). They are well-known AWS APIs, not verified in this survey.
- Vast, Latitude, Nebius, Hyperbolic, Hot Aisle and Jarvislabs findings are from documentation snippets, not calls.
- Third-party price aggregators contradict each other and the providers on reserved rates (Lambda, DigitalOcean,
  Vultr); no reserved rate in this document comes from them.
- Provider-side field names can change; every [live] result is a snapshot from 2026-10-29.
