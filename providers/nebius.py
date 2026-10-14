"""Nebius: the public price page, no API key.

The page is a Next.js build. Its __NEXT_DATA__ script holds the page content as
a JSON string of blocks; the GPU prices are the "highlight-table-block" titled
"NVIDIA GPU Instances", one price per GPU-hour. A sibling block titled
"Preemptible NVIDIA GPU Instances" has spot prices and is never read. When a
price change is announced the table has gained a second column headed
"GPU-hour (Effective <date>)". Which column is current depends on the date, so
every price column is stored and normalize() picks by the fetch time.
"""

import json
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider, find_endpoint, get_page

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://nebius.com"
ENDPOINT = "/prices"

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_GPU_TABLE = "NVIDIA GPU Instances"
_LEAD = ["Item", "vCPUs", "RAM, GB"]
_PRICE = re.compile(r"^(from )?\$(\d+(?:,\d{3})*(?:\.\d+)?)$")
_EFFECTIVE = re.compile(r"Effective ([A-Z][a-z]+ \d{1,2}, \d{4})")
# Words that mark a column as something other than the on-demand rate.
_NOT_ON_DEMAND = re.compile(r"spot|preemptible|reserv|commit|month|year", re.I)


def _blocks(node, out: list) -> list:
    """Every highlight-table-block, including inside JSON-encoded strings."""
    if isinstance(node, dict):
        if node.get("type") == "highlight-table-block":
            out.append(node)
        for v in node.values():
            _blocks(v, out)
    elif isinstance(node, list):
        for v in node:
            _blocks(v, out)
    elif isinstance(node, str) and '"highlight-table-block"' in node:
        try:
            _blocks(json.loads(node), out)
        except ValueError:
            pass  # a string that merely mentions the block type
    return out


def _price_columns(header: list[str]) -> list[int]:
    """Indexes of the on-demand GPU-hour columns; raises on anything else."""
    cols = list(range(len(_LEAD), len(header)))
    if header[: len(_LEAD)] != _LEAD or not cols:
        raise ValueError(f"nebius: GPU table header changed: {header!r}")
    for i in cols:
        c = header[i]
        if _NOT_ON_DEMAND.search(c) or not ("GPU-hour" in c or "On-demand" in c):
            raise ValueError(f"nebius: unexpected GPU table column: {c!r}")
    if sum(1 for i in cols if not _EFFECTIVE.search(header[i])) != 1:
        raise ValueError(f"nebius: expected one undated price column: {header!r}")
    return cols


def extract(html: str) -> dict:
    """The on-demand GPU price table, as the page prints it.

    Raises when the table is missing or ambiguous, so a reshaped page fails the
    poll instead of recording nothing.
    """
    m = _NEXT_DATA.search(html)
    if m is None:
        raise ValueError("nebius: page no longer embeds __NEXT_DATA__")
    blocks = [b for b in _blocks(json.loads(m.group(1)), []) if (b.get("title") or "").strip() == _GPU_TABLE]
    # The page content can appear more than once; identical copies are one table.
    tables = []
    for b in blocks:
        content = (b.get("table") or {}).get("content")
        if not isinstance(content, list) or not content:
            raise ValueError("nebius: GPU table block has no content")
        if content not in tables:
            tables.append(content)
    if len(tables) != 1:
        raise ValueError(f"nebius: expected one on-demand GPU table, found {len(tables)}")
    header, rows = [str(c).strip() for c in tables[0][0]], tables[0][1:]
    _price_columns(header)
    if not rows or any(len(r) != len(header) or not all(isinstance(c, str) for c in r) for r in rows):
        raise ValueError("nebius: GPU table rows no longer match its header")
    return {"header": header, "rows": rows}


def _price(cell: str) -> tuple[Decimal, bool] | None:
    """(price, is_from_floor) for '$3.85' or 'from $1.55'; None when unpriced."""
    m = _PRICE.match(cell.strip())
    if m is None:
        if re.search(r"\d", cell):
            raise ValueError(f"nebius: price cell is not '$x.xx': {cell!r}")
        return None  # 'Contact us' and dashes
    return Decimal(m.group(2).replace(",", "")), bool(m.group(1))


class NebiusProvider(Provider):
    name = "nebius"
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
        """One table row becomes one single-GPU listing (prices are per GPU)."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]
        header, table = snapshot.payload["header"], snapshot.payload["rows"]

        cols = _price_columns(header)
        col = next(i for i in cols if not _EFFECTIVE.search(header[i]))
        # A dated column replaces the plain one once its date has arrived.
        for i in cols:
            m = _EFFECTIVE.search(header[i])
            if m and datetime.strptime(m.group(1), "%B %d, %Y").date() <= snapshot.fetched_at.date():
                col = i

        listings = []
        for row in table:
            item = row[0].strip()
            parsed = _price(row[col])
            if parsed is None:
                continue  # published without a price
            per_gpu, floor = parsed
            vcpu = row[1].strip()
            ram = row[2].strip()
            listings.append(
                ComputeListing(
                    provider="nebius",
                    sku=item,
                    listing_id=item,
                    raw_gpu_name=item,
                    canonical_gpu_name=canonical.canonical_gpu_name(item),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=per_gpu,
                    price_per_instance_hour=per_gpu,
                    currency="USD",
                    market_type="on_demand",
                    # 'from $x' is a floor over several configurations, not a quote.
                    provider_tier="from_price" if floor else None,
                    interruptible=False,
                    available=None,
                    capacity=None,
                    capacity_unit=None,
                    # Per-GPU resource share; a range ('8-40') has no single value.
                    vcpu=int(vcpu) if vcpu.isdigit() else None,
                    ram_gb=float(ram) if ram.replace(".", "", 1).isdigit() else None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
