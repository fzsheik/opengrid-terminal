"""OVHcloud: archived captures of www.ovhcloud.com/en/public-cloud/prices/ (the international site, priced in USD).

Live, OpenGrid reads OVHcloud's USD catalogues (providers/ovhcloud.py: "WS" international and "US"). The archived
page prints the same flavors as tables: one <tr data-planCode="<flavor>.consumption"> per hourly flavor, under a
header Name | Memory | vCore | GPU | Storage | ... | Price [| Launch price | Price from <date>]. Only tables with a
GPU column are read; the GPU cell reads "2×A100 80 GB" (count × model, VRAM). Each row becomes one on-demand
listing at the regular "Price" column ("$6.15 /hour", for the whole instance), never a promotional column.

From late Aug 2026 the page is rendered from embedded plan objects instead ({"name", "planCode", "prices": [{"os":
"linux", "prices": {"hour": {"formatted": "$2.99", "price": 2.99}}}], "specifications": [{"id": "gpu", "value":
"1x H100 (80 GB HBM2)"}, ...]}); ovh_plan_json reads those: instance flavors only (not the ai-training.* /
ai-deploy.* products on the same page), the Linux hourly price, priced in "$" only.

The raw GPU name is the live one's shape (model + "<VRAM>GB": "A100 80GB"), so canonical.py treats both alike.
listing_id is "page:<flavor>": the page does not say which catalogue (WS / US) it shows, though its prices match WS.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal

from backfill.parsers import dec, listing, text
from backfill.sources import own_page
from models import ComputeListing

_TABLE = re.compile(r"<table[^>]*>(?P<t>.*?)</table>", re.S)
_TH = re.compile(r"<th[^>]*>(?P<c>.*?)</th>", re.S)
_ROW = re.compile(r'<tr[^>]*data-planCode="(?P<plan>[^"]+)\.consumption"[^>]*>(?P<r>.*?)</tr>', re.S)
_TD = re.compile(r"<td[^>]*>(?P<c>.*?)</td>", re.S)
_GPU = re.compile(r"^(?:(?P<n>\d+)\s*×\s*)?(?P<model>.+?)\s+(?P<vram>\d+)\s*GB$")
_PRICE = re.compile(r"^\$\s*(?P<v>\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*hour$")
_GB = re.compile(r"^(\d+(?:\.\d+)?)\s*GB$")


def ovh_price_tables(page: str, at: datetime) -> list[ComputeListing]:
    out, tables = [], 0
    for tm in _TABLE.finditer(page):
        t = tm["t"]
        head = [text(c) for c in _TH.findall(t)]
        if "GPU" not in head or "Name" not in head or "Price" not in head:
            continue
        tables += 1
        i_name, i_gpu, i_price = head.index("Name"), head.index("GPU"), head.index("Price")
        i_mem = head.index("Memory") if "Memory" in head else None
        i_vcore = head.index("vCore") if "vCore" in head else None
        for rm in _ROW.finditer(t):
            cells = [text(c) for c in _TD.findall(rm["r"])]
            if len(cells) <= max(i_name, i_gpu, i_price):
                raise ValueError(f"ovhcloud(archive): {rm['plan']}: {len(cells)} cells under a {len(head)}-column header")
            flavor = cells[i_name]
            if flavor != rm["plan"]:
                raise ValueError(f"ovhcloud(archive): row {rm['plan']!r} names itself {flavor!r}")
            g = _GPU.match(cells[i_gpu])
            if g is None:
                raise ValueError(f"ovhcloud(archive): {flavor}: GPU cell {cells[i_gpu]!r} is not '[n×]model NN GB'")
            if not re.search(r"\d", cells[i_price]):
                continue  # no published hourly price
            p = _PRICE.match(cells[i_price])
            if p is None:
                raise ValueError(f"ovhcloud(archive): {flavor}: price cell {cells[i_price]!r} is not '$x /hour'")
            count = int(g["n"] or 1)
            mem = _GB.match(cells[i_mem]) if i_mem is not None else None
            out.append(listing("ovhcloud", sku=flavor, listing_id=f"page:{flavor}", raw=f"{g['model']} {g['vram']}GB",
                               count=count, instance_price=dec(p["v"]), at=at, tier="vm",
                               vcpu=int(cells[i_vcore]) if i_vcore is not None and cells[i_vcore].isdigit() else None,
                               ram_gb=float(mem.group(1)) if mem else None))
    if not tables:
        raise ValueError("ovhcloud(archive): no price table with a GPU column")
    if not out:
        raise ValueError("ovhcloud(archive): GPU tables have no priced flavor")
    return out


_PLAN = re.compile(r'\{"availability":\{')
_FLAVOR = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")   # an instance flavor code: "h100-380", "t1-le-45"
_SPEC_GPU = re.compile(r"^(?P<n>\d+)\s*x\s+(?P<model>.+?)\s+\((?P<vram>\d+)\s*GB\b[^)]*\)$")


def ovh_plan_json(page: str, at: datetime) -> list[ComputeListing]:
    dec_, out, seen, plans = json.JSONDecoder(), [], set(), 0
    for m in _PLAN.finditer(page):
        try:
            o, _ = dec_.raw_decode(page, m.start())
        except ValueError:
            continue
        if not isinstance(o, dict) or "planCode" not in o or not isinstance(o.get("specifications"), list):
            continue
        if not _FLAVOR.match(str(o["planCode"])):
            continue  # another product on the page (ai-training.*, ai-deploy.*): not a Public Cloud instance
        plans += 1
        spec = {x.get("id"): x.get("value") for x in o["specifications"] if isinstance(x, dict)}
        if not spec.get("gpu"):
            continue  # a CPU flavor
        g = _SPEC_GPU.match(spec["gpu"])
        if g is None:
            raise ValueError(f"ovhcloud(archive): {o['planCode']}: gpu spec {spec['gpu']!r} is not 'Nx model (NN GB ...)'")
        linux = [p for p in o.get("prices") or [] if isinstance(p, dict) and p.get("os") == "linux"]
        hour = ((linux[0].get("prices") or {}).get("hour") or {}) if len(linux) == 1 else None
        if hour is None:
            raise ValueError(f"ovhcloud(archive): {o['planCode']}: {len(linux)} linux price entries, expected 1")
        if not (hour.get("formatted") or "").startswith("$"):
            continue  # a copy of the list in another currency
        price = hour.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError(f"ovhcloud(archive): {o['planCode']}: hourly price is not a number: {price!r}")
        if price <= 0 or o["planCode"] in seen:
            continue
        seen.add(o["planCode"])
        mem = _GB.match(spec.get("memory") or "")
        out.append(listing("ovhcloud", sku=o["planCode"], listing_id=f"page:{o['planCode']}",
                           raw=f"{g['model']} {g['vram']}GB", count=int(g["n"]), instance_price=Decimal(str(price)),
                           at=at, tier="vm", vcpu=int(spec["vcore"]) if str(spec.get("vcore") or "").isdigit() else None,
                           ram_gb=float(mem.group(1)) if mem else None))
    if not plans:
        raise ValueError("ovhcloud(archive): no embedded plan objects")
    if not out:
        raise ValueError("ovhcloud(archive): plan objects have no priced GPU flavor")
    return out


SOURCES = [own_page("ovhcloud", "www.ovhcloud.com/en/public-cloud/prices/", ovh_price_tables, ovh_plan_json)]
