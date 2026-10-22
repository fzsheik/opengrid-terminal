"""Voltage Park: archived captures of www.voltagepark.com/pricing.

Live, OpenGrid reads Voltage Park's API (providers/voltagepark.py). The archived page prints three plan cards
(class "pricing11_plan"): a label ("On-Demand, Ethernet", "On-demand, 3200 Gbps Infiniband", "Long-Term Reserve"), a
price ("$1.99/hr", per GPU-hour, as the live API's bare-metal rates) or "Contact for pricing", and a size line
("1-1016 HGX H100 GPUs"). Each priced on-demand card becomes one listing of the card's smallest size at the printed
per-GPU rate; the GPU is the card's own "HGX H100" (written "NVIDIA HGX H100", NVIDIA's product name). Reserve cards
and "starts at $1.99" marketing copy are not read; from Jul 2026 every card says "Contact for pricing", so those
captures store nothing.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

_CARD = re.compile(r'class="pricing11_plan"')
_PLAN = re.compile(r"^>?\s*(?P<label>On-[Dd]emand[^$]*?)\s+(?:\$(?P<p>\d+(?:\.\d+)?)/hr|Contact for pricing)\b")
_SIZE = re.compile(r"\b(?P<lo>\d+)-\d+\s+(?P<gpu>HGX H100|HGX H200|HGX B200|H100|H200|B200) GPUs\b")


def voltagepark_cards(page: str, at: datetime) -> list[ComputeListing]:
    cards = [text(c[:4000]) for c in _CARD.split(page)[1:]]
    if not cards:
        raise ValueError("voltagepark(archive): no pricing11_plan cards")
    out = []
    for card in cards:
        m = _PLAN.match(card)
        if m is None:
            continue  # Long-Term Reserve
        if m["p"] is None:
            continue  # Contact for pricing
        size = _SIZE.search(card)
        if size is None:
            raise ValueError(f"voltagepark(archive): priced card {m['label']!r} names no 'N-M <GPU> GPUs' size")
        count, gpu = int(size["lo"]), size["gpu"]
        per_gpu = dec(m["p"])
        out.append(listing("voltagepark", sku=m["label"], listing_id=slug(m["label"]), raw=f"NVIDIA {gpu}",
                           count=count, instance_price=per_gpu * count, at=at, tier="voltagepark-page"))
    if not out:
        raise ValueError("voltagepark(archive): no on-demand card has a price")
    return out


SOURCES = [own_page("voltagepark", "www.voltagepark.com/pricing", voltagepark_cards)]
