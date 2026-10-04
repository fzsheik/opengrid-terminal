"""Voltage Park: two public surfaces, no API key.

    bare-metal locations   per-GPU price on whole 8-GPU nodes, ethernet or
                           InfiniBand fabric, with a count of GPUs in stock
    instant-deploy presets VM shapes with a per-instance hourly rate and the
                           locations that currently have stock
"""

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://cloud-api.voltagepark.com/api/v1"
BARE_METAL = "/bare-metal/locations"
PRESETS = "/instant-deploy-presets/"


class VoltageParkProvider(Provider):
    name = "voltagepark"
    # Two requests per poll against anonymous endpoints.
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
        return list(await asyncio.gather(self.get(BARE_METAL), self.get(PRESETS)))

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        listings = []

        for snapshot in find_endpoint(by_endpoint, BARE_METAL)[:1]:
            for loc in (snapshot.payload or {}).get("results") or []:
                specs = loc.get("specs_per_node") or {}
                raw_name = specs.get("gpu_model")
                node_gpus = specs.get("gpu_count") or 0
                if not raw_name or not node_gpus:
                    continue
                # Two published per-GPU prices, one per interconnect fabric.
                for fabric in ("ethernet", "infiniband"):
                    per_gpu = to_decimal(loc.get(f"gpu_price_{fabric}"))
                    if per_gpu is None:
                        continue
                    in_stock = loc.get(f"gpu_count_{fabric}")
                    listings.append(
                        ComputeListing(
                            provider="voltagepark",
                            sku=f"{raw_name}:bare-metal:{fabric}",
                            listing_id=f"bm:{loc.get('id')}:{fabric}",
                            raw_gpu_name=raw_name,
                            canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                            gpu_count=node_gpus,
                            region=None,
                            country=None,
                            price_per_gpu_hour=per_gpu,
                            price_per_instance_hour=per_gpu * node_gpus,
                            currency="USD",
                            market_type="on_demand",
                            provider_tier=f"bare-metal-{fabric}",
                            interruptible=False,
                            available=None if in_stock is None else in_stock > 0,
                            capacity=in_stock,
                            capacity_unit=None if in_stock is None else "gpu",
                            vcpu=specs.get("cpu_count"),
                            ram_gb=specs.get("ram_gb"),
                            storage_gb=specs.get("storage_gb"),
                            observed_at=snapshot.fetched_at,
                        )
                    )

        for snapshot in find_endpoint(by_endpoint, PRESETS)[:1]:
            presets = snapshot.payload
            if not isinstance(presets, list):
                log.warning("voltagepark: unexpected presets shape %s", type(presets).__name__)
                continue
            for preset in presets:
                res = preset.get("resources") or {}
                gpus = res.get("gpus") or {}
                rate = to_decimal(preset.get("compute_rate_hourly"))
                if len(gpus) != 1 or rate is None:
                    continue  # CPU-only, or a mixed-GPU shape we cannot name
                raw_name, spec = next(iter(gpus.items()))
                count = (spec or {}).get("count") or 0
                if not count:
                    continue
                places = preset.get("location_ids_with_availability")
                listings.append(
                    ComputeListing(
                        provider="voltagepark",
                        sku=f"{raw_name}:vm:{count}x",
                        listing_id=f"vm:{preset.get('id')}",
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=None,
                        country=None,
                        # compute_rate_hourly is per instance; storage bills apart.
                        price_per_gpu_hour=rate / count,
                        price_per_instance_hour=rate,
                        currency="USD",
                        market_type="on_demand",
                        provider_tier="vm",
                        interruptible=False,
                        available=None if places is None else len(places) > 0,
                        capacity=None if places is None else len(places),
                        capacity_unit=None if places is None else "location",
                        vcpu=res.get("vcpu_count"),
                        ram_gb=res.get("ram_gb"),
                        storage_gb=res.get("storage_gb"),
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
