"""Massed Compute: the public on-demand price page, no API key.

Each GPU model is a heading followed by a table of shapes (quantity, vCPU, RAM,
storage, instance price). Prices are per instance, so the per-GPU rate is
derived.
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

BASE_URL = "https://vm.massedcompute.com"
ENDPOINT = "/pricing"

# A model heading, or a table; document order ties each table to its heading.
_BLOCK = re.compile(
    r'sm:text-\[15px\]">(?P<model>[^<]+)</span>|(?P<table><table.*?</table>)', re.S
)
_TAGS = re.compile(r"<[^>]+>")
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)\s*/hr$")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", html)).strip()


def extract(html: str) -> dict:
    """Every priced shape on the page, grouped under its model heading.

    Raises when no priced shape is found or a table has no heading, so a
    redesign fails the poll instead of recording nothing.
    """
    model, shapes = None, []
    for m in _BLOCK.finditer(html):
        if m["model"]:
            model = _text(m["model"])
            continue
        header = [_text(c) for c in re.findall(r"<th.*?</th>", m["table"], re.S)]
        if header[:5] != ["Qty", "vCPU", "RAM", "Storage", "Price"]:
            continue
        if model is None:
            raise ValueError("massedcompute: price table with no model heading before it")
        for tr in re.findall(r"<tr.*?</tr>", m["table"], re.S)[1:]:
            cells = [_text(c) for c in re.findall(r"<td.*?</td>", tr, re.S)]
            if len(cells) < 5:
                continue
            shapes.append(dict(zip(["model", "qty", "vcpu", "ram", "storage", "price"], [model, *cells[:5]])))
    if not shapes:
        raise ValueError("massedcompute: no priced shapes found - page reshaped")
    return {"shapes": shapes}


def _number(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


class MassedComputeProvider(Provider):
    name = "massedcompute"
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
        """One table row becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for s in snapshot.payload["shapes"]:
            qty = re.sub(r"\D", "", s["qty"])
            price = _PRICE.match(s["price"])
            if not qty or int(qty) == 0 or price is None:
                continue
            count = int(qty)
            per_instance = Decimal(price.group(1).replace(",", ""))
            vcpu, ram, storage = _number(s["vcpu"]), _number(s["ram"]), _number(s["storage"])
            listings.append(
                ComputeListing(
                    provider="massedcompute",
                    sku=f"{s['model']}:{count}x",
                    listing_id=f"{s['model']}:{count}x",
                    raw_gpu_name=s["model"],
                    canonical_gpu_name=canonical.canonical_gpu_name(s["model"]),
                    gpu_count=count,
                    region=None,
                    country=None,
                    price_per_gpu_hour=per_instance / count,
                    price_per_instance_hour=per_instance,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier=None,
                    interruptible=False,
                    available=None,
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None if vcpu is None else int(vcpu),
                    ram_gb=ram,
                    storage_gb=storage,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
