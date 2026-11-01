"""Atlantic.Net: the public GPU cloud price page, no API key.

atlantic.net/gpu-server-hosting/ prints a "GPU Cloud Hosting Pricing Plans" table:
one row per plan (e.g. "AL40S.192GB") with RAM, vCPU, GPU ("1 x L40S"), SSD,
transfer and a price cell holding three spans, one per billing term the page's
toggle offers: od-price (On-Demand), one-y-price (1-Year) and three-y-price
(3-Year). Each span prints "$x/mo" and "$x/hr" for the WHOLE plan.

Read: the od-price "$x/hr" figure only. The extractor fails the poll if the
toggle no longer offers "On-Demand" under data-term="onDemand", because the span
class alone would then no longer say what the price means.

Skipped: the 1-year and 3-year term prices (committed terms; not modelled) and
the monthly figures. The dedicated-GPU and HIPAA pages sell monthly servers and
are not read. Atlantic.Net's cloud API needs an account key.

Policy (checked 2026-10-21): robots.txt allows the page, and atlantic.net/ai-policy/
permits crawling public pages for factual comparisons at up to 1 request/second;
one request per poll is far below that.

Atlantic.Net is a general-purpose hosting cloud, so it never counts toward
neocloud indices.
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

BASE_URL = "https://www.atlantic.net"
ENDPOINT = "/gpu-server-hosting/"

_ON_DEMAND_TERM = re.compile(r'data-term="onDemand"[^>]*>\s*On-Demand\s*</button>')
_TABLE = re.compile(r'<table class="main-pricing-table[^"]*">(?P<body>.*?)</table>', re.S)
_HEAD = re.compile(r'<th[^>]*>\s*<button[^>]*>(?P<h>[^<]+)</button>\s*</th>')
_ROW = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S)
_CELL = re.compile(r'<td class="[^"]*" data-sort-value="[^"]*">(?P<v>.*?)</td>', re.S)
_OD = re.compile(
    r'<span class="od-price[^"]*"[^>]*><span[^>]*>(?P<mo>[^<]*)</span><span[^>]*>(?P<hr>[^<]*)</span></span>'
)
_HOURLY = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)/hr$")
_GPU = re.compile(r"^(?P<count>\d+)\s*x\s*(?P<model>\S.*)$")
_COLUMNS = ["Plan", "RAM", "vCPU", "GPU", "SSD", "Transfer", "Price"]


def extract(html: str) -> dict:
    """Every plan row: its cells as printed, and its on-demand hourly price.

    Raises when the On-Demand toggle, the table, its header or a row's on-demand
    price is missing, so a redesigned page fails the poll.
    """
    if _ON_DEMAND_TERM.search(html) is None:
        raise ValueError("atlanticnet: no 'On-Demand' billing-term toggle - page reshaped")
    table = _TABLE.search(html)
    if table is None:
        raise ValueError("atlanticnet: main pricing table not found - page reshaped")
    header = [_html.unescape(h["h"]).strip() for h in _HEAD.finditer(table["body"])]
    if header != _COLUMNS:
        raise ValueError(f"atlanticnet: pricing table header changed: {header!r}")
    plans = []
    for r in _ROW.finditer(table["body"]):
        cells = [c["v"] for c in _CELL.finditer(r["row"])]
        if not cells:
            continue  # the header row
        if len(cells) != len(_COLUMNS):
            raise ValueError(f"atlanticnet: a plan row has {len(cells)} cells, not {len(_COLUMNS)}")
        od = _OD.search(cells[6])
        if od is None or _HOURLY.match(od["hr"].strip()) is None:
            raise ValueError("atlanticnet: a plan row lost its on-demand '$x/hr' price - page reshaped")
        text = [_html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells[:6]]
        plans.append(dict(zip(["plan", "ram", "vcpu", "gpu", "ssd", "transfer"], text),
                          on_demand_hourly=od["hr"].strip()))
    if not plans:
        raise ValueError("atlanticnet: no plan rows found - page reshaped")
    return {"plans": plans}


def _gb(cell: str | None) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*GB$", cell or "")
    return float(m.group(1)) if m else None


class AtlanticNetProvider(Provider):
    name = "atlanticnet"
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
        """One plan becomes one on-demand listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for p in snapshot.payload["plans"]:
            m = _GPU.match(p["gpu"])
            if m is None or int(m["count"]) <= 0:
                log.warning("atlanticnet: unreadable GPU cell %r for %s; skipped", p["gpu"], p["plan"])
                continue
            count, model = int(m["count"]), m["model"].strip()
            price = Decimal(_HOURLY.match(p["on_demand_hourly"]).group(1).replace(",", ""))
            if price <= 0:
                continue
            vcpu = p.get("vcpu") or ""
            listings.append(
                ComputeListing(
                    provider="atlanticnet",
                    sku=p["plan"],
                    listing_id=p["plan"],
                    # As printed ("L40S", "H100NVL"); the page gives no VRAM column.
                    raw_gpu_name=model,
                    canonical_gpu_name=canonical.canonical_gpu_name(model),
                    gpu_count=count,
                    region=None,  # one price list for every data centre
                    country=None,
                    # The printed hourly price is for the whole plan.
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="vm",
                    interruptible=False,
                    available=None,  # the page says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=int(vcpu) if vcpu.isdigit() else None,
                    ram_gb=_gb(p.get("ram")),
                    storage_gb=_gb(p.get("ssd")),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
