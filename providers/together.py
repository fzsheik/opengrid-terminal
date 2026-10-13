"""Together AI GPU Clusters: the public price page, no API key.

The "GPU Clusters" section of together.ai/pricing has a table headed "On-demand
hourly rates and reserved capacity" that states "All prices are per GPU per
hour". Its columns are Preemptible Compute (pay as you go), ON-Demand (pay as
you go), then four Reserved terms (3-30, 31-90, 91-180, 181+ days).

Only the two pay-as-you-go columns are read: on-demand as on_demand, preemptible
as spot (interruptible). Reserved terms are commitments and are not read.
The page states no node size or region, so a listing is one GPU at the printed
per-GPU rate, the same convention as Nebius.

Everything else on the page (serverless and dedicated inference, fine-tuning,
sandboxes) is not GPU rental and is ignored.
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

BASE_URL = "https://www.together.ai"
ENDPOINT = "/pricing"

_SECTION = re.compile(r">GPU Clusters</h2>(?P<body>.*?)(?:<h2[ >]|$)", re.S)
_TABLE_TITLE = "On-demand hourly rates and reserved capacity"
_UNIT = "All prices are per GPU per hour."
_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(?P<c>.*?)</td>", re.S)
_TH = re.compile(r"<th[^>]*>(?P<c>.*?)</th>", re.S)
_TAGS = re.compile(r"<[^>]+>")
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)$")
EXPECTED_HEADER = ["Hardware", "Preemptible Compute", "ON-Demand", "Reserved"]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", fragment)).replace(" ", " ").strip()


def extract(html: str) -> dict:
    """The per-GPU price table of the GPU Clusters section, cells as printed.

    Raises when the section, the table, its per-GPU unit statement or its column
    layout is missing, so a reshaped page fails the poll instead of recording nothing.
    """
    sec = _SECTION.search(html)
    if sec is None:
        raise ValueError("together: 'GPU Clusters' section not found - page reshaped")
    body = sec["body"]
    at = body.find(_TABLE_TITLE)
    if at == -1:
        raise ValueError(f"together: '{_TABLE_TITLE}' table not found")
    if _UNIT not in _text(body[at:at + 2000]):
        raise ValueError("together: the table no longer says its prices are per GPU per hour")
    table = _TABLE.search(body, at)
    if table is None:
        raise ValueError("together: no table after its title")
    trs = _TR.findall(table["t"])
    header = [_text(c) for c in _TH.findall(trs[0])] if trs else []
    if header[:4] != EXPECTED_HEADER:
        raise ValueError(f"together: GPU table header changed: {header!r}")
    rows = []
    for tr in trs:
        cells = [_text(c) for c in _TD.findall(tr)]
        if len(cells) >= 3:
            rows.append({"hardware": cells[0], "preemptible": cells[1], "on_demand": cells[2]})
    if not rows:
        raise ValueError("together: GPU table has no rows - page reshaped")
    return {"unit": "per GPU per hour", "rows": rows}


def _price(cell: str) -> Decimal | None:
    """'$3.99' -> 3.99; a dash or 'Contact us' -> None. A digit in any other shape raises."""
    m = _PRICE.match(cell.strip())
    if m is None:
        if re.search(r"\d", cell):
            raise ValueError(f"together: price cell is not '$x.xx': {cell!r}")
        return None
    return Decimal(m.group(1).replace(",", ""))


class TogetherProvider(Provider):
    name = "together"
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
        """One row becomes an on-demand listing and a preemptible (spot) listing, where priced."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            name = r["hardware"]
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            for market, cell, suffix in (("on_demand", r["on_demand"], ""), ("spot", r["preemptible"], ":preemptible")):
                price = _price(cell)
                if price is None or price <= 0:
                    continue  # "Contact us" or a dash: no published price
                listings.append(
                    ComputeListing(
                        provider="together",
                        sku=name,
                        listing_id=slug + suffix,
                        raw_gpu_name=name,
                        canonical_gpu_name=canonical.canonical_gpu_name(name),
                        gpu_count=1,
                        region=None,
                        country=None,
                        price_per_gpu_hour=price,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type=market,
                        provider_tier="gpu-cluster",
                        interruptible=market == "spot",
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
