"""Akamai Cloud (Linode): the public plan catalogue, no API key.

GET /v4/linode/types is anonymous (Linode's API docs mark it as needing no
token). Each type carries `gpus` (GPU count), `price.hourly` for the WHOLE plan
and `region_prices` (regions billed differently from the base price).

Only types with gpus > 0 are read. The GPU model is not a field: it is in the
plan label ("Dedicated 32GB + RTX6000 GPU x1", "RTX4000 Ada x2 Medium"), so the
normalizer pulls the "RTX..." token out of it; a GPU label with no such token
keeps the whole label as its name and stays unmapped.

Akamai/Linode is a general-purpose cloud, so it never counts toward neocloud indices.
"""

import logging
import re
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.linode.com"
ENDPOINT = "/v4/linode/types?page_size=500"
_GPU_TOKEN = re.compile(r"\b(RTX\s?\w+(?: Ada)?)\b")


def gpu_name(label: str) -> str:
    """The GPU named in a plan label, e.g. "RTX6000" or "RTX4000 Ada"."""
    m = _GPU_TOKEN.search(label or "")
    return m.group(1) if m else (label or "")


class LinodeProvider(Provider):
    name = "linode"
    # One anonymous request per poll; list prices change rarely.
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
        raw = await self.get(ENDPOINT)
        body = raw.payload
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            return [raw]  # stored as-is; normalize() refuses it
        # Keep the GPU types only, verbatim (plus the paging fields): the rest are CPU plans.
        return [reshaped(raw, {**{k: v for k, v in body.items() if k != "data"},
                               "data": [t for t in body["data"] if t.get("gpus")]})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU type becomes one listing, plus one per region priced differently."""
        rows = find_endpoint(by_endpoint, "/v4/linode/types")
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("linode: /linode/types has no `data` list - response reshaped")
        if (body.get("pages") or 1) > (body.get("page") or 1):
            raise ValueError("linode: /linode/types is paged beyond page 1 - some types would be missed")
        listings = []
        for t in body["data"]:
            count = t.get("gpus")
            if not count:
                continue  # CPU plan
            if "id" not in t or not isinstance(t.get("price"), dict):
                raise ValueError(f"linode: GPU type lost its id or price block: {t.get('id')!r}")
            raw_name = gpu_name(t.get("label") or "")
            prices = [(None, t["price"].get("hourly"))]
            prices += [(rp.get("id"), (rp.get("hourly"))) for rp in t.get("region_prices") or []]
            for region, hourly in prices:
                price = to_decimal(hourly)
                if price is None or price <= 0:
                    continue  # no hourly price (a monthly-only figure is never converted)
                listing_id = t["id"] if region is None else f"{t['id']}:{region}"
                mem_mb, disk_mb = t.get("memory"), t.get("disk")
                listings.append(
                    ComputeListing(
                        provider="linode",
                        sku=t["id"],
                        listing_id=listing_id,
                        raw_gpu_name=raw_name,
                        canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                        gpu_count=count,
                        # The base price applies to every region not in region_prices.
                        region=region,
                        country=None,
                        # price.hourly is for the whole plan.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type="on_demand",
                        provider_tier="vm",
                        interruptible=False,
                        available=None,  # a plan catalogue, not stock
                        capacity=None,
                        capacity_unit=None,
                        vcpu=t.get("vcpus"),
                        ram_gb=None if mem_mb is None else mem_mb / 1024,
                        storage_gb=None if disk_mb is None else disk_mb / 1024,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
