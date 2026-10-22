"""Verda (formerly DataCrunch): archived captures of verda.com/pricing (from Jul 2026; datacrunch.io has none).

Live, OpenGrid reads Verda's API (providers/verda.py). The archived page prints instance tables headed
Hardware | CPUs | RAM | GPU VRAM | Price | Spot price, one row per instance type ("8x H100 SXM5 80GB", "$35.41/h",
"$17.70/h"): the whole instance's hourly price, on-demand and spot. The same header is used under several sections:
only tables in the "GPU instances" section (<h2 id="gpu-instances">, which includes its Confidential computing
table) are read; "Serverless containers" (per-container pricing, with CPU rows) is not. A table belongs to the
nearest <h2 id> before it. Each priced cell becomes one listing of the
printed GPU count; the raw name is the Hardware cell without its "Nx " prefix, the same name the live API gives
("H100 SXM5 80GB", "B200 CC SXM6 180GB"). The serverless-container table (headed "Price (1x GPU)"), CPU and storage
tables are ignored.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

HEADER = ("Hardware", "CPUs", "RAM", "GPU VRAM", "Price", "Spot price")
_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TH = re.compile(r"<th[^>]*>(?P<c>.*?)</th>", re.S)
_TR = re.compile(r"<tr[^>]*>(?P<r>.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(?P<c>.*?)</td>", re.S)
_HW = re.compile(r"^(?P<n>\d+)x\s+(?P<name>.+)$")
_PRICE = re.compile(r"^\$(?P<v>\d+(?:,\d{3})*(?:\.\d+)?)/h$")
_GB = re.compile(r"^(\d+(?:\.\d+)?)\s*GB$")


_H2 = re.compile(r'<h2\b[^>]*\bid="(?P<id>[^"]+)"')
READ = ("gpu-instances",)   # its Confidential computing sub-table included; not "serverless-containers" etc.


def _section(page: str, at: int) -> str | None:
    last = None
    for m in _H2.finditer(page, 0, at):
        last = m["id"]
    return last


def verda_instance_tables(page: str, at: datetime) -> list[ComputeListing]:
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        if tuple(text(c) for c in _TH.findall(tm["t"])) != HEADER or _section(page, tm.start()) not in READ:
            continue
        tables += 1
        for rm in _TR.finditer(tm["t"]):
            cells = [text(c) for c in _TD.findall(rm["r"])]
            if not cells:
                continue  # the header row
            if len(cells) != len(HEADER):
                raise ValueError(f"verda(archive): a row has {len(cells)} cells: {cells!r}")
            hw = _HW.match(cells[0])
            if hw is None:
                raise ValueError(f"verda(archive): hardware cell {cells[0]!r} is not 'Nx <GPU>'")
            count, name = int(hw["n"]), hw["name"]
            ram = _GB.match(cells[2])
            for market, cell in (("on_demand", cells[4]), ("spot", cells[5])):
                if not re.search(r"\d", cell):
                    continue  # no price for this market
                p = _PRICE.match(cell)
                if p is None:
                    raise ValueError(f"verda(archive): {cells[0]} {market} cell {cell!r} is not '$x/h'")
                out.append(listing("verda", sku=cells[0], listing_id=slug(f"{cells[0]} {cells[1]}v") + ("" if market == "on_demand" else ":spot"),
                                   raw=name, count=count, instance_price=dec(p["v"]), at=at, market=market,
                                   tier="verda-page", vcpu=int(cells[1]) if cells[1].isdigit() else None,
                                   ram_gb=float(ram.group(1)) if ram else None))
    if not tables:
        raise ValueError("verda(archive): no 'Hardware ... Price | Spot price' instance table")
    if not out:
        raise ValueError("verda(archive): instance tables have no priced row")
    return out


SOURCES = [own_page("verda", "verda.com/pricing", verda_instance_tables)]
