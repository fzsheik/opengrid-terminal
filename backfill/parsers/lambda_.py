"""Lambda: archived captures of lambda.ai/pricing (OpenGrid's live Lambda data comes from the instance-types API,
which needs a key, so this page is read only from the archive).

The page's "Instances pricing" block has one tab per instance size ("8x", "4x", "2x", "1x"), each a table with one
row per GPU: Plan, VRAM/GPU, vCPUs, RAM, STORAGE, PRICE/GPU/HR*. The price is per GPU, so the instance price is
derived (x GPU count), the inverse of the live API (cents per instance). Only tabs named "<N>x" are read: the
cluster tabs (DURATION, GPU COUNT columns) are multi-week commitments, not on-demand instances.

Page names are translated to the gpu_description the live API reports for the same product (PAGE_TO_API), so
archived and live listings share a canonical GPU. The page states SXM / PCIe and VRAM, which is what makes each
translation unambiguous; a (plan, VRAM) pair not in the table keeps the page's own name (and maps only if
canonical.py knows it).
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

# (plan as printed, VRAM GB per GPU as printed) -> the live API's gpu_description for that product.
PAGE_TO_API = {
    ("NVIDIA B200 SXM6", 180): "B200 (180 GB SXM6)",
    ("NVIDIA H100 SXM", 80): "H100 (80 GB SXM5)",          # Lambda's only H100 SXM is SXM5
    ("NVIDIA H100 PCIe", 80): "H100 (80 GB PCIe)",
    ("NVIDIA A100 SXM", 80): "A100 (80 GB SXM4)",          # A100 SXM exists only as SXM4
    ("NVIDIA A100 SXM", 40): "A100 (40 GB SXM4)",
    ("NVIDIA A100 PCIe", 40): "A100 (40 GB PCIe)",
    ("NVIDIA GH200", 96): "GH200 (96 GB)",
    ("NVIDIA Tesla V100", 16): "Tesla V100 (16 GB)",
    ("NVIDIA A6000", 48): "A6000 (48 GB)",
    ("NVIDIA A10", 24): "A10 (24 GB PCIe)",
    ("NVIDIA Quadro RTX 6000", 24): "RTX 6000 (24 GB)",
}

_TAB = re.compile(r'<button[^>]*role="tab"[^>]*aria-controls="(?P<id>[^"]+)"[^>]*>(?P<label>[^<]+)</button>'
                  r'|<button[^>]*aria-controls="(?P<id2>[^"]+)"[^>]*role="tab"[^>]*>(?P<label2>[^<]+)</button>')
_ROW = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_CELL = re.compile(r'<t[hd][^>]*data-label="(?P<label>[^"]+)"[^>]*>(?P<v>.*?)</t[hd]>', re.S)
_COUNT_TAB = re.compile(r"^(\d+)x$")
_VRAM = re.compile(r"^(\d+(?:\.\d+)?)\s*GB$")
_GIB = re.compile(r"^(\d+(?:\.\d+)?)\s*GiB$")
_STORAGE = re.compile(r"^(\d+(?:\.\d+)?)\s*(TiB|GiB)\b")
_PRICE_LABEL = "PRICE/GPU/HR*"


def _panel(page: str, panel_id: str, after: int) -> str:
    """The panel a tab controls: the first element with that id AFTER the tab button (some designs reuse panel
    ids between the cluster tab set and the instance tab set)."""
    at = page.find(f'id="{panel_id}"', after)
    if at == -1:
        raise ValueError(f"lambda(archive): tab panel {panel_id!r} missing")
    nxt = page.find('role="tabpanel"', at + 10)
    return page[at: nxt if nxt != -1 else len(page)]


def _row(cells: dict, count: int, at: datetime) -> ComputeListing | None:
    """One labelled row (Plan, VRAM/GPU, vCPUs, RAM, STORAGE, PRICE/GPU/HR*) of a <count>x instance; None when it
    publishes no price."""
    vm = _VRAM.match(cells["VRAM/GPU"])
    if vm is None:
        raise ValueError(f"lambda(archive): unreadable VRAM {cells['VRAM/GPU']!r}")
    price = dec(cells[_PRICE_LABEL])
    if price is None:
        if re.search(r"\d", cells[_PRICE_LABEL]):
            raise ValueError(f"lambda(archive): unreadable price {cells[_PRICE_LABEL]!r}")
        return None  # "—" / contact sales: no published price
    plan, vram = cells["Plan"], float(vm.group(1))
    raw = PAGE_TO_API.get((plan, int(vram)) if vram.is_integer() else None, f"{plan} {cells['VRAM/GPU']}")
    ram = _GIB.match(cells.get("RAM", ""))
    st = _STORAGE.match(cells.get("STORAGE", ""))
    storage = None if st is None else float(st.group(1)) * (1024 if st.group(2) == "TiB" else 1)
    return listing(
        "lambda", sku=f"{plan} {cells['VRAM/GPU']}:{count}x",
        listing_id=f"{slug(plan)}-{slug(cells['VRAM/GPU'])}:{count}x", raw=raw, count=count,
        instance_price=price * count, at=at,
        vcpu=int(cells["vCPUs"]) if cells.get("vCPUs", "").isdigit() else None,
        ram_gb=float(ram.group(1)) if ram else None, storage_gb=storage)


def instance_tabs(page: str, at: datetime) -> list[ComputeListing]:
    """The tabbed "Instances pricing" tables (one tab per instance size)."""
    tabs = [(m["id"] or m["id2"], text(m["label"] or m["label2"]), m.end()) for m in _TAB.finditer(page)]
    sized = [(pid, int(_COUNT_TAB.match(label).group(1)), end) for pid, label, end in tabs if _COUNT_TAB.match(label)]
    if not sized:
        raise ValueError("lambda(archive): no '<N>x' instance tabs")
    out = []
    for pid, count, end in sized:
        rows = 0
        for tr in _ROW.finditer(_panel(page, pid, end)):
            cells = {c["label"]: text(c["v"]) for c in _CELL.finditer(tr["r"])}
            if "Plan" not in cells:
                continue  # the header row
            if _PRICE_LABEL not in cells or "VRAM/GPU" not in cells:
                raise ValueError(f"lambda(archive): a {count}x row lacks its labelled price or VRAM: {cells}")
            row = _row(cells, count, at)
            if row is not None:
                rows += 1
                out.append(row)
        if not rows:
            raise ValueError(f"lambda(archive): the {count}x tab has no priced rows")
    return out


_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TD = re.compile(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", re.S)
_PLAN = re.compile(r"^On-demand (?P<n>\d+)x (?P<plan>.+)$")


def header_tables(page: str, at: datetime) -> list[ComputeListing]:
    """Oct - Nov 2025: tables whose first row is the column header (VRAM/GPU ... PRICE/GPU/HR*) and whose rows
    name their own size and plan: "On-demand 8x NVIDIA H100 SXM"."""
    out = []
    for t in _TABLE.finditer(page):
        trs = [[text(c["v"]) for c in _TD.finditer(r["r"])] for r in _ROW.finditer(t["t"])]
        if not trs or _PRICE_LABEL not in trs[0] or "VRAM/GPU" not in trs[0]:
            continue
        header = trs[0]
        for cells in trs[1:]:
            m = _PLAN.match(cells[0]) if cells else None
            if m is None:
                continue
            if len(cells) != len(header):
                raise ValueError(f"lambda(archive): row {cells[0]!r} has {len(cells)} cells, header {len(header)}")
            labelled = {**dict(zip(header[1:], cells[1:])), "Plan": m["plan"]}
            row = _row(labelled, int(m["n"]), at)
            if row is not None:
                out.append(row)
    if not out:
        raise ValueError("lambda(archive): no 'On-demand <N>x' rows under a PRICE/GPU/HR* header")
    return out


SOURCES = [own_page("lambda", "lambda.ai/pricing", instance_tabs, header_tables)]
