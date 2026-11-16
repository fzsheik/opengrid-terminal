# Provider capacity

How much of each GPU SKU a provider says it can supply, by region, read from the provider's own API.

Kind: **observed** (the provider's own report). Code: `capacity.py`, each provider's `normalize_capacity()` in
`providers/<name>.py`. API: `GET /v1/capacity`, `GET /v1/capacity/history`.

## Providers and what each reports

Thirty-five providers, every one read from data its own module fetches and stores raw first (the 29 provider modules
that define `normalize_capacity`, plus the six clouds read through `providers/shadeform.py`: Latitude, Denvr, Boost
Run, Horizon, Phyntec and Paperspace). Most of it was already
being fetched for prices; Koyeb, Linode, Scaleway, Massed Compute, Verda, Hot Aisle, Crusoe, AWS, Nosana and Hetzner each add a capacity source.

| Provider | Source | Shape | What the provider says |
|---|---|---|---|
| RunPod | GraphQL `lowestPrice.stockStatus` | tier | `High` / `Medium` / `Low` / `None` per GPU type, cloud (secure / community) and pod size. Pod size matters, so the sku is `<gpu>:<cloud>:<count>` (the listing_id). |
| Koyeb | `GET /v1/catalog/usage` | tier | `LOW` / `MEDIUM` / `HIGH` per instance type and region; types with no per-region word are stored provider-wide. |
| Scaleway | Scaleway's own `.../products/servers/availability`, all 9 zones, paged | tier | `available` / `scarce` / `shortage` per server type and zone. Prices come from Scaleway's own EUR catalogue, converted at the ECB rate (`/methodology/fx`). |
| Hyperstack | `GET /core/stocks` + `/core/flavors` | count | Deployable VMs per region, model and size (`configurations`); in-stock flag per flavor; model GPU estimate (`available`, a floor such as "10+") kept in raw_value. |
| Lium | `GET /api/executors` | count | Free GPUs out of all GPUs, summed per machine type and city. |
| Jarvislabs | `GET /misc/server_meta` (API key) | count | Free GPUs per GPU type, region and workload (vm / container), from `effective_num_free_devices` (what Jarvislabs' own SDK routes launches on). Sku is `<gpu>:1x:<workload>`. |
| Voltage Park | `/bare-metal/locations`, `/instant-deploy-presets/` | count, boolean | GPUs in stock per location and fabric; VM presets list the locations with stock. Locations are ids. |
| Hyperbolic | `/v2/alpha/on-demand/rental-options` | count | `totalAvailable` GPUs per option; in stock only when the option is enabled. When `totalAvailable` is missing (as it often is), in_stock is unknown. |
| Linode (Akamai) | `GET /v4/regions/availability` (all pages) | boolean | Whether the plan can be launched in that region now. |
| Verda | `GET /v1/instance-availability`, on-demand and spot (Cloud API credentials) | boolean | Per location, the instance types that can be deployed now. Every priced GPU type gets a row at every location in the response: in stock exactly when the location lists it. Sku is the listing_id (`<type>`, `<type>:spot`); spot is asked separately and differs. Without credentials, no capacity; prices are public either way. |
| Lambda | `regions_with_capacity_available` | boolean | Where there is stock, never how much. A type with no listed region gets one provider-wide `false`. |
| DigitalOcean | `/v2/sizes` `available` + `regions` | boolean | Whether new Droplets can be created with the size, and where. Not confirmed to track live stock. |
| DeepInfra | `GET /v1/containers/gpu_availability` (API key) | boolean | `available` per GPU configuration (`8xB200-180GB`), the flag its dashboard shows as "Availability status". Not defined further. Provider-wide. Sku is the price page's (`8x NVIDIA B200`), so stock joins to page prices. Prices always come from the public page: the API's own `usd_per_hour` differs from it and is kept raw, never published. Without the key, no capacity. |
| Hot Aisle | Its own `GET /teams/{handle}/virtual_machines/available/` (API key) | count | `Quantity`: VMs of that type (GPU model and count) that can be provisioned now, provider-wide; unit is instances. The response lists only types in stock, so a size the page prices but the response omits is `false` with no count. Sku is the price page's (`MI300x:1x:vm`). Prices always come from the public page; the API's `OnDemandPrice` is cross-checked against it each poll and never published. Bare-metal availability is stored raw only. Without the key, no capacity. |
| Vast | Keyed census: `POST /api/v0/bundles/`, verified on-demand offers, rentable and not rentable, per GPU model | count (free + total) | Free GPUs = distinct `gpu_ids` in rentable offers; total = each machine's GPUs (`num_gpus / gpu_frac`) over every machine either search returns. Provider-wide. Total means GPUs on verified machines listed now. A census of 512 offers (the search cap) withholds counts. Prices still from the public search. |
| Latitude | Latitude's own `GET /plans` (API key) | count, tier | Per plan and location: in stock (`locations.in_stock`), servers in stock (`in_stock_count`), and the region's `stock_level` as tier. Count unit is servers. Without the key, Shadeform's boolean. Prices still from Shadeform. |
| Clore.ai | `GET /v1/marketplace` (API key), the same call as prices | count (free + total) | Per GPU name, provider-wide: free GPUs = GPUs on servers with `rented: false` ("Is server rented on demand"), total = GPUs on every listed server. Counted over the servers the price book can hold (priced in USD, one GPU model, a GPU count that agrees with `gpu_array`), so free equals the listings' capacity. Sku is the listing sku (`RTX 4090 24GB`); a model with every server rented gets `false` and no price. Free means free of an on-demand rental: a spot renter on it can be displaced. |
| Thunder Compute | `GET /v2/status` (API key), fetched with prices | boolean | `available` / `unavailable` per instance size (`specs` map), provider-wide. Sku is the listing sku (`h100_x8`); one row per priced size the map names. Its `gpu_type` map names sizes that have no price (A6000 x2-x8) and is not read. Not defined further by Thunder. |
| Salad | `POST .../availability/sce-gpu-availability` per GPU class (API key), fetched with prices | count (free only) | `available_gpu_{high,medium,low,batch}`: free nodes at each priority; Salad sells one GPU per instance, so the unit is instances. Salad calls them estimates of online nodes; higher priority can preempt lower, so the counts are cumulative (high >= medium >= low >= batch in every live class) and never added up. No total. Sku is the listing_id (`<class id>:<priority>`), as RunPod, because counts differ by priority. `is_high_demand` ("Whether the GPU class is in high demand", no threshold) says nothing about stock: kept in raw_value, mapped to neither tier nor in_stock. |
| Crusoe | Crusoe's own `GET /v1/capacities` (signed API key pair) | boolean | `quantity` per VM type and location. Read as yes/no (quantity > 0), not a count: every row seen says 0 or 1, CPU types included, so it is not a stock level; the quantity is kept in raw_value. The API repeats each (type, location) row; one row per key. GPU types only, from Crusoe's type catalogue. Sku is the Crusoe product name, the same as the listing sku; regions are Crusoe's location names (`us-east1-a`). Without the key (or when its last capacity snapshot is over 6 hours older than Shadeform's), Shadeform's boolean; never both. Prices still from Shadeform: the API has none. |
| AWS | `GetSpotPlacementScores` (read-only IAM key, SigV4) | tier | AWS's spot placement score, 1-10, per region: how likely a spot request for one instance of the type is to succeed now. Five fixed request configurations, one per type (`p5.48xlarge`, `p5en.48xlarge`, `p6-b200.48xlarge`, `p6-b300.48xlarge`, `p4d.24xlarge`), one instance each, regional scores over the six spot regions (us-east-1, us-east-2, us-west-2, eu-west-2, eu-north-1, ap-northeast-1). Refreshed every 2 hours, not every poll: AWS gives accounts with no spot history a low target-capacity limit and may refuse new configurations within 24 h, so the set is small and never changes. Sku is `<type>:spot` (spot only: the score says nothing about on-demand); region is the region code, so a row covers the spot listings in every AZ of that region (`<type>:<az>:spot`). A region AWS does not score gets no row. `in_stock` stays null: AWS says a score is a recommendation, not a guarantee of capacity. Without the key, no capacity; on-demand prices are public either way. |
| Akash | Akash Console `GET /v1/gpu-prices` (no key), the same call as prices | count (free + total) | Per GPU (vendor, model, RAM, interface), provider-wide: `availability.available` GPUs (allocatable and not yet allocated) of `availability.total`. The same figure as the listing's `capacity`. A GPU with no bid in 31 days gets a capacity row and no listing. |
| CUDO Compute | `GET /v1/vms/machine-types`, `GET /v1/machines-types` (no key), the same calls as prices | count | Per data centre: VMs `totalGpuFree` (unit GPUs, sku `vm:<machine type>`); bare metal `machinesFree` (unit instances, sku `baremetal:<type id>`). The API lists only what is in stock or recently stocked. CPU-only types are skipped. |
| GPU.ai | `GET /v1/pricing` (no key), the same call as prices | count | `available`: launchable offerings (instances, not GPUs) per offering, by GPU.ai's region. Sku is `<gpu>x<count>:<deployment>/<class>`. GPU.ai resells other clouds' GPUs, so its listings never vote in market statistics; capacity is stored and shown the same way. |
| Hydra Host | `GET /api/v1/inventory` (no key), the same call as prices | count (free + total) | One row per active GPU device, and a device is one physical server: `stockStatus` `on demand` = in stock, 1 of 1 server free; `reserve` and `preorder` = not rentable now, 0 of 1. Unit is instances (servers of `gpu_count` GPUs); different devices are different machines, so they add up. Total means servers listed now, including reserve and preorder ones that may not be racked yet. Any other word is kept in raw_value with `in_stock` and both counts null. Sku is the device id (the listing sku); region is the device's location. Before 2026-11-16 the same rows were yes/no only. |
| SimplePod | `GET /instances/market/list?rentalStatus=active`, containers and VMs (no key), the same calls as prices | count (free only) | `numOfInstances`, which SimplePod's API reference defines as "Number of instances available for this variant": instances of one offer (one host, one GPU count) that can be rented now, in the offer's data centre; unit is instances. Sku is the listing_id (`docker:887`), because the count is per offer. One host's offers draw on the same cards (8 GPUs with 5 free: 5 x 1 GPU, 2 x 2 GPUs, 1 x 5 GPUs), so never add them up; the host id is kept in raw_value. No total: a fully rented host leaves the list. An offer listed but not rentable on demand (`isAvailableForDemand` false, a host error or maintenance) is `false` with no count. |
| Nosana | `GET /api/nodes/queued-nodes` (no key) | count (free only) | One row per host waiting in a market's queue (`nodeAddress`, `marketAddress`, state `QUEUED`). Nosana's Jobs program docs: "When there a more nodes than jobs in a given Market, the queue will fill up with nodes", and a queued node "will pick up new job postings". So the count is the hosts idle now and ready for a job, per market, provider-wide; unit is instances (one job, one GPU each). Sku is the market slug (the listing sku); only markets that have a listing. A market with no queued host is 0 and `false`: a job posted now waits. No total: busy hosts are not listed. Whether a job in a PREMIUM market can fall through to its linked COMMUNITY market is not documented, so each market counts its own queue only. An empty list fails the read instead of reading as sold out. |
| Hetzner | `GET /_resources/app/data/app/live_data_upfront.json` (no key), joined to the price page by the Robot product id each card carries | boolean | The file Hetzner's own order configurator reads: product number -> the datacenters where it can be ordered now. One row per GEX model and location the price page lists: in stock exactly when the file lists that datacenter for the product. Hetzner does not document the file; the meaning is the configurator's behaviour (a location is selectable only when listed; with none, the button reads "Currently not available"). A product the file does not name at all is unknown, not `false`: the configurator skips the check for some products, and which ones is not in the data read here. Base configuration only (GEX131's larger variants are other products). No count and no delivery time. |
| Hugging Face Endpoints | `GET /v2/provider` (no key), the same call as prices | boolean | Per compute (AWS, GCP or Azure instance in a region): in stock when its `status` is `available` and its region and vendor are up. Resold capacity of clouds OpenGrid also reads directly, so it never votes in market statistics. |
| Denvr, Boost Run, Horizon, Phyntec, Paperspace | Shadeform `availability` | boolean | Whether Shadeform can launch the type in that region now. |
| Massed Compute | Its own `GET /api/v1/gpu-inventory` (API key) | count | `capacity_available`: instances of that product (GPU, count, spot or not) that can be launched now, provider-wide; the in-stock regions (`regions_with_capacity_available`) are kept in raw_value. Count unit is instances. Sku is the listing sku. Counts for different sizes of one GPU draw on the same pool, so never add them up. Without the key, Shadeform's boolean (skus are Shadeform's type names, joined to listings by GPU and count); never both. |

**Not covered, and why.** Nebius, OVHcloud and the other hyperscalers (Azure, Oracle):
need an account. AWS on-demand has no capacity signal (spot placement scores are for spot only). (Google Cloud: with
the read-only service account the poller asks calendar-mode advice, which is future-reservation availability, not
on-demand stock; this project gets "The service is not available for this project." so the answers are stored raw
and no rows are produced, `/methodology/gcp-regions`.) (OVHcloud's anonymous dedicated-server list does give a delivery word, such as `1H-low`, `72H` or
`unavailable`, per GPU server configuration and datacenter; it is stored raw only, because those bare-metal servers
are not listings, so there is no price for a row to join to. For Public Cloud GPU flavors OVHcloud has no anonymous
answer, and its keyed `productAvailability` lists the regions a plan is sold in, not stock.) Vultr:
`GET /v2/regions/{id}/availability` is public and lists the plans available in a region now, but it and every GPU
plan's `locations` are empty for all GPU plans (re-checked 2026-11-15 in seven regions), and nothing says whether
that means sold out or not shown to anonymous callers, so no row is made. Alibaba: its keyed
`DescribeAvailableResource` states stock per zone (`StatusCategory`), but no real answer has been seen yet.
Key needed and not held: Civo (`out_of_capacity`), Gcore (flavor `capacity`), Mithril (`/v2/spot/availability`),
CoreWeave (spot capacity score, which its terms keep confidential). No stock signal found, with or without a key:
Together, IBM, Exoscale, IONOS, Atlantic.Net, E2E, Bitdeer, Sakura, Yotta, Arkane, VESSL, Baseten, Beam, Modal,
Replicate, Fireworks.

## Shape and unit per provider

What each provider's rows hold, in its own unit. "Instance" means one VM, server, node or host of `gpu_count` GPUs;
"stock" means a count of what is free now; an estimate is the provider's own estimate of free capacity. A cap (the
most GPUs one order can take, the largest machine of a type) is never stored as a count.

