"""Jarvislabs: the public price page, no API key.

jarvislabs.ai/pricing is server-rendered with its On-Demand tab selected. The
desktop table has one row per GPU: name, VRAM, RAM, vCPU, a 1x/2x/4x/8x
configuration picker, and the price for the selected configuration, labelled
"On-demand · /hr". The section is headed "per GPU pricing", and the page renders
the 1x configuration selected, so the printed price is one GPU's hourly rate.

A row whose selected configuration is not 1x, or whose price is not labelled
on-demand, raises: the page has changed what it prints. Spot and reserved tabs
are filled in client-side and are not read.
"""

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

BASE_URL = "https://jarvislabs.ai"
ENDPOINT = "/pricing"

_PANEL = re.compile(r'<div role="tabpanel" id="plan-panel" aria-labelledby="plan-tab-on-demand">(?P<p>.*?)<div class="md:hidden">', re.S)
_ROW_START = re.compile(r'<div class="grid items-center px-7 py-5')
_NAME = re.compile(r"<h3[^>]*>(?P<v>[^<]+)</h3>")
_NUM_CELL = re.compile(r'<div class="text-right text-\[14px\][^"]*"[^>]*>(?P<v>\d+)(?:<span[^>]*>GB</span>)?</div>')
_PRESSED = re.compile(r'<button[^>]*aria-pressed="true"[^>]*>(?P<v>\d+)<!-- -->')
_PRICE_CELL = re.compile(r">\$(?P<v>\d+(?:,\d{3})*(?:\.\d+)?)</div></div><div[^>]*>(?P<label>[^<]*)</div>")
_TAGS = re.compile(r"<[^>]+>")


def extract(html: str) -> dict:
    """Every row of the On-Demand desktop table, as printed.

    Raises when the panel is missing, a row lost a field, the selected
    configuration is not 1x, or a price is not labelled on-demand.
    """
    if "per GPU pricing" not in html:
        raise ValueError("jarvislabs: the page no longer says its GPU prices are per GPU")
    panel = _PANEL.search(html)
    if panel is None:
        raise ValueError("jarvislabs: On-Demand price panel not found - page reshaped")
    body = panel["p"]
    starts = [m.start() for m in _ROW_START.finditer(body)] + [len(body)]
    rows = []
    for a, b in zip(starts, starts[1:]):
        row = body[a:b]
        name, pressed, price = _NAME.search(row), _PRESSED.search(row), _PRICE_CELL.search(row)
        nums = [m["v"] for m in _NUM_CELL.finditer(row)]
        if name is None or pressed is None or price is None or len(nums) < 3:
            raise ValueError("jarvislabs: a price row lost a field - page reshaped")
        if pressed["v"] != "1":
            raise ValueError(f"jarvislabs: {name['v']} renders the {pressed['v']}x price, not 1x")
        if not price["label"].startswith("On-demand"):
            raise ValueError(f"jarvislabs: {name['v']} price is labelled {price['label']!r}, not on-demand")
        rows.append({"gpu": name["v"].strip(), "vram_gb": nums[0], "ram_gb": nums[1], "vcpu": nums[2],
                     "config": "1x", "price": "$" + price["v"]})
    if not rows:
        raise ValueError("jarvislabs: no price rows found - page reshaped")
    return {"rows": rows}


def raw_name(gpu: str, vram: str) -> str:
    """The row name with its printed VRAM, unless the name already carries one:
    "NVIDIA H200 SXM" + 141 -> "NVIDIA H200 SXM 141GB"; "NVIDIA A100 80GB" stays."""
    return gpu if re.search(r"\d+\s?GB\b", gpu, re.I) else f"{gpu} {vram}GB"


class JarvislabsProvider(Provider):
    name = "jarvislabs"
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
        """One row becomes one single-GPU on-demand listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            price = Decimal(r["price"].lstrip("$").replace(",", ""))
            if price <= 0:
                continue
            name = raw_name(r["gpu"], r["vram_gb"])
            slug = re.sub(r"[^a-z0-9]+", "-", r["gpu"].lower()).strip("-")
            listings.append(
                ComputeListing(
                    provider="jarvislabs",
                    sku=f"{r['gpu']}:1x",
                    listing_id=f"{slug}:1x",
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=price,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=None,
                    interruptible=False,
                    available=None,  # the page says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=int(r["vcpu"]),
                    ram_gb=float(r["ram_gb"]),
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
