"""Crusoe: archived captures of www.crusoe.ai/cloud/pricing.

Live, OpenGrid reads Crusoe through Shadeform's catalogue (providers/shadeform.py, "via-shadeform"). The archived
page is Crusoe's own "GPU instances pricing" list, headed GPU model | On-demand | Current spot. Each item is a model
heading ("NVIDIA H100") with tags (VRAM "80GB", platform "HGX" / "PCIe" / "SXM" / "NVL72") and two price cells
reading "$3.90/GPU-hr" or "Contact sales". A priced cell becomes one listing of ONE GPU at the printed per-GPU rate
(the page states no instance size; the Together / Nebius convention): On-demand as on_demand, Current spot as spot.

Raw GPU name: the heading, VRAM and platform tag as printed ("NVIDIA A100 80GB PCIe"), except that an HGX
platform is written the way NVIDIA names the product and CoreWeave's live parser reads it ("NVIDIA HGX H100
80GB"); canonical.py decides the variant. Other tables on the page (CPU, storage, managed inference) are ignored.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

HEADER = ["GPU model", "On-demand", "Current spot"]
_HEAD = re.compile(r'class="pricing-table-heading')
_HCELL = re.compile(r'<div class="pricing-heading[^"]*">(.*?)</div>', re.S)
_MODEL = re.compile(r'class="pricing-item-heading">(.*?)</h4>', re.S)
_TAG = re.compile(r'class="pricing-tag[^"]*">(.*?)</div>', re.S)
_CELL = re.compile(r'class="pricing-rich w-richtext">(.*?)</div>', re.S)
_PRICE = re.compile(r"^\$(\d+(?:\.\d+)?)\s*/\s*GPU-hr$")
_VRAM = re.compile(r"^(\d+)\s*GB$")


def raw_name(model: str, tags: list[str]) -> str:
    vram = next((t.replace(" ", "") for t in tags if _VRAM.match(t)), None)
    rest = [t for t in tags if t and not _VRAM.match(t)]
    if "HGX" in rest and model.startswith("NVIDIA ") and not model.startswith("NVIDIA HGX"):
        model = "NVIDIA HGX " + model[len("NVIDIA "):]
        rest.remove("HGX")
    return " ".join([model, *([vram] if vram else []), *[t for t in rest if t not in model.split()]])


def crusoe_gpu_list(page: str, at: datetime) -> list[ComputeListing]:
    heads = [m.start() for m in _HEAD.finditer(page)]
    out, tables = [], 0
    for i, s in enumerate(heads):
        if [text(c) for c in _HCELL.findall(page[s:s + 800])][:3] != HEADER:
            continue
        tables += 1
        body = page[s: heads[i + 1] if i + 1 < len(heads) else len(page)]
        for item in re.split(r'role="listitem"', body)[1:]:
            m = _MODEL.search(item)
            if not m:
                continue
            model, tags = text(m.group(1)), [text(t) for t in _TAG.findall(item)]
            cells = [text(c) for c in _CELL.findall(item)]
            if len(cells) < 2:
                raise ValueError(f"crusoe(archive): {model} has {len(cells)} price cells, expected 2")
            raw = raw_name(model, tags)
            for market, cell in (("on_demand", cells[0]), ("spot", cells[1])):
                if not re.search(r"\d", cell):
                    continue  # Contact sales
                pm = _PRICE.match(cell)
                if pm is None:
                    raise ValueError(f"crusoe(archive): {model} {market} cell is not '$x/GPU-hr': {cell!r}")
                out.append(listing("crusoe", sku=raw, listing_id=slug(raw) + ("" if market == "on_demand" else ":spot"),
                                   raw=raw, count=1, instance_price=dec(pm.group(1)), at=at, market=market,
                                   tier="crusoe-page"))
    if not tables:
        raise ValueError("crusoe(archive): no 'GPU model | On-demand | Current spot' table")
    if not out:
        raise ValueError("crusoe(archive): GPU table has no priced cell")
    return out


SOURCES = [own_page("crusoe", "www.crusoe.ai/cloud/pricing", crusoe_gpu_list)]
