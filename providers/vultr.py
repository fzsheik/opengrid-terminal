"""Vultr: the public plan catalogue, no API key.

GET /v2/plans?type=vcg (Cloud GPU VMs) and GET /v2/plans-metal (bare metal) are
anonymous. Each plan carries gpu_type, gpu_count, hourly_cost for the WHOLE
plan, deploy_ondemand / deploy_preemptible flags and the locations it deploys in.

Only GPU plans with deploy_ondemand true are read, at hourly_cost. Vultr's GPU
bare-metal plans (H100, B200, MI300X, ...) all say deploy_ondemand false, i.e.
not rentable on demand, so they produce no listing; their "preemptible" price is
not documented as a spot market and is not read either.

Vultr is a general-purpose cloud, so it never counts toward neocloud indices.
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

BASE_URL = "https://api.vultr.com"
VM_ENDPOINT = "/v2/plans?type=vcg&per_page=500"
METAL_ENDPOINT = "/v2/plans-metal?per_page=500"
_KEYS = {VM_ENDPOINT: "plans", METAL_ENDPOINT: "plans_metal"}


class VultrProvider(Provider):
    name = "vultr"
    # Two anonymous requests per poll; list prices change rarely.
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
        out = []
        for path, key in _KEYS.items():
            raw = await self.get(path)
            plans = (raw.payload or {}).get(key) if isinstance(raw.payload, dict) else None
            if plans is None:
                out.append(raw)
                continue
            # Keep the GPU plans only, verbatim: the rest are CPU servers.
            out.append(reshaped(raw, {key: [p for p in plans if p.get("gpu_count")]}))
        return out

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One on-demand GPU plan becomes one listing."""
        listings = []
        for path, key in _KEYS.items():
            rows = find_endpoint(by_endpoint, path.split("?")[0] + "?")
            if not rows or not isinstance(rows[0].payload, dict):
                continue
            snapshot = rows[0]
            tier = "vm" if key == "plans" else "bare-metal"
            for p in snapshot.payload.get(key) or []:
                count = p.get("gpu_count") or 0
                if not count or not p.get("deploy_ondemand"):
                    continue  # CPU plan, or not rentable on demand
                price = to_decimal(p.get("hourly_cost"))
                if price is None or price <= 0:
                    continue
                locations = p.get("locations") or []
                raw_name = p.get("gpu_type") or ""
                ram_mb = p.get("ram")
                listings.append(
                    ComputeListing(
                        provider="vultr",
                        sku=p["id"],
                        listing_id=p["id"],
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        region=",".join(locations)[:64] or None,
                        country=None,
                        # hourly_cost is for the whole plan.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type="on_demand",
                        provider_tier=tier,
                        interruptible=False,
                        # `locations` is where a plan is offered, not live stock (that is a
                        # separate per-region call): unknown, never assumed in stock.
                        available=None,
                        capacity=None,
                        capacity_unit=None,
                        vcpu=p.get("vcpu_count") or p.get("cpu_threads"),
                        ram_gb=None if ram_mb is None else ram_mb / 1024,
                        storage_gb=None if p.get("disk") is None else p["disk"] * (p.get("disk_count") or 1),
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
