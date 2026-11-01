"""Koyeb: archived captures of www.koyeb.com/pricing. Parser kept, NOT registered.

The page prints one card per GPU instance: a name ("A100 SXM", "2x H100", "RTX-4000-SFF-ADA"), "$2.15 /hr" for the
whole instance, and vCPU / RAM / Disk. It prints NO VRAM, and the bare names map to the wrong variant in
canonical.py ("A100 SXM" -> A100 40GB SXM4, while Koyeb's is the 80GB card the live API names
"gpu-nvidia-a100-sxm 80GB"; "H200" -> H200 SXM5, which live deliberately leaves unmapped). So every listing here is
built with canonical_gpu_name None (stored unmapped, never charted) and the source is not in SOURCES: registering it
gains nothing until names carrying the VRAM exist. 31 captures (Nov 2025 - Oct 2026) parse; e.g. "H100" $3.30/hr in
Nov 2025 vs $2.50/hr in Oct 2026 (the live price).
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, slug, text
from backfill.sources import own_page
from models import ComputeListing

_CARD = re.compile(r'<div class="row items-center gap-3">(?P<name>.*?)</div>\$<!-- -->(?P<p>[\d.,]+)<!-- --> /hr</div>', re.S)
_MULTI = re.compile(r"^(\d+)x\s+(.+)$")
_LABELLED = re.compile(r'text-dark/50">(?P<label>vCPU|RAM|Disk)</div><div class="typo-body-lg">(?P<v>[^<]+)</div>')


def koyeb_cards(page: str, at: datetime) -> list[ComputeListing]:
    out = []
    cards = list(_CARD.finditer(page))
    for i, m in enumerate(cards):
        rest = page[m.end(): cards[i + 1].start() if i + 1 < len(cards) else m.end() + 1500]
        name = text(re.sub(r"<span.*?</span>", "", m["name"], flags=re.S))   # drop the "Popular" badge
        mm = _MULTI.match(name)
        count, model = (int(mm.group(1)), mm.group(2)) if mm else (1, name)
        price = dec(m["p"])
        if price is None or price <= 0:
            continue
        spec: dict[str, str] = {}
        for x in _LABELLED.finditer(rest):
            spec.setdefault(x["label"], x["v"])       # this card's own labels come first
        out.append(ComputeListing(
            provider="koyeb", sku=name, listing_id=slug(name), raw_gpu_name=model, canonical_gpu_name=None,
            gpu_count=count, region=None, country=None, price_per_gpu_hour=price / count,
            price_per_instance_hour=price, currency="USD", market_type="on_demand", provider_tier="koyeb-page",
            interruptible=False, available=None, capacity=None, capacity_unit=None,
            vcpu=int(spec["vCPU"]) if spec.get("vCPU", "").isdigit() else None,
            ram_gb=float(spec["RAM"][:-2]) if re.fullmatch(r"\d+GB", spec.get("RAM", "")) else None,
            storage_gb=float(spec["Disk"][:-2]) if re.fullmatch(r"\d+GB", spec.get("Disk", "")) else None,
            observed_at=at))
    if not out:
        raise ValueError("koyeb(archive): no priced instance card")
    return out


PAGE_SOURCE = own_page("koyeb", "www.koyeb.com/pricing", koyeb_cards)
SOURCES: list = []
