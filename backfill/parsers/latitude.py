"""Latitude.sh: archived captures of www.latitude.sh/pricing.

Live, OpenGrid reads Latitude through Shadeform's catalogue (providers/shadeform.py, "via-shadeform"). The
archived page instead embeds Latitude's own plans API response in its Next.js payload: one object per plan
({"id": "plan_...", "type": "plans", "attributes": {slug, specs.gpu {count, type}, regions [{name, pricing
{USD {hour}}}]}}). Each GPU plan becomes one on-demand listing per region at the printed USD hourly price for
the whole server (per-GPU = hour / gpu count), as the live Shadeform rows are per instance.

Differences from live, stated: the price is Latitude's own page, not Shadeform's listing of it (they can differ,
Shadeform lagging a repricing); the region is Latitude's region name; the GPU name is Latitude's gpu.type
("NVIDIA H100 80GB NVLink"), which canonical.py does not map today (stored with gpu = null until it does).
Captures without the embedded plans (stub pages, ~28 KB) raise and store nothing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal

from backfill.parsers import listing, slug
from backfill.sources import own_page
from models import ComputeListing

_PLAN = re.compile(r'\{"id":"plan_')


def _plans(page: str) -> list[dict]:
    flat = page.replace('\\"', '"')          # the payload is a JSON string inside a <script> push
    dec, out = json.JSONDecoder(), []
    for m in _PLAN.finditer(flat):
        try:
            o, _ = dec.raw_decode(flat, m.start())
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("type") == "plans" and isinstance(o.get("attributes"), dict):
            out.append(o)
    return out


def latitude_plans_json(page: str, at: datetime) -> list[ComputeListing]:
    plans = _plans(page)
    if not plans:
        raise ValueError("latitude(archive): no embedded plans payload")
    out = []
    for p in plans:
        a = p["attributes"]
        gpu = (a.get("specs") or {}).get("gpu")
        if not isinstance(gpu, dict) or not gpu.get("count"):
            continue  # a CPU plan
        count, gtype = gpu["count"], gpu.get("type")
        if not isinstance(count, int) or not isinstance(gtype, str) or not gtype:
            raise ValueError(f"latitude(archive): GPU plan {a.get('slug')!r} has an unreadable gpu spec: {gpu!r}")
        mem = (a["specs"].get("memory") or {}).get("total")
        cpu = a["specs"].get("cpu") or {}
        vcpu = cpu.get("cores") * cpu.get("count") if isinstance(cpu.get("cores"), int) and isinstance(cpu.get("count"), int) else None
        for r in a.get("regions") or []:
            usd = ((r.get("pricing") or {}).get("USD") or {})
            hour = usd.get("hour")
            if hour is None:
                continue  # no USD hourly price for this region (absent or null)
            if isinstance(hour, bool) or not isinstance(hour, (int, float)):
                raise ValueError(f"latitude(archive): {a.get('slug')} {r.get('name')}: hour is not a number: {hour!r}")
            if hour <= 0:
                continue
            out.append(listing(
                "latitude", sku=a.get("slug") or a.get("name"), listing_id=f"{a.get('slug')}:{slug(r.get('name') or '')}",
                raw=gtype, count=count, instance_price=Decimal(str(hour)), at=at, region=(r.get("name") or None),
                tier="metal/latitude-page", vcpu=vcpu, ram_gb=float(mem) if isinstance(mem, (int, float)) else None))
    if not out:
        raise ValueError("latitude(archive): plans payload has no priced GPU plan")
    return out


# NOT registered: the newest captures disagree with the live (Shadeform) prices for the same SKUs by ~1.7x
# (Oct 2026: g3-h100-small page $3.37/h vs Shadeform $1.99/h; g4-rtx6kpro-large $48 vs $27.28), so storing them
# under provider "latitude" would draw a step at the boundary that is a source change, not a price move. Kept
# ready (27 captures, 25 parsed) in case the two are reconciled; register PAGE_SOURCE to import it.
PAGE_SOURCE = own_page("latitude", "www.latitude.sh/pricing", latitude_plans_json)
SOURCES: list = []
