"""Vultr: archived captures of www.vultr.com/pricing.

Live, OpenGrid reads Vultr's plans API (providers/vultr.py). The archived page is one long price list; only its
<h2>Cloud GPU</h2> section is read, which states "Pricing shown is on-demand and billed hourly". Each card there is
an <h3> model ("NVIDIA HGX H100", "AMD MI300X", "NVIDIA PCIe A100") followed by a table headed GPU | GPU RAM | vCPUs
| RAM | Storage | Bandwidth | Hourly Price (2025) / Price starting at (2026), one row per size: GPU count, total GPU
RAM, and a price "$2.300/GPU/hr" (per GPU) or "$2.397/hr" (whole instance). A mobile copy of each table follows
under an <h3>Pricing</h3>; only the first table after a model heading is read. The Bare Metal section ("Starting
from" prices, which the page says are for pre-paid contract terms) is never read.

Raw GPU name: the model with the per-GPU memory (GPU RAM / count): "NVIDIA HGX H100 80GB". Fractional vGPU sizes
(a "1 GPU" row with 10 GB of an 80 GB A100) are skipped: only rows at the card's full memory per GPU are read.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

_H2 = re.compile(r"<h2[^>]*>(?P<t>.*?)</h2>", re.S)
_H3 = re.compile(r"<h3[^>]*>(?P<t>.*?)</h3>", re.S)
_HEAD = re.compile(r'class="pt__header"[^>]*>(?P<h>.*?)</div>\s*<div class="pt__body', re.S)
_CELLH = re.compile(r'<div class="pt__cell[^"]*"[^>]*>(?P<c>.*?)</div>', re.S)
_ROW = re.compile(r'<div class="pt__row-content"[^>]*>(?P<r>.*?</div>)\s*</div>', re.S)
HEAD = ("GPU", "GPU RAM", "vCPUs", "RAM", "Storage", "Bandwidth")
PRICE_HEADS = ("Hourly Price", "Price starting at")
_PRICE = re.compile(r"^\$(?P<v>\d+(?:,\d{3})*(?:\.\d+)?)\s*/(?P<unit>GPU/hr|hr)$")


def _cells(row: str) -> list[str]:
    # every cell is a div whose first <strong> holds the value; the unit follows it
    return [text(c) for c in _CELLH.findall(row)]


def vultr_cloud_gpu(page: str, at: datetime) -> list[ComputeListing]:
    starts = [(m.start(), text(m["t"])) for m in _H2.finditer(page)]
    sec = next(((s, starts[i + 1][0] if i + 1 < len(starts) else len(page)) for i, (s, t) in enumerate(starts)
                if t == "Cloud GPU"), None)
    if sec is None:
        raise ValueError("vultr(archive): no 'Cloud GPU' section")
    body = page[sec[0]:sec[1]]
    heads = [(m.end(), text(m["t"])) for m in _H3.finditer(body)]
    out = []
    for i, (pos, model) in enumerate(heads):
        if model == "Pricing":
            continue
        end = heads[i + 1][0] if i + 1 < len(heads) else len(body)
        hm = _HEAD.search(body, pos, end)
        if hm is None:
            continue  # a model card without a table ("Contact sales")
        head = tuple(text(c) for c in _CELLH.findall(hm["h"]))
        if head[:6] != HEAD or len(head) != 7 or head[6] not in PRICE_HEADS:
            raise ValueError(f"vultr(archive): {model}: table header {head!r}")
        table_end = body.find('class="pt__header"', hm.end())
        rows = body[hm.end(): min(x for x in (table_end, end) if x != -1)]
        parsed = []
        for rm in _ROW.finditer(rows):
            cells = _cells(rm["r"])
            if len(cells) < 7:
                raise ValueError(f"vultr(archive): {model}: a row has {len(cells)} cells: {cells!r}")
            if not any("$" in c for c in cells[6:]):
                continue  # "Contact Sales"
            if len(cells) != 7:
                raise ValueError(f"vultr(archive): {model}: a priced row has {len(cells)} cells: {cells!r}")
            p = _PRICE.match(cells[6].replace(" ", ""))
            count_m, ram_m = re.match(r"^(\d+)", cells[0]), re.match(r"^(\d+(?:\.\d+)?)\s*GB", cells[1])
            if p is None or count_m is None or ram_m is None:
                raise ValueError(f"vultr(archive): {model}: unreadable row {cells!r}")
            count = int(count_m.group(1))
            parsed.append((cells, p, count, float(ram_m.group(1)) / count))
        # Fractional vGPU sizes ("1" GPU with 10 GB of an 80 GB A100) are not a whole GPU: only rows whose memory per
        # GPU is the card's full memory (the largest in its table) are read, as the live poller skips fractions.
        full = max((r[3] for r in parsed), default=0)
        for cells, p, count, per in parsed:
            if per < full:
                continue
            vram = int(per)
            price = dec(p["v"])
            inst = price * count if p["unit"] == "GPU/hr" else price
            out.append(listing("vultr", sku=f"{model} {count}x", listing_id=slug(f"{model} {count}x {cells[2]}"),
                               raw=f"{model} {vram}GB", count=count, instance_price=inst, at=at,
                               tier="vultr-page",
                               vcpu=int(re.match(r"\d+", cells[2]).group(0)) if re.match(r"\d+", cells[2]) else None))
    if not out:
        raise ValueError("vultr(archive): Cloud GPU section has no priced row")
    return out


SOURCES = [own_page("vultr", "www.vultr.com/pricing", vultr_cloud_gpu)]
