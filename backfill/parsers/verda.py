"""Verda (formerly DataCrunch): archived captures of verda.com/pricing (from Jul 2026; datacrunch.io has none).

Live, OpenGrid reads Verda's API (providers/verda.py). The archived page prints instance tables headed
Hardware | CPUs | RAM | GPU VRAM | Price | Spot price, one row per instance type ("8x H100 SXM5 80GB", "$35.41/h",
"$17.70/h"): the whole instance's hourly price, on-demand and spot. The same header is used under several sections:
only tables in the "GPU instances" section (<h2 id="gpu-instances">, which includes its Confidential computing
table) are read; "Serverless containers" (per-container pricing, with CPU rows) is not. A table belongs to the
nearest <h2 id> before it. Each priced cell becomes one listing of the
printed GPU count; the raw name is the Hardware cell without its "Nx " prefix, the same name the live API gives
("H100 SXM5 80GB", "B200 CC SXM6 180GB"), looked up the way the live normalizer does it (providers.verda.canonical_name:
"RTX PRO 6000 96GB" is the Server Edition, per Verda's product page). The serverless-container table (headed "Price (1x GPU)"), CPU and storage
tables are ignored.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing
from providers.verda import canonical_name

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
                o = listing("verda", sku=cells[0], listing_id=slug(f"{cells[0]} {cells[1]}v") + ("" if market == "on_demand" else ":spot"),
                            raw=name, count=count, instance_price=dec(p["v"]), at=at, market=market,
                            tier="verda-page", vcpu=int(cells[1]) if cells[1].isdigit() else None,
                            ram_gb=float(ram.group(1)) if ram else None)
                out.append(o.model_copy(update={"canonical_gpu_name": canonical_name(name)}))
    if not tables:
        raise ValueError("verda(archive): no 'Hardware ... Price | Spot price' instance table")
    if not out:
        raise ValueError("verda(archive): instance tables have no priced row")
    return out


# --------------------------------------------------------------------------------------------------------------
# datacrunch.io/products (DataCrunch, Verda's former name), Sep 2020 - Dec 2021.
# --------------------------------------------------------------------------------------------------------------
# The instance name is Verda's instance type ("1V100.6V", "8A6000.80V", "1A100.22V"; "...M" = bare metal), which is
# what the live API's listing ids are, so it is the listing id. The GPU model is the table's "GPU Model" cell
# (2021) or the name's own code (2020: "4V100.20V" is "4x V100", as the page itself explains under each table).
# Raw name: model + per-GPU VRAM, in the API's spelling where the model is unambiguous ("Tesla V100 16GB",
# "RTX A6000 48GB"); "A100 80GB" does not say SXM or PCIe and stays unmapped.
_DC_CODE = re.compile(r"^(?P<n>\d+)(?P<code>[A-Z0-9]+?)(?:\.(?P<mem>\d+)(?P<form>[SP]))?\.(?P<cpu>\d+)(?P<kind>[VM])$")
DC_MODEL = {"V100": "Tesla V100", "A6000": "RTX A6000", "RTX A6000": "RTX A6000", "A100": "A100",
            "RTX6000": "Quadro RTX 6000", "V100 16GB": "Tesla V100", "A100 80GB": "A100", "A100 40GB": "A100"}
# Page spellings -> the live API's, where they differ only in spacing / prefix.
DC_RAW = {"RTX6000 Ada 48GB": "RTX 6000 Ada 48GB"}
_DC_PLAIN = re.compile(r"^\$?(?P<v>\d+(?:\.\d+)?)(?:/h)?$")
_DC_NUM_GB = re.compile(r"^(?P<v>\d+(?:\.\d+)?)(?:\s*GB)?$")


def _dc_table_rows(page: str):
    for tm in _TABLE.finditer(page):
        rows = [[text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rm["r"], re.S)] for rm in _TR.finditer(tm["t"])]
        if rows and rows[0]:
            yield rows[0], rows[1:]


def datacrunch_products(page: str, at: datetime) -> list[ComputeListing]:
    """Two designs of the instance tables:
      2020 - Jul 2021: Instance | GPUs | vCPU (or CPU) | Mem [GiB] | GPU Mem [GiB] | GPU P2P | Storage | Price [$/h]
                       ("0.65": the whole instance, dollars per hour; GPU Mem is the instance total)
      Sep 2021 - 2024: Instance name | GPU Model | GPU | CPU | RAM | VRAM | [P2P |] Price on demand (On demand
                       price) | weekly / monthly / 6-month / 2-year prices ("$2.20/h"; commitments, never read)
    For most of 2022 the page fills its tables by script (header, no rows): reported, nothing read."""
    out, tables, empty = [], 0, 0
    for head, rows in _dc_table_rows(page):
        h = [x.strip() for x in head]
        hl = [x.lower() for x in h]
        if hl[:1] == ["instance"] and h[-1] == "Price [$/h]":
            name_i, price_i, count_i, model_i = 0, len(h) - 1, h.index("GPUs"), None
            vram_i, total_vram = h.index("GPU Mem [GiB]"), True
        elif hl[:1] == ["instance name"] and ("price on demand" in hl or "on demand price" in hl) and "gpu model" in hl:
            price_i = hl.index("price on demand") if "price on demand" in hl else hl.index("on demand price")
            name_i, count_i, model_i = 0, hl.index("gpu"), hl.index("gpu model")
            vram_i, total_vram = hl.index("vram"), True
        else:
            continue
        tables += 1
        if not [r for r in rows if [c.lower() for c in r] != hl]:
            empty += 1
            continue
        for cells in rows:
            if [c.lower() for c in cells] == hl:
                continue  # the header repeated (2023: desktop and mobile layouts in one table)
            if len(cells) != len(h):
                raise ValueError(f"datacrunch(archive): row {cells[:1]} has {len(cells)} cells, header {len(h)}")
            if cells[name_i].startswith("CPU.") and cells[count_i] == "0":
                continue  # a CPU-only type listed in a GPU table
            code = _DC_CODE.match(cells[name_i])
            if code is None or not cells[count_i].isdigit() or int(cells[count_i]) != int(code["n"]):
                raise ValueError(f"datacrunch(archive): instance {cells[name_i]!r} / GPUs {cells[count_i]!r}")
            count = int(code["n"])
            price_cell = cells[price_i]
            pm = _DC_PLAIN.match(price_cell)
            if pm is None:
                if re.search(r"\d", price_cell):
                    raise ValueError(f"datacrunch(archive): {cells[name_i]} price {price_cell!r}")
                continue  # Coming Soon
            vm = _DC_NUM_GB.match(cells[vram_i])
            if vm is None:
                raise ValueError(f"datacrunch(archive): {cells[name_i]} VRAM {cells[vram_i]!r}")
            per_gpu_vram = float(vm["v"]) / count if total_vram else float(vm["v"])
            model = re.sub(r"^(?:NVidia|NVIDIA|Nvidia)\s+", "", cells[model_i] if model_i is not None else code["code"])
            base = re.sub(r"\s*\d+GB$", "", model)
            base = DC_MODEL.get(model, DC_MODEL.get(base, base))
            if base == "A100" and code["form"] == "S":
                base = "A100 SXM4"  # the type code says it: "1A100.40S.22V" is the API's "A100 SXM4 40GB"
            raw = DC_RAW.get(f"{base} {per_gpu_vram:g}GB", f"{base} {per_gpu_vram:g}GB")
            o = listing("verda", sku=cells[name_i], listing_id=cells[name_i], raw=raw, count=count,
                        instance_price=dec(pm["v"]), at=at, tier="datacrunch-page")
            out.append(o.model_copy(update={"canonical_gpu_name": canonical_name(raw)}))
    if not tables:
        raise ValueError("datacrunch(archive): no instance table")
    if not out:
        if empty == tables:
            raise ValueError("datacrunch(archive): instance tables are empty (filled by script)")
        raise ValueError("datacrunch(archive): instance tables have no priced row")
    return out


SOURCES = [own_page("verda", "verda.com/pricing", verda_instance_tables),
           own_page("verda", "datacrunch.io/products", datacrunch_products)]
