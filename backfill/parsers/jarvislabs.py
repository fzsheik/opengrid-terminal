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

    jarvislabs_docs_tables  Jan 2022 - Jul 2023 (a docs-style page): per family a table GPU | vCPUS | Ram | Reserved |
                            Spot | Weekly | Monthly (Spot and Reserved swap places in 2023), GPU printed "A100 - 40GB",
                            prices "$ 1.10/hr" per GPU (the page: "You can choose upto 8 GPU on a single instance").
                            "Reserved" is the page's word for a non-preemptible hourly instance ("Instances which are
                            not reserved are called as spot or preemptible instances"), so it is read as on-demand;
                            Spot as spot ("<slug>:1x:spot"). Weekly / monthly columns and the bare-metal commitment
                            table (7 - 365 days) are never read.
    jarvislabs_cards        Aug 2021 - Jan 2022: "GPU Virtual Machines", one card per GPU VM: name ("NVIDIA QUADRO
                            RTX 5000"), "16 GB of GPU Memory", "$ 0.49 per hour". Read as one GPU on demand; ids are
                            the printed names' ("nvidia-quadro-rtx-5000:1x"), not mapped onto the later design's.

Captures from 2024 show one GPU at a time in a picker, and those from Jan - Nov 2025 render prices client-side (no
price in the HTML); neither can be read.

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


_DOCS_GPU = re.compile(r"^(?P<name>[A-Za-z0-9 ]+?) - (?P<vram>\d+)\s?GB$")
_PER_HOUR = re.compile(r"^\$\s*(\d+(?:\.\d+)?)/hr$")


def jarvislabs_docs_tables(page: str, at: datetime) -> list[ComputeListing]:
    from backfill.retired.jarvislabs_page import raw_name

    out, read = [], 0
    for head, rows in _tables(page):
        if not head or head[0] != "GPU" or "Reserved" not in head or "Spot" not in head:
            continue  # the bare-metal commitment table, or anything else
        read += 1
        for r in rows:
            m = _DOCS_GPU.match(r["GPU"])
            if m is None:
                raise ValueError(f"jarvislabs(docs): unreadable GPU cell {r['GPU']!r}")
            name = f"NVIDIA {m.group('name').strip()}"
            ram = _gb(r.get("Ram", ""))
            for col, market in (("Reserved", "on_demand"), ("Spot", "spot")):
                p = _PER_HOUR.match(r[col].strip())
                if p is None:
                    if r[col].strip() in ("", "-"):
                        continue
                    raise ValueError(f"jarvislabs(docs): {name} {col} cell is not '$x/hr': {r[col]!r}")
                price = Decimal(p.group(1))
                if price <= 0:
                    continue
                out.append(listing("jarvislabs", sku=f"{name}:1x", raw=raw_name(name, m.group("vram")), count=1,
                                   listing_id=f"{slug(name)}:1x" + (":spot" if market == "spot" else ""),
                                   instance_price=price, at=at, market=market,
                                   vcpu=int(r["vCPUS"]) if r.get("vCPUS", "").isdigit() else None,
                                   ram_gb=float(ram) if ram else None))
    if not read:
        raise ValueError("jarvislabs(docs): no 'GPU | ... | Reserved | Spot' table")
    return out


_CARD = re.compile(r'<div[^>]*class="[^"]*\bcard-body\b[^"]*"[^>]*>(?P<c>.*?)</div>', re.S)
_CARD_NAME = re.compile(r"<h4[^>]*card-header[^>]*>(.*?)</h4>", re.S)
_CARD_VRAM = re.compile(r"<h6[^>]*card-subtitle[^>]*>(.*?)</h6>", re.S)
_CARD_PRICE = re.compile(r"<h6[^>]*>\s*\$(.*?)</h6>", re.S)


def jarvislabs_cards(page: str, at: datetime) -> list[ComputeListing]:
    from backfill.retired.jarvislabs_page import raw_name

    out = []
    for m in _CARD.finditer(page):
        c = m.group("c")
        name, vram, price = _CARD_NAME.search(c), _CARD_VRAM.search(c), _CARD_PRICE.search(c)
        if name is None:
            continue
        v = re.fullmatch(r"(\d+)\s?GB of GPU Memory", text(vram.group(1)) if vram else "")
        p = re.fullmatch(r"(\d+(?:\.\d+)?) per hour", text(price.group(1)) if price else "")
        if v is None or p is None:
            raise ValueError(f"jarvislabs(cards): card {text(name.group(1))!r} lacks 'NN GB of GPU Memory' or '$x per hour'")
        gpu = text(name.group(1))
        out.append(listing("jarvislabs", sku=f"{gpu}:1x", listing_id=f"{slug(gpu)}:1x", raw=raw_name(gpu, v.group(1)),
                           count=1, instance_price=Decimal(p.group(1)), at=at))
    if not out:
        raise ValueError("jarvislabs(cards): no GPU card")
    return out


def _source() -> Source:
    base = live_page("jarvislabs", "jarvislabs", "jarvislabs.ai/pricing", jarvislabs_two_tables, jarvislabs_docs_tables,
                     jarvislabs_cards)
    (live_name, live), *older = base.parsers
    return Source(base.provider, base.url, ((f"{live_name} + NVIDIA prefix", _vendor_prefixed_live(live)), *older))


SOURCES = [_source()]
