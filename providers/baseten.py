"""Baseten: the public price page, no API key.

baseten.co/pricing is a Next.js page whose price tables travel in its embedded
React Server Components stream (self.__next_f.push). The "Dedicated Deployments"
table is an InstancePriceTableRecord whose instanceList gives, per instance:
name ("H100"), instanceType ("gpu" | "cpu"), specs ("80 GiB VRAM"),
pricePerMinute and pricePerHour, all in USD.

Read: the GPU instances of that table, at Baseten's own pricePerHour (billing is
per minute; the hourly figure is checked against pricePerMinute x 60 to within a
cent, and a row that disagrees is skipped). The "Training" table repeats the
same instances and prices and is not read again. Model API (per-token) prices
are not GPU rental and are ignored.

The price is for the smallest instance carrying one such GPU, CPUs and memory
included (Baseten's instance reference lists e.g. H100 at 16 vCPU, 118 GiB), but
the pricing page prints no vCPU or RAM, so those stay None.

Skipped: "H100 MIG" (a fractional H100, 40 GiB: not a whole card). The raw name
is "NVIDIA <name> <VRAM>GB" from the printed specs; "A100 80GB" and "H100 80GB"
state no SXM or PCIe and stay unmapped.

Baseten runs on other clouds' capacity, so it is classed general_cloud like Koyeb.
"""

import json
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page, to_decimal

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://www.baseten.co"
ENDPOINT = "/pricing/"

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_HEADING = '"heading":"Dedicated Deployments"'
_VRAM = re.compile(r"^(\d+)\s*GiB\s+(?:VRAM|VM)$")
_FIELDS = ("name", "instanceType", "specs", "pricePerMinute", "pricePerHour")


def extract(html: str) -> dict:
    """The Dedicated Deployments instance list, each instance's fields as published.

    Raises when the stream, the table or an instance's fields are missing.
    """
    stream = "".join(json.loads(f'"{m.group(1)}"') for m in _PUSH.finditer(html))
    if not stream:
        raise ValueError("baseten: no Next.js data stream in the page - page reshaped")
    at = stream.find(_HEADING)
    if at == -1:
        raise ValueError("baseten: 'Dedicated Deployments' price table not found - page reshaped")
    start = stream.find('"instanceList":', at)
    if start == -1:
        raise ValueError("baseten: price table has no instanceList")
    # Numbers are kept as their printed strings, so prices stay exact.
    decoder = json.JSONDecoder(parse_float=str, parse_int=str)
    instances, _ = decoder.raw_decode(stream, start + len('"instanceList":'))
    if not isinstance(instances, list) or not instances:
        raise ValueError("baseten: price table's instanceList is empty - page reshaped")
    rows = []
    for i in instances:
        if not isinstance(i, dict) or any(k not in i for k in _FIELDS):
            raise ValueError(f"baseten: instance lost one of {_FIELDS}: {i!r}"[:300])
        rows.append({k: i[k] for k in _FIELDS})
    return {"instances": rows}


class BasetenProvider(Provider):
    name = "baseten"
    # One page load per poll.
    polling = PollingPolicy(interval_seconds=900, timeout_seconds=45)

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__(
            client
            or httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"user-agent": "opengrid-terminal/0.1"},
                timeout=45,
                follow_redirects=True,
            )
        )

    async def fetch(self) -> list[RawResponse]:
        return [await get_page(self, ENDPOINT, extract)]

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One whole-GPU instance becomes one single-GPU listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for i in snapshot.payload["instances"]:
            if i["instanceType"] != "gpu":
                continue
            name = str(i["name"]).strip()
            if "MIG" in name.upper():
                continue  # a fraction of one card, not a GPU
            vram = _VRAM.match(str(i["specs"]).strip())
            if vram is None:
                log.warning("baseten: %s specs %r give no VRAM; skipped", name, i["specs"])
                continue
            hourly, per_minute = to_decimal(i["pricePerHour"]), to_decimal(i["pricePerMinute"])
            if hourly is None or per_minute is None or hourly <= 0:
                continue
            if abs(per_minute * 60 - hourly) > Decimal("0.01"):
                log.warning("baseten: %s per hour %s disagrees with per minute %s; skipped", name, hourly, per_minute)
                continue
            raw_name = f"NVIDIA {name} {vram.group(1)}GB"
            listings.append(
                ComputeListing(
                    provider="baseten",
                    sku=name,
                    listing_id=re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=hourly,
                    # The smallest instance with this GPU, CPUs and memory included.
                    price_per_instance_hour=hourly,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="serverless",
                    interruptible=False,
                    available=None,  # the page says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None,
                    ram_gb=None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
