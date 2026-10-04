"""SaladCloud: idle consumer GPUs, priced per GPU-hour at four priority tiers."""

import logging
from functools import partial
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import Provider, PollingPolicy, find_endpoint, gather_limited, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.salad.com/api/public"

# Salad priority tier -> the field holding that tier's free-GPU COUNT.
# Naming trap: "available_gpu_high" is a NUMBER of free GPUs, not a flag. It
# feeds `capacity`; the API exposes no availability boolean of its own, so
# `available` is derived from the count below.
SALAD_TIER_TO_AVAILABILITY = {
    "batch": "available_gpu_batch",
    "low": "available_gpu_low",
    "medium": "available_gpu_medium",
    "high": "available_gpu_high",
}


class SaladProvider(Provider):
    name = "salad"
    # 44 requests per poll (1 classes + 1 quotas + one availability POST per
    # class) against a 240/min key limit: ~1% of budget at 15 min. A poll takes
    # 25 to 40 seconds and a slow call can add 20 more, so 45 cut polls off and
    # lost them whole; this leaves room.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=150)

    def __init__(self, api_key: str, org: str, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"Salad-Api-Key": api_key, "accept": "application/json"},
                timeout=20,
            )
        )
        self._org = org

    async def fetch(self) -> list[RawResponse]:
        org = self._org
        classes = await self.get(f"/organizations/{org}/gpu-classes")
        quotas = await self.get(f"/organizations/{org}/quotas")
        out = [classes, quotas]

        # Availability is per GPU class, one POST each. Rate limit is 240/min.
        items = (classes.payload or {}).get("items") or []
        out += await gather_limited(
            [
                partial(
                    self.post,
                    f"/organizations/{org}/availability/sce-gpu-availability",
                    json={"gpu_classes": [class_id]},
                )
                for c in items
                if (class_id := c.get("id"))
            ]
        )
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        classes_rows = find_endpoint(by_endpoint, "gpu-classes")
        if not classes_rows:
            return []
        snapshot = classes_rows[0]
        items = (snapshot.payload or {}).get("items") or []

        # The availability response carries no class id, so match it back to the
        # class id we sent in the request body.
        availability: dict[str, dict] = {}
        for row in find_endpoint(by_endpoint, "sce-gpu-availability"):
            asked = (row.request or {}).get("gpu_classes") or []
            if asked and isinstance(row.payload, dict):
                availability[asked[0]] = row.payload

        listings = []
        for gpu_class in items:
            class_id = gpu_class.get("id")
            raw_name = gpu_class.get("name")
            if not class_id or not raw_name:
                continue
            avail = availability.get(class_id)
            for price in gpu_class.get("prices") or []:
                tier = price.get("priority")
                field = SALAD_TIER_TO_AVAILABILITY.get(tier)
                if field is None:
                    log.warning("salad: unknown priority %r on %s", tier, raw_name)
                    continue
                capacity = avail.get(field) if avail else None
                listings.append(
                    ComputeListing(
                        provider="salad",
                        sku=class_id,
                        listing_id=f"{class_id}:{tier}",
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        # Salad reports no multi-GPU shape; it sells single GPUs.
                        gpu_count=1,
                        region=None,
                        country=None,
                        price_per_gpu_hour=to_decimal(price.get("price")),
                        price_per_instance_hour=to_decimal(price.get("price")),
                        currency="USD",
                        # The four priorities are Salad's own tiers, not a spot market.
                        market_type="on_demand",
                        provider_tier=tier,
                        # Volunteered consumer nodes can drop at any tier.
                        interruptible=True,
                        available=None if capacity is None else capacity > 0,
                        capacity=capacity,
                        capacity_unit="gpu",
                        vcpu=None,
                        ram_gb=None,
                        storage_gb=None,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
