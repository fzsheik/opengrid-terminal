"""What kind of provider each one is, and where its data comes from.

Hand-maintained, like canonical.py. Used for provider-class indices, data-trust
labels and provider pages. A provider missing here is reported as "unclassified"
rather than guessed.

provider_class
    hyperscaler     AWS, GCP, Azure, OCI
    neocloud        GPU-specialist clouds selling their own capacity
    general_cloud   general-purpose clouds that also sell GPUs
    marketplace     many independent hosts set the price (one listing, many sellers)
    decentralized   consumer / volunteer / network-incentivised supply

source_type (how OpenGrid reads prices)
    authenticated_api   provider API, with an account key
    public_api          provider API, no key needed
    public_page         a public pricing page we read
    aggregator          another service's listing of this provider (e.g. Shadeform)
    public_pricing_file a published machine-readable price file

visibility (methodology/data-visibility.md): which tier a source's data is in
    public      may be republished or sold: site, public API, indices, fixings, exports, settlement
    internal    OpenGrid may read and use it (routing, recommendations, deployment) but never republish it:
                never in a public page, public API answer, statistic, index vote, fixing or export
Every source is public unless INTERNAL_SOURCES below names it, with the reason. The tier is decided here only;
normalize.save / capacity.save stamp it on each row (compute_listings.visibility, capacity_*.visibility,
archived_prices.visibility) and public readers filter on that column.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderMeta:
    name: str
    display_name: str
    provider_class: str
    source_type: str
    source: str            # the host we read
    website: str
    note: str = ""


_ALL = [
    ProviderMeta("aws", "AWS", "hyperscaler", "public_pricing_file", "b0.p.awsstatic.com", "https://aws.amazon.com/ec2/pricing/"),
    ProviderMeta("alibaba", "Alibaba Cloud", "hyperscaler", "authenticated_api", "ecs.<region>.aliyuncs.com",
                 "https://www.alibabacloud.com/product/ecs-pricing-list",
                 "GPU spot per zone and the pay-as-you-go price it is stated against; needs an AccessKey"),
    ProviderMeta("lambda", "Lambda", "neocloud", "authenticated_api", "cloud.lambda.com", "https://lambda.ai"),
    ProviderMeta("runpod", "RunPod", "neocloud", "authenticated_api", "api.runpod.io", "https://www.runpod.io",
                 "secure cloud and community cloud are listed separately"),
    ProviderMeta("vast", "Vast.ai", "marketplace", "public_api", "console.vast.ai", "https://vast.ai",
                 "host-set prices; the median row stands for Vast, the cheapest row is excluded from market math"),
    ProviderMeta("salad", "Salad", "decentralized", "authenticated_api", "api.salad.com", "https://salad.com",
                 "volunteer consumer nodes; always interruptible"),
    ProviderMeta("hyperstack", "Hyperstack", "neocloud", "authenticated_api", "infrahub-api.nexgencloud.com", "https://www.hyperstack.cloud"),
    ProviderMeta("hyperbolic", "Hyperbolic", "marketplace", "public_api", "api.hyperbolic.ai", "https://hyperbolic.ai"),
    ProviderMeta("nebius", "Nebius", "neocloud", "public_page", "nebius.com", "https://nebius.com",
                 "on-demand from the price page; spot per region from the public spot-price API (api.nebius.cloud, "
                 "no key)"),
    ProviderMeta("digitalocean", "DigitalOcean", "general_cloud", "authenticated_api", "api.digitalocean.com", "https://www.digitalocean.com"),
    ProviderMeta("lium", "Lium", "decentralized", "public_api", "lium.io", "https://lium.io", "Bittensor subnet marketplace"),
    ProviderMeta("massedcompute", "Massed Compute", "neocloud", "authenticated_api", "vm.massedcompute.com", "https://massedcompute.com",
                 "gpu-inventory API (MASSEDCOMPUTE_API_KEY): prices and launchable-instance counts; without the key, the /pricing page"),
    ProviderMeta("verda", "Verda", "neocloud", "public_api", "api.verda.com", "https://verda.com", "formerly DataCrunch"),
    ProviderMeta("voltagepark", "Voltage Park", "neocloud", "public_api", "cloud-api.voltagepark.com", "https://www.voltagepark.com"),
    ProviderMeta("crusoe", "Crusoe", "neocloud", "aggregator", "api.shadeform.ai", "https://crusoe.ai",
                 "prices as listed by Shadeform; with CRUSOE_ACCESS_KEY_ID + CRUSOE_SECRET_KEY, capacity per location "
                 "from Crusoe's own Cloud API (api.cloud.crusoe.ai)"),
    ProviderMeta("latitude", "Latitude.sh", "neocloud", "aggregator", "api.shadeform.ai", "https://www.latitude.sh", "prices as listed by Shadeform"),
    ProviderMeta("denvr", "Denvr", "neocloud", "aggregator", "api.shadeform.ai", "https://www.denvr.com", "prices as listed by Shadeform"),
    ProviderMeta("boostrun", "Boost Run", "neocloud", "aggregator", "api.shadeform.ai", "https://boostrun.com",
                 "prices as listed by Shadeform"),
    ProviderMeta("horizon", "Horizon Compute", "neocloud", "aggregator", "api.shadeform.ai", "https://horizoncompute.com",
                 "prices as listed by Shadeform; owns its H100/H200 clusters in Iceland"),
    ProviderMeta("phyntec", "Phyntec", "neocloud", "aggregator", "api.shadeform.ai", "https://phyntec.net",
                 "prices as listed by Shadeform; own RTX 4090/5090 bare metal in Wyoming"),
    ProviderMeta("paperspace", "Paperspace", "neocloud", "aggregator", "api.shadeform.ai", "https://www.paperspace.com",
                 "DigitalOcean's GPU cloud; prices as listed by Shadeform, $0.04-0.10/GPU-h above Paperspace's own "
                 "on-demand list (2026-11-01)"),
    ProviderMeta("coreweave", "CoreWeave", "neocloud", "public_page", "www.coreweave.com", "https://www.coreweave.com",
                 "on-demand and spot instance prices; per-GPU rate = instance price / GPU count"),
    ProviderMeta("together", "Together AI", "neocloud", "public_page", "www.together.ai", "https://www.together.ai",
                 "GPU Clusters on-demand and preemptible rates, printed per GPU-hour"),
    ProviderMeta("jarvislabs", "Jarvislabs", "neocloud", "authenticated_api", "backendn.jarvislabs.net", "https://jarvislabs.ai"),
    ProviderMeta("hotaisle", "Hot Aisle", "neocloud", "public_page", "hotaisle.xyz", "https://hotaisle.xyz",
                 "AMD-only; MI300X VM prices per GPU-hour; with an account key, VM stock counts from its API "
                 "(its prices there are cross-checked, not published)"),
    ProviderMeta("vultr", "Vultr", "general_cloud", "public_api", "api.vultr.com", "https://www.vultr.com",
                 "only plans Vultr marks deployable on demand"),
    ProviderMeta("gcp", "Google Cloud", "hyperscaler", "authenticated_api", "cloudbilling.googleapis.com",
                 "https://cloud.google.com/compute/gpus-pricing",
                 "Cloud Billing Catalog API (API key); VM prices assembled from GPU, vCPU, RAM and SSD SKUs, in the regions "
                 "Compute Engine offers each machine type (read-only service account; Iowa only without it)"),
    ProviderMeta("azure", "Microsoft Azure", "hyperscaler", "public_api", "prices.azure.com",
                 "https://azure.microsoft.com/pricing/details/virtual-machines/linux/",
                 "Retail Prices API, pay-as-you-go Linux list prices in eastus and eastus2; "
                 "per-GPU rate = VM price / GPU count"),
    ProviderMeta("linode", "Akamai Cloud (Linode)", "general_cloud", "public_api", "api.linode.com", "https://www.linode.com",
                 "GPU plans from the anonymous plan catalogue; per-GPU rate = plan price / GPU count"),
    ProviderMeta("ovhcloud", "OVHcloud", "general_cloud", "public_api", "api.us.ovhcloud.com, ca.api.ovh.com",
                 "https://www.ovhcloud.com", "Public Cloud GPU flavors from the two USD catalogues (US and World)"),
    ProviderMeta("scaleway", "Scaleway", "general_cloud", "public_api", "api.scaleway.com", "https://www.scaleway.com",
                 "Scaleway's own public catalogue, EUR list prices converted to USD at the ECB reference rate "
                 "(Shadeform's USD listing until 2026-11-01)"),
    ProviderMeta("koyeb", "Koyeb", "general_cloud", "public_api", "app.koyeb.com", "https://www.koyeb.com",
                 "serverless GPU instances, billed per second; hourly list price"),
    ProviderMeta("oracle", "Oracle Cloud", "hyperscaler", "public_pricing_file", "apexapps.oracle.com",
                 "https://www.oracle.com/cloud/compute/pricing/",
                 "global list prices per GPU-hour; shapes from Oracle's pricing page"),
    ProviderMeta("ibm", "IBM Cloud", "general_cloud", "public_api", "globalcatalog.cloud.ibm.com",
                 "https://www.ibm.com/cloud/gpu", "VPC GPU virtual servers; on-demand and spot per region"),
    ProviderMeta("thundercompute", "Thunder Compute", "neocloud", "authenticated_api", "api.thundercompute.com",
                 "https://www.thundercompute.com", "instance list prices incl. base vCPUs and 100 GB disk per GPU; live stock"),
    ProviderMeta("arkane", "Arkane Cloud", "neocloud", "public_page", "arkanecloud.com", "https://arkanecloud.com",
                 "on-demand Cloud GPU prices per GPU-hour, read as 1-GPU listings"),
    ProviderMeta("vessl", "VESSL AI", "neocloud", "public_page", "vessl.ai", "https://vessl.ai",
                 "on-demand Container Compute per GPU-hour on VESSL-run shared clusters"),
    ProviderMeta("mithril", "Mithril", "neocloud", "public_page", "mithril.ai", "https://mithril.ai",
                 "formerly ML Foundry; spot prices per instance, a published 7-day average updated weekly"),
    ProviderMeta("nosana", "Nosana", "decentralized", "public_api", "api.nosana.com", "https://nosana.com",
                 "Solana GPU network; validated (premium) markets, host USD rate plus network fee; interruptible"),
    ProviderMeta("civo", "Civo", "general_cloud", "public_page", "www.civo.com", "https://www.civo.com",
                 "on-demand GPU instance prices; per-GPU rate = instance price / GPU count; commitment terms not read"),
    ProviderMeta("atlanticnet", "Atlantic.Net", "general_cloud", "public_page", "www.atlantic.net",
                 "https://www.atlantic.net/gpu-server-hosting/", "GPU cloud plans, on-demand hourly; 1- and 3-year terms not read"),
    ProviderMeta("deepinfra", "DeepInfra", "neocloud", "public_page", "deepinfra.com", "https://deepinfra.com/gpu-instances",
                 "GPU instances (containers billed by the minute); model API prices not read; with an account key, "
                 "in-stock flags from its container API (its prices there are not published)"),
    ProviderMeta("e2e", "E2E Networks", "general_cloud", "public_pricing_file", "www.e2enetworks.com",
                 "https://www.e2enetworks.com/pricing",
                 "Indian cloud; USD on-demand list from its pricing.json feed, per GPU-hour; INR-only spot and committed rates not read"),
    ProviderMeta("akash", "Akash Network", "decentralized", "public_api", "console-api.akash.network",
                 "https://akash.network",
                 "open GPU marketplace; Akash Console's bid-derived summary: the allocatable-weighted average of each "
                 "provider's chosen bid (up to 31 days old), per GPU; free/total GPU counts"),
    ProviderMeta("hydrahost", "Hydra Host", "marketplace", "public_api", "brokkr.hydrahost.com", "https://hydrahost.com",
                 "bare-metal servers from independent suppliers; one listing per device, on-demand and interruptible "
                 "(cents per hour in the API); deployable now only when stockStatus is \"on demand\""),
    ProviderMeta("cudo", "CUDO Compute", "neocloud", "public_api", "rest.compute.cudo.org", "https://www.cudocompute.com",
                 "VMs (smallest 1-GPU config: GPU + minimum vCPU and RAM; disk extra) and bare metal (no-commitment "
                 "price per server); free GPUs / servers per data centre"),
    ProviderMeta("gpuai", "GPU.ai", "marketplace", "public_api", "api.gpu.ai", "https://gpu.ai",
                 "reseller: aggregates other providers' GPUs (its terms) without naming them; listed as "
                 "resold:undisclosed and kept out of market statistics"),
    ProviderMeta("exoscale", "Exoscale", "general_cloud", "public_api", "portal.exoscale.com", "https://www.exoscale.com",
                 "Swiss cloud; USD map of its public price feed; GPU model and size per family from its pricing page; "
                 "local storage extra; GPU access after account screening"),
    ProviderMeta("huggingface", "Hugging Face Inference Endpoints", "general_cloud", "public_api",
                 "api.endpoints.huggingface.cloud", "https://endpoints.huggingface.co",
                 "managed endpoints on AWS, GCP and Azure capacity; listings are resold:<cloud> and kept out of "
                 "market statistics"),
    ProviderMeta("gcore", "Gcore", "general_cloud", "public_page", "gcore.com", "https://gcore.com/cloud/ai-gpu",
                 "GPU Cloud bare metal (self-serve 8-GPU servers) and Virtual GPU; EUR list prices excl. VAT converted "
                 "to USD at the ECB reference rate"),
    ProviderMeta("fireworks", "Fireworks AI", "general_cloud", "public_page", "fireworks.ai", "https://fireworks.ai",
                 "on-demand (dedicated) inference deployments, USD per GPU-hour; INTERNAL: its terms forbid "
                 "republishing the Service's content"),
    ProviderMeta("ionos", "IONOS Cloud", "general_cloud", "public_page", "docs.ionos.com", "https://cloud.ionos.com",
                 "Cloud GPU VM (H200 PCIe, 1/2/4/8 GPUs) from IONOS CLOUD Inc.'s published USD price list; "
                 "Frankfurt and Worcester"),
    ProviderMeta("hetzner", "Hetzner", "general_cloud", "public_page", "www.hetzner.com",
                 "https://www.hetzner.com/dedicated-rootserver/matrix-gpu/",
                 "dedicated GEX GPU servers at the printed hourly rate (EUR excl. VAT, converted at the ECB rate); "
                 "a one-time setup fee applies"),
    ProviderMeta("yotta", "Yotta Shakti Cloud", "neocloud", "public_page", "shakticloud.ai", "https://shakticloud.ai/pricing/",
                 "on-demand GPU VMs (H100 SXM, L40S) at the printed INR hourly rate, converted at the ECB INR->USD cross "
                 "rate; GST treatment not stated"),
    ProviderMeta("sakura", "Sakura Internet", "general_cloud", "public_page", "ai.sakura.ad.jp",
                 "https://ai.sakura.ad.jp/gpu/koukaryoku-dok/",
                 "Koukaryoku DOK GPU containers billed per second; JPY prices INCLUDE consumption tax (rate not stated) "
                 "and are kept tax-inclusive (provider_tier tax_inclusive), converted at the ECB JPY->USD cross rate"),
    ProviderMeta("simplepod", "SimplePod", "marketplace", "public_api", "api.simplepod.ai", "https://simplepod.ai",
                 "own racks plus community hosts; one listing per open on-demand offer (Docker and VM); disk billed extra"),
    ProviderMeta("clore", "Clore.ai", "marketplace", "authenticated_api", "api.clore.ai", "https://clore.ai",
                 "host-set USD prices per server per day, plus the renter fee; the median row stands for Clore, "
                 "the cheapest row is excluded from market math"),
    ProviderMeta("modal", "Modal", "general_cloud", "public_page", "modal.com", "https://modal.com",
                 "serverless GPUs, billed per second; GPU-only rate (CPU and memory extra), preemptible by default"),
    ProviderMeta("replicate", "Replicate", "general_cloud", "public_page", "replicate.com", "https://replicate.com",
                 "serverless hardware, billed per second; whole hardware type incl. CPU and RAM; committed-spend rows not read"),
    ProviderMeta("baseten", "Baseten", "general_cloud", "public_page", "www.baseten.co", "https://www.baseten.co",
                 "serverless dedicated deployments, billed per minute; smallest instance per GPU incl. CPU and memory"),
    ProviderMeta("beam", "Beam", "general_cloud", "public_page", "www.beam.cloud", "https://www.beam.cloud",
                 "serverless GPUs, billed per millisecond; pay-as-you-go GPU-only rate (CPU and memory extra)"),
]

PROVIDER_META: dict[str, ProviderMeta] = {m.name: m for m in _ALL}
PROVIDER_CLASSES = ("hyperscaler", "neocloud", "general_cloud", "marketplace", "decentralized")


# --------------------------------------------------------------------------
# Data visibility: the single registry of INTERNAL sources (everything else is public)
# --------------------------------------------------------------------------

PUBLIC, INTERNAL = "public", "internal"
VISIBILITIES = (PUBLIC, INTERNAL)
ALL_SOURCES = "*"          # an entry for every source of a provider: the provider is wholly internal
DATA_KINDS = ("listings", "capacity", "signals")


@dataclass(frozen=True)
class InternalSource:
    """One source whose data OpenGrid may read and use, but not republish.

    provider  the provider name (PROVIDERS key)
    source    the endpoint / feed it comes from, exactly as the provider module stores it (RawSnapshot.endpoint,
              CapacityObservation.source); ALL_SOURCES makes every row of the provider internal
    data      what it feeds: listings (prices), capacity (stock rows), signals (anything else routing may read)
    reason    why it is internal (terms, confidentiality); written for the methodology page, no confidential numbers
    used_by   where OpenGrid uses it
    """

    provider: str
    source: str
    data: tuple[str, ...]
    reason: str
    used_by: str


INTERNAL_SOURCES: tuple[InternalSource, ...] = (
    InternalSource(
        "deepinfra", "/v1/containers/gpu_availability", ("listings",),
        "The container API's hourly prices are not the public price-page prices and may be specific to OpenGrid's "
        "account; DeepInfra's terms (s.17) count non-public pricing as Confidential Information. The public "
        "listings stay the /gpu-instances page prices. The same response's in-stock flag stays public capacity.",
        "routing, recommendations and job placement (internal listings '<model>:<n>x:api')"),
    InternalSource(
        "aws", "https://spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json", ("signals",),
        "AWS Spot Instance Advisor interruption bands and savings: AWS's site terms restrict gathering and "
        "republishing data from aws.amazon.com, so the bands are never shown publicly.",
        "routing (routing/internal_data.spot_interruption_bands); spot is not ranked yet, so no route uses it today"),
    InternalSource(
        "fireworks", ALL_SOURCES, ("listings", "capacity", "signals"),
        "Fireworks AI's Terms of Service (2026-07-10) cover the website as part of the Service and, in clause (e), "
        "forbid copying or distributing any part of the Service. Its on-demand deployment prices may be read at a "
        "human request rate (hourly) and used, never republished, so the whole provider is internal.",
        "routing, recommendations and job placement (internal listings from fireworks.ai/pricing)"),
)


def internal_entries(provider: str | None = None) -> list[InternalSource]:
    return [e for e in INTERNAL_SOURCES if provider is None or e.provider == provider]


def provider_internal(provider: str) -> bool:
    """True when every source of `provider` is internal (an ALL_SOURCES entry)."""
    return any(e.provider == provider and e.source == ALL_SOURCES for e in INTERNAL_SOURCES)


def visibility(provider: str, source: str | None = None, data: str = "listings") -> str:
    """The tier of `data` read from `provider`'s `source`. PUBLIC unless the registry names it (or the provider)."""
    for e in INTERNAL_SOURCES:
        if e.provider == provider and (e.source == ALL_SOURCES or (source is not None and e.source == source)) \
                and (data in e.data or e.source == ALL_SOURCES):
            return INTERNAL
    return PUBLIC


