"""Akamai Cloud (Linode): the public plan catalogue, no API key.

GET /v4/linode/types is anonymous (Linode's API docs mark it as needing no
token). Each type carries `gpus` (GPU count), `price.hourly` for the WHOLE plan
and `region_prices` (regions billed differently from the base price).

Only types with gpus > 0 are read. The GPU model is not a field: it is in the
plan label ("Dedicated 32GB + RTX6000 GPU x1", "RTX4000 Ada x2 Medium"), so the
normalizer pulls the "RTX..." token out of it; a GPU label with no such token
keeps the whole label as its name and stays unmapped.

Capacity: GET /v4/regions/availability (also anonymous, paged at most 500 rows) says, per region
and plan, whether the plan can be launched there now (`available` true/false). Only GPU plans are
kept. A boolean, so it fills in_stock and nothing else.

Akamai/Linode is a general-purpose cloud, so it never counts toward neocloud indices.
"""

import logging
import re
from typing import TYPE_CHECKING

import httpx

import canonical
from models import CapacityObservation, ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://api.linode.com"
ENDPOINT = "/v4/linode/types?page_size=500"
AVAILABILITY = "/v4/regions/availability?page_size=500"
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
            return [raw, await self._availability(None)]  # stored as-is; normalize() refuses it
        # Keep the GPU types only, verbatim (plus the paging fields): the rest are CPU plans.
        gpu_types = [t for t in body["data"] if t.get("gpus")]
        types = reshaped(raw, {**{k: v for k, v in body.items() if k != "data"}, "data": gpu_types})
        return [types, await self._availability({t.get("id") for t in gpu_types})]

    async def _availability(self, gpu_plans: set | None) -> RawResponse:
        """Every page of /regions/availability as one response, GPU plans only. A page that fails
        returns that failure, so a partial set is never stored as a complete one."""
        rows, page, pages, first = [], 1, 1, None
        while page <= pages:
            resp = await self.get(f"{AVAILABILITY}&page={page}")
            body = resp.payload
            if not resp.ok or not isinstance(body, dict) or not isinstance(body.get("data"), list):
                return resp
            first = first or resp
            pages = body.get("pages") or 1
            # Without the type list, fall back to the plan id naming GPU plans ("g1-gpu-rtx6000-1").
            rows += [r for r in body["data"] if (r.get("plan") in gpu_plans if gpu_plans else "gpu" in str(r.get("plan")))]
            page += 1
        return reshaped(first, {"pages": pages, "data": rows})

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

    @staticmethod
    def normalize_capacity(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[CapacityObservation]:
        """One (GPU plan, region) availability boolean becomes one observation."""
        types, avail = find_endpoint(by_endpoint, "/v4/linode/types"), find_endpoint(by_endpoint, "/v4/regions/availability")
        if not types or not avail:
            return []
        body = avail[0].payload
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError("linode: /regions/availability has no `data` list - response reshaped")
        plans = {t.get("id"): t for t in (types[0].payload or {}).get("data") or [] if t.get("gpus")}
        out = []
        for r in body["data"]:
            t = plans.get(r.get("plan"))
            if t is None or not r.get("region"):
                continue  # a CPU plan, or a row missing its region
            raw_name = gpu_name(t.get("label") or "")
            available = r.get("available")
            out.append(CapacityObservation(
                provider="linode", sku=t["id"], region=r["region"], raw_gpu_name=raw_name,
                canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=t["gpus"],
                in_stock=available if isinstance(available, bool) else None,
                raw_value=str(available).lower(), source=AVAILABILITY, observed_at=avail[0].fetched_at))
        return out
