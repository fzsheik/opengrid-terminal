"""Hyperstack: archived captures of www.hyperstack.cloud/gpu-pricing.

Live, OpenGrid reads Hyperstack's API (providers/hyperstack.py, which needs a key). The archived page prints price
cards built from divs: one headed GPU Model | VRAM (GB) | Max pCPUs per GPU | Max RAM (GB) per GPU | Pricing Per
Hour (the on-demand price PER GPU, "$3.20") and one headed GPU Model | Spot VM. Each priced row becomes one listing of
ONE GPU at the printed per-GPU rate (the live API's flavors are 1x..8x of the same per-GPU rate), on_demand or spot.
Reservation, CPU, storage and inference cards are ignored. The design is the same in every capture (Oct 2025 on).

Raw GPU name: the page's model with its VRAM column ("NVIDIA H100 SXM 80GB"; a spot row takes the VRAM printed for
the same model in the on-demand card). The page's names differ from the API's ("H100-80G-PCIe"), so several do not
map in canonical.py today ("NVIDIA H100 80GB" is Hyperstack's PCIe card, but the name alone does not say so).
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

ON_DEMAND = ("GPU Model", "VRAM (GB)", "Max pCPUs per GPU", "Max RAM (GB) per GPU", "Pricing Per Hour")
SPOT = ("GPU Model", "Spot VM")
_CARD = re.compile(r'class="page-price_card_main[^"]*">(?P<c>.*?)(?=class="page-price_card_main|\Z)', re.S)
_HEAD = re.compile(r'class="page-price_card_head df">(?P<h>.*?)</div>\s*</div>', re.S)
_ROW = re.compile(r'class="page-price_card_row_item df">(?P<r>.*?)(?=class="page-price_card_row_item df"|\Z)', re.S)
_COL = re.compile(r'class="page-price_card_row_item_col [^"]*">\s*<span>(?P<v>.*?)</span>', re.S)
_SPAN = re.compile(r"<span>(.*?)</span>", re.S)


def _cards(page: str) -> dict[tuple, list[list[str]]]:
    out: dict[tuple, list[list[str]]] = {}
    for cm in _CARD.finditer(page):
        hm = _HEAD.search(cm["c"])
        if hm is None:
            continue
        head = tuple(text(x) for x in _SPAN.findall(hm["h"]))
        rows = [[text(v) for v in _COL.findall(r["r"])] for r in _ROW.finditer(cm["c"][hm.end():])]
        out.setdefault(head, rows)
    return out


def hyperstack_cards(page: str, at: datetime) -> list[ComputeListing]:
    cards = _cards(page)
    if ON_DEMAND not in cards:
        raise ValueError("hyperstack(archive): no on-demand 'GPU Model ... Pricing Per Hour' card")
    vram: dict[str, str] = {}
    out = []
    for market, head, i_price in (("on_demand", ON_DEMAND, 4), ("spot", SPOT, 1)):
        for cells in cards.get(head, []):
            if len(cells) != len(head):
                raise ValueError(f"hyperstack(archive): a {market} row has {len(cells)} cells: {cells!r}")
            model = cells[0]
            if market == "on_demand":
                vram[model] = cells[1]
            if not re.search(r"\d", cells[i_price]):
                continue  # "Contact us" and the like
            price = dec(cells[i_price])
            if price is None:
                raise ValueError(f"hyperstack(archive): {model} {market} price {cells[i_price]!r} is not '$x.xx'")
            v = vram.get(model, "")
            raw = f"{model} {v}GB" if v.isdigit() else model
            out.append(listing("hyperstack", sku=model, listing_id=slug(model) + ("" if market == "on_demand" else ":spot"),
                               raw=raw, count=1, instance_price=price, at=at, market=market, tier="hyperstack-page"))
    if not out:
        raise ValueError("hyperstack(archive): price cards have no priced row")
    return out


SOURCES = [own_page("hyperstack", "www.hyperstack.cloud/gpu-pricing", hyperstack_cards)]
