"""Exoscale: archived captures of portal.exoscale.com/api/pricing/opencompute (from Jan 2021), the very price feed
the live poller reads (providers/exoscale.py on branch providers/batch-nokey).

Read exactly as live: the "usd" map's "running_<family>_<size>" hourly rates (whole instance), GPU model / count /
zones per (family, size) from FAMILIES, which is the live module's table (Exoscale's pricing page, read 2026-11-01).
listing id "<family>.<size>", provider_tier "vm". A family or size not in the table (the legacy "gpu" family, a
P100 generation no longer on the page) is skipped, as live. Stated assumption: a family's sizes had the same GPU
counts in 2021 - 2025 as on today's page (the feed itself names no GPU count; gpu2's and gpu3's archived rates
scale with today's counts).

FAMILIES is copied from that branch until it is merged; then it can be imported from providers.exoscale.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from backfill.parsers import listing
from backfill.pending_names import canonical_name
from backfill.sources import Source
from models import ComputeListing

_KEY = re.compile(r"^running_(gpu[a-z0-9]*)_(small|medium|large|huge)$")
# family -> (raw GPU name, zones, {size: (GPUs, vCPUs, RAM GB)}): providers/exoscale.py FAMILIES, 2026-11-01.
FAMILIES: dict[str, tuple[str, tuple[str, ...], dict[str, tuple[int, int, int]]]] = {
    "gpua30": ("Exoscale GPUA30 (NVIDIA A30 24GB)", ("CH-GVA-2",),
               {"small": (1, 12, 56), "medium": (2, 16, 90), "large": (3, 24, 120), "huge": (4, 48, 225)}),
    "gpu2": ("Exoscale GPU2 (Tesla V100 16GB)", ("AT-VIE-1",),
             {"small": (1, 12, 56), "medium": (2, 16, 90), "large": (3, 24, 120), "huge": (4, 48, 225)}),
    "gpu3": ("Exoscale GPU3 (NVIDIA A40 48GB)", ("DE-FRA-1",),
             {"small": (1, 12, 56), "medium": (2, 24, 120), "large": (4, 48, 224), "huge": (8, 96, 448)}),
    "gpua5000": ("Exoscale GPUA5000 (NVIDIA RTX A5000 24GB)", ("AT-VIE-2",),
                 {"small": (1, 12, 56), "medium": (2, 24, 112), "large": (4, 48, 224)}),
    "gpu3080ti": ("Exoscale GPU3080ti (NVIDIA RTX 3080 Ti 12GB)", ("AT-VIE-2",),
                  {"small": (1, 12, 56), "medium": (2, 24, 112), "large": (4, 48, 224)}),
    "gpurtx6000pro": ("Exoscale GPURTX6000PRO (NVIDIA RTX PRO 6000 Blackwell 96GB)",
                      ("DE-FRA-1", "HR-ZAG-1", "CH-DK-2", "AT-VIE-2"),
                      {"small": (1, 36, 120), "medium": (2, 72, 240), "large": (4, 144, 480), "huge": (8, 248, 960)}),
}


def _country(zones: tuple[str, ...]) -> str | None:
    codes = {z.split("-")[0] for z in zones}
    return codes.pop() if len(codes) == 1 else None


def exoscale_opencompute(body: bytes, at: datetime) -> list[ComputeListing]:
    d = json.loads(body)
    usd = d.get("usd") if isinstance(d, dict) else None
    if not isinstance(usd, dict):
        raise ValueError("exoscale(archive): pricing has no `usd` map")
    out = []
    for key, value in sorted(usd.items()):
        m = _KEY.match(key)
        if not m:
            continue  # CPU instances, storage, network
        family, size = m.groups()
        spec = FAMILIES.get(family)
        if spec is None or size not in spec[2]:
            continue  # not in the pricing page's GPU table (legacy "gpu" family): skipped, as live
        try:
            price = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"exoscale(archive): {key} = {value!r}")
        if not price.is_finite() or price <= 0:
            continue
        name, zones, sizes = spec
        gpus, vcpu, ram = sizes[size]
        o = listing("exoscale", sku=f"{family}.{size}", listing_id=f"{family}.{size}", raw=name, count=gpus,
                    instance_price=price, at=at, region="; ".join(zones), country=_country(zones), tier="vm",
                    vcpu=vcpu, ram_gb=float(ram))
        out.append(o.model_copy(update={"canonical_gpu_name": canonical_name(name)}))
    if not out:
        raise ValueError("exoscale(archive): no GPU key the pricing table can read")
    return out


SOURCES = [Source("exoscale", "portal.exoscale.com/api/pricing/opencompute",
                  (("backfill.parsers.exoscale.exoscale_opencompute (live normalizer's rules)", exoscale_opencompute),))]
