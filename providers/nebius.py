"""Nebius: the public price page, no API key.

The page embeds its GPU price table as escaped JSON. It prints a price per
GPU-hour, and when a price change is announced it adds a second column headed
"GPU-hour (Effective <date>)". Which column is current depends on the date, so
both are stored and normalize() picks by the fetch time.
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

_TABLE_START = '"table":{"content":[["Item","vCPUs","RAM, GB"'
_TABLE_END = ']],"customColumnWidth"'
_PRICE = re.compile(r"^(from )?\$(\d+(?:,\d{3})*(?:\.\d+)?)$")
_EFFECTIVE = re.compile(r"Effective ([A-Z][a-z]+ \d{1,2}, \d{4})")


def extract(html: str) -> dict:
    """The GPU price table, as the page prints it.

    Raises when the table is missing or ambiguous, so a reshaped page fails the
    poll instead of recording nothing.
    """
    text = html.replace("\\", "")
    tables = []
    pos = text.find(_TABLE_START)
    while pos != -1:
        start = pos + len('"table":{"content":')
        end = text.find(_TABLE_END, pos)
        if end == -1:
            raise ValueError("nebius: price table no longer terminates as expected")
        tables.append(json.loads(text[start : end + 2]))
        pos = text.find(_TABLE_START, end)

    # The preemptible table shares its first columns; the on-demand one is ours.
    ours = [t for t in tables if any("On-demand" in c for c in t[0])]
    if len(ours) != 1:
        raise ValueError(f"nebius: expected one on-demand GPU table, found {len(ours)}")
    header, rows = ours[0][0], ours[0][1:]
    if not rows or any(len(r) != len(header) for r in rows):
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

        base = next(i for i, c in enumerate(header) if c.startswith("On-demand"))
        col = base
        # A dated column replaces the plain one once its date has arrived.
        for i, c in enumerate(header):
            m = _EFFECTIVE.search(c)
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
