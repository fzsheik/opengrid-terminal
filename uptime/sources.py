"""Which UptimeProject services map to a provider OpenGrid tracks.

UptimeProject lists services (AWS EC2 us-east-1, Azure Blob, ...), not our providers, so the mapping is
explicit and hand-maintained like provider_meta.py. A service whose UptimeProject `provider` string is not
in UPSTREAM is ignored (Hetzner, Cloudflare, OpenAI ... are not providers we track). Unknown providers are
never guessed.
"""

from provider_meta import _ALL

# UptimeProject `provider` field -> OpenGrid provider slug
UPSTREAM: dict[str, str] = {
    "Amazon Web Services": "aws",
    "Microsoft Azure": "azure",
    "Google Cloud": "gcp",
    "DigitalOcean": "digitalocean",
    "Akamai / Linode": "linode",
    "OVHcloud": "ovhcloud",
    "Scaleway": "scaleway",
}

# The category that says something about running compute. The rest are kept (they are the same providers'
# control planes) but the page opens on this one.
COMPUTE_CATEGORY = "iaas"

ATTRIBUTION = "UptimeProject (uptimeproject.org), CC BY 4.0"
SOURCE_URL = "https://uptimeproject.org"


def provider_for(upstream_provider: str | None) -> str | None:
    slug = UPSTREAM.get((upstream_provider or "").strip())
    return slug if slug in {m.name for m in _ALL} else None
