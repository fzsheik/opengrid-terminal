"""Mithril (formerly ML Foundry): the public price page, no API key.

mlfoundry.com/pricing now redirects to mithril.ai/pricing, a Framer page whose
"Compute" table prints one row per instance type: GPU ("H200 SXM5"), GPU count,
vCPUs, RAM, disk, and "Spot (7-day avg)" as "$3.93 instance/hr", i.e. for the
WHOLE instance. Mithril's API (api.mithril.ai) needs an account key; the page is
the only public source.

Prices are spot: Mithril's platform sets them "algorithmically based on supply
and demand", and the page says they are "updated weekly and represent an
average of the last 7 days". That sentence is required, so a change in what the
number means fails the poll. Listings are spot and interruptible; the 7-day
average is recorded as published, not as a live quote.

Framer class names change on every publish, so rows are read from the table's
text in order (name, GPUs, vCPUs, RAM GB, disk GB, price, "instance/hr"). Two
types can share a GPU and count (two 1x H200 rows with different CPU, RAM and
disk); the listing id carries the specs to keep them apart. Mithril's reserved
capacity ("Reservations") is quoted separately and is not read.
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

BASE_URL = "https://mithril.ai"
ENDPOINT = "/pricing"

_AVERAGE = "represent an average of the last 7 days"
_SECTION = re.compile(r">Spot \(7-day avg\)</p>(?P<body>.*?)" + re.escape("Mithril's omnicloud platform"), re.S)
_TEXT = re.compile(r'<p[^>]*class="framer-text"[^>]*>(?P<v>[^<]*)</p>')
_INT = re.compile(r"^\d{1,3}(?:,\d{3})*$")
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)$")


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def extract(html: str) -> dict:
    """Every compute row, as printed.

    Raises when the 7-day-average note or the table is gone, or a row does not
    read as name, GPUs, vCPUs, RAM GB, disk GB, price, "instance/hr".
    """
    html = html.replace("&#x27;", "'").replace("&#39;", "'")
    if _AVERAGE not in html:
        raise ValueError("mithril: the '7-day average' note is gone - price meaning unknown")
    sec = _SECTION.search(html)
    if sec is None:
        raise ValueError("mithril: 'Spot (7-day avg)' table not found - page reshaped")
    tokens = [_html.unescape(m["v"]).strip() for m in _TEXT.finditer(sec["body"])]
    tokens = [t for t in tokens if t]
    rows = []
    for i in range(0, len(tokens), 9):
        r = tokens[i: i + 9]
        if (len(r) != 9 or not all(_INT.match(x) for x in (r[1], r[2], r[3], r[5])) or r[4] != "GB"
                or r[6] != "GB" or _PRICE.match(r[7]) is None or r[8] != "instance/hr"):
            raise ValueError(f"mithril: a compute row does not read as expected: {r!r}")
        rows.append({"gpu": r[0], "gpus": r[1], "vcpus": r[2], "ram_gb": r[3], "disk_gb": r[5], "spot": r[7]})
    if not rows:
        raise ValueError("mithril: no compute rows found - page reshaped")
    return {"rows": rows}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class MithrilProvider(Provider):
    name = "mithril"
    # One page load per poll; the page itself changes weekly.
    # Slow tier: the page publishes a weekly 7-day average; every 6 h.
    polling = PollingPolicy(interval_seconds=21600, timeout_seconds=45, tier="slow")

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
        """One row becomes one spot listing for the whole instance."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            count = _int(r["gpus"])
            price = Decimal(_PRICE.match(r["spot"]).group(1).replace(",", ""))
            if count < 1 or price <= 0:
                log.warning("mithril: %s has %r GPUs at %r; skipped", r["gpu"], r["gpus"], r["spot"])
                continue
            vcpu, ram, disk = _int(r["vcpus"]), _int(r["ram_gb"]), _int(r["disk_gb"])
            listings.append(
                ComputeListing(
                    provider="mithril",
                    sku=f"{r['gpu']}:{count}x",
                    listing_id=f"{_slug(r['gpu'])}:{count}x:{vcpu}c-{ram}g-{disk}d",
                    raw_gpu_name=r["gpu"],
                    canonical_gpu_name=canonical.canonical_gpu_name(r["gpu"]),
                    gpu_count=count,
                    region=None,
                    country=None,
                    # "instance/hr": the printed price is for the whole instance.
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="spot",
                    provider_tier="vm",
                    interruptible=True,
                    available=None,  # a weekly average says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=vcpu,
                    ram_gb=float(ram),
                    storage_gb=float(disk),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
