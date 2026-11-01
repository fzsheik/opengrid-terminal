"""Jarvislabs: archived designs of jarvislabs.ai/pricing the live parser (providers/jarvislabs.py) rejects.

    jarvislabs_two_tables   Dec 2025 - Apr 2026: a "Managed Workbench Instances" table (GPU Type, Generation, VRAM,
                            RAM, vCPUs, $/hour) and a VM table (Dec: with "Number of GPUs"; Apr: under "Prices shown
                            per GPU."). The workbench table is read as one GPU's hourly rate, like the live 1x price,
                            ONLY when every GPU it shares with the VM table's per-GPU rows (1-GPU rows, or the table
                            that says "Prices shown per GPU") has the same price there; otherwise the capture raises.

    live parser, vendor-prefixed   May - Jul 2026 the live layout printed "H100 SXM" where the page (and OpenGrid's
                            live data) now prints "NVIDIA H100 SXM". Archived captures go through the live
                            parser on a copy of the page with "NVIDIA " added to row names that lack it, so names,
                            canonical GPUs and listing ids stay continuous with live data. Nothing else changes.

Captures from Oct - Nov 2025 render prices client-side (no price in the HTML) and cannot be read.

Names follow the live parser: the printed GPU with "NVIDIA " prefixed (the page sells NVIDIA GPUs only; every row
carries the Nvidia logo) and backfill.retired.jarvislabs_page.raw_name() appending the printed VRAM; listing ids
"<slug>:1x" as live.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from backfill.parsers import dec, listing, slug, text
from backfill.sources import Source, live_page
from models import ComputeListing

_TABLE = re.compile(r"<table\b.*?</table>", re.S)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", re.S)
_WORKBENCH = ["GPU Type", "Generation", "VRAM", "RAM", "vCPUs", "$/hour"]


def _tables(page: str) -> list[tuple[list[str], list[dict]]]:
    out = []
    for t in _TABLE.findall(page):
        rows = [[text(c) for c in _CELL.findall(tr)] for tr in _TR.findall(t)]
        if not rows:
            continue
        head = rows[0]
        out.append((head, [dict(zip(head, r)) for r in rows[1:] if len(r) == len(head)]))
    return out


def _gb(cell: str) -> str | None:
    m = re.fullmatch(r"(\d+)\s*GB", cell or "")
    return m.group(1) if m else None


def _base(name: str) -> str:
    return re.sub(r"\s+SXM$", "", name.strip(), flags=re.I).lower()


def jarvislabs_two_tables(page: str, at: datetime) -> list[ComputeListing]:
    from backfill.retired.jarvislabs_page import raw_name

    tables = _tables(page)
    work = [rows for head, rows in tables if head == _WORKBENCH]
    if len(work) != 1:
        raise ValueError(f"jarvislabs(archive): expected one workbench table headed {_WORKBENCH}, found {len(work)}")
    per_gpu: dict[str, Decimal] = {}  # the VM table's per-GPU prices, by base name
    for head, rows in tables:
        if head == _WORKBENCH or "GPU Type" not in head or "$/hour" not in head:
            continue
        if "Number of GPUs" in head:
            per_gpu.update({_base(r["GPU Type"]): dec(r["$/hour"]) for r in rows if r["Number of GPUs"] == "1"})
        elif "Prices shown per GPU" in text(page):
            per_gpu.update({_base(r["GPU Type"]): dec(r["$/hour"]) for r in rows})
    out, checked = [], 0
    for r in work[0]:
        price, vram = dec(r["$/hour"]), _gb(r["VRAM"])
        if price is None or vram is None:
            raise ValueError(f"jarvislabs(archive): unreadable workbench row {r}")
        other = per_gpu.get(_base(r["GPU Type"]))
        if other is not None:
            if other != price:
                raise ValueError(f"jarvislabs(archive): {r['GPU Type']} is ${price} in the workbench table but ${other} "
                                 "per GPU in the VM table; cannot tell what the workbench price covers")
            checked += 1
        name = f"NVIDIA {r['GPU Type'].strip()}"
        ram = _gb(r["RAM"])
        out.append(listing("jarvislabs", sku=f"{name}:1x", listing_id=f"{slug(name)}:1x", raw=raw_name(name, vram),
                           count=1, instance_price=price, at=at,
                           vcpu=int(r["vCPUs"]) if r["vCPUs"].isdigit() else None,
                           ram_gb=float(ram) if ram else None))
    if not checked:
        raise ValueError("jarvislabs(archive): no workbench GPU appears in a per-GPU VM table; per-GPU basis unproven")
    return out


_ROW_NAME = re.compile(r"(<h3[^>]*>)(?!NVIDIA )([^<]+</h3>)")


def _vendor_prefixed_live(live):
    def parse(body: bytes, at: datetime) -> list[ComputeListing]:
        page = body.decode("utf-8", errors="replace")
        return live(_ROW_NAME.sub(r"\1NVIDIA \2", page).encode(), at)
    return parse


def _source() -> Source:
    base = live_page("jarvislabs", "jarvislabs", "jarvislabs.ai/pricing", jarvislabs_two_tables)
    (live_name, live), *older = base.parsers
    return Source(base.provider, base.url, ((f"{live_name} + NVIDIA prefix", _vendor_prefixed_live(live)), *older))


SOURCES = [_source()]
