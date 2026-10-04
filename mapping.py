"""How each provider's API fills in ComputeListing.

One entry per provider, declared here rather than buried in the normalizer, so
that adding a provider starts with writing down what maps to what — and so the
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
    endpoints={"GET /prices": "the public price page; the GPU table is embedded escaped JSON"},
    fields=_fields(
        "nebius",
        sku=(RAW, "table row: Item"),
        listing_id=(RAW, "table row: Item"),
        raw_gpu_name=(RAW, "table row: Item", 'e.g. "NVIDIA HGX H100"'),
        gpu_count=(CONSTANT, "1", "the table prices one GPU-hour"),
        region=(ABSENT, "-", "one price list with no region"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(RAW, "the current On-demand GPU-hour column", "picked by date; see quirks"),
        price_per_instance_hour=(DERIVED, "= price_per_gpu_hour", "single GPU"),
        provider_tier=(DERIVED, '"from_price" for "from $x" cells, else None', "a floor, not a quote"),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "table row: vCPUs", "per-GPU share; None for a range"),
        ram_gb=(RAW, "table row: RAM, GB", "per-GPU share; None for a range"),
        storage_gb=(ABSENT, "-"),
    ),
    quirks=[
        'Price change in flight: the page has "On-demand" and "GPU-hour (Effective October 1, 2026)" '
        "columns. We use the dated column once its date has arrived, else the plain one. "
        "On 2026-10-04 that is the higher price.",
        '"Contact us" rows (GB200/GB300 NVL72) have no price and are skipped.',
        'L40S prices are "from $x": a floor over several CPU configs, tagged from_price.',
        "Scraped from page HTML. A reshaped page fails the poll rather than recording nothing.",
    ],
)


MASSEDCOMPUTE = ProviderMapping(
    provider="massedcompute",
    display_name="Massed Compute",
    endpoints={"GET /pricing": "the public price page; one table of shapes per GPU model"},
    fields=_fields(
        "massedcompute",
        sku=(DERIVED, "{model heading}:{qty}x", ""),
        listing_id=(DERIVED, "{model heading}:{qty}x"),
        raw_gpu_name=(RAW, "model heading", 'e.g. "A100 SXM4 (80GB)"'),
        gpu_count=(RAW, "table Qty", 'printed "x 8"'),
        region=(ABSENT, "-", "one price list with no region"),
        country=(ABSENT, "-"),
        price_per_gpu_hour=(DERIVED, "= Price / Qty"),
        price_per_instance_hour=(RAW, "table Price", 'printed "$52.80 /hr"'),
        provider_tier=(ABSENT, "-"),
        available=(ABSENT, "-", "the page says nothing about stock"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "table vCPU"),
        ram_gb=(RAW, "table RAM", 'printed "2800 GB"'),
        storage_gb=(RAW, "table Storage", "unit not printed; assumed GB"),
    ),
    quirks=[
        "Scraped from page HTML: each model heading is tied to the table that follows it.",
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
    endpoints={"GET awsstatic price file": "every EC2 type in us-east-1, Linux; no credentials"},
    fields=_fields(
        "aws",
        sku=(RAW, "Instance Type", 'e.g. "p5.48xlarge"'),
        listing_id=(DERIVED, "{Instance Type}:us-east-1"),
        raw_gpu_name=(LOOKUP, "GPU_INSTANCES in providers/aws.py", "the price file has no GPU model; ours does"),
        gpu_count=(LOOKUP, "GPU_INSTANCES in providers/aws.py", "the price file has no GPU count either"),
        region=(CONSTANT, '"us-east-1"', "one region per file"),
        country=(CONSTANT, '"US"'),
        price_per_gpu_hour=(DERIVED, "= price / gpu_count"),
        price_per_instance_hour=(RAW, "price", "on-demand, Linux, shared tenancy"),
        provider_tier=(CONSTANT, '"hyperscaler"'),
        available=(ABSENT, "-", "a price file says nothing about capacity"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "vCPU"),
        ram_gb=(DERIVED, "Memory, parsed from '2048 GiB'"),
        storage_gb=(ABSENT, "-", "EBS-only or local NVMe is not normalized"),
    ),
    quirks=[
        "The GPU model and count are NOT in the price file; they come from a hand-kept table "
        "(GPU_INSTANCES). A new instance family is logged and skipped, never guessed.",
        "List prices: big buyers rarely pay them, so AWS sits well above the neoclouds. "
        "p5.48xlarge is 8x H100 at $55.04/hr = $6.88 per GPU-hour.",
        "Only us-east-1 Linux on-demand. Polled hourly; list prices change rarely.",
    ],
)


VERDA = ProviderMapping(
    provider="verda",
    display_name="Verda (formerly DataCrunch)",
    endpoints={"GET /v1/instance-types": "every instance type with on-demand and spot price; anonymous"},
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
        provider_tier=(DERIVED, '"confidential" for ".CC" types'),
        interruptible=(DERIVED, "= (market_type == spot)"),
        available=(ABSENT, "-", "availability needs a login; stock is unknown"),
        capacity=(ABSENT, "-"),
        capacity_unit=(ABSENT, "-"),
        vcpu=(RAW, "cpu.number_of_cores"),
        ram_gb=(RAW, "memory.size_in_gigabytes"),
        storage_gb=(RAW, "storage.size_in_gigabytes", "None if the field is absent"),
    ),
    quirks=[
        "Prices are for the whole instance, on-demand and spot alike; 8x types divide by 8.",
        "Spot is exactly half the on-demand price on every type seen, so it is kept apart "
        "as interruptible and never compared with on-demand.",
        'Confidential-computing (".CC") types cost slightly more for the same GPU and stay unmapped.',
        'The B300 is listed at 268GB (usable) against the 288GB others print; it shares one name.',
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
            *extra_quirks,
        ],
    )


CRUSOE = _shadeform("crusoe", "Crusoe", ["Several sizes of one GPU share a name; prices differ by size and fabric."])
LATITUDE = _shadeform("latitude", "Latitude.sh", ['"RTXPro6000" hides a server/workstation variant and stays unmapped.'])
DENVR = _shadeform("denvr", "Denvr", ["Includes Intel Gaudi 2, which has no NVIDIA equivalent to compare against."])


PROVIDER_MAPPINGS: dict[str, ProviderMapping] = {
    m.provider: m
    for m in (
        SALAD, HYPERSTACK, LAMBDA, RUNPOD,
        HYPERBOLIC, VOLTAGEPARK, LIUM, VAST, NEBIUS, MASSEDCOMPUTE,
        DIGITALOCEAN, AWS,
        VERDA, CRUSOE, LATITUDE, DENVR,
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
