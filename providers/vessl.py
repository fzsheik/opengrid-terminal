"""VESSL AI: the public price page, no API key.

vessl.ai/pricing (served from /en/pricing) prints the On-demand "Container
Compute" table: GPU ("NVIDIA H100 SXM"), a "Self-serve" badge, VRAM ("80GB"),
architecture and the On-Demand price ("$2.98/hr"). The "What gets billed" card
says Container Compute is billed "per GPU, per hour"; that line is required. The
page renders every row twice (a desktop table and mobile cards); a row whose two
copies disagree on VRAM or price is skipped.

Each row becomes one 1-GPU listing: the page prices one GPU and does not say
which GPU counts a container can take.

VESSL runs these containers on shared clusters it operates and sets its own
per-second price; the page names no other cloud for them (its docs name its own
on-premises cluster in Korea and an OCI region among VESSL Cloud's hosts, but
not per GPU). Not read: Reserved VM Clusters ("Quoted by term"), Provisioned
Throughput (per PTU) and storage.
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

BASE_URL = "https://vessl.ai"
ENDPOINT = "/en/pricing"

_UNIT = re.compile(r">Container Compute</h3>\s*<p[^>]*>per GPU, per hour</p>")
_SECTION = re.compile(r">Container Compute</h2>(?P<body>.*?)No quotas or usage limits", re.S)
_TABLE = re.compile(r"<tbody>(?P<body>.*?)</tbody>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<cells>.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(?P<v>.*?)</td>", re.S)
_NAME = re.compile(r'<span class="[^"]*font-semibold text-foreground">(?P<v>[^<]+)</span>')
# Mobile card: name, then "80GB<!-- -->, <!-- -->Hopper", then the price.
_CARD = re.compile(
    r'<span class="font-semibold text-foreground">(?P<name>[^<]+)</span>.*?'
    r'<div class="mt-1 text-sm text-muted-foreground">(?P<vram>[^<]+)<.*?'
    r'<span class="text-2xl font-semibold text-foreground">(?P<price>[^<]+)</span>', re.S)
_PRICE = re.compile(r"^\$(\d+(?:\.\d+)?)/hr$")
_VRAM = re.compile(r"^(\d+)GB$")


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()


def extract(html: str) -> dict:
    """Every On-demand Container Compute row, cross-checked with its mobile card.

    Raises when the per-GPU billing line, the section or the table is missing.
    """
    if _UNIT.search(html) is None:
        raise ValueError("vessl: Container Compute is no longer stated 'per GPU, per hour' - pricing basis unknown")
    sec = _SECTION.search(html)
    table = _TABLE.search(sec["body"]) if sec else None
    if table is None:
        raise ValueError("vessl: On-demand Container Compute table not found - page reshaped")
    cards = {_text(m["name"]): (_text(m["vram"]).split(",")[0].strip(), _text(m["price"]))
             for m in _CARD.finditer(sec["body"][table.end():])}
    rows = []
    for tr in _TR.finditer(table["body"]):
        cells = [c["v"] for c in _TD.finditer(tr["cells"])]
        name = _NAME.search(cells[0]) if cells else None
        if len(cells) < 4 or name is None:
            raise ValueError("vessl: a Container Compute row lost its cells - page reshaped")
        row = {"gpu": _text(name["v"]), "vram": _text(cells[1]), "arch": _text(cells[2]), "price": _text(cells[3])}
        mobile = cards.get(row["gpu"])
        # The card prints "$2.98" and its "/hr" in a separate span.
        if mobile is None or mobile != (row["vram"], row["price"].removesuffix("/hr")):
            row["conflict"] = True
        rows.append(row)
    if not rows:
        raise ValueError("vessl: no Container Compute rows found - page reshaped")
    return {"rows": rows}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class VesslProvider(Provider):
    name = "vessl"
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
        """One priced row becomes one 1-GPU container listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            if r.get("conflict"):
                log.warning("vessl: %s prints a different VRAM or price on its mobile card; skipped", r["gpu"])
                continue
            m = _PRICE.match(r["price"])
            if m is None:
                continue  # "Quoted with sales" or similar: no self-serve price
            price = Decimal(m.group(1))
            vram = _VRAM.match(r["vram"])
            if price <= 0 or vram is None:
                log.warning("vessl: %s has price %r, VRAM %r; skipped", r["gpu"], r["price"], r["vram"])
                continue
            name = f"{r['gpu']} {vram.group(1)}GB"  # "NVIDIA H100 SXM 80GB"
            listings.append(
                ComputeListing(
                    provider="vessl",
                    sku=r["gpu"],
                    listing_id=f"{_slug(name)}:1x",
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=1,
                    region=None,
                    country=None,
                    price_per_gpu_hour=price,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="container",
                    interruptible=False,
                    available=None,  # "Self-serve" is an offer type, not stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None,
                    ram_gb=None,
                    storage_gb=None,
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
