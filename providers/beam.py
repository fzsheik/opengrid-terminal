"""Beam: the public price page, no API key.

beam.cloud/pricing is read as text (tags dropped; its CSS class names are
generated and change between builds). Its Serverless "GPU Tasks" table has one
row per GPU (name, interface, "24 GB VRAM", price "/sec"; the server-rendered
default, a "Per hour" toggle is client-side), grouped under "Start instantly" or
"With committed spend".

Read: the "Start instantly" group only (today the RTX 4090), the pay-as-you-go
rate. The committed-spend group (H100, RTX 5090) needs a contract: no listing.
Per second x 3600 gives the hourly rate, exactly in Decimal.

The serverless price is for the GPU ALONE: CPU and memory attached to a GPU are
billed on top at their own rates, so the listing carries provider_tier
"gpu-attachment" like GCP's attachable GPUs, and no CPU or memory is added.

Not read: the On-demand "Machine" rows, which are priced "from $x/hr" (a floor
over configurations, not an offer); Clusters ("Contact us"); sandboxes, VMs
without GPUs and CPU-only rates.

Beam pools capacity across other clouds, so it is classed general_cloud like Koyeb.
"""

import html as _html
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://www.beam.cloud"
ENDPOINT = "/pricing"

_DROP = re.compile(r"<!--.*?-->|<svg.*?</svg>|<script.*?</script>|<style.*?</style>", re.S)
_TAG = re.compile(r"<[^>]+>")
_START = "GPU Tasks"
_END = "CPU and memory · CPU-only workloads"
_PAYG, _COMMITTED = "Start instantly", "With committed spend"
_VRAM = re.compile(r"^(\d+) GB VRAM$")
_PRICE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
_PER_HOUR = {"/sec": Decimal(3600), "/hr": Decimal(1)}


def _tokens(html: str) -> list[str]:
    text = _TAG.sub("\n", _DROP.sub("", html))
    return [t for t in (_html.unescape(x).replace("\xa0", " ").strip() for x in text.split("\n")) if t]


def extract(html: str) -> dict:
    """Every serverless GPU row with its group (pay-as-you-go or committed), as printed.

    Raises when the table is missing or empty or a row is not "$x /sec" under a group.
    """
    toks = _tokens(html)
    if _START not in toks:
        raise ValueError("beam: serverless 'GPU Tasks' table not found - page reshaped")
    start = toks.index(_START)
    if _END not in toks[start:]:
        raise ValueError("beam: end of the 'GPU Tasks' table not found - page reshaped")
    table = toks[start + 1:toks.index(_END, start)]
    rows, group = [], None
    for i, t in enumerate(table):
        if t in (_PAYG, _COMMITTED):
            group = t
        if _VRAM.match(t) is None:
            continue
        # name, interface, "24 GB VRAM", "24 GB", "$0.000192", "/sec"
        if i < 2 or i + 3 >= len(table):
            raise ValueError(f"beam: serverless row cut short around {t!r}")
        name, iface, price, unit = table[i - 2], table[i - 1], table[i + 2], table[i + 3]
        if group is None or _PRICE.match(price) is None or unit not in _PER_HOUR:
            raise ValueError(f"beam: serverless row {name!r} is not '$x /sec' in a group: {table[i - 2:i + 4]!r}")
        rows.append({"gpu": name, "interface": iface, "vram": t, "price": price, "unit": unit, "group": group})
    if not rows:
        raise ValueError("beam: no serverless GPU rows - page reshaped")
    return {"serverless": rows}


class BeamProvider(Provider):
    name = "beam"
    # One page load per poll.
    # Slow tier: a scraped pricing page that changes by announcement; hourly (a quarter of the old footprint).
    polling = PollingPolicy(interval_seconds=3600, timeout_seconds=45, tier="slow")

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
        """A pay-as-you-go serverless GPU becomes one single-GPU, GPU-only listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["serverless"]:
            if r["group"] != _PAYG:
                continue  # committed spend only
            price = Decimal(_PRICE.match(r["price"]).group(1)) * _PER_HOUR[r["unit"]]
            if price <= 0:
                continue
            # "RTX 4090", "PCIE", "24 GB VRAM" -> "RTX 4090 PCIE 24GB"
            raw_name = f"{r['gpu']} {r['interface']} {_VRAM.match(r['vram']).group(1)}GB"
            slug = re.sub(r"[^a-z0-9]+", "-", f"{r['gpu']} {r['interface']}".lower()).strip("-")
            listings.append(
                ComputeListing(
                    provider="beam",
                    sku=r["gpu"],
                    listing_id=f"serverless:{slug}",
                    raw_gpu_name=raw_name,
                    canonical_gpu_name=canonical.canonical_gpu_name(raw_name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=price,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    # The GPU alone; attached CPU and memory are billed on top.
                    provider_tier="gpu-attachment",
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
