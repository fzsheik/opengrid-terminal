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
    ProviderMeta("lambda", "Lambda", "neocloud", "authenticated_api", "cloud.lambdalabs.com", "https://lambda.ai"),
    ProviderMeta("runpod", "RunPod", "neocloud", "authenticated_api", "api.runpod.io", "https://www.runpod.io",
                 "secure cloud and community cloud are listed separately"),
    ProviderMeta("vast", "Vast.ai", "marketplace", "public_api", "console.vast.ai", "https://vast.ai",
                 "host-set prices; the median row stands for Vast, the cheapest row is excluded from market math"),
    ProviderMeta("salad", "Salad", "decentralized", "authenticated_api", "api.salad.com", "https://salad.com",
                 "volunteer consumer nodes; always interruptible"),
    ProviderMeta("hyperstack", "Hyperstack", "neocloud", "authenticated_api", "infrahub-api.nexgencloud.com", "https://www.hyperstack.cloud"),
    ProviderMeta("hyperbolic", "Hyperbolic", "marketplace", "public_api", "api.hyperbolic.ai", "https://hyperbolic.ai"),
    ProviderMeta("nebius", "Nebius", "neocloud", "public_page", "nebius.com", "https://nebius.com"),
    ProviderMeta("digitalocean", "DigitalOcean", "general_cloud", "authenticated_api", "api.digitalocean.com", "https://www.digitalocean.com"),
    ProviderMeta("lium", "Lium", "decentralized", "public_api", "lium.io", "https://lium.io", "Bittensor subnet marketplace"),
    ProviderMeta("massedcompute", "Massed Compute", "neocloud", "public_page", "vm.massedcompute.com", "https://massedcompute.com"),
    ProviderMeta("verda", "Verda", "neocloud", "public_api", "api.verda.com", "https://verda.com", "formerly DataCrunch"),
    ProviderMeta("voltagepark", "Voltage Park", "neocloud", "public_api", "cloud-api.voltagepark.com", "https://www.voltagepark.com"),
    ProviderMeta("crusoe", "Crusoe", "neocloud", "aggregator", "api.shadeform.ai", "https://crusoe.ai", "prices as listed by Shadeform"),
    ProviderMeta("latitude", "Latitude.sh", "neocloud", "aggregator", "api.shadeform.ai", "https://www.latitude.sh", "prices as listed by Shadeform"),
    ProviderMeta("denvr", "Denvr", "neocloud", "aggregator", "api.shadeform.ai", "https://www.denvr.com", "prices as listed by Shadeform"),
]

PROVIDER_META: dict[str, ProviderMeta] = {m.name: m for m in _ALL}
PROVIDER_CLASSES = ("hyperscaler", "neocloud", "general_cloud", "marketplace", "decentralized")


def meta(name: str) -> ProviderMeta:
    return PROVIDER_META.get(name) or ProviderMeta(name, name, "unclassified", "unknown", "", "")


def as_dict(name: str) -> dict:
    m = meta(name)
    return {"name": m.name, "display_name": m.display_name, "provider_class": m.provider_class,
            "source_type": m.source_type, "source": m.source, "website": m.website, "note": m.note}