| Provider | Shape | Unit | Figure | Total? |
|---|---|---|---|---|
| Akash | count | gpu | stock | yes |
| Clore.ai | count | gpu | stock | yes |
| CUDO Compute | count | gpu (VMs), instance (bare metal) | stock | no |
| GPU.ai | count | instance | stock (resold capacity of other clouds) | no |
| Hot Aisle | count; sizes the response omits are no with no count | instance | stock | no |
| Hydra Host | count | instance (one device = one server) | stock | yes (servers listed) |
| Hyperbolic | count where `totalAvailable` is given, else unknown | gpu | stock | no |
| Hyperstack | count; flavors /core/stocks omits are yes/no | instance | stock | no |
| Jarvislabs | count | gpu | stock | no |
| Latitude | count and tier | instance (servers) | stock | no |
| Lium | count | gpu | stock | yes |
| Massed Compute | count (yes/no via Shadeform without its key) | instance | stock | no |
| Nosana | count | instance (hosts queued, one GPU each) | stock | no |
| Salad | count | instance (one GPU each) | estimate | no |
| SimplePod | count | instance (per offer; one host's offers overlap) | stock | no |
| Vast | count | gpu | stock | yes |
| Voltage Park | count (bare metal), yes/no (VM presets) | gpu | stock | no |
| AWS | tier | - | likelihood (spot placement score) | - |
| Koyeb, RunPod, Scaleway | tier | - | level | - |
| Boost Run, Crusoe, DeepInfra, Denvr, DigitalOcean, Hetzner, Horizon, Hugging Face, Lambda, Linode, Paperspace, Phyntec, Thunder Compute, Verda | yes/no | - | - | - |

**Where a count was looked for and does not exist (2026-11-16).** Read-only, keyless checks of each provider's
spec or answer, no account created and nothing launched:

- RunPod: GraphQL `lowestPrice` fields `maxUnreservedGpuCount`, `availableGpuCounts`, `rentedCount`, `totalCount`
  are null for every GPU type, both clouds, with and without `dataCenterId`. REST v2's `maxCount` is defined in its
  own spec as "a ceiling, not a stock level"; GraphQL's `maxGpuCountSecureCloud` / `CommunityCloud` are the same
  ceiling. Tier stays. (Per-datacenter tiers exist, `dataCenters.gpuAvailability` and REST `dataCenters`; not read yet.)
- Verda: the spec has no quantity anywhere; availability calls answer type names or true/false.
- Hyperstack: `/core/stocks` is the only stock call; its 11 yes/no rows are flavors whose GPU model it does not list.
- Hyperbolic: options without `totalAvailable` have no other keyless source.
- Voltage Park: `available_vms` per preset and location (`/virtual-machines/instant/locations`) and free GPUs per
  host node (`/locations/`) exist, behind an account key OpenGrid does not hold.
- Shadeform (Crusoe without its key, Denvr, Boost Run, Horizon, Phyntec, Paperspace, Latitude and Massed Compute
  without theirs): the spec defines `available` as a boolean and nothing else.
- Crusoe: `quantity` is 0 or 1 on every row, CPU types included, so it stays yes/no (above).
- Hugging Face: `quota.maxAccelerators` / `usedAccelerators` are the caller's account quota, not stock.
- DigitalOcean (`/v2/sizes`), Lambda (`regions_with_capacity_available`), Linode (`/v4/regions/availability`),
  Thunder (`/v2/status`), DeepInfra (`gpu_availability`), Hetzner (its configurator file): yes/no only.
- Koyeb (`/v1/catalog/usage`) and Scaleway (`servers/availability`): words only.
- AWS: placement scores stay scores; they are a likelihood, not a count.

**Vast's census cadence.** Vast caps keyed searches at 20,000 result rows a day, so the census does not run every poll:
after each census the next waits rows used / 16,000 of a day (at least 1 hour, at most 12 hours), and a quota refusal
waits out Vast's stated `retry_after`. Vast rows are therefore flagged stale only after 3 x 12 hours, and the
capacity-silence alert for Vast fires after 24 hours rather than 3. Prices use the public search and are unaffected.

## Tier words today

Each provider's own words, mapped one-to-one where the meaning is the same and never guessed otherwise. This mapping is
provisional until the cross-provider standard is agreed; it lives in one dictionary per provider module.

| Provider | Words | Mapped to | Provider's own definition |
|---|---|---|---|
| RunPod | High, Medium, Low, None | high, medium, low; `None` = out of stock | Documented: Low means "very few GPUs available". High and Medium are not defined. |
| Koyeb | HIGH, MEDIUM, LOW | high, medium, low | None published. |
| Latitude | high, medium, low, unavailable | high, medium, low; `unavailable` = out of stock | None published; region-level, so every location in the region gets the same word. |
| Scaleway | available, scarce, shortage | high, low; `shortage` = out of stock | API reference lists the values only. Third-party guides: only `available` guarantees immediate creation; `shortage` fails at creation; `scarce` sometimes still creates. |
| AWS | spot placement score 1-10 | 1-3 low, 4-6 medium, 7-10 high; `in_stock` null | Documented only at the ends: 10 = "highly likely - but not guaranteed - to succeed", 1 = "not likely to succeed at all". The score is for exactly the configuration asked (here one instance, regional, `capacity-optimized` allocation) at that moment; the same request can score differently an hour later. The cut points are OpenGrid's: three near-equal bands over the scale, with 7+ as high because AWS reserves its "highly likely" wording for the top end. The score itself is kept in `raw_value` (`placement score 8/10 (target 1 instance, regional)`). |

## One model, three shapes

Providers report capacity in shapes that cannot be converted into each other without inventing information, so each
observation keeps its shape:

- **`in_stock`** (true / false / null) is filled for every shape and is the only field comparable across providers.
  A tier or a count implies it (`LOW` means in stock; zero free GPUs means not); a boolean is it. The one exception is AWS's spot placement score, a likelihood rather than a stock report: tier only, `in_stock` null.
- **`tier`** (`low` / `medium` / `high`) only where the provider gives a level. A provider word OpenGrid does not
  recognise is never guessed: `tier` and `in_stock` stay null and the word is kept in `raw_value`.
- **`available_count` / `total_count`** only where the provider gives counts (Lium, Massed Compute, ...). Their ratio is real utilization.
- **`raw_value`** is always the provider's own word or number, for audit.

Rows key on `(provider, sku, region)` and use the same `sku` as the provider's price listings, so capacity joins to
price. GPU names map through the same canonical table as prices; a name that does not map (for example Koyeb's
"A100 80GB" with no form factor) is stored with `gpu` null, never guessed.

## Storage and freshness

Capacity is fetched in the same poll as prices and stored raw first (`raw_snapshots`). `capacity_current` holds the
newest state; `capacity_changes` gets a row **only when** `in_stock`, `tier` or a count changes, so a gap means
nothing moved. Both can be rebuilt from the raw snapshots.

A row not refreshed within three of its provider's poll intervals is returned with `stale: true` (for example, the
provider stopped listing that SKU in that region). Capacity runs after prices in the same refresh and fails open: a
capacity error never stops prices from updating.

