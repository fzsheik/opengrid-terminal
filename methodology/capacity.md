# Provider capacity

How much of each GPU SKU a provider says it can supply, by region, read from the provider's own API.

Kind: **observed** (the provider's own report). Code: `capacity.py`, each provider's `normalize_capacity()` in
`providers/<name>.py`. API: `GET /v1/capacity`, `GET /v1/capacity/history`.

## Providers and what each reports

Twenty providers, every one read from data its own module fetches and stores raw first. Most of it was already
being fetched for prices; Koyeb, Linode, Scaleway, Massed Compute and Verda each add a capacity source.

| Provider | Source | Shape | What the provider says |
|---|---|---|---|
| RunPod | GraphQL `lowestPrice.stockStatus` | tier | `High` / `Medium` / `Low` / `None` per GPU type, cloud (secure / community) and pod size. Pod size matters, so the sku is `<gpu>:<cloud>:<count>` (the listing_id). |
| Koyeb | `GET /v1/catalog/usage` | tier | `LOW` / `MEDIUM` / `HIGH` per instance type and region; types with no per-region word are stored provider-wide. |
| Scaleway | Scaleway's own `.../products/servers/availability`, all 9 zones, paged | tier | `available` / `scarce` / `shortage` per server type and zone. Prices still come from Shadeform. |
| Hyperstack | `GET /core/stocks` + `/core/flavors` | count | Deployable VMs per region, model and size (`configurations`); in-stock flag per flavor; model GPU estimate (`available`, a floor such as "10+") kept in raw_value. |
| Lium | `GET /api/executors` | count | Free GPUs out of all GPUs, summed per machine type and city. |
| Jarvislabs | `GET /misc/server_meta` (API key) | count | Free GPUs per GPU type, region and workload (vm / container), from `effective_num_free_devices` (what Jarvislabs' own SDK routes launches on). Sku is `<gpu>:1x:<workload>`. |
| Voltage Park | `/bare-metal/locations`, `/instant-deploy-presets/` | count, boolean | GPUs in stock per location and fabric; VM presets list the locations with stock. Locations are ids. |
| Hyperbolic | `/v2/alpha/on-demand/rental-options` | count | `totalAvailable` GPUs per option; in stock only when the option is enabled. When `totalAvailable` is missing (as it often is), in_stock is unknown. |
| Linode (Akamai) | `GET /v4/regions/availability` (all pages) | boolean | Whether the plan can be launched in that region now. |
| Verda | `GET /v1/instance-availability`, on-demand and spot (Cloud API credentials) | boolean | Per location, the instance types that can be deployed now. Every priced GPU type gets a row at every location in the response: in stock exactly when the location lists it. Sku is the listing_id (`<type>`, `<type>:spot`); spot is asked separately and differs. Without credentials, no capacity; prices are public either way. |
| Lambda | `regions_with_capacity_available` | boolean | Where there is stock, never how much. A type with no listed region gets one provider-wide `false`. |
| DigitalOcean | `/v2/sizes` `available` + `regions` | boolean | Whether new Droplets can be created with the size, and where. Not confirmed to track live stock. |
| Vast | Keyed census: `POST /api/v0/bundles/`, verified on-demand offers, rentable and not rentable, per GPU model | count (free + total) | Free GPUs = distinct `gpu_ids` in rentable offers; total = each machine's GPUs (`num_gpus / gpu_frac`) over every machine either search returns. Provider-wide. Total means GPUs on verified machines listed now. A census of 512 offers (the search cap) withholds counts. Prices still from the public search. |
| Latitude | Latitude's own `GET /plans` (API key) | count, tier | Per plan and location: in stock (`locations.in_stock`), servers in stock (`in_stock_count`), and the region's `stock_level` as tier. Count unit is servers. Without the key, Shadeform's boolean. Prices still from Shadeform. |
| Crusoe, Denvr, Boost Run, Horizon, Phyntec | Shadeform `availability` | boolean | Whether Shadeform can launch the type in that region now. |
| Massed Compute | Its own `GET /api/v1/gpu-inventory` (API key) | count | `capacity_available`: instances of that product (GPU, count, spot or not) that can be launched now, provider-wide; the in-stock regions (`regions_with_capacity_available`) are kept in raw_value. Count unit is instances. Sku is the listing sku. Counts for different sizes of one GPU draw on the same pool, so never add them up. Without the key, Shadeform's boolean (skus are Shadeform's type names, joined to listings by GPU and count); never both. |

**Not covered, and why.** Salad: a demand flag, not stock. Nebius, OVHcloud and the hyperscalers:
need an account. Vultr: its availability endpoint is empty for every GPU plan. CoreWeave, Together, Hot Aisle, IBM:
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

## What this does not show

- A provider's own report, not a launch test. "In stock" means the provider says so.
- Tiers are each provider's own scale: Koyeb's `LOW` and another provider's `low` are not calibrated against each other.
- Lium's counts include machines that are listed but may not be reachable; the public data does not say which.
