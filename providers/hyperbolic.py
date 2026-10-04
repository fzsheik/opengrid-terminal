"""Hyperbolic: public on-demand rental options, no API key.

One anonymous endpoint returns a bare list of rentable shapes. Each entry is a
whole-option price, so the per-GPU rate is derived.
"""

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.hyperbolic.ai"
ENDPOINT = "/v2/alpha/on-demand/rental-options"


class HyperbolicProvider(Provider):
    name = "hyperbolic"
    # One request per poll against an anonymous endpoint.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=30)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=30,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        return [await self.get(ENDPOINT)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One rental option becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        options = snapshot.payload
        if not isinstance(options, list):
            log.warning("hyperbolic: unexpected payload shape %s", type(options).__name__)
            return []

        listings = []
        for opt in options:
            gpu_count = opt.get("gpuCount") or 0
            cents = opt.get("costPerHourCents")
            gpu_type, form = opt.get("gpuType"), opt.get("gpuFormFactor")
            if not gpu_count or cents is None or not gpu_type:
                continue
            raw_name = f"{gpu_type} {form}" if form else gpu_type
            # costPerHourCents is the WHOLE option, in cents.
            per_instance = Decimal(str(cents)) / 100
            per_gpu = per_instance / gpu_count

            region, machine = opt.get("region"), opt.get("machineType")
            link = opt.get("connectionType")
            total = opt.get("totalAvailable")

            # Node specs only describe the option when they add up to its GPUs.
            nodes = opt.get("nodes") or []
            exact = nodes and sum(n.get("gpuCount") or 0 for n in nodes) == gpu_count
            listings.append(
                ComputeListing(
                    provider="hyperbolic",
                    sku=f"{raw_name}:{gpu_count}x",
                    listing_id=f"{raw_name}:{gpu_count}x:{region}:{machine}:{link}",
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=gpu_count,
                    region=region,
                    country=None,
                    price_per_gpu_hour=per_gpu,
                    price_per_instance_hour=per_instance,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=machine,
                    interruptible=False,
                    # A missing totalAvailable means unknown, never zero.
                    available=None if total is None else bool(opt.get("enabled")) and total > 0,
                    capacity=total,
                    capacity_unit=None if total is None else "gpu",
                    vcpu=sum(n.get("vcpuCount") or 0 for n in nodes) or None if exact else None,
                    ram_gb=sum(n.get("ramGb") or 0 for n in nodes) or None if exact else None,
                    storage_gb=sum(n.get("storageGb") or 0 for n in nodes) or None if exact else None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