When a provider changes where its capacity comes from (for example Massed Compute moving from Shadeform's yes/no to
its own API's counts), current rows from the source it no longer uses are removed at the next refresh, so the two
sources are never shown side by side. Their history stays in `capacity_changes`.

A row its provider has stopped reporting (a machine type that left a marketplace, a withdrawn plan) is removed from the current state once it has not been seen for a day, or for six of the provider's capacity reads when those are further apart (Vast's census). Until then it only carries `stale: true`. Its history ends with one row that has no state and says "no longer reported" with the time it was last seen. Only providers that answered in the same run are touched, so a provider whose read failed loses nothing. Before 2026-11-16 such rows stayed for ever with their last state.

## Stock counts per GPU (admin)

`GET /v1/ops/availability/counts?gpu=...` (admin only, internal rows included; code `api/stock_counts.py`) puts every
provider's report for one GPU type in one table, one line per provider and region. Without `gpu`, one table per GPU.

- **GPUs free**: a count in GPUs is taken as stated; a count in instances becomes instances x GPUs per instance
  (`gpu_count`). A count with no stated unit is shown raw and not converted.
- **Overlap**: rows that draw on the same cards are never added. Within one provider and region the line takes the
  largest of them in GPUs and marks it `at_least` (a floor), except for providers whose rows are different machines
  (Akash, Clore, Hydra Host, Latitude, Lium, Nosana, Vast), which are added. SimplePod is taken per host (largest
  offer of each host, summed over hosts), since its raw value names the host.
