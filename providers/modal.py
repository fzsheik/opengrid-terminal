"""Modal: the public price page, no API key.

modal.com/pricing has a "Resource costs" block whose "GPU Tasks" list prints one
row per GPU with a price "/ sec" (the server-rendered default; a "Per hour"
toggle is client-side). The exact figure sits in a screen-reader span next to
the animated digits, e.g. "0.001097" for the H100. Per second x 3600 gives the
hourly rate, exactly in Decimal.

The GPU price is for the GPU ALONE: Modal bills CPU cores and memory on top
(their own rows, with a 0.125-core minimum per container), so a listing is the
GPU's own rate and carries provider_tier "gpu-attachment" like GCP's attachable
GPUs. No CPU or memory is added: the amount depends on the container.

Modal Functions are preemptible by default (the page says so), so every listing
is interruptible. Sandboxes are billed at 3x CPU/memory list price, not read.
Region selection multiplies prices; the list price (no region pinned) is read.
A GPU type runs 1 to 8 per container at the same per-GPU rate, so a listing is
one GPU, the Together/Nebius convention.

Modal's GPU guide states every H100 and H200 on the platform is SXM and the H200
has 141 GB. A100 rows give VRAM but not SXM or PCIe, "RTX PRO 6000" no edition
and "A10" may be the A10G: those stay unmapped.

Modal pools capacity across other clouds, so it is classed general_cloud like
Koyeb and never counts toward neocloud indices.
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

BASE_URL = "https://modal.com"
ENDPOINT = "/pricing"

_GPU_LIST = re.compile(r">\s*GPU Tasks</span>.*?<ul[^>]*>(?P<ul>.*?)</ul>", re.S)
_LI = re.compile(r"<li[^>]*>(?P<li>.*?)</li>", re.S)
_NAME = re.compile(r"^\s*<span[^>]*>(?P<name>[^<]+)</span>")
_EXACT = re.compile(r'<span class="sr-only">(?P<v>[^<]*)</span>')
_UNIT = re.compile(r"<span[^>]*>(?P<u>/ [^<]+)</span>")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
_PER_HOUR = {"/ sec": Decimal(3600), "/ hr": Decimal(1), "/ hour": Decimal(1)}


def extract(html: str) -> dict:
    """Every row of the GPU Tasks list: name, exact price and its unit, as printed.

    Raises when the list is missing or empty, or a row's price or unit is not
    one we read, so a reshaped page fails the poll instead of recording nothing.
    """
    m = _GPU_LIST.search(html)
    if m is None:
        raise ValueError("modal: 'GPU Tasks' price list not found - page reshaped")
    rows = []
    for li in _LI.finditer(m["ul"]):
        body = li["li"]
        name, exact, units = _NAME.search(body), _EXACT.search(body), _UNIT.findall(body)
        unit = units[-1] if units else None
        if name is None or exact is None or unit is None:
            raise ValueError(f"modal: GPU row lost its name, price or unit: {body[:120]!r}")
        price, per = exact["v"].strip(), _html.unescape(unit).strip()
        if not _NUMBER.match(price) or per not in _PER_HOUR:
            raise ValueError(f"modal: GPU row price {price!r} {per!r} is not a number per sec/hr")
        rows.append({"gpu": _html.unescape(name["name"]).strip(), "price": price, "unit": per})
    if not rows:
        raise ValueError("modal: GPU Tasks list has no rows - page reshaped")
    return {"rows": rows}


class ModalProvider(Provider):
    name = "modal"
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
        """One GPU row becomes one single-GPU, GPU-only listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            hourly = Decimal(r["price"]) * _PER_HOUR[r["unit"]]
            if hourly <= 0:
                continue
            name = r["gpu"]
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            listings.append(
                ComputeListing(
                    provider="modal",
                    sku=name,
                    listing_id=slug,
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=hourly,
                    price_per_instance_hour=hourly,
                    currency="USD",
                    market_type="on_demand",
                    # The GPU alone; CPU and memory are billed on top.
                    provider_tier="gpu-attachment",
                    interruptible=True,  # Modal Functions are preemptible by default
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
