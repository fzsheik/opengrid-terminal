"""DeepInfra GPU Instances: the public price page, no API key.

deepinfra.com/gpu-instances sells GPU containers billed by the minute. Its
"On-Demand Cloud Pricing" table has one row per GPU configuration, each cell
labelled for small screens ("GPU config" "1x NVIDIA B200", "GPU RAM" "180 GB",
"vCPU", "RAM", "Disk", "Price" "$4.89/hour"). GPU RAM is the instance total and
the price is for the WHOLE instance, so both are divided by the GPU count.

Read: that table only, by its cell labels rather than column position. The hero
cards above it repeat some of the same prices and are not read (no double count).
DeepInfra's per-token model API prices live on other pages and are never read;
its container API needs an account key. robots.txt allows the page and the
terms say nothing against automated reads (checked 2026-10-21).

The page names the card "NVIDIA B200 SXM6" in its spec block; the GPU name we
store is "<config model> <per-GPU VRAM>GB", e.g. "NVIDIA B200 180GB".
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

BASE_URL = "https://deepinfra.com"
ENDPOINT = "/gpu-instances"

_SECTION = re.compile(r">On-Demand Cloud Pricing</h2>(?P<body>.*?)</table>", re.S)
_ROW = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S)
# <span aria-hidden="true">GPU RAM<!-- --> </span><span ...>180 GB</span>
_LABELLED = re.compile(
    r'<span[^>]*aria-hidden="true">(?P<label>[^<]+?)(?:<!-- -->)?\s*</span><span[^>]*>(?P<v>[^<]*)</span>'
)
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)/hour$")
_CONFIG = re.compile(r"^(?P<count>\d+)x\s+(?P<model>\S.*)$")
_FIELDS = {"GPU config": "config", "GPU RAM": "gpu_ram", "vCPU": "vcpu", "RAM": "ram", "Disk": "disk", "Price": "price"}


def extract(html: str) -> dict:
    """Every row of the on-demand table, keyed by its cell labels.

    Raises when the table is gone or a row lost its config or price, so a
    redesigned page fails the poll.
    """
    sec = _SECTION.search(html)
    if sec is None:
        raise ValueError("deepinfra: 'On-Demand Cloud Pricing' table not found - page reshaped")
    rows = []
    for r in _ROW.finditer(sec["body"]):
        cells = {_FIELDS[m["label"].strip()]: _html.unescape(m["v"]).strip()
                 for m in _LABELLED.finditer(r["row"]) if m["label"].strip() in _FIELDS}
        if not cells:
            continue  # the header row
        if "config" not in cells or "price" not in cells or _PRICE.match(cells["price"]) is None:
            raise ValueError(f"deepinfra: a row lost its GPU config or '$x/hour' price: {cells!r}")
        rows.append(cells)
    if not rows:
        raise ValueError("deepinfra: no rows in the on-demand table - page reshaped")
    return {"rows": rows}


def _num(cell: str | None, unit: str) -> float | None:
    m = re.match(rf"^\s*(\d+(?:\.\d+)?)\s*{unit}\b", cell or "")
    return float(m.group(1)) if m else None


def _storage_gb(cell: str | None) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(TB|GB)\b", cell or "")
    if m is None:
        return None
    return float(m.group(1)) * (1000 if m.group(2) == "TB" else 1)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class DeepInfraProvider(Provider):
    name = "deepinfra"
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
        """One on-demand row becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            m = _CONFIG.match(r["config"])
            if m is None or int(m["count"]) <= 0:
                log.warning("deepinfra: unreadable GPU config %r; skipped", r["config"])
                continue
            count, model = int(m["count"]), m["model"].strip()
            total_vram = _num(r.get("gpu_ram"), "GB")
            if total_vram is None or total_vram % count:
                log.warning("deepinfra: %s has GPU RAM %r, not a whole per-GPU figure; skipped",
                            r["config"], r.get("gpu_ram"))
                continue
            name = f"{model} {int(total_vram // count)}GB"
            price = Decimal(_PRICE.match(r["price"]).group(1).replace(",", ""))
            vcpu = _num(r.get("vcpu"), "cores")
            listings.append(
                ComputeListing(
                    provider="deepinfra",
                    sku=r["config"],
                    listing_id=f"{_slug(model)}:{count}x",
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=count,
                    region=None,  # no region on the page
                    country=None,
                    # The printed price is for the whole instance.
                    price_per_gpu_hour=price / count,
                    price_per_instance_hour=price,
                    currency="USD",
                    market_type="on_demand",
                    provider_tier="container",
                    interruptible=False,
                    available=None,  # the page says nothing about stock
                    capacity=None,
                    capacity_unit=None,
                    vcpu=None if vcpu is None else int(vcpu),
                    ram_gb=_num(r.get("ram"), "GB"),
                    storage_gb=_storage_gb(r.get("disk")),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
