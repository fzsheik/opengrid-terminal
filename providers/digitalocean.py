"""DigitalOcean: GPU Droplets, one endpoint for the whole size catalogue.

/v2/sizes lists every Droplet size with its hourly price for the whole Droplet.
GPU sizes carry a `gpu_info` block, so they are told apart from CPU sizes by
that, not by name.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.digitalocean.com"
ENDPOINT = "/v2/sizes"
PAGE_SIZE = 200
MAX_PAGES = 5

# Region slugs start with a city code; the country is not in the API.
COUNTRY_BY_REGION_PREFIX = {
    "nyc": "US", "sfo": "US", "atl": "US", "ric": "US", "mem": "US", "mkc": "US",
    "tor": "CA", "ams": "NL", "lon": "GB", "fra": "DE", "blr": "IN", "sgp": "SG", "syd": "AU",
}  # fmt: skip


class DigitalOceanProvider(Provider):
    name = "digitalocean"
    # One request per poll (80 sizes fit in one page) against 5,000 requests/hour.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=30)

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {api_key}", "accept": "application/json"},
                timeout=30,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        first = await self.get(ENDPOINT, params={"per_page": PAGE_SIZE})
        responses = [first]
        # 80 sizes today, so this loop normally runs once.
        page = 1
        while first.ok and page < MAX_PAGES and _has_next(responses[-1]):
            page += 1
            responses.append(await self.get(ENDPOINT, params={"per_page": PAGE_SIZE, "page": page}))
        return responses

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU size becomes one listing."""
        listings = []
        for snapshot in find_endpoint(by_endpoint, ENDPOINT):
            for size in (snapshot.payload or {}).get("sizes") or []:
                gpu = size.get("gpu_info")
                price = to_decimal(size.get("price_hourly"))
                count = (gpu or {}).get("count") or 0
                if not gpu or not count or price is None:
                    continue  # CPU-only size
                raw_name = gpu.get("model")
                slug = size["slug"]
                regions = size.get("regions") or []
                spot = slug.endswith("-spot")
                vram = gpu.get("vram") or {}
                listings.append(
                    ComputeListing(
                        provider="digitalocean",
                        sku=slug,
                        listing_id=slug,
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=",".join(regions) or None,
                        country=_country(regions),
                        # price_hourly is for the whole Droplet.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type="spot" if spot else "on_demand",
                        provider_tier="liquid-cooled" if "-lc" in slug else None,
                        interruptible=spot,
                        # No regions listed means we cannot say where it deploys: unknown.
                        available=True if regions and size.get("available") else None,
                        capacity=None,
                        capacity_unit=None,
                        vcpu=size.get("vcpus"),
                        ram_gb=None if not size.get("memory") else size["memory"] / 1024,
                        storage_gb=size.get("disk"),
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings


def _has_next(raw: RawResponse) -> bool:
    pages = ((raw.payload or {}).get("links") or {}).get("pages") or {}
    return bool(pages.get("next"))


def _country(regions: list[str]) -> str | None:
    """A country only when every region listed is in the same one."""
    found = {COUNTRY_BY_REGION_PREFIX.get(r[:3]) for r in regions}
    return found.pop() if len(found) == 1 and None not in found else None
