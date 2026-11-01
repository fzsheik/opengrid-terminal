"""Arkane Cloud: the public price page, no API key.

arkanecloud.com/pricing prints, under "Cloud GPU pricing (per hour, billed per
second)", one table row per GPU: GPU type, "Price per GPU" ("$3.99/hr"), CPU,
RAM and VRAM. Each cell carries a data-row-title label, which is what is read,
so a reordered table cannot shift a number into the wrong field. The page states
"Prices in USD."; that line is required.

Each row becomes one 1-GPU listing at the per-GPU price. The page's FAQ says
instances scale from 1 to 8 GPUs at that per-GPU rate, but it does not say which
counts each GPU comes in, so larger shapes are not invented.

Arkane is a French GPU cloud; its FAQ says its own B300 and RTX PRO 6000 servers
run in Paris and Lyon. No other cloud is named as the source of the rest.

Not read: the Serverless GPU table (scale-to-zero endpoints; one of its rows
prints an "H200" with 80 GB), the per-token Models API and image prices, CPU
instances, storage, and reserved clusters (quoted per contract).
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

BASE_URL = "https://arkanecloud.com"
ENDPOINT = "/pricing/"

_SECTION = re.compile(r"Cloud GPU pricing \(per hour, billed per second\)(?P<body>.*?)Serverless GPU pricing", re.S)
_ROW = re.compile(r'<tr\s+data-column-title="(?P<gpu>[^"]+)"\s*>(?P<cells>.*?)</tr>', re.S)
_CELL = re.compile(r'<td data-row-title="(?P<label>[^"]+)">(?P<v>.*?)</td>', re.S)
_PRICE = re.compile(r"^\$(\d+(?:\.\d+)?)/hr$")
_GB = re.compile(r"^(\d+(?:\.\d+)?)\s*GB$")


def _clean(cell: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()


def extract(html: str) -> dict:
    """Every on-demand Cloud GPU row, by its labelled cells.

    Raises when the section, the USD statement or a row's price is missing.
    """
    if "Prices in USD." not in html:
        raise ValueError("arkane: 'Prices in USD.' is gone - currency no longer stated")
    sec = _SECTION.search(html)
    if sec is None:
        raise ValueError("arkane: 'Cloud GPU pricing' section not found - page reshaped")
    rows = []
    for m in _ROW.finditer(sec["body"]):
        cells = {c["label"].strip(): _clean(c["v"]) for c in _CELL.finditer(m["cells"])}
        if "Price per GPU" not in cells:
            raise ValueError(f"arkane: row {m['gpu']!r} has no 'Price per GPU' cell - page reshaped")
        rows.append({"gpu": _html.unescape(m["gpu"]).strip(), "price": cells["Price per GPU"],
                     "cpu": cells.get("CPU"), "ram": cells.get("RAM"), "vram": cells.get("VRAM")})
    if not rows:
        raise ValueError("arkane: no GPU rows found - page reshaped")
    return {"rows": rows}


def raw_name(gpu: str, vram: str | None) -> str:
    """The row's GPU type with its printed VRAM: "NVIDIA B300 288GB"."""
    m = _GB.match(vram or "")
    return f"{gpu} {m.group(1)}GB" if m else gpu


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class ArkaneProvider(Provider):
    name = "arkane"
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
        """One priced row becomes one 1-GPU listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            m = _PRICE.match(r["price"])
            if m is None:
                log.warning("arkane: %s price %r is not '$x.xx/hr'; skipped", r["gpu"], r["price"])
                continue
            price = Decimal(m.group(1))
            if price <= 0:
                continue
            name = raw_name(r["gpu"], r["vram"])
            ram = _GB.match(r["ram"] or "")
            cpu = r["cpu"] or ""
            listings.append(
                ComputeListing(
                    provider="arkane",
                    sku=r["gpu"],
                    listing_id=f"{_slug(name)}:1x",
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=1,
                    region=None,
                    country=None,  # only the B300 / RTX PRO 6000 are said to be in France
                    price_per_gpu_hour=price,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="vm",
                    interruptible=False,
                    available=None,  # the page says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=int(cpu) if cpu.isdigit() else None,
                    ram_gb=float(ram.group(1)) if ram else None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