- **Shape** per line: count, tier, yes-no, or none (rows exist but every state is unknown). Yes/no and tiers never
  become numbers. **Figure**: stock or estimate (Salad) for counts, level or likelihood (AWS) for tiers.
- **Gaps**: a provider that prices the GPU but has no stock row for it is listed as "no stock feed" (or "its stock
  feed has no row for this GPU" when the provider has a feed for other GPUs).
- **Summary line**: GPUs known free, added over count lines only, with resold capacity (GPU.ai, Hugging Face) shown
  apart so another cloud's GPUs are not counted twice; how many more providers are in stock or out of stock by
  yes/no or tier; how many are unknown, gaps included.
- Each line says its age (since the oldest row in it was read) and `stale` on the same rule as `/v1/capacity`.

## What reaches routing and listings

Since 2026-11-16 the stock feeds are not only shown here: routing (`/methodology/best-execution`) and the listing read
use them. Code: `capacity_history.py`.

**In-stock history.** `capacity_changes` is change-only, so a row's state holds from its `observed_at` until the next
row for the same (provider, sku, region). Over 7 and 30 days time is split into in stock, out of stock, unknown (a
row with `in_stock` null: AWS's placement score, a word OpenGrid does not map) and no data (before the first row,
after a "no longer reported" row, and past the stale rule). The state is the row's own `in_stock`, which each
provider's normalizer already derives from its words and counts (a RunPod `Low` is in stock, `None` is not); where only
a count is given, above zero is in stock. No threshold is added. A closing "no longer reported" row is not "out of
stock": the data ends at its "last seen" time plus the stale window. A current row's last state holds until its last
sighting plus the stale window (three capacity intervals), then becomes no data. The in-stock share is in-stock hours
/ hours with a stock answer; it needs 24 such hours. Several rows combine as "in stock when any is" (one region or
size in stock is enough to launch), unknown before out.

**Joining capacity to listings.** Capacity uses the provider's listing `sku` or `listing_id`. Where the two sides
use different words the join is spelled out per provider:

| Providers | Join | Grain |
|---|---|---|
| Lambda, Hyperstack, CUDO, Akash, Thunder Compute, Nosana, Hot Aisle, Hugging Face, SimplePod, Massed Compute (API), Salad, RunPod (pods), Clore, Vast, DeepInfra, Hydra Host (region compared on its first 64 characters), GPU.ai | sku or listing_id, same region or provider-wide | listing. Clore and Vast: one provider-wide row for the median and cheapest listings. GPU.ai: one row per offering, all offerings of a sku and region combine. DeepInfra: the page and API listings share the row. |
| Koyeb, DigitalOcean, Scaleway, Hetzner | sku; the capacity region must be one of the regions the listing is priced for (a joined list) | listing (any of its regions) |
| Verda, Linode (base price), Voltage Park VMs | sku or listing_id; the listing has no region (priced for every location), so every location's row joins | listing (any location) |
| Voltage Park bare metal | sku, and the location id in the listing id (`bm:<id>:<fabric>`) | listing |
| Lium | sku and city; a row with no city joins only listings with no city | listing |
| Jarvislabs | `<sku>:<workload from the listing id>`, on-demand listings only (the counts are on-demand free GPUs) | listing |
| Hyperbolic | `<sku>:<machine>:<link>` from the listing id, same region | listing |
| Shadeform clouds (Latitude, Denvr, Boost Run, Horizon, Phyntec, Paperspace), Crusoe | sku; the listing region is Shadeform's display text for all its regions and cannot be compared with region codes, so every region row joins | listing (any region) |
| Massed Compute without its key | Shadeform type names never equal the page's sku: GPU and GPU count | GPU and size |
| AWS | never: spot placement scores, no in/out answer, and listings are per AZ | none |

A listing that does not join may still be scored on the provider's rows for its GPU combined (persistence only,
never "out of stock now").

