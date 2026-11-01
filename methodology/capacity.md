# Provider capacity

How much of each GPU SKU a provider says it can supply, by region, read from the provider's own API.

Kind: **observed** (the provider's own report). Code: `capacity.py`, each provider's `normalize_capacity()` in
`providers/<name>.py`. API: `GET /v1/capacity`, `GET /v1/capacity/history`.

## Providers and what each reports

Twenty-five providers, every one read from data its own module fetches and stores raw first. Most of it was already
being fetched for prices; Koyeb, Linode, Scaleway, Massed Compute, Verda, Hot Aisle and Crusoe each add a capacity source.

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
| Denvr, Boost Run, Horizon, Phyntec | Shadeform `availability` | boolean | Whether Shadeform can launch the type in that region now. |
| Massed Compute | Its own `GET /api/v1/gpu-inventory` (API key) | count | `capacity_available`: instances of that product (GPU, count, spot or not) that can be launched now, provider-wide; the in-stock regions (`regions_with_capacity_available`) are kept in raw_value. Count unit is instances. Sku is the listing sku. Counts for different sizes of one GPU draw on the same pool, so never add them up. Without the key, Shadeform's boolean (skus are Shadeform's type names, joined to listings by GPU and count); never both. |

**Not covered, and why.** Nebius, OVHcloud and the hyperscalers:
need an account. (OVHcloud's anonymous dedicated-server list does give a delivery word, such as `1H-low`, `72H` or
`unavailable`, per GPU server configuration and datacenter; it is stored raw only, because those bare-metal servers
are not listings, so there is no price for a row to join to.) Vultr: its availability endpoint is empty for every GPU plan. CoreWeave, Together, IBM:
no capacity data found.

## Tier words today

Each provider's own words, mapped one-to-one where the meaning is the same and never guessed otherwise. This mapping is
provisional until the cross-provider standard is agreed; it lives in one dictionary per provider module.

| Provider | Words | Mapped to | Provider's own definition |
|---|---|---|---|
| RunPod | High, Medium, Low, None | high, medium, low; `None` = out of stock | Documented: Low means "very few GPUs available". High and Medium are not defined. |
| Koyeb | HIGH, MEDIUM, LOW | high, medium, low | None published. |
| Latitude | high, medium, low, unavailable | high, medium, low; `unavailable` = out of stock | None published; region-level, so every location in the region gets the same word. |
| Scaleway | available, scarce, shortage | high, low; `shortage` = out of stock | API reference lists the values only. Third-party guides: only `available` guarantees immediate creation; `shortage` fails at creation; `scarce` sometimes still creates. |

## One model, three shapes

Providers report capacity in shapes that cannot be converted into each other without inventing information, so each
observation keeps its shape:

- **`in_stock`** (true / false / null) is filled for every shape and is the only field comparable across providers.
  A tier or a count implies it (`LOW` means in stock; zero free GPUs means not); a boolean is it.
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

## What this does not show

- A provider's own report, not a launch test. "In stock" means the provider says so.
- Tiers are each provider's own scale: Koyeb's `LOW` and another provider's `low` are not calibrated against each other.
- Lium's counts include machines that are listed but may not be reachable; the public data does not say which.
- Salad's counts are its own estimate of online nodes; its guide says a launch can still find nodes taken from
  lower-priority work, and that nodes may not be reassigned at once.
