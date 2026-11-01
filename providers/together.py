"""Together AI GPU Clusters: the public price page, no API key.

The "GPU Clusters" section of together.ai/pricing has a table headed "On-demand
hourly rates and reserved capacity" that states "All prices are per GPU per
hour". Its columns are Preemptible Compute (pay as you go), ON-Demand (pay as
you go), then four Reserved terms (3-30, 31-90, 91-180, 181+ days).

Only the two pay-as-you-go columns become listings: on-demand as on_demand,
preemptible as spot (interruptible).

Reserved terms (raw only). The same page load also stores the four reserved
columns, cells verbatim with the term labels the table prints, under their own
endpoint label RESERVED_ENDPOINT, so normalize() never sees them, the way Azure's
reservations are kept (providers/azure.py). Reserved prices are commitments: they
never become listings or enter indices or statistics until a reserved-price data
model exists. reserved_prices() reads them. A reshaped reserved block is stored as
a failed fetch of that label only; the on-demand prices are unaffected.
The page states no node size or region, so a listing is one GPU at the printed
per-GPU rate, the same convention as Nebius.

Everything else on the page (serverless and dedicated inference, fine-tuning,
sandboxes) is not GPU rental and is ignored.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

import canonical
from models import ComputeListing, RawResponse
from providers.base import PollingPolicy, Provider

if TYPE_CHECKING:
    from tables import RawSnapshot

log = logging.getLogger(__name__)

BASE_URL = "https://www.together.ai"
ENDPOINT = "/pricing"
# The reserved columns of the same page, stored under their own label (never read by normalize()).
RESERVED_ENDPOINT = "/pricing#reserved"

_SECTION = re.compile(r">GPU Clusters</h2>(?P<body>.*?)(?:<h2[ >]|$)", re.S)
_TABLE_TITLE = "On-demand hourly rates and reserved capacity"
_UNIT = "All prices are per GPU per hour."
_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(?P<c>.*?)</td>", re.S)
_TH = re.compile(r"<th[^>]*>(?P<c>.*?)</th>", re.S)
_TAGS = re.compile(r"<[^>]+>")
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)$")
_TD_SPAN = re.compile(r"<td(?P<a>[^>]*)>(?P<c>.*?)</td>", re.S)
_COLSPAN = re.compile(r'colspan="?(\d+)')
_TERM = re.compile(r"^(?P<lo>\d+)(?:-(?P<hi>\d+)|(?P<plus>\+)) days$")
EXPECTED_HEADER = ["Hardware", "Preemptible Compute", "ON-Demand", "Reserved"]


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", fragment)).replace(" ", " ").strip()


def _price_table(html: str) -> list[str]:
    """The <tr> fragments of the GPU Clusters per-GPU price table. Raises when the page is reshaped."""
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
    return _TR.findall(table["t"])


def extract_reserved(html: str) -> dict:
    """The reserved columns of the same table, cells as printed, one entry per term the sub-header names.

    The sub-header row reads "Pay as you go, Pay as you go, 3-30 days, 31-90 days, ...". A cell spanning several
    term columns (colspan, as "Contact us" does) is repeated for each term it covers. Raises when the header, the
    term labels or a row's width no longer fit, and when no row has a reserved cell at all.
    """
    trs = _price_table(html)
    header = [_text(c) for c in _TH.findall(trs[0])] if trs else []
    if header[:4] != EXPECTED_HEADER:
        raise ValueError(f"together: GPU table header changed: {header!r}")
    sub = [_text(c) for c in _TH.findall(trs[1])] if len(trs) > 1 else []
    terms = sub[2:]
    if sub[:2] != ["Pay as you go", "Pay as you go"] or not terms or not all(_TERM.match(t) for t in terms):
        raise ValueError(f"together: reserved term labels changed: {sub!r}")
    rows = []
    for tr in trs[2:]:
        cells = []
        for m in _TD_SPAN.finditer(tr):
            span = _COLSPAN.search(m["a"])
            cells += [_text(m["c"])] * (int(span.group(1)) if span else 1)
        if not cells:
            continue
        if len(cells) != 3 + len(terms):
            raise ValueError(f"together: row {cells[:1]!r} has {len(cells)} columns, expected {3 + len(terms)}")
        rows.append({"hardware": cells[0], "reserved": dict(zip(terms, cells[3:]))})
    if not rows:
        raise ValueError("together: GPU table has no reserved rows - page reshaped")
    return {"unit": "per GPU per hour", "terms": terms, "rows": rows}


def extract(html: str) -> dict:
    """The per-GPU price table of the GPU Clusters section, cells as printed.

    Raises when the section, the table, its per-GPU unit statement or its column
    layout is missing, so a reshaped page fails the poll instead of recording nothing.
    """
    trs = _price_table(html)
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
        """One page load, two stored responses: the pay-as-you-go table (listings) and the reserved columns (raw
        only). Each extract fails on its own, so a reshaped reserved block never costs the on-demand prices."""
        started_wall, started = datetime.now(timezone.utc), time.perf_counter()
        try:
            resp = await self._client.get(ENDPOINT)
            resp.raise_for_status()
        except Exception as exc:
            return [self._response(ENDPOINT, started_wall, started, None, None, exc)]
        out = []
        for endpoint, parse in ((ENDPOINT, extract), (RESERVED_ENDPOINT, extract_reserved)):
            try:
                out.append(self._response(endpoint, started_wall, started, resp.status_code, parse(resp.text)))
            except Exception as exc:
                out.append(self._response(endpoint, started_wall, started, resp.status_code, None, exc))
        return out

    def _response(self, endpoint, fetched_at, started, status, payload, exc=None) -> RawResponse:
        return RawResponse(provider=self.name, endpoint=endpoint, fetched_at=fetched_at, status_code=status,
                           duration_ms=int((time.perf_counter() - started) * 1000), ok=exc is None, payload=payload,
                           error=None if exc is None else f"{type(exc).__name__}: {exc}"[:500])

    @staticmethod
    def normalize(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[ComputeListing]:
        """One row becomes an on-demand listing and a preemptible (spot) listing, where priced."""
        # Exact label: RESERVED_ENDPOINT shares the "/pricing" prefix and must never be read here.
        rows = by_endpoint.get(ENDPOINT) or []
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


@dataclass(frozen=True)
class TogetherReservedPrice:
    """One Together GPU Clusters reserved rate: per GPU-hour for a commitment of min_days..max_days. Never a listing."""

    hardware: str               # as printed, e.g. "NVIDIA HGX H100"
    canonical_gpu_name: str | None
    term: str                   # as printed, e.g. "31-90 days"
    min_days: int
    max_days: int | None        # None for an open-ended "181+ days"
    price_per_gpu_hour: Decimal


def reserved_prices(by_endpoint: "dict[str, list[RawSnapshot]]") -> list[TogetherReservedPrice]:
    """Every priced reserved cell in the stored reserved snapshot. "Contact us" and dashes are not prices. Pure."""
    rows = by_endpoint.get(RESERVED_ENDPOINT) or []
    body = rows[0].payload if rows and isinstance(rows[0].payload, dict) else {}
    out = []
    for r in body.get("rows") or []:
        for term, cell in (r.get("reserved") or {}).items():
            m = _TERM.match(term)
            try:
                price = _price(cell)
            except ValueError:
                continue  # a cell in a new shape is kept raw, never guessed
            if m is None or price is None or price <= 0:
                continue
            out.append(TogetherReservedPrice(
                hardware=r["hardware"], canonical_gpu_name=canonical.canonical_gpu_name(r["hardware"]), term=term,
                min_days=int(m["lo"]), max_days=None if m["plus"] else int(m["hi"]), price_per_gpu_hour=price))
    return out
