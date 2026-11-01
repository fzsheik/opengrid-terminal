"""How each provider's API fills in ComputeListing.

One entry per provider, declared here rather than buried in the normalizer, so
that adding a provider starts with writing down what maps to what, and so the
differences between providers stay visible.

Render it with `mapping` in the terminal or GET /mapping.

To add a provider: append a ProviderMapping below with one FieldMap per
ComputeListing field, then write the matching normalizer in normalize.py.
`check_mapping_coverage()` fails loudly if the two ever drift apart.
"""

from dataclasses import dataclass, field

from models import ComputeListing

# How a value is obtained.
RAW = "raw"  # copied straight out of a response
DERIVED = "derived"  # computed from one or more raw values
CONSTANT = "constant"  # fixed for this provider
LOOKUP = "lookup"  # resolved through our own tables (canonical.py)
ABSENT = "absent"  # the provider does not expose it; always None


@dataclass(frozen=True)
class FieldMap:
    field: str  # ComputeListing field name
    kind: str  # RAW / DERIVED / CONSTANT / LOOKUP / ABSENT
    source: str  # endpoint and JSON path, a constant value, or the rule
    note: str = ""  # caveat, difference, or why it is like this


@dataclass(frozen=True)
class ProviderMapping:
    provider: str
    display_name: str
    endpoints: dict[str, str]  # endpoint -> what it contributes
    fields: list[FieldMap]
    quirks: list[str] = field(default_factory=list)

    def by_field(self) -> dict[str, FieldMap]:
        return {f.field: f for f in self.fields}


def _usd_only() -> list["FieldMap"]:
    """The FX provenance fields of a provider priced in USD: never converted (fx.py)."""
    return [FieldMap(f, ABSENT, "-", "priced in USD: never converted") for f in FX_FIELDS]


FX_FIELDS = ("original_currency", "original_price_per_gpu_hour", "original_price_per_instance_hour", "fx_rate",
             "fx_rate_date", "fx_source")


SALAD = ProviderMapping(
    provider="salad",
    display_name="SaladCloud",
    endpoints={
        "GET /organizations/{org}/gpu-classes": "GPU classes and their four tier prices",
        "POST /organizations/{org}/availability/sce-gpu-availability": "GPU counts per tier, one call per class",
        "GET /organizations/{org}/quotas": "account limits; not used by the normalizer",
    },
    fields=[
        FieldMap("provider", CONSTANT, '"salad"'),
        FieldMap("sku", RAW, "gpu-classes -> items[].id", "a uuid, not a human name"),
        FieldMap("listing_id", DERIVED, "{class.id}:{priority}", "one listing per class per tier"),
        FieldMap("raw_gpu_name", RAW, "gpu-classes -> items[].name", 'e.g. "RTX 4090 (24 GB)"'),
        FieldMap("canonical_gpu_name", LOOKUP, "canonical.py", "None when unmapped; never guessed"),
        FieldMap("gpu_count", CONSTANT, "1", "API reports no multi-GPU shape at all"),
        FieldMap("region", ABSENT, "-", "Salad exposes no region dimension"),
        FieldMap("country", ABSENT, "-", "follows from region being absent"),
        FieldMap("price_per_gpu_hour", RAW, "gpu-classes -> items[].prices[].price", "string in the JSON"),
        FieldMap("price_per_instance_hour", DERIVED, "= price_per_gpu_hour", "always a single GPU"),
        FieldMap("currency", CONSTANT, '"USD"', "not stated by the API; assumed"),
        FieldMap("market_type", CONSTANT, '"on_demand"', "the four tiers are priority, NOT a spot market"),
        FieldMap("provider_tier", RAW, "items[].prices[].priority", "batch | low | medium | high"),
        FieldMap("interruptible", CONSTANT, "True", "volunteer consumer nodes can drop at any tier"),
        FieldMap("available", DERIVED, "capacity > 0", "no boolean in the API; None if the availability call failed"),
        FieldMap("capacity", RAW, "availability -> available_gpu_{tier}  (a COUNT)", 'Salad\'s "available_gpu_*" holds a NUMBER of free GPUs, not a flag'),
        FieldMap("capacity_unit", CONSTANT, '"gpu"', "individual GPUs, unlike Hyperstack's VMs"),
        FieldMap("vcpu", ABSENT, "-", "not exposed"),
        FieldMap("ram_gb", ABSENT, "-", "not exposed"),
        FieldMap("storage_gb", ABSENT, "-", "not exposed"),
        FieldMap("observed_at", RAW, "raw_snapshots.fetched_at", "provider fetch time, not normalize time"),
        *_usd_only(),
    ],
    quirks=[
        "One GPU class becomes four listings, one per priority tier.",
        "Availability needs a POST per class (~42 calls) and the response carries "
        "no class id, so it is matched back via the stored request body.",
        "capacity counts GPUs here, but VMs on Hyperstack: never sum the two.",
        'Naming trap: Salad\'s "available_gpu_high" is a COUNT of free GPUs, not a '
        "boolean. It feeds capacity; our `available` is then derived as count > 0. "
        "The API exposes no availability flag of its own.",
        "No instance specs at all: vcpu/ram/storage are always None.",
        '"Stable Diffusion Compatible" and a 1070/1080/1080Ti bundle are sold as '
        "GPU classes but are not single GPUs, so both stay unmapped on purpose.",
    ],
)


HYPERSTACK = ProviderMapping(
    provider="hyperstack",
    display_name="Hyperstack (NexGen Cloud Infrahub)",
    endpoints={
        "GET /core/flavors": "the real SKUs: cpu, ram, disk, gpu count, stock flag",
        "GET /pricebook": "hourly rate per GPU, keyed by GPU name",
        "GET /core/stocks": "deployable VM counts per region, model and GPU count",
        "GET /core/regions": "country per region",
        "GET /core/gpus": "which GPU exists in which region; not used by the normalizer",
    },
    fields=[
        FieldMap("provider", CONSTANT, '"hyperstack"'),
        FieldMap("sku", RAW, "flavors -> data[].flavors[].name", 'e.g. "n3-H100x1-bigroot"'),
        FieldMap("listing_id", DERIVED, "{region_name}:{flavor.name}", "flavor names repeat across regions"),
        FieldMap("raw_gpu_name", RAW, "flavors -> flavors[].gpu", 'e.g. "H100-80G-PCIe-spot"'),
        FieldMap("canonical_gpu_name", LOOKUP, "canonical.py, after stripping -spot", "suffix is market, not silicon"),
        FieldMap("gpu_count", RAW, "flavors -> flavors[].gpu_count"),
        FieldMap("region", RAW, "flavors -> flavors[].region_name", "CANADA-1, CANADA-2, NORWAY-1, US-1"),
        FieldMap("country", LOOKUP, "regions -> join on name -> country", "second endpoint needed"),
        FieldMap("price_per_gpu_hour", LOOKUP, "pricebook -> value where name == flavor.gpu", "separate endpoint"),
        FieldMap("price_per_instance_hour", DERIVED, "= price x gpu_count", "GPUs bill per GPU; cpu/ram/disk included"),
        FieldMap("currency", CONSTANT, '"USD"', "not stated by the API; assumed"),
        FieldMap("market_type", DERIVED, '"-spot" suffix ? spot : on_demand'),
        FieldMap("provider_tier", ABSENT, "-", "no tier concept; spot/on-demand is the whole story"),
        FieldMap("interruptible", DERIVED, "= (market_type == spot)"),
        FieldMap("available", RAW, "flavors -> flavors[].stock_available", "the flavor's own flag, more precise than counts"),
        FieldMap("capacity", LOOKUP, "stocks -> configurations['{n}x'] by (region, gpu, count)", "counts deployable VMs"),
        FieldMap("capacity_unit", CONSTANT, '"instance"'),
        FieldMap("vcpu", RAW, "flavors -> flavors[].cpu"),
        FieldMap("ram_gb", RAW, "flavors -> flavors[].ram"),
        FieldMap("storage_gb", RAW, "flavors -> flavors[].disk", "bigroot SKUs differ only here"),
        FieldMap("observed_at", RAW, "raw_snapshots.fetched_at", "provider fetch time, not normalize time"),
        *_usd_only(),
    ],
    quirks=[
        "Listings come from /core/flavors, NOT from the stock configurations map: "
        "that map reports sizes such as 10x H100 for which no flavor exists.",
        "Sibling flavors differing only by disk (n3-H100x1 vs n3-H100x1-bigroot) "
        "share one capacity number, because stock is per (region, model, count).",
        "One listing needs four endpoints joined: flavors, pricebook, stocks, regions.",
        "The pricebook prices GPUs that are never sold as flavors (RTX-4090, "
        "H100-80G-SXM5-IB, L40-sm). Those are kept in reference_prices, not listings.",
        "capacity counts VMs here, but GPUs on Salad: an 8x VM counts as 1.",
    ],
)


LAMBDA = ProviderMapping(
    provider="lambda",
    display_name="Lambda (Lambda Labs Cloud)",
    endpoints={
        "GET /instance-types": "everything: price, specs and which regions have capacity",
        "GET /images": "OS images; captured raw, not used by the normalizer",
        "GET /file-systems": "account filesystems; captured raw, not used by the normalizer",
    },
    fields=[
        FieldMap("provider", CONSTANT, '"lambda"'),
        FieldMap("sku", RAW, "instance-types -> data[].instance_type.name", 'e.g. "gpu_8x_h100_sxm5"'),
        FieldMap("listing_id", DERIVED, "{instance_type.name}:{region or 'any'}", "one listing per region with capacity"),
        FieldMap("raw_gpu_name", RAW, "data[].instance_type.gpu_description", 'e.g. "H100 (80 GB SXM5)"'),
        FieldMap("canonical_gpu_name", LOOKUP, "canonical.py", "None when unmapped; never guessed"),
        FieldMap("gpu_count", RAW, "data[].instance_type.specs.gpus", "0 for CPU-only types, which are skipped"),
        FieldMap("region", RAW, "data[].regions_with_capacity_available[].name", "None when sold out everywhere"),
        FieldMap("country", DERIVED, 'region prefix, "us-*" -> US', "region description also names the city"),
        FieldMap("price_per_gpu_hour", DERIVED, "price_cents_per_hour / 100 / specs.gpus", "DIVIDED: the API quotes the whole instance"),
        FieldMap("price_per_instance_hour", DERIVED, "price_cents_per_hour / 100", "the native figure; cents, not dollars"),
        FieldMap("currency", CONSTANT, '"USD"', "not stated by the API; assumed"),
        FieldMap("market_type", CONSTANT, '"on_demand"', "no spot market on the public API"),
        FieldMap("provider_tier", ABSENT, "-", "no tier concept"),
        FieldMap("interruptible", CONSTANT, "False", "dedicated VMs, not preemptible"),
        FieldMap("available", DERIVED, "region is in regions_with_capacity_available"),
        FieldMap("capacity", ABSENT, "-", "Lambda says WHERE there is stock, never HOW MUCH"),
        FieldMap("capacity_unit", ABSENT, "-", "nothing to put a unit on"),
        FieldMap("vcpu", RAW, "data[].instance_type.specs.vcpus", "whole instance, not per GPU"),
        FieldMap("ram_gb", RAW, "data[].instance_type.specs.memory_gib"),
        FieldMap("storage_gb", RAW, "data[].instance_type.specs.storage_gib"),
        FieldMap("observed_at", RAW, "raw_snapshots.fetched_at", "provider fetch time, not normalize time"),
        *_usd_only(),
    ],
    quirks=[
        "Price is in CENTS and per INSTANCE, the inverse of Hyperstack's per-GPU "
        "dollars. Getting it backwards makes an 8x H100 box look 8x cheap.",
        "capacity is a list of regions, never a number: we learn where there is "
        "stock but not how deep it is, so capacity and capacity_unit stay None.",
        "One instance type fans out to one listing per region with capacity. A "
        "type with an empty list still gets one listing with region=None, so its "
        "published price keeps being tracked while it is sold out.",
        "The published OpenAPI advertises data.types[] plus a separate "
        "regions_by_instance_type map. The live API returns neither: data is a "
        "dict keyed by instance type name. Verified against the real endpoint.",
        "CPU-only types (specs.gpus == 0) are skipped, as with Hyperstack flavors.",
        "A100 here is 40GB, but 80GB on Hyperstack; B200 here is 180GB, but 192GB "
        "on Hyperstack. Same model number, different card: distinct canonical names.",
        '"RTX 6000 (24 GB)" is the Turing Quadro RTX 6000, NOT the RTX 6000 Ada 48GB.',
        "One endpoint covers the whole provider, versus 4 for Hyperstack and 43 for Salad.",
    ],
)


