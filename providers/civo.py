"""Civo: the public price page, no API key.

civo.com/pricing has an "NVIDIA GPUs" section with one table per GPU model, each
headed "<model> GPU pricing" (e.g. "NVIDIA H100 SXM GPU pricing"). A row is one
instance size: "N x NVIDIA H100 - 80GB", RAM, vCPU, storage, then an on-demand
price and a commitment price, each printed hourly and monthly. The on-demand
hourly figure is for the WHOLE instance; the per-GPU rate is it divided by N.

Read: the on-demand hourly cell of every row in that section. The GPU name is the
table heading's model (which carries the variant: "H100 SXM", "H200 SXM") plus the
row's per-GPU VRAM when the heading does not already state it.

Skipped: every commitment price (6 to 36 month terms; not modelled), the monthly
figures, and on-demand cells printed "N/A" (the B200 is sold on commitment only).
Civo's API (api.civo.com/v2/sizes) needs an account key (why Civo was declined
on 2026-10-13); this keyless page prints the same on-demand prices in USD.
robots.txt allows /pricing and Civo's terms say nothing against automated reads
(checked 2026-10-21).

Civo is a general-purpose cloud, so it never counts toward neocloud indices.
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

BASE_URL = "https://www.civo.com"
ENDPOINT = "/pricing"

_SECTION = re.compile(r'<section class="pricing-product" id="nvidia-gpus">(?P<body>.*?)</section>', re.S)
_HEADING = re.compile(r"<h4>(?P<model>[^<]+?) GPU pricing</h4>")
_ROW = re.compile(r"<tr>(?P<row>.*?)</tr>", re.S)
_DETAIL = re.compile(r'<div class="product-detail">(?P<v>[^<]+)</div>')
_CELL = re.compile(r'<td data-title="(?P<label>RAM|vCPU|Storage)">(?P<v>[^<]*)')
_ON_DEMAND = re.compile(
    r'<td data-title="On-demand price"[^>]*>\s*<div[^>]*data-option="hourly"[^>]*>(?P<v>[^<]*)<span>per hour</span>'
)
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)$")
# "8 x NVIDIA H200 - 141GB HBM3e" / "8 x NVIDIA B300" (no VRAM printed)
_SHAPE = re.compile(r"^(?P<count>\d+)\s*x\s*(?P<model>.+?)(?:\s+-\s+(?P<vram>\d+)\s*GB\b.*)?$")


def extract(html: str) -> dict:
    """Every row of every GPU table, as printed.

    Raises when the GPU section, a table heading, or a row's size / on-demand cell
    is missing, so a redesigned page fails the poll instead of recording nothing.
    """
    sec = _SECTION.search(html)
    if sec is None:
        raise ValueError("civo: 'nvidia-gpus' section not found - page reshaped")
    body = sec["body"]
    heads = list(_HEADING.finditer(body))
    if not heads:
        raise ValueError("civo: no '<model> GPU pricing' heading in the GPU section")
    rows = []
    for i, h in enumerate(heads):
        chunk = body[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        for r in _ROW.finditer(chunk):
            row = r["row"]
            if "<th" in row:
                continue  # the header row
            detail, od = _DETAIL.search(row), _ON_DEMAND.search(row)
            if detail is None or od is None:
                raise ValueError(f"civo: a {h['model']} row lost its size or on-demand cell - page reshaped")
            cells = {c["label"]: _html.unescape(c["v"]).strip() for c in _CELL.finditer(row)}
            rows.append({
                "table": _html.unescape(h["model"]).strip(),
                "size": _html.unescape(detail["v"]).strip(),
                "ram": cells.get("RAM"),
                "vcpu": cells.get("vCPU"),
                "storage": cells.get("Storage"),
                "on_demand_hourly": _html.unescape(od["v"]).strip(),
            })
    if not rows:
        raise ValueError("civo: no GPU rows found - page reshaped")
    return {"rows": rows}


def _price(cell: str) -> Decimal | None:
    """'$1.29' -> 1.29; 'N/A' -> None. Anything else raises."""
    if cell.strip().upper() == "N/A":
        return None
    m = _PRICE.match(cell.strip())
    if m is None:
        raise ValueError(f"civo: on-demand price is not '$x.xx': {cell!r}")
    return Decimal(m.group(1).replace(",", ""))


def _gb(cell: str | None) -> float | None:
    """'96 GB' -> 96, '2.5TB' -> 2560; anything else None."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(GB|TB)\b", cell or "")
    if m is None:
        return None
    return float(m.group(1)) * (1024 if m.group(2) == "TB" else 1)


def raw_name(table: str, model: str, vram: str | None) -> str:
    """The table heading's model plus the row's per-GPU VRAM: "NVIDIA H100 SXM 80GB".

    The heading carries the variant (SXM) and the row the memory; a heading that
    already states its memory ("NVIDIA L40S 48GB") is used as is.
    """
    if vram and not re.search(r"\d+\s*GB", table):
        return f"{table} {vram}GB"
    return table


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class CivoProvider(Provider):
    name = "civo"
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
        """One row with an on-demand hourly price becomes one listing."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            m = _SHAPE.match(r["size"])
            if m is None or int(m["count"]) <= 0:
                log.warning("civo: unreadable size %r in %s; skipped", r["size"], r["table"])
                continue
            count, model = int(m["count"]), m["model"].strip()
            # "NVIDIA H100" must be the model the table is about ("NVIDIA H100 SXM").
            if not r["table"].lower().startswith(model.lower()):
                log.warning("civo: row %r sits under %r; skipped", r["size"], r["table"])
                continue
            price = _price(r["on_demand_hourly"])
            if price is None or price <= 0:
                continue  # commitment-only (B200)
            name = raw_name(r["table"], model, m["vram"])
            vcpu = (r.get("vcpu") or "").strip()
            listings.append(
                ComputeListing(
                    provider="civo",
                    sku=f"{r['table']}:{count}x",
                    listing_id=f"{_slug(r['table'])}:{count}x",
                    raw_gpu_name=name,
                    canonical_gpu_name=canonical.canonical_gpu_name(name),
                    gpu_count=count,
                    region=None,  # the page prices all regions alike
                    country=None,
                    # The printed price is for the whole instance.
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
                    ram_gb=_gb(r.get("ram")),
                    storage_gb=_gb(r.get("storage")),
                    observed_at=snapshot.fetched_at,
                )
            )
        return listings
