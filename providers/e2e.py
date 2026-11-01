"""E2E Networks: the published price feed, no API key.

e2enetworks.com/pricing.json is E2E's own machine-readable rate card (the site
points agents at it from robots-allowed pages and llms.txt). Each entry of
`gpuInstances` is one GPU model with its VRAM, the vCPU/RAM of the 1x
configuration, `unit: "per hour"` and `pricing.usd.hourly` / `pricing.inr.hourly`.
The feed's notes say USD is a separately published price list, not an INR
conversion, and the pricing page says "On-demand prices are per GPU-hour", so a
listing is one GPU at the USD hourly rate (the same convention as Together).

Read: the USD on-demand rate only. Skipped: the committed (3+ month) and spot
rates, which the feed publishes in INR only (no FX here), and the monthly and
annual terms, which are "Contact Sales". A row whose unit is not "per hour"
fails the poll; a row with no USD price is skipped with a warning.

The data centres are in India (Delhi NCR, Chennai), but the feed does not say
which GPU is where, so region and country are left empty. E2E sells a full
cloud (CPU VMs, databases, Kubernetes), so it is a general-purpose cloud.
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

BASE_URL = "https://www.e2enetworks.com"
ENDPOINT = "/pricing.json"


class E2EProvider(Provider):
    name = "e2e"
    # One anonymous request per poll; the feed is cached for an hour at the edge.
    polling = PollingPolicy(interval_seconds=1800, timeout_seconds=30)

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
        if not isinstance(body, dict) or not isinstance(body.get("gpuInstances"), list):
            return [raw]  # stored as-is; normalize() refuses it
        # Keep the GPU rows verbatim; CPU plans and storage are not GPU rental.
        return [reshaped(raw, {k: body.get(k) for k in ("generatedAt", "notes", "gpuInstances")})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One GPU row with a USD hourly price becomes one single-GPU on-demand listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("gpuInstances"), list) or not body["gpuInstances"]:
            raise ValueError("e2e: feed has no `gpuInstances` list - response reshaped")

        listings = []
        for g in body["gpuInstances"]:
            if not isinstance(g, dict) or not g.get("id") or not g.get("model") or "pricing" not in g:
                raise ValueError(f"e2e: GPU row lost its id, model or pricing: {g!r}"[:300])
            if g.get("unit") != "per hour":
                raise ValueError(f"e2e: {g['id']} is priced {g.get('unit')!r}, not 'per hour'")
            price = to_decimal(((g["pricing"] or {}).get("usd") or {}).get("hourly"))
            if price is None or price <= 0:
                log.warning("e2e: %s has no USD hourly price; skipped", g["id"])
                continue
            vram = g.get("vramGB")
            raw_name = g["model"] + (f" {vram}GB" if isinstance(vram, int) and vram > 0 else "")
            listings.append(
                ComputeListing(
                    provider="e2e",
                    sku=g["id"],
                    listing_id=f"{g['id']}:1x",
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    # "On-demand prices are per GPU-hour": the 1x instance is one GPU.
                    price_per_gpu_hour=price,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="vm",
                    interruptible=False,
                    available=None,  # a rate card, not stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=g.get("vcpus") if isinstance(g.get("vcpus"), int) else None,
                    ram_gb=float(g["ramGB"]) if isinstance(g.get("ramGB"), (int, float)) else None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings

