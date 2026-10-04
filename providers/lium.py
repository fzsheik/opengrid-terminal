"""Lium: a marketplace of rentable GPU machines, public JSON, no API key.

Each executor is one host machine with its own price. The raw body carries live
GPU utilization, driver and port details that change every request, so the
stored payload keeps only the fields we read.
"""

import logging
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://lium.io"
ENDPOINT = "/api/executors"

KIB_PER_GIB = 1024 * 1024


def _keep(executor: dict) -> dict:
    """The fields normalize() reads, verbatim."""
    specs = executor.get("specs") or {}
    loc = executor.get("location") or {}
    return {
        "id": executor.get("id"),
        "machine_name": executor.get("machine_name"),
        "price_per_gpu": executor.get("price_per_gpu"),
        "gpu_count": executor.get("gpu_count"),
        "available_gpu_count": executor.get("available_gpu_count"),
        "tier": executor.get("tier"),
        "reliability_score": executor.get("reliability_score"),
        "country_code": loc.get("country_code"),
        "city": loc.get("city"),
        "is_spot": specs.get("is_spot"),
        "cpu_count": (specs.get("cpu") or {}).get("count"),
        "ram_total": (specs.get("ram") or {}).get("total"),
        "disk_total": (specs.get("hard_disk") or {}).get("total"),
    }


class LiumProvider(Provider):
    name = "lium"
    # One request per poll against an anonymous endpoint (about 1 MB raw).
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=45,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        raw = await self.get(ENDPOINT)
        if isinstance(raw.payload, list):
            raw = reshaped(raw, [_keep(e) for e in raw.payload])
        return [raw]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One executor (a whole host) becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        if not isinstance(snapshot.payload, list):
            log.warning("lium: unexpected payload shape %s", type(snapshot.payload).__name__)
            return []

        listings = []
        for e in snapshot.payload:
            per_gpu = to_decimal(e.get("price_per_gpu"))
            gpu_count = e.get("gpu_count") or 0
            raw_name = e.get("machine_name")
            if per_gpu is None or not gpu_count or not raw_name:
                continue
            free = e.get("available_gpu_count")
            ram, disk = e.get("ram_total"), e.get("disk_total")
            listings.append(
                ComputeListing(
                    provider="lium",
                    sku=raw_name,
                    listing_id=str(e["id"]),
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=gpu_count,
                    region=e.get("city"),
                    country=e.get("country_code"),
                    price_per_gpu_hour=per_gpu,
                    price_per_instance_hour=per_gpu * gpu_count,
                    currency="USD",
                    market_type="spot" if e.get("is_spot") else "on_demand",
                    provider_tier=e.get("tier"),
                    interruptible=bool(e.get("is_spot")),
                    available=None if free is None else free > 0,
                    capacity=free,
                    capacity_unit=None if free is None else "gpu",
                    vcpu=e.get("cpu_count"),
                    ram_gb=None if not ram else round(ram / KIB_PER_GIB, 1),
                    storage_gb=None if not disk else round(disk / KIB_PER_GIB, 1),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
