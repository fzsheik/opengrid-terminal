"""CUDO Compute: archived captures of www.cudocompute.com/pricing and /products/gpu-cloud (2023 - 2025).

Live, OpenGrid reads CUDO's machine-type API (providers/cudo.py on branch providers/batch-nokey): a 1-GPU VM priced
a la carte as gpuPriceHr + the smallest allowed vCPU and RAM, listing id "vm:<machineType>@<data centre>", raw name
the API's gpuModelId ("nvidia-h100"), provider_tier "vm".

The archived pages print a table GPU model | On-demand cost | 1 month | 3 months | 6 months, one row per GPU:
"H100 SXM 80GB HBM2e (3.35 TB/s bandwidth) | from $2.45 /hr | from $2.44 /hr Save $7.44 | ...". The on-demand
cell is the GPU component's lowest price across data centres ("from"); vCPUs and memory are priced in the next
table (from $0.0021 / vCPU-hour, $0.0013 / GB-hour), so the full smallest VM is a few cents more. Stored: that
GPU-component price, one GPU, provider_tier "vm-gpu-component", listing id "vm-gpu:<model>" (the page names no
machine type or data centre, so it is never the live listing). Commitment columns are not read; "Quote on
request" rows (the 2026 enterprise page) have no price.

GPU names: the page's model ("H100 SXM 80GB") is translated to the API's gpuModelId where the live module's
names are known (PAGE_TO_API), so the canonical GPU is the live one; others keep the page's name.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.pending_names import canonical_name
from backfill.sources import own_page
from models import ComputeListing

# page model (memory included, bandwidth dropped) -> live gpuModelId
PAGE_TO_API = {"H100 SXM 80GB": "nvidia-h100", "H100 PCIe 94GB": "nvidia-h100-nvl-pcie", "L40S 48GB": "nvidia-l40s",
               "A100 PCIe 80GB": "nvidia-a100-pcie"}
_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd][^>]*>(?P<c>.*?)</t[hd]>", re.S)
_MODEL = re.compile(r"^(?P<m>.+?\d+(?:\.\d+)?(?:GB|TB))\b")
_FROM = re.compile(r"^from \$(?P<v>\d+(?:\.\d+)?) /hr$")


def cudo_gpu_table(page: str, at: datetime) -> list[ComputeListing]:
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        rows = [[text(c) for c in _CELL.findall(r)] for r in _TR.findall(tm["t"])]
        if not rows or rows[0][:2] != ["GPU model", "On-demand cost"]:
            continue
        tables += 1
        for cells in rows[1:]:
            if len(cells) < 2:
                continue
            mm = _MODEL.match(cells[0])
            if mm is None:
                raise ValueError(f"cudo(archive): GPU model cell {cells[0]!r} has no memory")
            fm = _FROM.match(cells[1])
            if fm is None:
                if re.search(r"\d", cells[1]):
                    raise ValueError(f"cudo(archive): on-demand cell {cells[1]!r} is not 'from $x /hr'")
                continue  # "-" / quote on request
            model = re.sub(r"\s+(HBM\w*|GDDR\w*)$", "", mm["m"]).strip()
            raw = PAGE_TO_API.get(model, model)
            o = listing("cudo", sku=f"vm-gpu:{model}", listing_id=f"vm-gpu:{slug(model)}", raw=raw, count=1,
                        instance_price=dec(fm["v"]), at=at, tier="vm-gpu-component")
            out.append(o.model_copy(update={"canonical_gpu_name": canonical_name(raw)}))
    if not tables:
        raise ValueError("cudo(archive): no 'GPU model | On-demand cost' table")
    if not out:
        raise ValueError("cudo(archive): the GPU table has no on-demand price")
    return out


SOURCES = [own_page("cudo", "www.cudocompute.com/pricing", cudo_gpu_table),
           own_page("cudo", "www.cudocompute.com/products/gpu-cloud", cudo_gpu_table)]