def enforce(provider: str, claimed: str | None) -> str:
    """The tier a row is STORED with. Fails closed: a wholly internal provider, an internal claim, or any value
    that is not exactly 'public' stores as internal. Called by normalize.save and capacity.save."""
    if provider_internal(provider) or claimed != PUBLIC:
        return INTERNAL
    return PUBLIC


def is_public_provider(name: str) -> bool:
    """False for a wholly internal provider: it is not named on public provider lists either."""
    return not provider_internal(name)


def visibility_registry() -> list[dict]:
    """The registry as data (admin endpoint, methodology checks)."""
    return [{"provider": e.provider, "source": e.source, "data": list(e.data), "visibility": INTERNAL,
             "reason": e.reason, "used_by": e.used_by} for e in INTERNAL_SOURCES]


def meta(name: str) -> ProviderMeta:
    return PROVIDER_META.get(name) or ProviderMeta(name, name, "unclassified", "unknown", "", "")


def as_dict(name: str) -> dict:
    m = meta(name)
    return {"name": m.name, "display_name": m.display_name, "provider_class": m.provider_class,
            "source_type": m.source_type, "source": m.source, "website": m.website, "note": m.note}


# --------------------------------------------------------------------------
# AI input: providers whose data must not be fed to a language model (assistant grounding, MCP tool answers)
# --------------------------------------------------------------------------
# Separate from the data tiers: these rows stay public on the site and API. Each entry names its source.
AI_INPUT_EXCLUDED: dict[str, str] = {
    "gcore": "gcore.com/robots.txt sets `Content-Signal: ai-train=no, ai-input=no` (checked 2026-11-01)",
}
_PROVIDER_KEYS = ("provider", "provider_id", "provider_name")


def ai_input_allowed(provider: str | None) -> bool:
    return (provider or "").lower() not in AI_INPUT_EXCLUDED


def scrub_ai_input(obj, excluded=None):
    """`obj` without any row of an AI-input-excluded provider: list items that are dicts naming one under
    provider / provider_id / provider_name are dropped, and dict entries keyed by one (by_provider maps) too.
    Aggregates computed over all providers (a market median) are left as they are. Returns a new object."""
    ex = AI_INPUT_EXCLUDED if excluded is None else excluded
    if isinstance(obj, list):
        return [scrub_ai_input(x, ex) for x in obj
                if not (isinstance(x, dict) and any(str(x.get(k) or "").lower() in ex for k in _PROVIDER_KEYS))]
    if isinstance(obj, dict):
        return {k: scrub_ai_input(v, ex) for k, v in obj.items() if not (isinstance(k, str) and k.lower() in ex)}
    return obj
