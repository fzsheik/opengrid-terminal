"""Denvr Dataworks: archived captures of www.denvr.com/pricing.

Live, OpenGrid reads Denvr through Shadeform's catalogue (providers/shadeform.py, "via-shadeform"). The archived
page is Denvr's own: a table headed Platform | GPUs | GPU VRAM | vCPUs | Memory | Local Storage | Interconnect |
Price/Hour, whose price cells read "$2.10 / GPU" (per GPU per hour) or "Reserved only". Each priced row becomes
one on-demand listing of the printed GPU count at the printed per-GPU rate.

The raw GPU name is the Platform cell with the VRAM cell appended ("NVIDIA H100 SXM 80GB"), the same convention
as CoreWeave's live parser, so canonical.py decides the variant; Denvr's own page, not Shadeform's listing.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

HEADER = ["Platform", "GPUs", "GPU VRAM", "vCPUs", "Memory", "Local Storage", "Interconnect", "Price/Hour"]
_P = re.compile(r"<p[^>]*>(?P<c>.*?)</p>", re.S)
_ITEM = re.compile(r'role="listitem"')
_PER_GPU = re.compile(r"^\$(\d+(?:\.\d+)?)\s*/\s*GPU$")
_GB = re.compile(r"^(\d+(?:\.\d+)?)\s*GB$")


def _rows(body: str, at: datetime) -> list[ComputeListing]:
    out = []
    for item in _ITEM.split(body)[1:]:
        cells = [text(c) for c in _P.findall(item)]
        if "GPU Count" in cells:
            break   # the mobile layout of the same table follows the desktop rows: already read
        if len(cells) < 8:
            continue
        row = dict(zip(HEADER, cells[:8]))
        price = row["Price/Hour"]
        if not re.search(r"\d", price):
            continue  # "Reserved only"
        m = _PER_GPU.match(price)
        if m is None or not row["GPUs"].isdigit():
            raise ValueError(f"denvr(archive): unreadable row {row!r}")
        count = int(row["GPUs"])
        vram = row["GPU VRAM"].replace(" ", "")
        raw = f"{row['Platform']} {vram}" if _GB.match(row["GPU VRAM"]) else row["Platform"]
        mem = _GB.match(row["Memory"])
        per_gpu = dec(m.group(1))
        out.append(listing("denvr", sku=f"{row['Platform']}:{count}x:{vram}",
                           listing_id=f"{slug(row['Platform'])}:{count}x:{slug(vram)}", raw=raw, count=count,
                           instance_price=per_gpu * count, at=at, tier="denvr-page",
                           vcpu=int(row["vCPUs"]) if row["vCPUs"].isdigit() else None,
                           ram_gb=float(mem.group(1)) if mem else None))
    return out


def denvr_table(page: str, at: datetime) -> list[ComputeListing]:
    """The GPU table: its header, then its rows (a role="list" after the header). In Jul 2025 (www.denvrdata.com)
    the page's markup put the GPU rows' list BEFORE the header and the CPU table's list after it: when the list after
    the header holds no "$x / GPU" row, the nearest list before the header (within its section) is read instead."""
    out, tables = [], 0
    for hm in re.finditer(r"<span[^>]*>\s*Platform\s*</span>", page):
        p_at = page.rfind("<p", 0, hm.start())
        head = [text(c) for c in _P.findall(page[p_at: hm.start() + 3000])][:8]
        if head != HEADER:
            continue
        tables += 1
        lst = page.find('role="list"', hm.end())
        end = page.find("</section>", lst)
        rows = _rows(page[lst: end if end != -1 else len(page)], at)
        if not rows:
            sec = page.rfind("<section", 0, hm.start())
            before = page.rfind('role="list"', sec if sec != -1 else 0, hm.start())
            if before != -1:
                rows = _rows(page[before: hm.start()], at)
        out += rows
    if not tables:
        raise ValueError("denvr(archive): no Platform ... Price/Hour table")
    if not out:
        raise ValueError("denvr(archive): table has no priced row")
    return out


SOURCES = [own_page("denvr", "www.denvr.com/pricing", denvr_table),
           # The same page under Denvr's former domain (2024 - Oct 2025). Its 2024 - May 2025 designs show only
           # "Starting at ... Dedicated" promotion cards (not on-demand prices): reported, never read.
           own_page("denvr", "www.denvrdata.com/pricing", denvr_table)]
