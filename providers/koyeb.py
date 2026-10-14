"""Koyeb: the public instance catalogue, no API key.

GET /v1/catalog/instances is anonymous and lists every instance type with
`price_hourly` for the WHOLE instance, `gpu` {count, name, memory} and the
regions it deploys in. Koyeb is a serverless platform billed per second; the
hourly figure is Koyeb's own (price_per_second x 3600, rounded).

Read: GPU instances with a positive hourly price, status AVAILABLE and at least
one region. RESTRICTED types (no regions; B200, 8x H100, ...) are sales-gated and
give no listing. Tenstorrent cards are listed at $0 (free preview): no listing.

A shape whose display name ("2x H100") disagrees with gpu.count is skipped,
never guessed: Koyeb's 4x Tenstorrent type says count 1. The GPU name is
gpu.name plus the per-GPU VRAM (gpu.memory / count), e.g. "gpu-nvidia-l40s 48GB".

Koyeb is a general-purpose (serverless) cloud, so it never counts toward neocloud indices.
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

BASE_URL = "https://app.koyeb.com"
ENDPOINT = "/v1/catalog/instances?limit=100"
_MULTI = re.compile(r"^(\d+)x\s")


def _gb(text) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(MB|GB)\s*", str(text or ""))
    if not m:
        return None
    return float(m.group(1)) / (1024 if m.group(2) == "MB" else 1)


class KoyebProvider(Provider):
    name = "koyeb"
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
        if not isinstance(body, dict) or not isinstance(body.get("instances"), list):
            return [raw]  # stored as-is; normalize() refuses it
        # Keep the GPU instances only, verbatim, plus how many the catalogue holds.
        return [reshaped(raw, {"count": body.get("count"), "listed": len(body["instances"]),
                               "instances": [i for i in body["instances"] if i.get("gpu")]})]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One available, priced GPU instance type becomes one listing."""
        rows = find_endpoint(by_endpoint, "/v1/catalog/instances")
        if not rows:
            return []
        snapshot = rows[0]
        body = snapshot.payload
        if not isinstance(body, dict) or not isinstance(body.get("instances"), list):
            raise ValueError("koyeb: catalogue has no `instances` list - response reshaped")
        if not isinstance(body.get("count"), int) or body["count"] > (body.get("listed") or 0):
            raise ValueError("koyeb: catalogue is paged beyond the first page - some types would be missed")
        listings = []
        for i in body["instances"]:
            gpu = i.get("gpu")
            if not gpu:
                continue
            if "id" not in i or "price_hourly" not in i or not isinstance(gpu.get("count"), int):
                raise ValueError(f"koyeb: GPU instance lost its id, price or count: {i.get('id')!r}")
            price = to_decimal(i["price_hourly"])
            regions = i.get("regions") or []
            if price is None or price <= 0 or i.get("status") != "AVAILABLE" or not regions:
                continue  # free preview, or sales-gated / not deployable
            count = gpu["count"]
            m = _MULTI.match(i.get("display_name") or "")
            if count < 1 or (int(m.group(1)) if m else 1) != count:
                log.warning("koyeb: %s says %r but gpu.count %r; skipped", i["id"], i.get("display_name"), count)
                continue
            # gpu.memory is the instance total ("160GB" for 2x); the name carries per-GPU VRAM.
            total = _gb(gpu.get("memory"))
            vram = None if total is None else total / count
            raw_name = (gpu.get("name") or "") + (f" {vram:g}GB" if vram else "")
            listings.append(
                ComputeListing(
                    provider="koyeb",
                    sku=i["id"],
                    listing_id=i["id"],
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=count,
                    region=",".join(regions)[:64],
                    country=None,
                    # price_hourly is for the whole instance.
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="serverless",
                    interruptible=False,
                    available=None,  # AVAILABLE is an offer status, not live stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=i.get("vcpu"),
                    ram_gb=_gb(i.get("memory")),
                    storage_gb=_gb(i.get("disk")),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
