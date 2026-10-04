"""Lambda Labs Cloud: dedicated GPU VMs, one endpoint for the whole catalogue.

Module is lambda_labs.py because `lambda` is a Python keyword; the provider
slug is still "lambda".
"""

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://cloud.lambdalabs.com/api/v1"

# Lambda's region names carry the country in their prefix.
COUNTRY_BY_REGION_PREFIX = {
    "us": "US",
    "eu": "DE",
    "asia": "JP",
    "me": "IL",
    "australia": "AU",
}


def country_for(region: str | None) -> str | None:
    if not region:
        return None
    return COUNTRY_BY_REGION_PREFIX.get(region.split("-")[0].lower())


class LambdaProvider(Provider):
    name = "lambda"
    # 3 requests per poll. Lambda publishes no rate limit and returns no rate
    # headers, so this is kept small on purpose.
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
        # No documented rate limit and no rate headers come back, so keep it small.
        return list(
            await asyncio.gather(
                self.get("/instance-types"),
                self.get("/images"),
                self.get("/file-systems"),
            )
        )

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One instance type becomes one listing per region with capacity.

        A type with no capacity anywhere still gets a single listing with
        region=None, so its published price keeps being tracked while sold out.
        """
        rows = [r for ep, rs in by_endpoint.items() if "/instance-types" in ep for r in rs]
        if not rows:
            return []
        snapshot = rows[0]

        # The live API returns `data` as a dict keyed by instance type name,
        # not the data.types[] array the published OpenAPI advertises.
        data = (snapshot.payload or {}).get("data") or {}
        if not isinstance(data, dict):
            log.warning("lambda: unexpected data shape %s", type(data).__name__)
            return []

        listings = []
        for type_name, entry in data.items():
            spec_block = entry.get("instance_type") or {}
            specs = spec_block.get("specs") or {}
            gpu_count = specs.get("gpus") or 0
            if not gpu_count:
                continue  # CPU-only instance type

            raw_name = spec_block.get("gpu_description") or spec_block.get("description") or type_name
            cents = spec_block.get("price_cents_per_hour")
            # Lambda quotes CENTS for the WHOLE INSTANCE: divide for a per-GPU
            # rate. This is the inverse of Hyperstack, which quotes per GPU.
            per_instance = None if cents is None else Decimal(cents) / 100
            per_gpu = None if per_instance is None else per_instance / gpu_count

            regions = [
                r.get("name") for r in entry.get("regions_with_capacity_available") or []
            ]
            for region in regions or [None]:
                listings.append(
                    ComputeListing(
                        provider="lambda",
                        sku=type_name,
                        listing_id=f"{type_name}:{region or 'any'}",
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=gpu_count,
                        region=region,
                        country=country_for(region),
                        price_per_gpu_hour=per_gpu,
                        price_per_instance_hour=per_instance,
                        currency="USD",
                        market_type="on_demand",
                        provider_tier=None,
                        interruptible=False,
                        # Appearing in the capacity list is the only stock signal.
                        available=region is not None,
                        # Lambda says WHERE there is stock, never HOW MUCH.
                        capacity=None,
                        capacity_unit=None,
                        vcpu=specs.get("vcpus"),
                        ram_gb=specs.get("memory_gib"),
                        storage_gb=specs.get("storage_gib"),
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
