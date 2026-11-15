"""VESSL AI: archived captures of vessl.ai/en/pricing (the live poller's page; vessl.ai/pricing redirects there).

Live (providers/vessl.py): the "Container Compute" table, required to be stated "per GPU, per hour"; each priced row
is one 1-GPU listing, listing id "<slug(GPU + VRAM)>:1x", raw name "NVIDIA H100 SXM 80GB", tier "container".
No archived capture carries that statement yet, so the live parser reads none of them.

Jun - Aug 2026 (vessl_gpu_tables): "GPU Cloud Pricing" / "GPU Pricing" with an Hourly / Daily / Monthly toggle and
tables headed GPU | VRAM | On-Demand Price (Jun - Jul: one table for "Instantly Available", one for "Available upon
Request") or GPU | VRAM | Architecture | On-Demand | Reserved (Aug). Each row names ONE GPU with its VRAM ("H100 SXM",
"80GB") and prints "$2.39/hr" in the on-demand column: a price per GPU-hour, read only with its "/hr". The reserved
column ("Up to 15% off", "Quoted by term") is never read; "Reserved only" rows have no on-demand price. Names gain the
"NVIDIA " prefix the live page prints, so ids and canonical GPUs join the live series. Every row the live parser would
read (self-serve or "On request") is read alike. The mobile card copies are not read (the tables carry every row).

Not read: vessl.ai/pricing 2022 - Jan 2026 (MLOps plans, no GPU prices; 2024 - 25 only "A100 starting at just
$1.80/h", a floor, not a price list) and the Mar - May 2026 captures (a client-rendered shell with no prices).
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import live_page
from models import ComputeListing

_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", re.S)
_SPAN = re.compile(r"<span[^>]*>(?P<v>[^<]+)</span>")
_PRICE = re.compile(r"^\$(\d+(?:\.\d+)?)\s*/hr$")
_VRAM = re.compile(r"^(\d+)GB$")
_ON_DEMAND = ("On-Demand Price", "On-Demand")


def vessl_gpu_tables(page: str, at: datetime) -> list[ComputeListing]:
    if not re.search(r">\s*Hourly\s*</button>", page):
        raise ValueError("vessl(archive): no Hourly price toggle")
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        rows = [[c["v"] for c in _CELL.finditer(r["r"])] for r in _TR.finditer(tm["t"])]
        head = [text(c) for c in rows[0]] if rows else []
        col = next((head.index(h) for h in _ON_DEMAND if h in head), None)
        if head[:2] != ["GPU", "VRAM"] or col is None:
            continue
        tables += 1
        for cells in rows[1:]:
            if len(cells) != len(head):
                raise ValueError(f"vessl(archive): a row has {len(cells)} cells, header {head}")
            span = _SPAN.search(cells[0])
            gpu = text(span["v"] if span else cells[0])
            vram, price = text(cells[1]).removesuffix(" VRAM"), text(cells[col])
            pm = _PRICE.match(price)
            if pm is None:
                if re.search(r"\d", price):
                    raise ValueError(f"vessl(archive): {gpu} on-demand price {price!r} is not '$x/hr'")
                continue  # "Reserved only": no on-demand price
            vm = _VRAM.match(vram)
            if vm is None:
                raise ValueError(f"vessl(archive): {gpu} VRAM {vram!r} is not one GPU's '<N>GB'")
            name = (gpu if gpu.startswith("NVIDIA ") else f"NVIDIA {gpu}") + f" {vm.group(1)}GB"
            out.append(listing("vessl", sku=gpu if gpu.startswith("NVIDIA ") else f"NVIDIA {gpu}",
                               listing_id=f"{slug(name)}:1x", raw=name, count=1, instance_price=dec(pm.group(1)),
                               at=at, tier="container"))
    if not tables:
        raise ValueError("vessl(archive): no 'GPU | VRAM | On-Demand' table")
    if not out:
        raise ValueError("vessl(archive): the GPU tables have no on-demand price")
    return out


SOURCES = [live_page("vessl", "vessl", "vessl.ai/en/pricing", vessl_gpu_tables)]