**Listing reads.** `GET /v1/listings` adds `stock` (`in_stock` / `out_of_stock` / `unknown`), `stock_source`
(`listing` when the listing states its own stock, `capacity feed` when it is silent and a fresh joined row answers),
`stock_observed_at`, `stock_feed` (a capacity row joins), `stock_rows_stale` and `in_stock_share_30d` (joined rows
only). `available` is unchanged: normalize owns it and nothing here writes it. Computed on read, public capacity rows
only; the point-in-time read (`as_of`) shows the listing's own stock only.

**Routing.** `availability_persistence` is the 30-day in-stock share when the provider reports capacity for the GPU;
`availability_now` takes a fresh joined row's answer for a silent listing and a fresh "out of stock" excludes it
(`sold_out_stock_feed`). Stale rows decide neither. A public ranking reads public rows only.

**Coverage.** `GET /v1/ops/availability/quality` (admin): per provider, the GPUs with a stock feed and their 7- and
30-day in-stock shares, live listings and how many join, rows current, stale and unmapped, and the last read. A
provider with listings and `stock_feed: false` has no stock feed.

## What this does not show

- A provider's own report, not a launch test. "In stock" means the provider says so.
- Tiers are each provider's own scale: Koyeb's `LOW` and another provider's `low` are not calibrated against each other.
- Lium's counts include machines that are listed but may not be reachable; the public data does not say which.
- AWS placement scores are a likelihood for one instance of one type, not a count; AWS says no score guarantees
  that a spot request is fulfilled, and spot instances can still be interrupted after launch.
- Salad's counts are its own estimate of online nodes; its guide says a launch can still find nodes taken from
  lower-priority work, and that nodes may not be reassigned at once.
