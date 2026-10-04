"""Vast.ai: a marketplace of third-party host offers, public JSON, no API key.

Offers are queried one GPU model at a time. A model can have hundreds of
offers at wildly different prices, and the cheapest is often one unverified
host, so each model becomes two listings: the median ask (the typical price)
and the cheapest ask (what a router would actually grab).
"""

import asyncio
import json
import logging
import statistics
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, reshaped

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://console.vast.ai"
ENDPOINT = "/api/v0/bundles/"

# Vast's own gpu_name values, each checked against the live API.
GPU_NAMES = [
    "H100 SXM", "H100 PCIE", "H100 NVL", "H200", "H200 NVL", "B200",
    "A100 SXM4", "A100 PCIE", "L40S", "A40", "RTX A6000", "RTX 6000Ada",
    "RTX PRO 6000 WS", "RTX PRO 6000 S",
    "RTX 5090", "RTX 5080", "RTX 4090", "RTX 3090",
]  # fmt: skip

LIMIT = 300
# Vast's documented limit is 1 request/second.
RATE_PAUSE_SECONDS = 1.2
# The API returns at most this many offers per query however large the limit,
# so a book this size is cut off and its counts are lower bounds.
API_CAP = 64


def _query(gpu_name: str) -> str:
    return json.dumps(
        {
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "type": "on-demand",
            "gpu_name": {"eq": gpu_name},
            "order": [["dph_total", "asc"]],
            "limit": LIMIT,
        }
    )


def _keep(offer: dict) -> dict:
    """The fields normalize() reads, verbatim."""
    return {
        k: offer.get(k)
        for k in (
            "id", "machine_id", "host_id", "gpu_name", "num_gpus", "dph_total",
            "gpu_ram", "geolocation", "reliability", "cpu_cores_effective",
            "cpu_ram", "disk_space",
        )
    }  # fmt: skip


class VastProvider(Provider):
    name = "vast"
    # One request per GPU model, paced to the 1/second limit: about 25 seconds.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=120)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"accept": "application/json", "user-agent": "opengrid-terminal/0.1"},
                timeout=40,
            )
        )

    async def _one(self, gpu_name: str) -> RawResponse:
        raw = await self.get(ENDPOINT, params={"q": _query(gpu_name)})
        offers = (raw.payload or {}).get("offers") if isinstance(raw.payload, dict) else None
        payload = None if offers is None else [_keep(o) for o in offers]
        # The model asked for goes in `request`: the body does not repeat it
        # when the book is empty.
        return reshaped(raw, payload, request={"gpu_name": gpu_name})

    async def fetch(self) -> list[RawResponse]:
        # The API allows one request per second and answers 429 beyond it, so
        # go one model at a time, with one retry after the stated wait.
        responses = []
        for name in GPU_NAMES:
            raw = await self._one(name)
            if raw.status_code == 429:
                await asyncio.sleep(2)
                raw = await self._one(name)
            responses.append(raw)
            await asyncio.sleep(RATE_PAUSE_SECONDS)
        return responses

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        listings = []
        for snapshot in find_endpoint(by_endpoint, ENDPOINT):
            gpu_name = (snapshot.request or {}).get("gpu_name")
            offers = [
                o
                for o in snapshot.payload or []
                if o.get("dph_total") and (o.get("num_gpus") or 0) > 0
            ]
            if not gpu_name or not offers:
                continue

            # Per-GPU ask for every offer, cheapest first.
            asks = sorted(
                Decimal(str(o["dph_total"])) / o["num_gpus"] for o in offers
            )
            cheapest = min(offers, key=lambda o: o["dph_total"] / o["num_gpus"])
            median = Decimal(str(statistics.median(asks)))
            total_gpus = sum(o["num_gpus"] for o in offers)
            # A capped book undercounts supply, so say nothing rather than a low number.
            capped = len(offers) >= API_CAP

            def listing(kind: str, per_gpu: Decimal, **extra) -> ComputeListing:
                return ComputeListing(
                    provider="vast",
                    sku=gpu_name,
                    listing_id=f"{gpu_name}:{kind}",
                    raw_gpu_name=gpu_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(gpu_name),
                    gpu_count=1,
                    region=extra.get("region"),
                    country=None,
                    price_per_gpu_hour=per_gpu,
                    price_per_instance_hour=per_gpu,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=kind,
                    interruptible=False,
                    available=True,
                    capacity=None if capped else total_gpus,
                    capacity_unit=None if capped else "gpu",
                    vcpu=extra.get("vcpu"),
                    ram_gb=extra.get("ram_gb"),
                    storage_gb=extra.get("storage_gb"),
                    observed_at=snapshot.fetched_at,
                )

            listings.append(listing("median", median))
            listings.append(
                listing(
                    "cheapest",
                    asks[0],
                    region=cheapest.get("geolocation"),
                    vcpu=None if not cheapest.get("cpu_cores_effective") else int(cheapest["cpu_cores_effective"]),
                    ram_gb=None if not cheapest.get("cpu_ram") else round(cheapest["cpu_ram"] / 1024, 1),
                    storage_gb=None if not cheapest.get("disk_space") else round(cheapest["disk_space"], 1),
                )
            )
        return listings
