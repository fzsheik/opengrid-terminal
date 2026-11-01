"""CoreWeave: archived designs of www.coreweave.com/pricing the live parser (providers/coreweave.py) rejects, and
the older a la carte pages (www.coreweave.com/gpu-cloud-pricing, Aug 2022 - May 2024; docs.coreweave.com's
resource-based pricing, 2021 - 2023).

A la carte (2021 - 2024): CoreWeave priced a GPU instance as a GPU component plus vCPUs ($0.01/hr each) plus RAM
($0.005/GB/hr), one GPU at a time. The pages print the GPU COMPONENT per GPU-hour ("GPU Component Cost Per Hour"),
which is what is stored: one GPU, listing id "<model>:gpu-component", provider_tier "a-la-carte". It is not an
instance price: a valid instance adds at least 1 vCPU and 2 GB RAM ($0.02/hr), a typical one far more (up to
48 vCPU + 256 GB per GPU, +$1.76/hr). So it is a lower bound on what a CoreWeave GPU cost then, and is never
the same listing as today's whole-instance rows.

Model names are the page's; COMPONENT_CANONICAL gives the canonical GPU only where the name states the variant
(CoreWeave's "NVLINK" A100 / V100 are the HGX SXM boards). "Quadro RTX 4000/5000" and the like stay unmapped.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import live_page, own_page
from models import ComputeListing

# --------------------------------------------------------------------------------------------------------------
# CoreWeave, designs before the regional tables (Nov 2025 - Feb 2026): one table, instance price only, no spot.
# --------------------------------------------------------------------------------------------------------------

_CW_SECTION = re.compile(r">On-demand GPU instances</h[24]>(?P<body>.*?)>On-demand CPU instances</h[24]>", re.S)
_CW_META = re.compile(r'class="table-meta-value[^"]*"[^>]*>(?P<v>[^<]*)</div>\s*<div[^>]*>(?P<label>[^<]+)</div>')
# "$49.24 / Hour" in one div (Dec 2025 - Feb 2026) or "$49.24</div><div>/ Hour" (Nov 2025).
_CW_PRICE = re.compile(r'class="table-meta-value[^"]*"[^>]*>\s*\$(?P<v>[\d,]+(?:\.\d+)?)\s*(?:</div>\s*<div>)?/\s*Hour')
_CW_MODEL = re.compile(r'<h3[^>]*class="table-model-name"[^>]*>(?P<m>[^<]+)</h3>')


def coreweave_single_table(page: str, at: datetime) -> list[ComputeListing]:
    from providers.coreweave import _COUNT, raw_name

    sec = _CW_SECTION.search(page)
    if sec is None:
        raise ValueError("coreweave(archive): 'On-demand GPU instances' section not found")
    out = []
    for row in re.split(r'role="listitem"', sec["body"])[1:]:
        models = {m["m"].strip() for m in _CW_MODEL.finditer(row)}
        meta = {m["label"].strip(): m["v"].strip() for m in _CW_META.finditer(row)}
        if "GPU Count" not in meta:
            continue  # a managed service row (SUNK, ...), not an instance shape
        prices = {p["v"] for p in _CW_PRICE.finditer(row)}
        if len(models) != 1 or len(prices) > 1:
            raise ValueError("coreweave(archive): a GPU row has several models or prices")
        if not prices:
            continue  # Contact sales
        cm = _COUNT.match(meta["GPU Count"])
        if cm is None or int(cm.group(1)) == 0:
            continue
        model, count, price = models.pop(), int(cm.group(1)), dec(prices.pop())
        vram = meta.get("VRAM")
        storage = dec(meta.get("Local Storage (TB)"))
        out.append(listing(
            "coreweave", sku=f"{model}:{count}x", listing_id=f"{slug(model)}:{count}x", raw=raw_name(model, vram),
            count=count, instance_price=price, at=at,
            vcpu=int(meta["vCPUs"]) if meta.get("vCPUs", "").isdigit() else None,
            ram_gb=float(meta["System RAM"].replace(",", "")) if dec(meta.get("System RAM")) else None,
            storage_gb=float(storage) * 1024 if storage else None))
    if not out:
        raise ValueError("coreweave(archive): no priced GPU rows")
    return out



# --------------------------------------------------------------------------------------------------------------
# A la carte GPU component prices (2021 - 2024).
# --------------------------------------------------------------------------------------------------------------

# (page model, VRAM GB) -> canonical GPU, where the page's name makes the product unambiguous.
COMPONENT_CANONICAL = {
    ("NVIDIA HGX H100", 80): "NVIDIA H100 80GB SXM5",
    ("NVIDIA H100 PCIe", 80): "NVIDIA H100 80GB PCIe",
    ("A100 80GB NVLINK", 80): "NVIDIA A100 80GB SXM4",
    ("A100 80GB PCIe", 80): "NVIDIA A100 80GB PCIe",
    ("A100 40GB NVLINK", 40): "NVIDIA A100 40GB SXM4",
    ("A100 40GB PCIe", 40): "NVIDIA A100 40GB PCIe",
    ("A40", 48): "NVIDIA A40 48GB",
    ("RTX A6000", 48): "NVIDIA RTX A6000 48GB",
    ("RTX A5000", 24): "NVIDIA RTX A5000 24GB",
    ("RTX A4000", 16): "NVIDIA RTX A4000 16GB",
    ("Tesla V100 NVLINK", 16): "NVIDIA Tesla V100 16GB SXM2",
    ("NVIDIA L40S", 48): "NVIDIA L40S 48GB",
    ("NVIDIA L40", 48): "NVIDIA L40 48GB",
    ("NVIDIA GH200", 96): "NVIDIA GH200 96GB",
}

COMPONENT_HEADER = ["GPU Model", "VRAM (GB)", "Max vCPUs per GPU ($0.01/hr)", "Max RAM (GB) per GPU ($0.005/hr)",
                    "GPU Component Cost Per Hour"]
_DESKTOP = re.compile(r'<div class="desktop">\s*<div class="table">(?P<t>.*?)(?=<div class="(?:mobile|desktop)"|$)', re.S)
_WCOL = re.compile(r'<div class="[^"]*\bw-col\b[^"]*">')
_BODY_ROW = re.compile(r'<div class="table-body-row[^"]*">')
_DOLLAR = re.compile(r"^\$(?P<v>\d+(?:\.\d+)?)(?:\s*Reserve Now)?$")


def _component(model: str, vram: str, price: str, at: datetime):
    m = _DOLLAR.match(price)
    if m is None:
        if re.search(r"\d", price):
            raise ValueError(f"coreweave(a la carte): {model} price cell {price!r} is not '$x'")
        return None  # "Reserve Now" alone: no on-demand price
    if not vram.isdigit():
        raise ValueError(f"coreweave(a la carte): {model} VRAM {vram!r}")
    o = listing("coreweave", sku=f"{model}:gpu-component", listing_id=f"{slug(model)}:gpu-component",
                raw=f"{model} {vram}GB", count=1, instance_price=dec(m["v"]), at=at, tier="a-la-carte")
    return o.model_copy(update={"canonical_gpu_name": COMPONENT_CANONICAL.get((model, int(vram)))})


def coreweave_component_table(page: str, at: datetime) -> list[ComputeListing]:
    """www.coreweave.com/gpu-cloud-pricing: the desktop table GPU Model | VRAM (GB) | Max vCPUs | Max RAM |
    GPU Component Cost Per Hour (the page's mobile cards repeat it, sometimes with different numbers: not read)."""
    out, tables = [], 0
    for tm in _DESKTOP.finditer(page):
        t = tm["t"]
        head_at = t.find('class="table-header"')
        body_at = t.find('class="table-body"')
        if head_at == -1 or body_at == -1:
            continue
        head = [text(c) for c in _WCOL.split(t[head_at:t.rfind("<div", 0, body_at)])[1:]]
        head = [h.replace("( $", "($").replace(" /hr", "/hr").replace("( ", "(") for h in head]
        if not head or head[0] != "GPU Model":
            continue
        if head != COMPONENT_HEADER:
            raise ValueError(f"coreweave(a la carte): GPU table header {head!r}")
        tables += 1
        for row in _BODY_ROW.split(t[body_at:])[1:]:
            cells = [text(c) for c in _WCOL.split(row)[1:]]
            if len(cells) != len(head):
                raise ValueError(f"coreweave(a la carte): row {cells!r} has {len(cells)} cells")
            o = _component(cells[0], cells[1], cells[4], at)
            if o is not None:
                out.append(o)
    if not tables:
        raise ValueError("coreweave(a la carte): no 'GPU Model ... GPU Component Cost Per Hour' table")
    if not out:
        raise ValueError("coreweave(a la carte): no priced GPU row")
    return out


SOURCES = [live_page("coreweave", "coreweave", "www.coreweave.com/pricing", coreweave_single_table),
           own_page("coreweave", "www.coreweave.com/gpu-cloud-pricing", coreweave_component_table)]
