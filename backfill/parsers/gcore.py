"""Gcore: archived captures of gcore.com/pricing/ai (from Feb 2025) and gcore.com/cloud/ai-gpu (Sep 2023 - Apr 2025).

Live, OpenGrid scrapes gcore.com/pricing/ai (providers/gcore.py): the self-serve bare-metal columns ("Get started")
and the Virtual GPU rows, EUR excl. VAT, converted at the ECB EUR->USD rate in force at fetch. 2026 captures are read by
that live parser unchanged. Older designs:

  gcore_on_demand_columns  pricing/ai, mid - late 2025: the same bare-metal column table, but its price row is
                           "On‑demand/h" ("€22.04 (€2.76 per GPU)") followed by "Reserve 12 / 36 months" rows (never
                           read), and the column's button sits in a row of its own. Read like live: "Get started"
                           columns only, the per-GPU figure must equal price / GPUs to the cent, same listing ids
                           ("bare-metal:8x-h100-sxm") and GPU names ("H100 SXM 80GB"). There is no Virtual GPU table in
                           this design (the "GPU Configuration ... Cost per hour" table below it is Everywhere Inference,
                           which live skips too).
  gcore_ai_gpu_flavors     cloud/ai-gpu, Sep - Dec 2023: "Flavor ID | Server config | GPUs | GPU Memory | Infiniband |
                           Quantity | Price" tables, Bare Metal ("bm3-ai-large-h100-80-8", "8xH100", "80 GB") and
                           Virtual instances ("g3-ai-24-232-1100-h100-80-1"), price "€ 31.24 / 1 hour € 23,212.82 / 1
                           month € ... / 1 year (€3.91 per H100)". The hourly figure is read (whole server / VM); where a
                           per-GPU figure is printed it must agree to the cent, else the row is skipped (logged; some
                           2024 captures print €11.44 for 8 x A100 beside "€1.52 per A100"). A row with only a "Book"
                           button (4 x L40S, 2024) has no price and is skipped. Listing ids in the live shape
                           ("bare-metal:8x-h100", "vm:1x-h100"); GPU names "H100 80GB", "A100 40GB": SXM or PCIe is not
                           stated, so they stay unmapped (as live leaves its Virtual GPU "H100 80GB").

Not read (reported as failures): pricing/ai Feb - mid 2025 and cloud/ai-gpu 2024 - 25, whose GPU tables price only
"1 Month Rolling" and 6 / 12 / 36-month reserved terms per GPU-volume tier (commitments, never read); and
gcore.com/pricing/cloud/ (2023 - 25), whose GPU Instances tables print "From" prices that "vary by location" for
flavours that do not name their GPU (g1-gpu: "Nvidia Graphic Cards").
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal

from backfill.eur import eur_listing, rate
from backfill.parsers import text
from backfill.sources import live_page, own_page
from models import ComputeListing
from providers import gcore

log = logging.getLogger(__name__)

_EUR = r"€\s*(\d+(?:,\d{3})*(?:\.\d+)?)"
_OD_PRICE = re.compile(rf"^{_EUR}\s*\({_EUR}\s+per GPU\)$")
_ROWS_TABLE = re.compile(r"<table\b.*?</table>", re.S)


def _eur(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def gcore_on_demand_columns(page: str, at: datetime) -> list[ComputeListing]:
    table = None
    for m in _ROWS_TABLE.finditer(page):
        rows = gcore._rows(m.group(0))
        labels = {r[0]: r[1:] for r in rows if r}
        if "GPU" in labels and any(k.replace("‑", "-") == "On-demand/h" for k in labels):
            table = rows
            break
    if table is None:
        raise ValueError("gcore(archive): no bare-metal table with an 'On-demand/h' row")
    labels = {r[0].replace("‑", "-"): r[1:] for r in table if r}
    head, buttons = table[0][1:], (table[1][1:] if len(table) > 1 and table[1] and table[1][0] == "" else None)
    if buttons is None or len(buttons) != len(head):
        raise ValueError("gcore(archive): no button row under the model row")
    od, shapes = labels["On-demand/h"], labels["GPU"]
    vram, ram, vcpu = labels.get("VRAM, GB") or [], labels.get("RAM, GB") or [], labels.get("vCPUs") or []
    out, fx_rate = [], None
    for i, model_head in enumerate(head):
        if buttons[i] != "Get started":
            continue  # "Contact sales": not self-serve
        cell = od[i] if i < len(od) else ""
        shape, price = gcore._SHAPE.match(shapes[i] if i < len(shapes) else ""), _OD_PRICE.match(cell)
        if shape is None or price is None:
            raise ValueError(f"gcore(archive): column {model_head!r}: GPU {shapes[i] if i < len(shapes) else None!r} "
                             f"/ on-demand {cell!r} unreadable")
        count, total, per_gpu = int(shape["count"]), _eur(price.group(1)), _eur(price.group(2))
        if count <= 0 or abs(total / count - per_gpu) > Decimal("0.01"):
            raise ValueError(f"gcore(archive): {shapes[i]}: per-GPU €{per_gpu} is not €{total} / {count}")
        model = shape["model"].strip()
        v = gcore._int(vram[i] if i < len(vram) else None)
        name = gcore.raw_gpu_name(model, v // count if v and v % count == 0 else None)
        fx_rate = fx_rate or rate("gcore", at)
        r, c = gcore._int(ram[i] if i < len(ram) else None), gcore._int(vcpu[i] if i < len(vcpu) else None)
        out.append(eur_listing("gcore", sku=f"bare-metal:{shapes[i].replace('×', 'x')}",
                               listing_id=f"bare-metal:{count}x-{gcore._slug(model)}", raw=name, count=count,
                               eur_instance_price=total, at=at, fx_rate=fx_rate, tier="bare-metal", vcpu=c,
                               ram_gb=float(r) if r is not None else None))
    if not out:
        raise ValueError("gcore(archive): no self-serve on-demand column")
    return out


_FLAVOR_HEAD = ("Flavor ID", "Server config", "GPUs", "GPU Memory", "Infiniband Interconnect (Gbit/s)", "Quantity",
                "Price")
_FLAVOR_SHAPE = re.compile(r"^(?P<count>\d+)\s*x\s*(?P<model>[A-Za-z][A-Za-z0-9 ]*?)$")
_FLAVOR_PRICE = re.compile(rf"^{_EUR}\s*/\s*1 hour\b.*?(?:\(€\s*(?P<per>\d+(?:[.,]\d+)?) per (?P<m>[A-Za-z0-9]+)\))?$")
_VM_CONFIG = re.compile(r"^(?P<vcpu>\d+)\s*vCPU\s*/\s*(?P<ram>\d+)\s*GB RAM\b")
_GB = re.compile(r"^(?P<gb>\d+)\s*GB$")
_TH = re.compile(r"<th\b[^>]*>(?P<c>.*?)</th>", re.S)


def gcore_ai_gpu_flavors(page: str, at: datetime) -> list[ComputeListing]:
    out, ids, tables, fx_rate = [], {}, 0, None
    for m in _ROWS_TABLE.finditer(page):
        t = m.group(0)
        rows = gcore._rows(t)
        if not rows or tuple(rows[0][:7]) != _FLAVOR_HEAD:
            continue
        tables += 1
        for r in rows[1:]:
            if not r:
                continue
            if len(r) < 7:
                raise ValueError(f"gcore(archive): flavour row {r!r} is shorter than its header")
            flavor, shape, mem, price = r[0], _FLAVOR_SHAPE.match(r[2]), _GB.match(r[3]), _FLAVOR_PRICE.match(r[6])
            if "€" not in r[6]:
                continue  # no price printed, only the "Book" button (sold through sales)
            if shape is None or mem is None or price is None:
                raise ValueError(f"gcore(archive): {flavor}: GPUs {r[2]!r} / memory {r[3]!r} / price {r[6]!r} unreadable")
            count, model, total = int(shape["count"]), shape["model"].strip(), _eur(price.group(1))
            if price["per"] is not None:
                per = Decimal(price["per"].replace(",", "."))
                if abs(total / count - per) > Decimal("0.01"):
                    log.warning("gcore(archive): %s: per-GPU €%s is not €%s / %d; skipped", flavor, per, total, count)
                    continue  # the page contradicts itself: neither figure is read (as live skips such a column)
            kind = "bare-metal" if flavor.startswith("bm") else "vm"
            listing_id = f"{kind}:{count}x-{gcore._slug(model)}"
            if ids.get(listing_id, flavor) != flavor:
                listing_id = f"{kind}:{flavor}"  # two flavours of one shape (A100 40GB / 80GB): keep them apart
            if listing_id in ids:
                continue  # the same table printed twice (desktop and mobile)
            ids[listing_id] = flavor
            cfg = _VM_CONFIG.match(r[1]) if kind == "vm" else None
            fx_rate = fx_rate or rate("gcore", at)
            out.append(eur_listing("gcore", sku=flavor, listing_id=listing_id,
                                   raw=gcore.raw_gpu_name(model, int(mem["gb"])), count=count,
                                   eur_instance_price=total, at=at, fx_rate=fx_rate, tier=kind,
                                   vcpu=int(cfg["vcpu"]) if cfg else None,
                                   ram_gb=float(cfg["ram"]) if cfg else None))
    if not tables:
        raise ValueError("gcore(archive): no 'Flavor ID ... Price' GPU table")
    if not out:
        raise ValueError("gcore(archive): flavour tables have no priced row")
    return out


SOURCES = [live_page("gcore", "gcore", "gcore.com/pricing/ai", gcore_on_demand_columns),
           own_page("gcore", "gcore.com/cloud/ai-gpu", gcore_ai_gpu_flavors)]
