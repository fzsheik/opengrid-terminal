"""Replicate: archived designs of replicate.com/pricing the live parser (providers/replicate.py) rejects.

Same semantics as live: the price is for the WHOLE hardware type (its CPUs and RAM included), billed per second; the
hourly rate is the per-second price x 3600 (where the page also prints an hourly figure it must agree to the cent, or
the capture raises), the per-GPU rate that / GPU count. Market on_demand, provider_tier "serverless", raw GPU name the
card name without its "<N>x " prefix and " GPU" suffix ("Nvidia A100 (80GB)"; unmapped where the variant is not
stated, as live). CPU-only hardware gives no listing. Hardware below the "Additional hardware" divider is for
committed-spend contracts only (the page says so) and is never read, as live.

    hardware_table   Jul 2023 - Sep 2025: a table Hardware | Price | GPU | CPU | GPU RAM | RAM ("$x per second ($y per
                     minute)" in Jul 2023, "$x/sec ($y/hr)" from Nov 2023; the second figure must agree). Sep - Oct
                     2023 split the price into "Public model price" | "Private model price": the public one is read (it is
                     the single price printed from Nov 2023). From 2024 each
                     hardware cell prints the hardware id under the name ("gpu-a100-large"): that is the listing id,
                     as live. Before (Nov 2023 - early 2024) only the name is printed; the id is the one the page itself
                     pairs with that name from 2024 (HARDWARE_ID), else the name's slug (no id ever printed for it).
    cards_2022       Jul 2022 - 2023: one card per hardware: "Nvidia T4 GPU" "$0.00055 per second (or, $0.033 per
                     minute)" "4x CPU 16GB GPU RAM 8GB RAM". One GPU per card (no card names a count). The per-minute
                     figure must be the per-second one x 60.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from backfill.parsers import listing, slug, text
from backfill.sources import live_page
from models import ComputeListing

# Hardware names the Nov 2023 table printed without an id -> the id the 2024 page prints under the same name.
HARDWARE_ID = {"Nvidia T4 GPU": "gpu-t4", "Nvidia A40 GPU": "gpu-a40-small", "Nvidia A40 (Large) GPU": "gpu-a40-large",
               "Nvidia A100 (80GB) GPU": "gpu-a100-large", "8x Nvidia A40 (Large) GPU": "gpu-a40-large-8x"}

_MULTI = re.compile(r"^(\d+)x\s+")
_COUNT = re.compile(r"^(\d+)x$")
_GB = re.compile(r"^(\d+(?:\.\d+)?)GB$")


def _raw(name: str) -> str:
    return re.sub(r"\s+GPU$", "", _MULTI.sub("", name)).strip()


def _make(name: str, hw_id: str, count: int, per_second: Decimal, at: datetime, *, cpu=None, ram=None,
          raw: str | None = None) -> ComputeListing:
    named = _MULTI.match(name)
    if count < 1 or (int(named.group(1)) if named else 1) != count:
        raise ValueError(f"replicate(archive): {name!r} says {named.group(1) if named else 1} GPUs, GPU column {count}")
    return listing("replicate", sku=hw_id, listing_id=hw_id, raw=raw or _raw(name), count=count,
                   instance_price=per_second * 3600, at=at, tier="serverless", vcpu=cpu, ram_gb=ram)


_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_THD = re.compile(r"<t[hd][^>]*>(?P<v>.*?)</t[hd]>", re.S)
_SPAN = re.compile(r"<span[^>]*>(?P<v>.*?)</span>", re.S)
_PRICE = re.compile(r"^\$\s*(?P<s>\d+(?:\.\d+)?)\s*/sec\s*\(?\$\s*(?P<h>\d+(?:\.\d+)?)\s*/hr\)?$")
# Jul - Oct 2023: "$0.0002 per second ($0.012 per minute)" / "$0.000225 / sec ($0.0135 / min)"
_PRICE_MIN = re.compile(r"^\$(?P<s>\d+(?:\.\d+)?)\s*(?:per second|/ sec) \(\$(?P<m>\d+(?:\.\d+)?)\s*(?:per minute|/ min)\)$")
_HEAD = ["Hardware", "Price", "GPU", "CPU", "GPU RAM", "RAM"]
_HEAD_PUBLIC_PRIVATE = ["Hardware", "Public model price", "Private model price", "GPU", "CPU", "GPU RAM", "RAM"]
_COUNT_INSIDE = re.compile(r"^(Nvidia) (\d+x) (.+)$")   # Jul 2023: "Nvidia 8x A40 GPU" -> "8x Nvidia A40 GPU"


def _per_second(name: str, cell: str) -> Decimal:
    """The per-second price of a price cell, checked against the hourly or per-minute figure printed with it."""
    pm = _PRICE.match(cell)
    if pm is not None:
        per_s = Decimal(pm["s"])
        if (per_s * 3600).quantize(Decimal("0.01")) != Decimal(pm["h"]).quantize(Decimal("0.01")):
            raise ValueError(f"replicate(archive): {name} ${per_s}/sec x 3600 is not ${pm['h']}/hr")
        return per_s
    mm = _PRICE_MIN.match(cell)
    if mm is None:
        raise ValueError(f"replicate(archive): {name} price {cell!r} is not '$x/sec $y/hr'")
    per_s = Decimal(mm["s"])
    if (per_s * 60).quantize(Decimal("0.0001")) != Decimal(mm["m"]).quantize(Decimal("0.0001")):
        raise ValueError(f"replicate(archive): {name} ${per_s}/sec x 60 is not ${mm['m']}/minute")
    return per_s


def hardware_table(page: str, at: datetime) -> list[ComputeListing]:
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        trs = [[c["v"] for c in _THD.finditer(r["r"])] for r in _TR.finditer(tm["t"])]
        head = [text(c) for c in trs[0]] if trs else []
        if head == _HEAD_PUBLIC_PRIVATE:
            # Sep - Oct 2023: the public-model price is read (the single price column before and after carries it)
            trs = [r[:2] + r[3:] for r in trs]
        elif head != _HEAD:
            continue
        tables += 1
        for raw_cells in trs[1:]:
            cells = [text(c) for c in raw_cells]
            if cells == ["Additional hardware"]:
                break  # committed-spend hardware below
            if len(cells) != len(_HEAD):
                raise ValueError(f"replicate(archive): row {cells!r} has {len(cells)} cells")
            spans = [text(s["v"]) for s in _SPAN.finditer(raw_cells[0])]
            if len(spans) >= 2:
                name, hw_id = spans[0], spans[1]
            else:
                name, hw_id = cells[0], None
            name = _COUNT_INSIDE.sub(r"\2 \1 \3", name)
            per_s = _per_second(name, cells[1])
            cm = _COUNT.match(cells[2])
            if cm is None:
                if cells[2] in ("-", ""):
                    continue  # CPU hardware
                raise ValueError(f"replicate(archive): {name} GPU cell {cells[2]!r}")
            hw_id = hw_id or HARDWARE_ID.get(name) or slug(name)
            cpu, ram = _COUNT.match(cells[3]), _GB.match(cells[5])
            out.append(_make(name, hw_id, int(cm.group(1)), per_s, at, cpu=int(cpu.group(1)) if cpu else None,
                             ram=float(ram.group(1)) if ram else None))
    if not tables:
        raise ValueError("replicate(archive): no 'Hardware | Price | GPU | CPU | GPU RAM | RAM' table")
    if not out:
        raise ValueError("replicate(archive): the hardware table has no GPU row")
    return out


_CARD = re.compile(r"<h4[^>]*>(?P<name>[^<]+)</h4>\s*<p[^>]*>(?P<price>.*?)</p>\s*<p[^>]*>(?P<spec>.*?)</p>", re.S)
_CARD_PRICE = re.compile(r"^\$(?P<s>\d+(?:\.\d+)?) per second \(or, \$(?P<m>\d+(?:\.\d+)?) per minute\)$")
_SPEC = re.compile(r"^(?P<cpu>\d+)x CPU (?:(?P<vram>\d+)GB GPU RAM )?(?P<ram>\d+)GB RAM$")


def cards_2022(page: str, at: datetime) -> list[ComputeListing]:
    out, cards = [], 0
    for m in _CARD.finditer(page):
        name, price, spec = text(m["name"]), text(m["price"]), text(m["spec"])
        pm = _CARD_PRICE.match(price)
        if pm is None:
            if "per second" in price:
                raise ValueError(f"replicate(archive): card {name!r} price {price!r}")
            continue  # an ordinary heading followed by prose
        sm = _SPEC.match(spec)
        if sm is None:
            raise ValueError(f"replicate(archive): card {name!r} spec {spec!r}")
        cards += 1
        per_s = Decimal(pm["s"])
        if (per_s * 60).quantize(Decimal("0.001")) != Decimal(pm["m"]).quantize(Decimal("0.001")):
            raise ValueError(f"replicate(archive): {name} ${per_s}/sec x 60 is not ${pm['m']}/minute")
        if "GPU" not in name:
            if sm["vram"]:
                raise ValueError(f"replicate(archive): card {name!r} states GPU RAM but names no GPU")
            continue  # CPU
        if not sm["vram"]:
            raise ValueError(f"replicate(archive): GPU card {name!r} states no GPU RAM")
        raw = _raw(name)
        raw = re.sub(r" (\d+GB)$", r" (\1)", raw)  # Apr 2023 "Nvidia A100 40GB GPU" -> "Nvidia A100 (40GB)"
        if not re.search(r"\(\d+GB\)", raw) and name not in HARDWARE_ID:
            raw = f"{raw} ({sm['vram']}GB)"  # "Nvidia A100" + "40GB GPU RAM" -> the later page's "Nvidia A100 (40GB)"
        out.append(_make(name, HARDWARE_ID.get(name) or slug(name), 1, per_s, at, cpu=int(sm["cpu"]),
                         ram=float(sm["ram"]), raw=raw))
    if not cards:
        raise ValueError("replicate(archive): no '<hardware> $x per second (or, $y per minute)' card")
    if not out:
        raise ValueError("replicate(archive): no GPU card")
    return out


SOURCES = [live_page("replicate", "replicate", "replicate.com/pricing", hardware_table, cards_2022)]
