"""CoreWeave: the public price page, no API key.

The "On-demand GPU instances" section prints one table per region (North
America, Europe). Each row is one instance shape: GPU model, GPU count, VRAM per
GPU, vCPUs, RAM, local storage, then an on-demand and a spot price for the WHOLE
instance. The per-GPU rate is derived by dividing by the GPU count.

Read from the row's labelled mobile block ("On-Demand Price: $x / Hour", "GPU
Count", "VRAM", ...) rather than by column position, so a reordered table
cannot shift a number into the wrong field.

Left out on purpose: the "Inference Single GPU Price" column (sold only to
CoreWeave inference-platform customers, per the page's own footnote) and every
"Contact sales" cell.
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

BASE_URL = "https://www.coreweave.com"
ENDPOINT = "/pricing"

_GPU_SECTION = re.compile(r">On-demand GPU instances</h2>(?P<body>.*?)>On-demand CPU instances</h2>", re.S)
_REGION = re.compile(r'<strong class="eyebrow">REGION:\s*(?P<region>[^<]+)</strong>')
# GPU rows carry "kubernetes-gpu-pricing" or "gpu-pricing-and-kubernetes-gpu-pricing" (the H100 row).
_ROW = re.compile(r'<div role="listitem" class="table-row-v2 w-dyn-item [a-z-]*gpu-pricing">')
_MODEL = re.compile(r'<h3[^>]*class="table-model-name"[^>]*>(?P<model>[^<]+)</h3>')
_ON_DEMAND = re.compile(r'<span class="instance-price">On-Demand Price:\s*<span class="item-value">(?P<v>[^<]*)</span>')
_SPOT = re.compile(r'<span class="spot-price">Spot Price:\s*<span class="item-value">(?P<v>[^<]*)</span>')
_META = re.compile(r'<div class="table-meta-value">(?P<v>[^<]*)</div>\s*<div>(?P<label>[^<]+)</div>')
# The desktop cells, in header order: count, VRAM, vCPUs, RAM, storage, on-demand, spot, inference.
_CELL = re.compile(r'<div class="table-v2-cell">\s*<div>(?P<v>[^<]*)</div>')
_PRICE = re.compile(r"^\$(\d+(?:,\d{3})*(?:\.\d+)?)$")
# "4^1": four GPUs, footnote 1 (an NVL72 instance is two superchips of two GPUs each).
_COUNT = re.compile(r"^(\d+)(?:\^\d+)?$")
# "(High Memory)" / "(Standard Memory)" describe system RAM, not the card.
_SYSTEM_MEMORY = re.compile(r"\s*\((High|Standard) Memory\)\s*$")


def _row(region: str, row: str) -> dict:
    """One GPU row, read from its labelled block and cross-checked with its desktop cells.

    The page renders every row twice (a desktop grid and a labelled mobile block).
    Where the two disagree on GPU count, VRAM or a price, the row is kept with
    `conflict` set and normalize() skips it: we cannot tell which one is right.
    """
    models = {m["model"].strip() for m in _MODEL.finditer(row)}
    od, spot = _ON_DEMAND.search(row), _SPOT.search(row)
    meta = {m["label"].strip(): m["v"].strip() for m in _META.finditer(row)}
    if len(models) != 1 or od is None or spot is None or "GPU Count" not in meta:
        raise ValueError("coreweave: a GPU row lost its labelled fields - page reshaped")
    cells = [c["v"].strip() for c in _CELL.finditer(row)]
    out = {
        "region": region,
        "model": models.pop(),
        "gpu_count": meta.get("GPU Count"),
        "vram_gb": meta.get("VRAM"),
        "vcpus": meta.get("vCPUs"),
        "ram_gb": meta.get("System RAM"),
        "storage_tb": meta.get("Local Storage (TB)"),
        "on_demand": od["v"].strip(),
        "spot": spot["v"].strip(),
    }
    desktop = dict(zip(["gpu_count", "vram_gb", "vcpus", "ram_gb", "storage_tb", "on_demand", "spot"], cells))
    conflict = [k for k in ("gpu_count", "vram_gb", "on_demand", "spot") if desktop.get(k, out[k]) != out[k]]
    if len(cells) < 7:
        conflict.append("desktop cells missing")
    if conflict:
        out["conflict"] = conflict
    return out


def extract(html: str) -> dict:
    """Every GPU instance row on the page, with its region and its printed cells.

    Raises when the section, a region heading or the labelled fields are missing,
    so a redesigned page fails the poll instead of recording nothing.
    """
    sec = _GPU_SECTION.search(html)
    if sec is None:
        raise ValueError("coreweave: 'On-demand GPU instances' section not found - page reshaped")
    body = sec["body"]
    heads = list(_REGION.finditer(body))
    if not heads:
        raise ValueError("coreweave: no REGION heading in the GPU section")
    rows = []
    for i, h in enumerate(heads):
        chunk = body[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        starts = [m.start() for m in _ROW.finditer(chunk)] + [len(chunk)]
        rows.extend(_row(h["region"].strip(), chunk[a:b]) for a, b in zip(starts, starts[1:]))
    if not rows:
        raise ValueError("coreweave: no GPU rows found - page reshaped")
    return {"rows": rows}


def _price(cell: str) -> Decimal | None:
    """'$49.24' -> 49.24; '' (Contact sales) and 'N/A' -> None. Anything else raises."""
    cell = cell.strip()
    if cell in ("", "N/A"):
        return None
    m = _PRICE.match(cell)
    if m is None:
        raise ValueError(f"coreweave: price cell is not '$x.xx': {cell!r}")
    return Decimal(m.group(1).replace(",", ""))


def _num(cell: str | None) -> float | None:
    if not cell:
        return None
    try:
        return float(cell.replace(",", ""))
    except ValueError:
        return None


def raw_name(model: str, vram: str | None) -> str:
    """The page's model name with its printed per-GPU VRAM: "NVIDIA HGX H100 80GB".

    The VRAM column is what tells a 96GB GH200 from a 144GB one; carrying it in the
    raw name also lets the quality layer check it against the canonical name.
    """
    base = _SYSTEM_MEMORY.sub("", model).strip()
    return f"{base} {vram}GB" if vram and vram.isdigit() else base


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


class CoreWeaveProvider(Provider):
    name = "coreweave"
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
        """One row becomes an on-demand listing and a spot listing, where each is priced."""
        rows = find_endpoint(by_endpoint, ENDPOINT)
        if not rows:
            return []
        snapshot = rows[0]

        listings = []
        for r in snapshot.payload["rows"]:
            if r.get("conflict"):
                log.warning("coreweave: %s (%s) prints two different %s; skipped", r["model"], r["region"], r["conflict"])
                continue
            m = _COUNT.match(r["gpu_count"] or "")
            if m is None or int(m.group(1)) == 0:
                log.warning("coreweave: unreadable GPU count %r for %s; skipped", r["gpu_count"], r["model"])
                continue
            count = int(m.group(1))
            name = raw_name(r["model"], r["vram_gb"])
            base_id = f"{_slug(r['model'])}:{count}x:{_slug(r['region'])}"
            storage_tb = _num(r["storage_tb"])
            for market, cell, suffix in (("on_demand", r["on_demand"], ""), ("spot", r["spot"], ":spot")):
                price = _price(cell)
                if price is None or price <= 0:
                    continue  # Contact sales, or no spot market for this shape
                vcpu = _num(r["vcpus"])
                listings.append(
                    ComputeListing(
                        provider="coreweave",
                        sku=f"{r['model']}:{count}x",
                        listing_id=base_id + suffix,
                        raw_gpu_name=name,
                        canonical_gpu_name=canonical.canonical_gpu_name(name),
                        gpu_count=count,
                        # The page's own region label ("North America", "Europe"): a
                        # continent, so no country is claimed.
                        region=r["region"].title()[:64],
                        country=None,
                        # The printed price is for the whole instance.
                        price_per_gpu_hour=price / count,
                        price_per_instance_hour=price,
                        currency="USD",
                        market_type=market,
                        provider_tier=None,
                        interruptible=market == "spot",
                        available=None,  # the page says nothing about stock
                        capacity=None,
                        capacity_unit=None,
                        vcpu=None if vcpu is None else int(vcpu),
                        ram_gb=_num(r["ram_gb"]),
                        # The page defines 1 TB as 2^40 bytes, i.e. 1024 GB.
                        storage_gb=None if storage_tb is None else storage_tb * 1024,
                        observed_at=snapshot.fetched_at,
                    )
                )
        return listings