RUNPOD = ProviderMapping(
    provider="runpod",
    display_name="RunPod",
    endpoints={
        "POST /graphql  { gpuTypes { ... } }": "catalogue: per-GPU price per cloud, VRAM, manufacturer",
        "POST /graphql  lowestPrice(gpuCount, secureCloud)": "price, stock status and pod minimums per cloud per size",
    },
    fields=[
        FieldMap("provider", CONSTANT, '"runpod"'),
        FieldMap("sku", DERIVED, "{gpuTypes[].id}:{cloud}", 'e.g. "NVIDIA A100 80GB PCIe:community"'),
        FieldMap("listing_id", DERIVED, "{id}:{cloud}:{gpuCount}", "one listing per cloud per pod size"),
        FieldMap("raw_gpu_name", RAW, "gpuTypes[].id", "the id, not displayName: it carries VRAM and variant"),
        FieldMap("canonical_gpu_name", LOOKUP, "canonical.py", "None when unmapped; never guessed"),
        FieldMap("gpu_count", DERIVED, "the gpuCount asked of lowestPrice", "1, 2, 4 or 8"),
        FieldMap("region", ABSENT, "-", "datacenter is knowable but costs ~50x the requests: see quirks"),
        FieldMap("country", ABSENT, "-", "follows from region being absent"),
        FieldMap("price_per_gpu_hour", DERIVED, "lowestPrice.uninterruptablePrice / gpuCount"),
        FieldMap("price_per_instance_hour", RAW, "lowestPrice(cloud, count).uninterruptablePrice", "pod total, cloud- and size-specific"),
        FieldMap("currency", CONSTANT, '"USD"', "not stated by the API; assumed"),
        FieldMap("market_type", CONSTANT, '"on_demand"', "spot fields duplicate on-demand exactly: see quirks"),
        FieldMap("provider_tier", DERIVED, '"secure" | "community"', "RunPod's two clouds: datacenter vs peer-hosted"),
        FieldMap("interruptible", DERIVED, "= (tier == community)", "community hosts are peer machines"),
        FieldMap("available", DERIVED, 'stockStatus not in (null, "None")'),
        FieldMap("capacity", ABSENT, "-", "stockStatus is a High/Medium/Low bucket, never a number"),
        FieldMap("capacity_unit", ABSENT, "-", "nothing to put a unit on"),
        FieldMap("vcpu", RAW, "lowestPrice.minVcpu", "pod minimum for that cloud and size"),
        FieldMap("ram_gb", RAW, "lowestPrice.minMemory", "pod minimum, in GB"),
        FieldMap("storage_gb", RAW, "lowestPrice.minDisk", "almost always null"),
        FieldMap("observed_at", RAW, "raw_snapshots.fetched_at", "provider fetch time, not normalize time"),
        *_usd_only(),
    ],
    quirks=[
        "GraphQL only. The REST API at rest.runpod.io/v1 has no GPU pricing "
        "endpoint, and introspection is disabled, so every field was validated "
        "by querying it against the live endpoint.",
        "NO SPOT MARKET IN THE DATA. secureSpotPrice == securePrice for all 50 "
        "types, communitySpotPrice == communityPrice for all 50, and "
        "minimumBidPrice == uninterruptablePrice in every priced combo. "
        "Modelling a spot listing would invent a market the API does not show.",
        "Two clouds, and lowestPrice takes a secureCloud flag, so each cloud "
        "reports its own price, stock and pod minimums. 49 of 200 (type, size) "
        "combos have genuinely different stock between the two clouds.",
        "Community is usually cheaper: A100 PCIe is 1.59 secure vs 1.19 community.",
        "The cloud booleans disagree with the prices: 9 types have a non-zero "
        "securePrice while secureCloud is false. Neither is used as the "
        "availability signal; a priced lowestPrice result is.",
        "Datacenter IS knowable but is left out: lowestPrice takes a "
        "dataCenterId filter and returns per-DC stock (RTX 4090 secure sits in "
        "6 of 51), but LowestPrice has no field naming the DC it found, so "
        "learning location means one request per datacenter, about 50x the "
        "cost. region stays None until that runs as a slower second pass.",
        "Community Cloud has no datacenter at all: 16 types have community "
        "stock unfiltered, and filtering any of them by dataCenterId returns "
        "nothing across all 51. Peer-hosted machines are not in RunPod's own "
        "facilities, so region would stay None for community regardless.",
        "rentedCount, totalCount, rentalPercentage and availableGpuCounts all "
        "exist on LowestPrice and are null in every combination tried, "
        "filtered or not. There is no capacity number to be had.",
        "oneWeekPrice and oneMonthPrice are null for all 50 types; three- and "
        "six-month reserved prices exist for ~32 but are not modelled as listings.",
        'The catalogue includes MIG slices ("B300 SXM6 AC MIG 1g.34gb") and one '
        'entry literally called "unknown" with zero price and zero VRAM.',
    ],
)


def _fields(provider: str, **given: FieldMap | tuple) -> list[FieldMap]:
    """Every ComputeListing field for a provider, in model order.

    The fields almost every provider treats the same way are filled in; pass
    `name=(KIND, source, note)` to say how this provider differs. A field that
    is neither defaulted nor given is left out, so check_mapping_coverage()
    reports it.
    """
    defaults: dict[str, tuple] = {
        "provider": (CONSTANT, f'"{provider}"', ""),
        "canonical_gpu_name": (LOOKUP, "canonical.py", "None when unmapped; never guessed"),
        "currency": (CONSTANT, '"USD"', "not stated by the API; assumed"),
        "market_type": (CONSTANT, '"on_demand"', ""),
        "interruptible": (CONSTANT, "False", ""),
        "observed_at": (RAW, "raw_snapshots.fetched_at", "provider fetch time, not normalize time"),
        # FX provenance (fx.py): only a provider priced in another currency fills these; see SCALEWAY.
        **{f: (ABSENT, "-", "priced in USD: never converted") for f in FX_FIELDS},
    }
    merged = {**defaults, **given}
    return [FieldMap(f, *merged[f]) for f in MODEL_FIELDS if f in merged]


MODEL_FIELDS = list(ComputeListing.model_fields)


HYPERBOLIC = ProviderMapping(
    provider="hyperbolic",
    display_name="Hyperbolic",
    endpoints={"GET /v2/alpha/on-demand/rental-options": "every rentable shape, anonymous, no key"},
    fields=_fields(
        "hyperbolic",
        sku=(DERIVED, "{gpuType} {gpuFormFactor}:{gpuCount}x", ""),
        listing_id=(DERIVED, "{gpu}:{n}x:{region}:{machineType}:{connectionType}", "one option per shape and fabric"),
        raw_gpu_name=(DERIVED, "gpuType + gpuFormFactor", 'e.g. "h200 sxm5"'),
        gpu_count=(RAW, "gpuCount"),
        region=(RAW, "region"),
        country=(ABSENT, "-", "region only; no country field"),
        price_per_gpu_hour=(DERIVED, "= costPerHourCents / 100 / gpuCount", "cents for the WHOLE option"),
        price_per_instance_hour=(DERIVED, "= costPerHourCents / 100"),
        provider_tier=(RAW, "machineType", "virtual-machine | bare-metal"),
        available=(DERIVED, "enabled and totalAvailable > 0", "None when totalAvailable is missing: unknown, not zero"),
        capacity=(RAW, "totalAvailable", "same figure on every size of one pool; not additive"),
        capacity_unit=(CONSTANT, '"gpu"', "assumed from 32 shown against 8 to 32 GPU shapes"),
        vcpu=(DERIVED, "sum(nodes[].vcpuCount)", "only when the nodes add up to gpuCount"),
        ram_gb=(DERIVED, "sum(nodes[].ramGb)", "same condition"),
        storage_gb=(DERIVED, "sum(nodes[].storageGb)", "same condition"),
    ),
    quirks=[
        "costPerHourCents prices the WHOLE option, so the per-GPU rate is cents / gpuCount / 100.",
        "The list is only what is rentable right now: sizes appear and disappear with inventory.",
        "The API also exposes Hyperbolic's supplier cost; it is deliberately not stored.",
    ],
)


VOLTAGEPARK = ProviderMapping(
    provider="voltagepark",
    display_name="Voltage Park",
    endpoints={
        "GET /bare-metal/locations": "per-GPU price on whole nodes, ethernet or InfiniBand, with GPUs in stock",
        "GET /instant-deploy-presets/": "VM shapes with a per-instance rate and the locations with stock",
    },
    fields=_fields(
        "voltagepark",
        sku=(DERIVED, "{gpu_model}:{bare-metal:fabric | vm:Nx}", ""),
        listing_id=(DERIVED, "bm:{location id}:{fabric} | vm:{preset id}", "one bare-metal listing per fabric"),
        raw_gpu_name=(RAW, "specs_per_node.gpu_model | the resources.gpus key", 'e.g. "h100-sxm5-80gb"'),
        gpu_count=(RAW, "specs_per_node.gpu_count | resources.gpus.*.count", "bare metal sells whole nodes"),
        region=(ABSENT, "-", "locations are UUIDs with no name"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(RAW, "gpu_price_{fabric} | compute_rate_hourly / count", "VM rate is per instance"),
        price_per_instance_hour=(DERIVED, "= per-GPU price x node GPUs | compute_rate_hourly", "VM rate excludes storage"),
        provider_tier=(DERIVED, "bare-metal-ethernet | bare-metal-infiniband | vm"),
        available=(DERIVED, "gpu_count_{fabric} > 0 | any location with stock"),
        capacity=(RAW, "gpu_count_{fabric} | len(location_ids_with_availability)", "GPUs on bare metal, LOCATIONS on VMs: never sum"),
        capacity_unit=(DERIVED, '"gpu" | "location"'),
        vcpu=(RAW, "specs_per_node.cpu_count | resources.vcpu_count"),
        ram_gb=(RAW, "specs_per_node.ram_gb | resources.ram_gb"),
        storage_gb=(RAW, "specs_per_node.storage_gb | resources.storage_gb"),
    ),
    quirks=[
        "Two price surfaces for the same hardware: bare metal quotes per GPU, VMs quote per instance.",
        "Bare metal carries two per-GPU prices, one per fabric, so one location is two listings.",
        "The VM presets carry no region; stock is a list of location ids with no names.",
    ],
)


LIUM = ProviderMapping(
    provider="lium",
    display_name="Lium (marketplace)",
    endpoints={"GET /api/executors": "every host machine for rent; about 1 MB, mostly live telemetry"},
    fields=_fields(
        "lium",
        sku=(RAW, "machine_name"),
        listing_id=(RAW, "id", "one per host machine"),
        raw_gpu_name=(RAW, "machine_name", 'e.g. "NVIDIA H100 80GB HBM3"'),
        gpu_count=(RAW, "gpu_count", "GPUs in the host; a renter may take fewer"),
        region=(RAW, "location.city"),
        country=(RAW, "location.country_code"),
        price_per_gpu_hour=(RAW, "price_per_gpu"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu x gpu_count", "the whole host"),
        market_type=(DERIVED, "specs.is_spot ? spot : on_demand"),
        provider_tier=(RAW, "tier", "secure | spot"),
        interruptible=(DERIVED, "= specs.is_spot"),
        available=(DERIVED, "available_gpu_count > 0"),
        capacity=(RAW, "available_gpu_count"),
        capacity_unit=(CONSTANT, '"gpu"'),
        vcpu=(RAW, "specs.cpu.count"),
        ram_gb=(DERIVED, "specs.ram.total / 1024^2", "units look like KiB; unverified"),
        storage_gb=(DERIVED, "specs.hard_disk.total / 1024^2", "units look like KiB; unverified"),
    ),
    quirks=[
        "A marketplace: each host sets its own price, so the same GPU spans a wide range.",
        "The raw body changes on every request (live GPU utilization), so only the listing "
        "fields are stored; the rest is not kept.",
        "ram and disk totals are assumed to be KiB: they only make sense as KiB for a "
        "192-core host, but the API does not say.",
    ],
)


VAST = ProviderMapping(
    provider="vast",
    display_name="Vast.ai (marketplace)",
    endpoints={"GET /api/v0/bundles/": "host offers, queried once per GPU model; verified, rentable, on-demand only"},
    fields=_fields(
        "vast",
        sku=(RAW, "gpu_name"),
        listing_id=(DERIVED, "{gpu_name}:{median|cheapest}", "a summary of a book, not one machine"),
        raw_gpu_name=(RAW, "offers[].gpu_name", 'e.g. "H100 SXM"'),
        gpu_count=(CONSTANT, "1", "priced per single GPU: dph_total / num_gpus"),
        region=(RAW, "geolocation of the cheapest offer", "None on the median row"),
        country=(ABSENT, "-", "geolocation is free text"),
        price_per_gpu_hour=(DERIVED, "median or minimum of dph_total / num_gpus", "over the offers returned"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        provider_tier=(CONSTANT, '"median" | "cheapest"'),
        available=(CONSTANT, "True", "a row exists only if a rentable offer does"),
        capacity=(DERIVED, "sum(num_gpus) over offers", "None when the book hit the API cap"),
        capacity_unit=(DERIVED, '"gpu"; None when capped'),
        vcpu=(RAW, "cheapest offer's cpu_cores_effective", "None on the median row"),
        ram_gb=(DERIVED, "cheapest offer's cpu_ram / 1024", "None on the median row"),
        storage_gb=(RAW, "cheapest offer's disk_space", "None on the median row"),
    ),
    quirks=[
        "The API returns at most 64 offers per query, whatever the limit, so a popular "
        "model's book is cut off: its median is over the 64 cheapest and capacity is withheld.",
        "Two listings per model: the median ask is the typical price; the cheapest is what "
        "a router would grab and is often a single host.",
        "Only verified hosts are queried. Unverified hosts are often cheaper.",
        "Some Vast names hide a memory size (A100 SXM4 is 40 or 80GB) and stay unmapped.",
    ],
)


NEBIUS = ProviderMapping(
    provider="nebius",
    display_name="Nebius",
    endpoints={
        "GET /prices": "the public price page (nebius.com); the GPU table is a block in its __NEXT_DATA__ JSON: on-demand",
        "GET /billing/v1alpha1/spot-price": "public spot-price API (api.nebius.cloud, no authentication), paged; "
                                            "one item per SKU (GPU platform x region): spot",
    },
    fields=_fields(
        "nebius",
        sku=(DERIVED, 'page: table Item; spot: "{Item}:{platform}"'),
        listing_id=(DERIVED, 'page: table Item; spot: "{Item}:{platform}:{region}:spot"',
                    "the on-demand ids are unchanged since the first fetch"),
        raw_gpu_name=(LOOKUP, 'page: table Item; spot: computeInstance.platform via PLATFORMS',
                      'e.g. "NVIDIA HGX H100" for both; an unknown platform keeps its productTitle'),
        gpu_count=(CONSTANT, "1", "both price one GPU-hour"),
        region=(RAW, "spot: region", 'e.g. "eu-north1"; the page has one price list with no region (None)'),
        country=(LOOKUP, "spot: region via COUNTRY (docs.nebius.com regions)", "None on the page"),
        price_per_gpu_hour=(RAW, 'page: the current on-demand GPU-hour column; spot: price.value',
                            "page column picked by date when a dated column is present; see quirks"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        market_type=(DERIVED, 'page -> "on_demand", spot API -> "spot"'),
        provider_tier=(DERIVED, '"from_price" for "from $x" page cells, else None', "a floor, not a quote"),
        interruptible=(DERIVED, "True for spot"),
        available=(ABSENT, "-", "neither source says anything about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "page: table row vCPUs", "per-GPU share; None for a range and for spot"),
        ram_gb=(RAW, "page: table row RAM, GB", "per-GPU share; None for a range and for spot"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        'The GPU table is the block titled "NVIDIA GPU Instances" in the page\'s __NEXT_DATA__ JSON. Since late Oct '
        '2026 it carries a "Preemptible, GPU-hour" column ("from $x" spot floors) next to "On-demand, GPU-hour"; '
        "that column is stored but never a listing. Spot prices come from the spot-price API.",
        'When a price change is announced the page adds a "GPU-hour (Effective <date>)" column; we use it once its '
        "date has arrived, else the plain one.",
        '"Contact us" rows (GB200/GB300 NVL72) have no price and are skipped.',
        '"NVIDIA RTX PRO 6000" names no edition on the page; Nebius\'s docs (VM types) say Server Edition, so the '
        'Nebius lookup (DOC_NAMES in providers/nebius.py) maps it there on both markets; the generic name stays unmapped elsewhere.',
        'L40S prices are "from $x": a floor over several CPU configs, tagged from_price.',
        "The spot API has spot prices only (no on-demand, reserved or commitment fields). Its 30-day history "
        "(/spot-price/list-series) is not polled.",
        "The two sources fail independently: a reshaped page (no block, two blocks, an unexpected column) or a spot "
        "response with zero readable items fails that call only, never the other market.",
    ],
)


MASSEDCOMPUTE = ProviderMapping(
    provider="massedcompute",
    display_name="Massed Compute",
    endpoints={
        "GET /api/v1/gpu-inventory": "official inventory API (Bearer MASSEDCOMPUTE_API_KEY); one entry per product, with price and capacity_available",
        "GET /pricing": "the public price page, one table of shapes per GPU model; read only without the key, or when the API fails",
    },
    fields=_fields(
        "massedcompute",
        sku=(DERIVED, "{model}:{n}x (+ :spot)", "model and n from instance_type.description; the page's heading and Qty"),
        listing_id=(DERIVED, "= sku"),
        raw_gpu_name=(RAW, "instance_type.description after the \"Nx \" (page: model heading)", 'e.g. "A100 SXM4 (80GB)"; "[Spot]" dropped'),
        gpu_count=(RAW, "description \"Nx\" (page: table Qty)", 'printed "x 8" on the page'),
        region=(ABSENT, "-", "one price list; the in-stock regions are capacity, not price"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= instance price / count"),
        price_per_instance_hour=(RAW, "price_cents_per_hour / 100 (page: table Price)", 'page prints "$52.80 /hr"'),
        market_type=(DERIVED, '"spot" for "_spot" products ("[Spot]"), else "on_demand"', "the page lists on-demand only"),
        interruptible=(DERIVED, "= spot"),
        provider_tier=(ABSENT, "-"),
        available=(ABSENT, "-", "stock is in capacity (capacity_available), not on the price row"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "specs.vcpu_count (page: table vCPU)"),
        ram_gb=(RAW, "specs.memory_gib (page: table RAM)", "GiB per the API; the page prints GB, same numbers"),
        storage_gb=(RAW, "specs.storage_gb (page: table Storage)"),
    ),
    quirks=[
        "With the key: the API only (one source per poll); the page is read without the key or when the API call fails.",
        "API descriptions carry the page's model text, so skus, listing ids and GPU mapping continue from the scraper.",
        "The API call never follows redirects: a bad token or wrong path answers 307 to /login, recorded as a failure.",
        "CPU-only products (cpu_*) are skipped.",
        'Several headings hide a variant ("H100 (80GB)" is PCIe or SXM), so they stay unmapped.',
        'Config variants ("[Premium]", "[ALT Config]", "NVLink") are separate headings and listings.',
    ],
)


DIGITALOCEAN = ProviderMapping(
    provider="digitalocean",
    display_name="DigitalOcean (GPU Droplets)",
    endpoints={"GET /v2/sizes": "every Droplet size; GPU ones carry a gpu_info block"},
    fields=_fields(
        "digitalocean",
        sku=(RAW, "sizes[].slug", 'e.g. "gpu-h100x8-640gb"'),
        listing_id=(RAW, "sizes[].slug"),
        raw_gpu_name=(RAW, "sizes[].gpu_info.model", 'e.g. "nvidia_h100"'),
        gpu_count=(RAW, "sizes[].gpu_info.count"),
        region=(RAW, "sizes[].regions joined", "None when the list is empty"),
        country=(LOOKUP, "region prefix", "only when every listed region is in one country"),
        price_per_gpu_hour=(DERIVED, "= price_hourly / gpu_info.count", "price_hourly is the whole Droplet"),
        price_per_instance_hour=(RAW, "sizes[].price_hourly"),
        market_type=(DERIVED, 'slug ends "-spot" ? spot : on_demand'),
        provider_tier=(DERIVED, '"liquid-cooled" for "-lc" slugs'),
        interruptible=(DERIVED, "= (market_type == spot)"),
        available=(DERIVED, "regions non-empty and available", "None when regions is empty: unknown, not sold out"),
        capacity=(ABSENT, "-", "no stock counts"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "sizes[].vcpus"),
        ram_gb=(DERIVED, "sizes[].memory / 1024", "memory is in MB"),
        storage_gb=(RAW, "sizes[].disk"),
    ),
    quirks=[
        "price_hourly prices the WHOLE Droplet, so an 8x size divides by 8 for the per-GPU rate.",
        'AMD sizes sit beside NVIDIA ones; "-spot" sizes are interruptible and kept apart.',
        "Many GPU sizes list no regions although `available` is true. We call that unknown, "
        "not in stock, because the API does not say where they deploy.",
        "Paperspace is a DigitalOcean company but has its own API; this token does not work there.",
    ],
)


AWS = ProviderMapping(
    provider="aws",
    display_name="AWS EC2 (hyperscaler list price)",
    endpoints={"GET awsstatic price file": "every EC2 type in us-east-1, Linux; no credentials",
               "GET spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json":
                   "Spot Advisor interruption band and savings per GPU type, region and OS; stored raw only",
               "POST ec2.<region>.amazonaws.com DescribeSpotPriceHistory":
                   "effective Linux spot price now per GPU type and AZ, six regions (read-only IAM key, SigV4)",
               "POST ec2.us-east-1.amazonaws.com GetSpotPlacementScores":
                   "capacity only (capacity tables): 1-10 score per region, five GPU types, every 2 h (key)",
               "POST ec2.us-east-1.amazonaws.com DescribeCapacityBlockOfferings":
                   "Capacity Block offerings, 1 instance x 24 h, daily; stored raw only (key)"},
    fields=_fields(
        "aws",
        sku=(RAW, "Instance Type / instanceType", 'e.g. "p5.48xlarge"'),
        listing_id=(DERIVED, "{Instance Type}:us-east-1 | {instanceType}:{availabilityZone}:spot",
                    "one on-demand listing per type; one spot listing per type and AZ"),
        raw_gpu_name=(LOOKUP, "GPU_INSTANCES in providers/aws.py", "the price file has no GPU model; ours does"),
        gpu_count=(LOOKUP, "GPU_INSTANCES in providers/aws.py", "the price file has no GPU count either"),
        region=(DERIVED, '"us-east-1" | availabilityZone', "one region per price file; spot is per AZ"),
        country=(LOOKUP, "SPOT_REGIONS in providers/aws.py", '"US" for the price file'),
        price_per_gpu_hour=(DERIVED, "= price / gpu_count"),
        price_per_instance_hour=(RAW, "price | spotPrice", "on-demand: Linux, shared tenancy; spot: Linux/UNIX"),
        market_type=(DERIVED, "price file -> on_demand, DescribeSpotPriceHistory -> spot"),
        interruptible=(DERIVED, "= (market_type == spot)"),
        provider_tier=(CONSTANT, '"hyperscaler"'),
        available=(ABSENT, "-", "neither a price file nor a spot price says anything about stock"),
        capacity=(ABSENT, "-", "spot placement scores are capacity rows (/v1/capacity), not listing fields"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "vCPU", "spot: the price file's value for the same type, else none"),
        ram_gb=(DERIVED, "Memory, parsed from '2048 GiB'", "spot: the price file's value for the same type"),
        storage_gb=(ABSENT, "-", "EBS-only or local NVMe is not normalized"),
    ),
    quirks=[
        "The GPU model and count are NOT in the price file; they come from a hand-kept table "
        "(GPU_INSTANCES), cross-checked against DescribeInstanceTypes. A new instance family is logged and "
        "skipped, never guessed.",
        "List prices: big buyers rarely pay them, so AWS sits well above the neoclouds. "
        "p5.48xlarge is 8x H100 at $55.04/hr = $6.88 per GPU-hour.",
        "On-demand: only us-east-1 Linux. Polled hourly; list prices change rarely.",
        "Spot (with the read-only key): DescribeSpotPriceHistory with StartTime = EndTime = now returns the price "
        "in effect now per type and AZ, in us-east-1, us-east-2, us-west-2, eu-west-2, eu-north-1 and "
        "ap-northeast-1. A region answer with no GPU row is a failed fetch.",
        "The Spot Advisor feed (interruption band r 0-4 = <5% .. >20%, savings s %) is stored raw with its "
        "Last-Modified header and read only by spot_advice(); never shown on public pages or APIs (AWS site "
        "terms). Routing has no hook for it: best execution ranks on-demand only and reliability is OpenGrid's "
        "own launches only. It was last rewritten 20 Mar 2026.",
    ],
)


VERDA = ProviderMapping(
    provider="verda",
    display_name="Verda (formerly DataCrunch)",
    endpoints={"GET /v1/instance-types": "every instance type with on-demand and spot price; anonymous",
               "GET /v1/instance-availability": "capacity only (capacity tables), with Cloud API credentials"},
    fields=_fields(
        "verda",
        sku=(RAW, "instance_type", 'e.g. "8H100.80S.176V"'),
        listing_id=(DERIVED, "{instance_type} | {instance_type}:spot", "one on-demand and one spot listing per type"),
        raw_gpu_name=(RAW, "name", 'e.g. "H100 SXM5 80GB"'),
        gpu_count=(RAW, "gpu.number_of_gpus"),
        region=(ABSENT, "-", "the catalogue has no regions"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= price / gpu.number_of_gpus"),
        price_per_instance_hour=(RAW, "price_per_hour | spot_price", "both are for the whole instance"),
        market_type=(DERIVED, "price_per_hour -> on_demand, spot_price -> spot"),
        provider_tier=(DERIVED, '"confidential" for ".CC" types (also ".CC.AMD" / ".CC.INTEL")'),
        interruptible=(DERIVED, "= (market_type == spot)"),
        available=(ABSENT, "-", "listings are provider-wide; per-location stock is in the capacity tables (credentials)"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "cpu.number_of_cores"),
        ram_gb=(RAW, "memory.size_in_gigabytes"),
        storage_gb=(RAW, "storage.size_in_gigabytes", "None if the field is absent"),
        currency=(DERIVED, '"USD"; an EUR-only type is converted', "currency field per type"),
        original_currency=(RAW, "currency", "set only on an EUR-only type (none seen as of 2026-11-01)"),
        original_price_per_gpu_hour=(DERIVED, "EUR price / gpu.number_of_gpus", "EUR-only types"),
        original_price_per_instance_hour=(RAW, "price_per_hour | spot_price in EUR", "EUR-only types"),
        fx_rate=(LOOKUP, "fx_rates (ECB EUR->USD)", "EUR-only types; fail closed when stale (methodology/fx.md)"),
        fx_rate_date=(LOOKUP, "fx_rates.rate_date", "EUR-only types"),
        fx_source=(CONSTANT, '"ecb"', "EUR-only types"),
    ),
    quirks=[
        "Prices are for the whole instance, on-demand and spot alike; 8x types divide by 8.",
        "A type priced in EUR only is converted at the ECB rate; a type priced in both is read in USD only. "
        "Any other currency is skipped. Every type was USD on 2026-11-01.",
        "Spot is exactly half the on-demand price on every type seen, so it is kept apart "
        "as interruptible and never compared with on-demand.",
        'Confidential-computing (".CC") types cost slightly more for the same GPU and stay unmapped.',
        'The B300 is listed at 268GB (usable) against the 288GB others print; it shares one name.',
        '"RTX PRO 6000 96GB" names no edition; Verda\'s product page says Server Edition, so the lookup uses that '
        "name (PAGE_NAMES in providers/verda.py) while raw_gpu_name keeps the API text.",
    ],
)


def _shadeform(provider: str, display: str, extra_quirks: list[str]) -> ProviderMapping:
    return ProviderMapping(
        provider=provider,
        display_name=f"{display} (via Shadeform)",
        endpoints={"GET api.shadeform.ai/v1/instances/types": f"all clouds' types; only cloud={provider} is kept"},
        fields=_fields(
            provider,
            sku=(RAW, "cloud_instance_type", "the cloud's own name for the type"),
            listing_id=(RAW, "shade_instance_type", "Shadeform's name for the type"),
            raw_gpu_name=(DERIVED, "gpu_type + SXM/PCIe variant (+ nvlink)", 'e.g. "A100_80G sxm"; see quirks'),
            gpu_count=(RAW, "num_gpus"),
            region=(DERIVED, "availability[].display_name, available ones first", "cut to 64 characters"),
            country=(DERIVED, '"US" when every region listed starts with US'),
            price_per_gpu_hour=(DERIVED, "= hourly_price / 100 / num_gpus"),
            price_per_instance_hour=(DERIVED, "= hourly_price / 100", "hourly_price is integer cents for the whole instance"),
            market_type=(DERIVED, "on_demand, or spot when only spot stock is listed"),
            provider_tier=(DERIVED, '"vm/via-shadeform" or "baremetal/via-shadeform"'),
            interruptible=(DERIVED, "= (market_type == spot)"),
            available=(DERIVED, "any availability[].available for the rental type", "a real flag per region"),
            capacity=(ABSENT, "-", "flags only, no counts"),
            capacity_unit=(ABSENT, "-"),
            vcpu=(RAW, "vcpus"),
            ram_gb=(RAW, "memory_in_gb"),
            storage_gb=(RAW, "storage_in_gb"),
        ),
        quirks=[
            f"{display} has no usable public API, so this is Shadeform's listing of it, not its own page. "
            "Where we can compare (Hyperstack, DigitalOcean, Voltage Park) prices match; "
            "Shadeform can lag a repricing (it still showed Nebius's pre-Oct-1 price).",
            "Never read a cloud this way if it also has its own feed here: that double-counts its capacity.",
            "Shadeform's interconnect field is sometimes wrong (Crusoe's sxm-ib type is tagged pcie), "
            "so SXM in the instance name wins over the field.",
            "A response without an instance_types list, or a price that is not integer cents, "
            "fails the normalize (previous listings kept) rather than reading as nothing for sale.",
            *extra_quirks,
        ],
    )


CRUSOE = _shadeform("crusoe", "Crusoe", [
    "Several sizes of one GPU share a name; prices differ by size and fabric.",
    "Crusoe's own Cloud API (signed, with an API key pair) is read for capacity only: it publishes no prices, "
    "so prices stay Shadeform's (providers/crusoe.py)."])
LATITUDE = _shadeform("latitude", "Latitude.sh", ['"RTXPro6000" hides a server/workstation variant and stays unmapped.'])
DENVR = _shadeform("denvr", "Denvr", ["Includes Intel Gaudi 2, which has no NVIDIA equivalent to compare against."])
BOOSTRUN = _shadeform("boostrun", "Boost Run", [
    "Every type is an 8-GPU bare-metal node; the per-GPU rate is the node price / 8.",
    'Its RTX 6000 Ada type is tagged "sxm5" by Shadeform (a PCIe card), so "RTX6000Ada sxm5" stays unmapped.',
])

HORIZON = _shadeform("horizon", "Horizon Compute", [
    "Both types are 8-GPU HGX nodes (H100, H200) in Iceland; the per-GPU rate is the node price / 8.",
    "Shadeform tags them interconnect sxm5 with nvlink false; HGX is the SXM baseboard, so they map to the SXM5 parts.",
    "Region is Icelandic, so country stays None (only all-US regions give a country).",
])
PHYNTEC = _shadeform("phyntec", "Phyntec", [
    "Two 8-GPU bare-metal types (RTX 4090, RTX 5090) in Casper, WY; the per-GPU rate is the node price / 8.",
    'Shadeform\'s cloud directory files it as "evergreen" (its former name); its instance types say "phyntec".',
    "Its own site prints monthly reservations only (not converted); the hourly price is Shadeform's.",
])


COREWEAVE = ProviderMapping(
    provider="coreweave",
    display_name="CoreWeave",
    endpoints={"GET /pricing": "the public price page; one GPU instance table per region"},
    fields=_fields(
        "coreweave",
        sku=(DERIVED, "{GPU Model}:{GPU Count}x"),
        listing_id=(DERIVED, "{model slug}:{count}x:{region slug}[:spot]", "one on-demand and one spot listing per row"),
        raw_gpu_name=(DERIVED, "GPU Model + VRAM column", 'e.g. "NVIDIA HGX H100 80GB"; "(High|Standard) Memory" dropped'),
        gpu_count=(RAW, "GPU Count", '"4^1" (footnote) reads as 4'),
        region=(RAW, "the table's REGION heading", '"North America" or "Europe": a continent'),
        country=(ABSENT, "-", "a continent is not a country"),
        price_per_gpu_hour=(DERIVED, "= instance price / GPU Count"),
        price_per_instance_hour=(RAW, "On-Demand Price | Spot Price", "per whole instance, per hour"),
        market_type=(DERIVED, "On-Demand Price -> on_demand, Spot Price -> spot"),
        provider_tier=(ABSENT, "-"),
        interruptible=(DERIVED, "= (market_type == spot)"),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "vCPUs"),
        ram_gb=(RAW, "System RAM (GB)"),
        storage_gb=(DERIVED, "Local Storage (TB) x 1024", "the page defines 1 TB = 1024 GB"),
    ),
    quirks=[
        "Prices are for the whole instance (8 GPUs on most rows); the per-GPU rate divides by GPU Count.",
        "Each row is printed twice (desktop grid, labelled mobile block). Both are read; a row whose "
        "two copies disagree on count, VRAM or a price is skipped, never guessed.",
        '"Contact sales" cells (GB300 NVL72, and B300 / one RTX PRO 6000 row on demand) have no price.',
        '"Inference Single GPU Price" is for CoreWeave inference-platform customers only and is not read.',
        '"NVIDIA A100" does not say SXM4 or PCIe and stays unmapped; GB200 NVL72 has no canonical name yet.',
        "Scraped from page HTML. A reshaped page fails the poll rather than recording nothing.",
    ],
)


TOGETHER = ProviderMapping(
    provider="together",
    display_name="Together AI (GPU Clusters)",
    endpoints={"GET /pricing": "the public price page; the GPU Clusters per-GPU table"},
    fields=_fields(
        "together",
        sku=(RAW, "Hardware", 'e.g. "NVIDIA HGX H100"'),
        listing_id=(DERIVED, "{hardware slug}[:preemptible]"),
        raw_gpu_name=(RAW, "Hardware"),
        gpu_count=(CONSTANT, "1", "the table prices one GPU-hour"),
        region=(ABSENT, "-", "no region on the page"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(RAW, "ON-Demand | Preemptible Compute column", '"All prices are per GPU per hour"'),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        market_type=(DERIVED, "ON-Demand -> on_demand, Preemptible Compute -> spot"),
        provider_tier=(CONSTANT, '"gpu-cluster"'),
        interruptible=(DERIVED, "= (market_type == spot)", "preemptible capacity can be reclaimed"),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-"),
        ram_gb=(ABSENT, "-"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "Only the two pay-as-you-go columns become listings; the four Reserved terms (3-30, 31-90, 91-180, "
        "181+ days) are commitments, stored raw under /pricing#reserved and read only by reserved_prices().",
        "The page states no node size, so a listing is one GPU at the printed rate (the Nebius convention).",
        "Inference, fine-tuning and sandbox prices on the same page are not GPU rental and are ignored.",
        "Scraped from page HTML; the extractor requires the 'per GPU per hour' statement and the column layout.",
    ],
)


JARVISLABS = ProviderMapping(
    provider="jarvislabs",
    display_name="Jarvislabs",
    endpoints={"GET /misc/server_meta": "authenticated catalogue (Bearer JARVIS_API_KEY); one row per region, GPU and workload type"},
    fields=_fields(
        "jarvislabs",
        sku=(DERIVED, "{GPU}:1x"),
        listing_id=(DERIVED, "{GPU}:1x:{region}:{workload_type}:{market}"),
        raw_gpu_name=(LOOKUP, "gpu_type via NAMES + vram", 'e.g. "H100" -> "NVIDIA H100 SXM 80GB"; an unknown code keeps its own text'),
        gpu_count=(CONSTANT, "1", "prices are per GPU-hour"),
        region=(RAW, "region", 'e.g. "india-noida-01"'),
        country=(DERIVED, "IN for india-* regions", "other regions unknown"),
        price_per_gpu_hour=(RAW, "price_per_hour (on-demand) or spot_price (spot)"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        provider_tier=(RAW, "workload_type", "vm or container"),
        available=(ABSENT, "-", "stock is a capacity observation (effective_num_free_devices), not a listing field"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "cpus_per_gpu"),
        ram_gb=(RAW, "ram_per_gpu"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "Every regional host returns the same catalogue, so one call covers all regions.",
        "reserved_pricing (1m / 1y / 2y per GPU-hour) is stored raw but not yet a listing.",
        '"A100 80GB" (SXM4 or PCIe?) and "RTX Pro 6000 Blackwell" (which edition?) stay unmapped, as on the page.',
    ],
)


HOTAISLE = ProviderMapping(
    provider="hotaisle",
    display_name="Hot Aisle",
    endpoints={"GET /pricing": "the public price page; one card per plan"},
    fields=_fields(
        "hotaisle",
        sku=(DERIVED, "{GPU}:{n}x:vm"),
        listing_id=(DERIVED, "{gpu}:{n}x:vm", "one listing per GPU count on a VM card"),
        raw_gpu_name=(DERIVED, '"AMD " + card GPU, upper-cased', 'e.g. "AMD MI300X"'),
        gpu_count=(RAW, "card counts", '"2x & 4x" gives two listings'),
        region=(ABSENT, "-", "the page names a Michigan facility, not a region code"),
        country=(DERIVED, '"US" while the page still names its Michigan facility'),
        price_per_gpu_hour=(RAW, "card price", 'printed "$2.99/GPU/hr"'),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour x n"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-", "printed as ranges ('8 or 13 CPU Cores')"),
        ram_gb=(ABSENT, "-"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "Only VM cards are read. The 8x bare-metal card says 'billed by the minute' while the page's own "
        "meta description calls it 'monthly terms', so whether it is on-demand is unclear and it is skipped.",
        "AMD Instinct only (MI300X today).",
    ],
)


VULTR = ProviderMapping(
    provider="vultr",
    display_name="Vultr",
    endpoints={
        "GET /v2/plans?type=vcg": "Cloud GPU VM plans; anonymous",
        "GET /v2/plans-metal": "bare-metal plans; anonymous; GPU ones carry gpu_type",
    },
    fields=_fields(
        "vultr",
        sku=(RAW, "plans[].id", 'e.g. "vcg-l40s-16c-180g-48vram"'),
        listing_id=(RAW, "plans[].id"),
        raw_gpu_name=(RAW, "plans[].gpu_type", 'e.g. "NVIDIA_L40S"'),
        gpu_count=(RAW, "plans[].gpu_count"),
        region=(RAW, "plans[].locations joined", "None when the list is empty"),
        country=(ABSENT, "-", "location ids are not countries"),
        price_per_gpu_hour=(DERIVED, "= hourly_cost / gpu_count"),
        price_per_instance_hour=(RAW, "plans[].hourly_cost", "the whole plan"),
        provider_tier=(DERIVED, '"vm" | "bare-metal" by endpoint'),
        available=(ABSENT, "-", "locations is where a plan is offered, not live stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "vcpu_count (VM) | cpu_threads (metal)"),
        ram_gb=(DERIVED, "ram / 1024", "ram is in MB"),
        storage_gb=(DERIVED, "disk x disk_count"),
    ),
    quirks=[
        "Only plans with deploy_ondemand true are read. Every GPU bare-metal plan seen (H100, B200, "
        "MI300X, MI325X, MI355X, L40S, GH200) says false, so they produce no listing.",
        "A general-purpose cloud: never part of a neocloud index.",
        "Vultr is also in Shadeform's catalogue; it is read from its own API only (never both).",
    ],
)


GCP = ProviderMapping(
    provider="gcp",
    display_name="Google Cloud Compute Engine (hyperscaler list price)",
    endpoints={"GET /v1/services/6F81-5844-456A/skus":
               "Cloud Billing Catalog API, paged; key in the X-Goog-Api-Key header (GCP_API_KEY)"},
    fields=_fields(
        "gcp",
        sku=(RAW, "machine type | attachable accelerator type", 'e.g. "a3-highgpu-8g"; "nvidia-tesla-t4"'),
        listing_id=(DERIVED, "{sku}:{region}"),
        raw_gpu_name=(LOOKUP, "MACHINES / ATTACHABLE in providers/gcp.py", 'the GCP accelerator type, e.g. "nvidia-h100-80gb"'),
        gpu_count=(LOOKUP, "MACHINES in providers/gcp.py", "1 for an attachable GPU"),
        region=(CONSTANT, '"us-central1"', "REGIONS: group-priced SKUs do not say where a machine is sold"),
        country=(DERIVED, '"US" for us-* regions'),
        price_per_gpu_hour=(DERIVED, "= VM price / gpu_count | the attachable GPU SKU"),
        price_per_instance_hour=(DERIVED, "GPUs x GPU SKU + vCPUs x core SKU + GiB x RAM SKU + SSD GiB x SSD SKU / 730",
                                 "each shape reproduces Google's Iowa page price exactly (tests/test_provider_gcp.py)"),
        provider_tier=(DERIVED, '"hyperscaler" (machine types) | "gpu-attachment" (GPU only, host VM extra)'),
        available=(ABSENT, "-", "a price list says nothing about capacity"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(LOOKUP, "MACHINES", "machine types only"),
        ram_gb=(LOOKUP, "MACHINES", "GiB; machine types only"),
        storage_gb=(ABSENT, "-", "bundled Local SSD is priced in, not normalized"),
    ),
    quirks=[
        "Google prices a GPU VM as separate SKUs (GPU, vCPU, RAM of its family, bundled Local SSD); the VM price "
        "is assembled from them. The per-GPU rate divides by the GPU count, as for AWS.",
        "SKU names vary by region (the H100 Mega GPU is 'Mega' in most regions, 'Plus' in Americas), so each "
        "component is matched by a pattern; a region priced twice with different values is skipped.",
        "Attachable GPUs (T4, P4, V100, P100 on N1) are the GPU alone; the N1 host is billed on top.",
        "Left out: g4-standard-6/12/24 (fractional cards) and a4-highgpu-8g (no on-demand page price to prove its shape).",
        "Spot, 1- and 3-year commitment and DWS Calendar Mode SKUs are stored raw, not yet listings.",
    ],
)


LINODE = ProviderMapping(
    provider="linode",
    display_name="Akamai Cloud (Linode)",
    endpoints={"GET /v4/linode/types": "every plan; anonymous; GPU plans carry gpus > 0"},
    fields=_fields(
        "linode",
        sku=(RAW, "data[].id", 'e.g. "g2-gpu-rtx4000a2-m"'),
        listing_id=(DERIVED, "data[].id[:region_prices[].id]", "one more listing per region priced differently"),
        raw_gpu_name=(DERIVED, 'the "RTX..." token of data[].label', '"RTX6000" | "RTX4000 Ada"; the whole label if none'),
        gpu_count=(RAW, "data[].gpus"),
        region=(DERIVED, "None for the base price, region_prices[].id otherwise", "the base price applies everywhere else"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= price.hourly / gpus"),
        price_per_instance_hour=(RAW, "data[].price.hourly | region_prices[].hourly", "the whole plan"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(ABSENT, "-", "a plan catalogue, not stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "data[].vcpus"),
        ram_gb=(DERIVED, "memory / 1024", "memory is in MB"),
        storage_gb=(DERIVED, "disk / 1024", "disk is in MB"),
    ),
    quirks=[
        "The GPU model is not a field; it is read from the plan label. GPU VRAM is not stated at all.",
        '"RTX6000" (g1 plans) is the Turing Quadro RTX 6000 per Linode\'s docs, but the API name '
        "alone does not say so, so it stays unmapped.",
        "Paging: a response with more than one page fails the poll rather than missing types.",
        "Monthly prices are null for GPU plans and are never converted to hourly.",
        "A general-purpose cloud: never part of a neocloud index.",
    ],
)


ORACLE = ProviderMapping(
    provider="oracle",
    display_name="Oracle Cloud (OCI, hyperscaler list price)",
    endpoints={"GET /pls/apex/cetools/api/v1/products/?currencyCode=USD": "the whole OCI price list; anonymous, documented"},
    fields=_fields(
        "oracle",
        sku=(LOOKUP, "SHAPES in providers/oracle.py", 'the OCI shape, e.g. "BM.GPU.H100.8"'),
        listing_id=(LOOKUP, "SHAPES in providers/oracle.py", "the shape; one listing per shape"),
        raw_gpu_name=(LOOKUP, "SHAPES in providers/oracle.py", "Oracle's pricing-page wording + printed VRAM"),
        gpu_count=(LOOKUP, "SHAPES in providers/oracle.py", "the price list has no shapes or counts"),
        region=(ABSENT, "-", "OCI list prices are global: one price in every commercial region"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(RAW, "items[].currencyCodeLocalizations[USD].prices[PAY_AS_YOU_GO].value",
                            'metricName "GPU Per Hour": already per GPU'),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour x gpu_count", "Oracle's own rule for server price"),
        provider_tier=(DERIVED, '"bare-metal" for BM.* shapes, "vm" for VM.*'),
        available=(ABSENT, "-", "a price list says nothing about capacity"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-", "not in the price list"),
        ram_gb=(ABSENT, "-", "not in the price list"),
        storage_gb=(ABSENT, "-", "not in the price list"),
    ),
    quirks=[
        'The price list prices a GPU PART ("Compute - GPU - H100"), not a shape. Which shapes bill '
        "against which part is taken from Oracle's compute pricing page into a hand-kept table "
        "(SHAPES); several shapes share one part (A10 x1/x2/x4, V100 x1/x2/x4/x8). A new GPU part is "
        "logged and skipped, never guessed.",
        "A part that stops being one flat pay-as-you-go 'GPU Per Hour' USD price fails the poll.",
        "Not read: NVIDIA AI Enterprise (a software licence billed per GPU), Cloud@Customer, VMware "
        "commits, Roving Edge, the P100 shapes Oracle lists as unavailable for new deployments, and "
        "preemptible (a flat 50% discount, not priced per part).",
        'H100 and A100 80GB/40GB (SXM or PCIe never stated), RTX PRO 6000 (edition not stated) and '
        "GB200 (no canonical name) stay unmapped.",
        "List prices: BM.GPU.H100.8 is $10.00 per GPU-hour, $80.00 per node.",
    ],
)


AZURE = ProviderMapping(
    provider="azure",
    display_name="Microsoft Azure (hyperscaler list price)",
    endpoints={
        "GET prices.azure.com/api/retail/prices?$filter=...": "Consumption meters of every NC/ND/NV size in one "
        "region, Windows excluded; anonymous; one call per region (eastus, eastus2), NextPageLink followed",
    },
    fields=_fields(
        "azure",
        sku=(RAW, "Items[].armSkuName", 'e.g. "Standard_ND96isr_H100_v5"'),
        listing_id=(DERIVED, "{armSkuName}:{region}[:spot]", "one listing per size, region and market"),
        raw_gpu_name=(LOOKUP, "GPU_SIZES in providers/azure.py", "the size page's accelerator string; the API has none"),
        gpu_count=(LOOKUP, "GPU_SIZES in providers/azure.py", "the API has no GPU count either"),
        region=(RAW, "Items[].armRegionName", "eastus | eastus2"),
        country=(DERIVED, '"US" for eastus/eastus2', 'Azure labels them "US East" / "US East 2"'),
        price_per_gpu_hour=(DERIVED, "= retailPrice / gpu_count"),
        price_per_instance_hour=(RAW, "Items[].retailPrice", 'the whole VM, unitOfMeasure "1 Hour"'),
        currency=(RAW, "Items[].currencyCode", "USD; a non-USD BillingCurrency fails the poll"),
        market_type=(DERIVED, 'skuName ends " Spot" ? spot : on_demand', '"Low Priority" meters are dropped'),
        provider_tier=(CONSTANT, '"hyperscaler"'),
        interruptible=(DERIVED, "= (market_type == spot)"),
        available=(ABSENT, "-", "a price list says nothing about capacity"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-", "not in the price rows"),
        ram_gb=(ABSENT, "-", "not in the price rows"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "The GPU model and count are NOT in the API; they come from a hand-kept table (GPU_SIZES) copied "
        "from learn.microsoft.com size pages. An unlisted size is logged and skipped, never guessed.",
        "Fractional sizes (NVadsA10 v5 1/6-1/2 GPU, RTX PRO 6000 v6 1/4-1/2 GPU) and the Promo, NVv3/NVv4, "
        "V710, GB200 and undocumented ND variants (is/flex/noIB/isrf) are not in the table.",
        "Some Windows products are abbreviated \"... Srs Win\" and slip past the 'Windows' filter; "
        "normalize() drops them too.",
        "Spot prices sometimes equal pay-as-you-go (ND H200 v5 in eastus2); recorded as published.",
        "The ND A100/H100/H200 sizes are 8-GPU NVLink (HGX) boards and map to the SXM parts, as AWS p4d/p5 do.",
        "The 256-char endpoint column cannot hold the encoded filter: the endpoint is stored as "
        "/api/retail/prices#<region> and the exact $filter in the snapshot's request.",
    ],
)


OVHCLOUD = ProviderMapping(
    provider="ovhcloud",
    display_name="OVHcloud Public Cloud",
    endpoints={
        "GET api.us.ovhcloud.com/1.0/order/catalog/public/cloud?ovhSubsidiary=US": "OVHcloud US catalogue, USD",
        "GET ca.api.ovh.com/1.0/order/catalog/public/cloud?ovhSubsidiary=WS": "OVHcloud World catalogue, USD",
        "GET {ca.api.ovh.com,api.us.ovhcloud.com}/v1/dedicated/server/datacenter/availabilities":
            "dedicated GPU server delivery words per datacenter; stored raw only",
    },
    fields=_fields(
        "ovhcloud",
        sku=(DERIVED, 'addons[].planCode minus ".consumption"', 'the flavor, e.g. "h100-380"'),
        listing_id=(DERIVED, "{subsidiary}:{flavor}", 'e.g. "WS:l40s-90"'),
        raw_gpu_name=(DERIVED, "blobs.technical.gpu.model + memory.size", 'e.g. "L40S 48GB"'),
        gpu_count=(RAW, "blobs.technical.gpu.number"),
        region=(ABSENT, "-", "a catalogue is per subsidiary, not per region"),
        country=(DERIVED, '"US" for the US catalogue, None for World', "OVHcloud US regions are all in the US"),
        price_per_gpu_hour=(DERIVED, "= instance price / gpu.number"),
        price_per_instance_hour=(DERIVED, "pricings[consumption].price / 1e8", "checked against formattedPrice"),
        currency=(RAW, "locale.currencyCode", "must be USD or the poll fails"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(ABSENT, "-", "a catalogue says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "blobs.technical.cpu.cores"),
        ram_gb=(RAW, "blobs.technical.memory.size"),
        storage_gb=(DERIVED, "sum of blobs.technical.storage.disks capacity x number"),
    ),
    quirks=[
        "Each subsidiary prices in its own currency; only the two USD catalogues are read. "
        "The EU catalogues (EUR, GBP) are not read or converted: they price the same flavors as World, "
        "so reading them would need a rule for which official price list counts (methodology/fx.md).",
        "Prices are integers in 1e-8 USD for the whole instance; a price that disagrees with its "
        "printed formattedPrice fails the poll.",
        'Only hourly ".consumption" addons are read; ".monthly" ones are never converted to hourly.',
        'Windows flavors ("win-...", licence bundled) and AI Notebook/Training/Deploy are not read.',
        'Only flavors tagged "active" and not "coming_soon" are read (RTX 5000 and A10 are coming soon; '
        "GTX 10xx are legacy).",
        '"H100 80GB" and "A100 80GB" do not say SXM or PCIe, "H200 141GB" not SXM or NVL: all unmapped. '
        "No canonical V100S yet.",
        "A general-purpose cloud: never part of a neocloud index.",
        "Dedicated (bare-metal) GPU server stock from the anonymous availabilities list is stored raw only "
        "(baremetal_availability): those servers are not Public Cloud flavors, so no listing to join to.",
    ],
)


_FX = "fx.convert at the ECB EUR->USD reference rate in force at fetch time (methodology/fx.md)"

SCALEWAY = ProviderMapping(
    provider="scaleway",
    display_name="Scaleway (own EUR catalogue, converted)",
    endpoints={
        "GET api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?product_types=instance":
            "Scaleway's own list price per Instance type and zone, EUR; anonymous; GPU rows kept",
        "GET api.scaleway.com/instance/v1/zones/{zone}/products/servers": "server types per zone (GPU ones kept)",
        "GET api.scaleway.com/instance/v1/zones/{zone}/products/servers/availability": "per-zone stock words",
    },
    fields=_fields(
        "scaleway",
        sku=(RAW, "properties.instance.offer_id", 'e.g. "H100-SXM-8-80G"'),
        listing_id=(DERIVED, "offer_id, or the Shadeform id the same product had before 2026-11-01",
                    'e.g. "H100" for H100-1-80G, kept so the price history continues; new types use the offer_id'),
        raw_gpu_name=(DERIVED, "hardware.gpu.type + VRAM from the offer_id", 'e.g. "H100-SXM 80GB"'),
        gpu_count=(RAW, "properties.hardware.gpu.count"),
        region=(DERIVED, "the zones the offer is priced in, joined", 'e.g. "fr-par-2; pl-waw-2"'),
        country=(DERIVED, "the zones' ISO prefix when they share one", "None across countries"),
        price_per_gpu_hour=(DERIVED, "= USD instance price / gpu count", _FX),
        price_per_instance_hour=(DERIVED, "retail_price (units + nanos) x 60 (per minute) x EUR->USD rate", _FX),
        currency=(CONSTANT, '"USD"', "after conversion; the original is in original_currency"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(DERIVED, "any priced zone says available or scarce", "False when every zone says shortage"),
        capacity=(ABSENT, "-", "per-zone stock words are capacity rows (capacity.py)"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "hardware.cpu.virtual.count"),
        ram_gb=(DERIVED, "hardware.ram.size / 2^30"),
        storage_gb=(DERIVED, "zone catalogue scratch_storage_max_size / 1e9", "None when the type has no scratch"),
        original_currency=(RAW, "retail_price.currency_code", 'must be "EUR" or the row is skipped'),
        original_price_per_gpu_hour=(DERIVED, "EUR instance-hour price / gpu count"),
        original_price_per_instance_hour=(DERIVED, "retail_price x 60 (per minute) or x 1 (per hour)"),
        fx_rate=(LOOKUP, "fx_rates (ECB EUR->USD)", "the newest rate published before the fetch; fail closed if > 5 days old"),
        fx_rate_date=(LOOKUP, "fx_rates.rate_date"),
        fx_source=(CONSTANT, '"ecb"'),
    ),
    quirks=[
        "Since 2026-11-01 Scaleway's prices are its own EUR list prices converted at the ECB reference rate. "
        "Before that they were Shadeform's USD listing of Scaleway, which sat 3-8% above the converted list "
        "price: that step is a source change, not a market move (methodology/fx.md).",
        "Shadeform no longer produces any Scaleway price row; Scaleway is counted once.",
        "Only general_availability types are listed. An offer priced differently in two zones becomes one "
        "listing per zone (listing_id@zone).",
        "H100-SXM 2/4/8 and RENDER-S (P100, unmapped) were never in Shadeform's list; they are new listings.",
        "No ECB rate, or one more than 5 days old: no Scaleway listing is converted (fail closed); the previous "
        "state ages out of statistics like any feed that stops.",
        "A general-purpose cloud: never part of a neocloud index.",
    ],
)


KOYEB = ProviderMapping(
    provider="koyeb",
    display_name="Koyeb (serverless GPU)",
    endpoints={"GET /v1/catalog/instances": "every instance type; anonymous"},
    fields=_fields(
        "koyeb",
        sku=(RAW, "instances[].id", 'e.g. "2-gpu-nvidia-h100"'),
        listing_id=(RAW, "instances[].id"),
        raw_gpu_name=(DERIVED, "gpu.name + per-GPU VRAM (gpu.memory / gpu.count)", 'e.g. "gpu-nvidia-l40s 48GB"'),
        gpu_count=(RAW, "instances[].gpu.count", 'must agree with the display name\'s "Nx" prefix'),
        region=(RAW, "instances[].regions joined"),
        country=(ABSENT, "-", '"na" / "eu" are continents'),
        price_per_gpu_hour=(DERIVED, "= price_hourly / gpu.count"),
        price_per_instance_hour=(RAW, "instances[].price_hourly", "the whole instance; billed per second"),
        provider_tier=(CONSTANT, '"serverless"'),
        available=(ABSENT, "-", "status AVAILABLE is an offer status, not stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "instances[].vcpu"),
        ram_gb=(DERIVED, 'instances[].memory, "128GB" -> 128'),
        storage_gb=(DERIVED, 'instances[].disk, "320GB" -> 320'),
    ),
    quirks=[
        "RESTRICTED types (no regions: B200, 4x H200, 8x shapes) are sales-gated and give no listing.",
        "Tenstorrent n300s types are listed at $0 (and the 4x one says gpu.count 1): no listing.",
        '"gpu-nvidia-h100", "gpu-nvidia-a100", "gpu-nvidia-h200" do not state the variant and '
        '"gpu-nvidia-rtx-pro-6000" not the edition: all unmapped.',
        "Paging: a catalogue larger than the first page fails the poll.",
        "A general-purpose (serverless) cloud: never part of a neocloud index.",
    ],
)


IBM = ProviderMapping(
    provider="ibm",
    display_name="IBM Cloud (VPC GPU virtual servers)",
    endpoints={"GET /api/v1/{plan id}/pricing/deployment": "Global Catalog: every regional deployment of one "
                                                            "GPU profile plan; anonymous for public services"},
    fields=_fields(
        "ibm",
        sku=(LOOKUP, "PROFILES in providers/ibm.py", 'the instance profile, e.g. "gx3d-160x1792x8h100"'),
        listing_id=(DERIVED, "{profile}:{deployment_location}[:spot]", "one on-demand and one spot listing per region"),
        raw_gpu_name=(LOOKUP, "PROFILES in providers/ibm.py", "IBM's profile docs; the pricing has no GPU model"),
        gpu_count=(LOOKUP, "PROFILES in providers/ibm.py", "also the 'x8h100' part of the profile name"),
        region=(RAW, "resources[].deployment_location", 'e.g. "us-south"'),
        country=(ABSENT, "-", 'the "USA" amount is the billing country, not the data-centre country'),
        price_per_gpu_hour=(DERIVED, "= price_per_instance_hour / gpu_count"),
        price_per_instance_hour=(RAW, "metrics[INSTANCE_HOURS_MULTI_TENANT | INSTANCE_MULTI_TENANT_SPOT]"
                                      ".amounts[USA].prices[0].price", "the whole virtual server, per hour"),
        market_type=(DERIVED, "INSTANCE_HOURS_MULTI_TENANT -> on_demand, INSTANCE_MULTI_TENANT_SPOT -> spot"),
        provider_tier=(CONSTANT, '"vm"', "VPC virtual servers only"),
        interruptible=(DERIVED, "= (market_type == spot)"),
        available=(ABSENT, "-", "a deployment is where a profile is sold, not live stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(DERIVED, "profile name {vcpu}x{GiB}x{gpus}{gpu}", "IBM's documented naming"),
        ram_gb=(DERIVED, "profile name {vcpu}x{GiB}x{gpus}{gpu}", "GiB"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "The pricing carries no GPU model or count; both come from a hand-kept table (PROFILES) of "
        "Global Catalog plan ids. A metric whose id does not name the profile fails the poll.",
        "Amounts are per billing country; only the USA (USD) amount is read. A tiered or missing "
        "USD amount fails the poll.",
        "A region can list several deployments (zones). They must agree on price; if not the region is skipped.",
        "Dedicated-host, reservation and OS-licence metrics are not read. The A100 profiles have no spot price.",
        "On 2026-10-13 the 8x H100 and 8x H200 profiles carry the same price ($85.85/hr in us-south).",
        "Intel Gaudi 3 has no canonical name yet and stays unmapped. A general-purpose cloud: never in a neocloud index.",
    ],
)


NOSANA = ProviderMapping(
    provider="nosana",
    display_name="Nosana (decentralized)",
    endpoints={"GET /api/markets/": "every GPU market with its USD host rate and network fee; anonymous"},
    fields=_fields(
        "nosana",
        sku=(RAW, "slug", 'e.g. "nvidia-4090"'),
        listing_id=(RAW, "slug"),
        raw_gpu_name=(RAW, "name", 'e.g. "NVIDIA 4090"'),
        gpu_count=(CONSTANT, "1", "a market whose name states a count is skipped"),
        region=(ABSENT, "-", "markets are global"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "usd_reward_per_hour x (1 + network_fee_percentage / 100)",
                            "Nosana's own docs price table uses this formula"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        currency=(CONSTANT, '"USD"', "usd_* field; NOS prices never read"),
        provider_tier=(CONSTANT, '"premium"', "only PREMIUM markets are read"),
        interruptible=(CONSTANT, "True", "jobs end at the market timeout and run on community hosts"),
        available=(ABSENT, "-", "the endpoint states no free hosts"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-"),
        ram_gb=(ABSENT, "-"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "usd_reward_per_hour is what the HOST earns; the renter pays it plus network_fee_percentage (10%), as "
        "Nosana's docs table and the on-chain job price (nos_job_price = nos_reward x 1.10) both show. "
        "nosana.com/pricing.md prints the bare host rate.",
        'COMMUNITY markets ("new or unvalidated"), OTHER (enterprise multi-GPU) and client-dedicated '
        "markets are not read.",
        "Most market names state no variant (\"NVIDIA H100\", \"NVIDIA 3060\") and stay unmapped.",
    ],
)


E2E = ProviderMapping(
    provider="e2e",
    display_name="E2E Networks",
    endpoints={"GET /pricing.json": "E2E's machine-readable rate card; anonymous"},
    fields=_fields(
        "e2e",
        sku=(RAW, "gpuInstances[].id", 'e.g. "h200"'),
        listing_id=(DERIVED, "{id}:1x"),
        raw_gpu_name=(DERIVED, 'model + " {vramGB}GB"', 'e.g. "NVIDIA H200 141GB"'),
        gpu_count=(CONSTANT, "1", 'the pricing page says "On-demand prices are per GPU-hour"'),
        region=(ABSENT, "-", "the feed does not say which GPU is in which Indian data centre"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(RAW, "gpuInstances[].pricing.usd.hourly", "USD is its own price list, not an INR conversion"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(ABSENT, "-", "a rate card, not stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "gpuInstances[].vcpus", "the 1x configuration"),
        ram_gb=(RAW, "gpuInstances[].ramGB", "the 1x configuration"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "Spot and committed (3+ month) rates are published in INR only and are not read; monthly/annual are 'Contact Sales'.",
        "A row whose unit is not 'per hour' fails the poll; a row with no USD price is skipped.",
        '"NVIDIA H100 80GB", "NVIDIA A100 80GB/40GB" (SXM or PCIe not stated), "NVIDIA RTX PRO 6000 96GB" '
        '(edition not stated), "NVIDIA V100 32GB" and "NVIDIA A30 24GB" (no canonical name yet) stay unmapped.',
        "A general-purpose cloud: never part of a neocloud index.",
    ],
)


SIMPLEPOD = ProviderMapping(
    provider="simplepod",
    display_name="SimplePod (marketplace)",
    endpoints={
        "GET /instances/market/list?rentalStatus=active": "open Docker offers; anonymous",
        "GET /instances/market/list?rentalStatus=active&mode=vm": "open VM offers; anonymous",
    },
    fields=_fields(
        "simplepod",
        sku=(RAW, "offers[].gpuModel"),
        listing_id=(DERIVED, "{docker|vm}:{offer id}", "one listing per offer; a host lists 1x..8x separately"),
        raw_gpu_name=(DERIVED, 'gpuModel + " ({gpuMemorySize / 1024} GB)"', 'e.g. "RTX 3060 (12 GB)"'),
        gpu_count=(RAW, "offers[].gpuCount"),
        region=(RAW, "offers[].rigRentalDatacenter.name", 'e.g. "EU-PL-01"'),
        country=(RAW, "offers[].rig.country"),
        price_per_gpu_hour=(DERIVED, "= pricePerGpu / gpuCount"),
        price_per_instance_hour=(RAW, "offers[].pricePerGpu", "despite the name, the whole offer per hour"),
        provider_tier=(DERIVED, '"container" | "vm" by endpoint, + "/" + data-centre type', 'e.g. "vm/enterprise"'),
        available=(CONSTANT, "True", "the market lists only offers open for rent now"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "offers[].cpuCoreCount"),
        ram_gb=(DERIVED, "offers[].systemMemory / 1024", "MiB in the API"),
        storage_gb=(RAW, "offers[].diskSize"),
    ),
    quirks=[
        "`pricePerGpu` is the price of the whole offer: one host's V100 offers read 0.1, 0.2 ... 0.5 for "
        "1..5 GPUs, and SimplePod's dashboard prints it as '{n}x {pricePerGpu / n} $/h'.",
        "Disk (pricePerDiskSize, $/month) is billed on top and not included.",
        "Offers not available on demand, or with host errors or maintenance, are skipped. A page as large "
        "as the requested page size fails the poll.",
        '"Tesla V100-SXM2-32GB (32 GB)", "RTX PRO 6000 Blackwell" (edition not stated) and the '
        '"RTX PRO 6000 MIG 2g.48gb" slice stay unmapped.',
    ],
)


CLORE = ProviderMapping(
    provider="clore",
    display_name="Clore.ai (marketplace)",
    endpoints={
        "GET /v1/marketplace": "every public server, rented or not; account key in the `auth` header; ~4 MB",
        "GET /v1/renter_fees": "the renter's fee per order type and currency; only the on-demand fee is kept",
    },
    fields=_fields(
        "clore",
        sku=(DERIVED, 'gpu_array model + " {specs.gpuram}GB"', 'e.g. "RTX 4090 24GB"'),
        listing_id=(DERIVED, "{sku}:{median|cheapest}", "a summary of a book, not one server"),
        raw_gpu_name=(DERIVED, 'gpu_array model + " {specs.gpuram}GB"', "specs.gpuram is per-GPU VRAM"),
        gpu_count=(CONSTANT, "1", "priced per single GPU"),
        region=(ABSENT, "-", "only a country code is published"),
        country=(RAW, "specs.net.cc of the cheapest server", "None on the median row"),
        price_per_gpu_hour=(DERIVED, 'median or minimum of price.on_demand["USD-Blockchain"] x (1 + renter fee %) / 24 / len(gpu_array)',
                            "the listed price is per whole server per day, without fees"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        currency=(CONSTANT, '"USD"', "USD-Blockchain: the USDT/USDC balance"),
        provider_tier=(CONSTANT, '"median" | "cheapest"'),
        available=(CONSTANT, "True", "a row exists only if a server that is not rented does"),
        capacity=(DERIVED, "sum(len(gpu_array)) over servers not rented"),
        capacity_unit=(CONSTANT, '"gpu"'),
        vcpu=(DERIVED, "cheapest server's specs.cpus threads (\"cores/threads\")", "None on the median row"),
        ram_gb=(RAW, "cheapest server's specs.ram", "None on the median row"),
        storage_gb=(DERIVED, "cheapest server's specs.disk trailing GB figure", "None on the median row"),
    ),
    quirks=[
        "Prices are per server per day in each accepted coin; only USD-Blockchain (USDT/USDC) is a USD "
        "price a renter pays. Servers that do not accept it give no price.",
        "The listed price excludes the renter's base fee (5% on demand today, read from /v1/renter_fees); "
        "it is added. Proof-of-Holding discounts are account-specific and not applied.",
        "Rented servers are skipped. A server whose 'Nx' disagrees with gpu_array, or that mixes models, is "
        "skipped, never guessed.",
        "Spot is a bid auction; price.spot is the host's minimum bid and price.usd.spot is 0, so spot is not read.",
        "A multi-GPU server may only rent whole unless partial_gpu_rental is on; the per-GPU figure is a share.",
        "Datacenter names without a form factor (\"H100 80GB\", \"Tesla T4 15GB\", \"L40S 45GB\") and "
        "odd VRAM figures (\"RTX 4090 23GB\") stay unmapped.",
    ],
)


MODAL = ProviderMapping(
    provider="modal",
    display_name="Modal (serverless GPU)",
    endpoints={"GET /pricing": "the public price page; the 'GPU Tasks' list, priced per second"},
    fields=_fields(
        "modal",
        sku=(RAW, "GPU row name", 'e.g. "Nvidia H100 SXM5"'),
        listing_id=(DERIVED, "slug of the GPU row name"),
        raw_gpu_name=(RAW, "GPU row name", 'e.g. "Nvidia A100, 80 GB"'),
        gpu_count=(CONSTANT, "1", "the rate is per GPU; 1 to 8 per container at the same rate"),
        region=(ABSENT, "-", "list price with no region pinned; region selection multiplies it"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= screen-reader exact price ($/sec) x 3600", "exact Decimal"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "GPU only: CPU and memory are billed on top"),
        currency=(CONSTANT, '"USD"', "printed with $"),
        provider_tier=(CONSTANT, '"gpu-attachment"', "the GPU's own rate, kept out of market math like GCP's attachable GPUs"),
        interruptible=(CONSTANT, "True", "Modal Functions are preemptible by default (the page says so)"),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-", "billed separately per core"),
        ram_gb=(ABSENT, "-", "billed separately per GiB"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "The GPU rate excludes CPU (min 0.125 core) and memory, which are billed per second on top; "
        "no CPU or memory is added, so listings are tagged gpu-attachment and never vote in an index.",
        "Modal's GPU guide states every H100/H200 is SXM and the H200 has 141 GB: those rows are mapped. "
        '"Nvidia A100, 80/40 GB" (SXM or PCIe?), "Nvidia RTX PRO 6000" (edition?) and "Nvidia A10" '
        "(A10 or A10G?) stay unmapped.",
        "Sandboxes (3x CPU/memory) and region multipliers are not read. A general-purpose (serverless) "
        "cloud: never part of a neocloud index.",
    ],
)


REPLICATE = ProviderMapping(
    provider="replicate",
    display_name="Replicate (serverless hardware)",
    endpoints={"GET /pricing": "the public price page; the 'Hardware pricing' cards"},
    fields=_fields(
        "replicate",
        sku=(RAW, "card hardware id", 'e.g. "gpu-l40s-2x"'),
        listing_id=(RAW, "card hardware id"),
        raw_gpu_name=(DERIVED, 'card name without "Nx " and " GPU"', 'e.g. "Nvidia A100 (80GB)"'),
        gpu_count=(RAW, 'card "GPU" spec, "2x" -> 2', "must agree with the name's Nx prefix"),
        region=(ABSENT, "-", "no region on the page"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= price_per_instance_hour / gpu_count"),
        price_per_instance_hour=(DERIVED, "= card $/sec x 3600", "the whole hardware type, CPUs and RAM included"),
        currency=(CONSTANT, '"USD"', "printed with $"),
        provider_tier=(CONSTANT, '"serverless"'),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, 'card "CPU" spec, "10x" -> 10'),
        ram_gb=(RAW, 'card "RAM" spec, "144GB" -> 144'),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        'Cards below the "Additional hardware" divider (every H200, 4x/8x A100 and H100, 4x/8x L40S) are '
        '"available with committed spend contracts": not pay-as-you-go, no listing.',
        '"Nvidia H100" and "Nvidia A100 (80GB)" state no SXM or PCIe and stay unmapped.',
        "A general-purpose (serverless) cloud: never part of a neocloud index.",
    ],
)


BASETEN = ProviderMapping(
    provider="baseten",
    display_name="Baseten (dedicated deployments)",
    endpoints={"GET /pricing/": "the public price page; its Next.js data stream carries the "
                                "'Dedicated Deployments' instance list"},
    fields=_fields(
        "baseten",
        sku=(RAW, "instanceList[].name", 'e.g. "H100"'),
        listing_id=(DERIVED, "slug of the name"),
        raw_gpu_name=(DERIVED, '"NVIDIA " + name + VRAM from specs', 'e.g. "NVIDIA H100 80GB"'),
        gpu_count=(CONSTANT, "1", "the smallest instance with this GPU"),
        region=(ABSENT, "-", "other regions are by sales contact"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(RAW, "instanceList[].pricePerHour", "checked against pricePerMinute x 60 to a cent"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "the instance's CPUs and memory included"),
        currency=(CONSTANT, '"USD"', "printed with $"),
        provider_tier=(CONSTANT, '"serverless"'),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-", "the pricing page prints none (the docs do)"),
        ram_gb=(ABSENT, "-"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "Billing is per minute; the published hourly figure is read. The Training table repeats the "
        "same prices and is not read twice. Model API (per-token) prices are ignored.",
        '"H100 MIG" (a fractional H100, 40 GiB) is skipped. "NVIDIA A100 80GB" and "NVIDIA H100 80GB" state '
        "no SXM or PCIe and stay unmapped.",
        "A general-purpose (serverless) cloud: never part of a neocloud index.",
    ],
)


BEAM = ProviderMapping(
    provider="beam",
    display_name="Beam (serverless GPU)",
    endpoints={"GET /pricing": "the public price page; the Serverless 'GPU Tasks' table"},
    fields=_fields(
        "beam",
        sku=(RAW, "GPU row name", 'e.g. "RTX 4090"'),
        listing_id=(DERIVED, "serverless:{slug of name + interface}"),
        raw_gpu_name=(DERIVED, "name + interface + VRAM", 'e.g. "RTX 4090 PCIE 24GB"'),
        gpu_count=(CONSTANT, "1", "the rate is per GPU"),
        region=(ABSENT, "-"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= row $/sec x 3600", "exact Decimal"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "GPU only: attached CPU and memory are billed on top"),
        currency=(CONSTANT, '"USD"', "printed with $"),
        provider_tier=(CONSTANT, '"gpu-attachment"', "the GPU's own rate, kept out of market math like GCP's attachable GPUs"),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-", "billed separately per core"),
        ram_gb=(ABSENT, "-", "billed separately per GiB"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        'Only the "Start instantly" (pay-as-you-go) group is read; the "With committed spend" rows give no listing.',
        'On-demand machines are priced "from $x/hr" (a floor, not an offer) and are not read; clusters are "Contact us".',
        "A general-purpose (serverless) cloud: never part of a neocloud index.",
    ],
)


ARKANE = ProviderMapping(
    provider="arkane",
    display_name="Arkane Cloud",
    endpoints={"GET /pricing/": "the public price page; the Cloud GPU table"},
    fields=_fields(
        "arkane",
        sku=(RAW, "row GPU type", 'e.g. "NVIDIA H100"'),
        listing_id=(DERIVED, "{slug(raw_gpu_name)}:1x"),
        raw_gpu_name=(DERIVED, "GPU type + VRAM cell", 'e.g. "NVIDIA B300 288GB"'),
        gpu_count=(CONSTANT, "1", "the page prices one GPU and does not list the counts per GPU"),
        region=(ABSENT, "-"),
        country=(ABSENT, "-", "only the B300 / RTX PRO 6000 are said to be in France"),
        price_per_gpu_hour=(RAW, '"Price per GPU" cell', 'printed "$3.99/hr"'),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "one GPU"),
        currency=(RAW, '"Prices in USD."', "required on the page"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, '"CPU" cell'),
        ram_gb=(RAW, '"RAM" cell'),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        "Cells are read by their data-row-title label, never by column position.",
        "Serverless GPU endpoints, the per-token Models API, CPU instances and reserved clusters are not read.",
        '"NVIDIA H100 80GB", "NVIDIA A100 80GB" (SXM or PCIe not stated) and "RTX PRO 6000 96GB" '
        "(edition not stated) stay unmapped.",
    ],
)


VESSL = ProviderMapping(
    provider="vessl",
    display_name="VESSL AI (Container Compute)",
    endpoints={"GET /en/pricing": "the public price page; the On-demand Container Compute table"},
    fields=_fields(
        "vessl",
        sku=(RAW, "row GPU", 'e.g. "NVIDIA H100 SXM"'),
        listing_id=(DERIVED, "{slug(raw_gpu_name)}:1x"),
        raw_gpu_name=(DERIVED, "GPU + VRAM cell", 'e.g. "NVIDIA H100 SXM 80GB"'),
        gpu_count=(CONSTANT, "1", "billed per GPU; the page does not list container sizes"),
        region=(ABSENT, "-"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(RAW, "On-Demand cell", 'printed "$2.98/hr"; cross-checked with the mobile card'),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "one GPU"),
        provider_tier=(CONSTANT, '"container"'),
        available=(ABSENT, "-", '"Self-serve" is an offer type, not stock'),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(ABSENT, "-"),
        ram_gb=(ABSENT, "-"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        'Container Compute must still be described as "per GPU, per hour", or the poll fails.',
        "VESSL sets its own per-second price on shared clusters it runs; the page names no other cloud "
        "per GPU. Reserved VM Clusters (quoted by term) and Provisioned Throughput are not read.",
    ],
)


MITHRIL = ProviderMapping(
    provider="mithril",
    display_name="Mithril (formerly ML Foundry)",
    endpoints={"GET /pricing": "the public price page (mlfoundry.com/pricing redirects here)"},
    fields=_fields(
        "mithril",
        sku=(DERIVED, "{gpu}:{N}x"),
        listing_id=(DERIVED, "{slug(gpu)}:{N}x:{vcpu}c-{ram}g-{disk}d", "two types can share GPU and count"),
        raw_gpu_name=(RAW, "row instance type", 'e.g. "H200 SXM5"'),
        gpu_count=(RAW, "row GPUs"),
        region=(ABSENT, "-"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= price_per_instance_hour / N"),
        price_per_instance_hour=(RAW, '"Spot (7-day avg)" cell', 'printed "$3.93 instance/hr"'),
        market_type=(CONSTANT, '"spot"', "auction-set prices"),
        provider_tier=(CONSTANT, '"vm"'),
        interruptible=(CONSTANT, "True", "spot"),
        available=(ABSENT, "-", "a weekly average says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "row vCPUs"),
        ram_gb=(RAW, "row RAM"),
        storage_gb=(RAW, "row Disk"),
    ),
    quirks=[
        "The price is a published 7-day average, updated weekly, not a live quote; the page's sentence "
        "saying so is required.",
        "Framer class names change on every publish: rows are read from the table's text in order.",
        "Mithril's API needs an account key; reserved capacity is quoted separately and not read.",
    ],
)


CIVO = ProviderMapping(
    provider="civo",
    display_name="Civo",
    endpoints={"GET /pricing": "the public price page; one table per GPU model in the NVIDIA GPUs section"},
    fields=_fields(
        "civo",
        sku=(DERIVED, "{table model}:{n}x", 'e.g. "NVIDIA H100 SXM:8x"'),
        listing_id=(DERIVED, "{table model slug}:{n}x"),
        raw_gpu_name=(DERIVED, "table heading model + row per-GPU VRAM", 'e.g. "NVIDIA H100 SXM 80GB"'),
        gpu_count=(RAW, 'row size "N x NVIDIA ..."'),
        region=(ABSENT, "-", "one price list for every region"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= price_per_instance_hour / gpu_count"),
        price_per_instance_hour=(RAW, "On-demand price, hourly", "the whole instance"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "vCPU column"),
        ram_gb=(RAW, "RAM column", '"2.5TB" -> 2560'),
        storage_gb=(RAW, "Storage column"),
    ),
    quirks=[
        "Commitment prices (6 to 36 months) and monthly figures are not read.",
        'On-demand cells printed "N/A" give no listing (the 8x B200 is commitment-only).',
        "A row whose model is not the table's model is skipped; a GPU section without tables fails the poll.",
        '"NVIDIA A100 40GB" / "NVIDIA A100 80GB" stay unmapped (SXM or PCIe not stated). "NVIDIA B300" '
        "prints no VRAM but the B300 is an SXM-only part.",
        "Civo's API needs an account key; the public page is keyless and priced in USD.",
        "A general-purpose cloud: never part of a neocloud index.",
    ],
)


ATLANTICNET = ProviderMapping(
    provider="atlanticnet",
    display_name="Atlantic.Net",
    endpoints={"GET /gpu-server-hosting/": "the public GPU cloud price table"},
    fields=_fields(
        "atlanticnet",
        sku=(RAW, "Plan column", 'e.g. "AL40S.192GB"'),
        listing_id=(RAW, "Plan column"),
        raw_gpu_name=(RAW, 'GPU column after "N x"', 'e.g. "L40S", "H100NVL"'),
        gpu_count=(RAW, 'GPU column "N x ..."'),
        region=(ABSENT, "-", "one price list for every data centre"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= price_per_instance_hour / gpu_count"),
        price_per_instance_hour=(RAW, 'Price cell, od-price span "$x/hr"', "the whole plan, On-Demand term"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "vCPU column"),
        ram_gb=(RAW, "RAM column"),
        storage_gb=(RAW, "SSD column"),
    ),
    quirks=[
        "Each price cell holds On-Demand, 1-Year and 3-Year spans; only On-Demand is read, and the poll "
        "fails if the page's On-Demand toggle disappears.",
        "Monthly figures, the dedicated-GPU and HIPAA pages (monthly servers) are not read.",
        "A general-purpose hosting cloud: never part of a neocloud index.",
    ],
)


DEEPINFRA = ProviderMapping(
    provider="deepinfra",
    display_name="DeepInfra (GPU Instances)",
    endpoints={
        "GET /gpu-instances": "the public price page; the On-Demand Cloud Pricing table (every listing field)",
        "GET /v1/containers/gpu_availability": "with DEEPINFRA_API_KEY (api.deepinfra.com): the `available` flag "
                                               "per GPU configuration, for capacity only",
    },
    fields=_fields(
        "deepinfra",
        sku=(RAW, "GPU config cell", 'e.g. "8x NVIDIA B200"; API stock rows rebuild it from "8xB200-180GB"'),
        listing_id=(DERIVED, "{model slug}:{n}x"),
        raw_gpu_name=(DERIVED, "config model + GPU RAM / n", 'e.g. "NVIDIA B200 180GB"'),
        gpu_count=(RAW, 'GPU config "Nx ..."'),
        region=(ABSENT, "-", "no region on the page"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= price_per_instance_hour / gpu_count"),
        price_per_instance_hour=(RAW, 'Price cell "$x/hour"', "the whole instance; billed per minute"),
        provider_tier=(CONSTANT, '"container"'),
        available=(ABSENT, "-", "stock is in capacity (normalize_capacity), from the API with a key"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "vCPU cell", '"16 cores" -> 16'),
        ram_gb=(RAW, "RAM cell"),
        storage_gb=(RAW, "Disk cell", '"2 TB SSD" -> 2000 (decimal TB)'),
    ),
    quirks=[
        "Cells are read by their labels, not position. The hero cards repeat some prices and are not read.",
        "The page's spec block names the card \"NVIDIA B200 SXM6\", so the B200 maps to the SXM part.",
        "The container API's usd_per_hour ($3.69 per B200 GPU-hour on 2026-11-01, vs $4.89 on the page) is kept "
        "raw and never published: it may be account-specific, and the terms treat non-public pricing as confidential.",
        "Per-token model API prices are on other pages and never read.",
    ],
)


THUNDERCOMPUTE = ProviderMapping(
    provider="thundercompute",
    display_name="Thunder Compute",
    endpoints={
        "GET /v2/pricing": "flat map of USD hourly rates; only {gpu}_x{count} keys are read",
        "GET /v2/specs": "the instance config behind each {gpu}_x{count} key",
        "GET /v2/status": "live stock per {gpu}_x{count} key: available / unavailable",
    },
    fields=_fields(
        "thundercompute",
        sku=(RAW, "pricing key", 'e.g. "a100xl_x2"'),
        listing_id=(RAW, "pricing key"),
        raw_gpu_name=(DERIVED, 'specs.displayName (any "(80GB)" dropped) + vramGB', 'e.g. "NVIDIA H100 80GB"'),
        gpu_count=(RAW, "specs.gpuCount", "must agree with the key's _x{count}"),
        region=(ABSENT, "-", 'docs say "North America" only'),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= pricing[key] / gpuCount"),
        price_per_instance_hour=(RAW, "pricing[key]", "the whole instance; h100_x2 = 2 x h100_x1"),
        currency=(CONSTANT, '"USD"', "not stated by the API; the pricing page prints $"),
        provider_tier=(CONSTANT, '"vm"'),
        available=(RAW, "status.specs[key]", '"available" -> True, "unavailable" -> False, else None'),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(DERIVED, "min(specs.vcpuOptions)", "the vCPUs included in the price; more cost additional_vcpus/hr"),
        ram_gb=(DERIVED, "vcpu x ramPerVCPUGiB, or the ramCapGiB rule", "GiB"),
        storage_gb=(DERIVED, "100 x gpuCount", "docs: first 100 GB per GPU included"),
    ),
    quirks=[
        'Legacy mode keys ("h100", "a100xl_native") and per-second "sandbox_*" rates (a sales-gated '
        "product) are not read; nor are disk, snapshot or extra-vCPU rates.",
        "Multi-GPU sizes are not always linear: a100xl_x4 is $5.96 (1.49/GPU) against 1.09/GPU for x1 and x2.",
        'The specs say "NVIDIA H100" and "NVIDIA A100 (80GB)" with no SXM/PCIe, so both stay unmapped '
        '(the marketing page says "H100 SXM"; that page is not the source read).',
        "Billed per minute; instances are never preempted (pricing FAQ).",
        "Read with the account's API token (THUNDER_API_KEY): the terms allow automated access only that way.",
    ],
)


PROVIDER_MAPPINGS: dict[str, ProviderMapping] = {
    m.provider: m
    for m in (
        SALAD, HYPERSTACK, LAMBDA, RUNPOD,
        HYPERBOLIC, VOLTAGEPARK, LIUM, VAST, NEBIUS, MASSEDCOMPUTE,
        DIGITALOCEAN, AWS,
        VERDA, CRUSOE, LATITUDE, DENVR, BOOSTRUN, HORIZON, PHYNTEC,
        COREWEAVE, TOGETHER, JARVISLABS, HOTAISLE, VULTR,
        GCP,
        AZURE,
        LINODE, OVHCLOUD, SCALEWAY, KOYEB,
        ORACLE, IBM,
        THUNDERCOMPUTE,
        MODAL, REPLICATE, BASETEN, BEAM,
        ARKANE, VESSL, MITHRIL,
        NOSANA,
        CIVO, ATLANTICNET, DEEPINFRA,
        E2E, SIMPLEPOD,
        CLORE,
    )
}  # fmt: skip

MODEL_FIELDS = list(ComputeListing.model_fields)


def check_mapping_coverage() -> dict[str, list[str]]:
    """Fields of ComputeListing that a provider mapping forgets, or invents.

    Keeps this file honest as the model grows: an empty result means every
    provider documents every field.
    """
    problems: dict[str, list[str]] = {}
    for name, mapping in PROVIDER_MAPPINGS.items():
        documented = set(mapping.by_field())
        issues = [f"missing: {f}" for f in MODEL_FIELDS if f not in documented]
        issues += [f"not a model field: {f}" for f in sorted(documented - set(MODEL_FIELDS))]
        if issues:
            problems[name] = issues
    return problems


def comparison_rows(providers: list[str] | None = None) -> list[dict]:
    """One row per ComputeListing field, one column per provider.

    `differs` marks a field the providers do not agree on: where one reads it
    straight from a response and another has to derive it, constant it, or
    cannot supply it at all. Those rows are where the model is doing real work.
    """
    wanted = providers or list(PROVIDER_MAPPINGS)
    rows = []
    for field_name in MODEL_FIELDS:
        row: dict = {"field": field_name}
        kinds = set()
        for name in wanted:
            fm = PROVIDER_MAPPINGS[name].by_field().get(field_name)
            row[name] = fm
            kinds.add(fm.kind if fm else None)
        row["differs"] = len(kinds) > 1
        rows.append(row)
    return rows


def quirk_rows(providers: list[str] | None = None) -> list[dict]:
    """Every provider quirk, flattened for display."""
    wanted = providers or list(PROVIDER_MAPPINGS)
    return [
        {"provider": name, "quirk": quirk}
        for name in wanted
        for quirk in PROVIDER_MAPPINGS[name].quirks
    ]


def as_dicts() -> list[dict]:
    """JSON-friendly view for the API."""
    return [
        {
            "provider": m.provider,
            "display_name": m.display_name,
            "endpoints": m.endpoints,
            "quirks": m.quirks,
            "fields": [
                {"field": f.field, "kind": f.kind, "source": f.source, "note": f.note}
                for f in m.fields
            ],
        }
        for m in PROVIDER_MAPPINGS.values()
    ]
