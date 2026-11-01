"""Hot Aisle: archived designs of hotaisle.xyz/pricing the live parser (providers/hotaisle.py) rejects.

Each parser rebuilds the live extract()'s payload (one card per plan: kind, plan, counts, gpu, "$x.xx/GPU/hr") and
hands it to HotAisleProvider.normalize, so counts, ids, VM-only and the Michigan country rule are the live ones.

    hotaisle_minified_cards   May - Jul 2026: the live card layout, minified (unclosed <p>), read as text
    hotaisle_headline_price   Apr 2026: plan cards without prices; one headline "$x/GPU/hr. Self-service. No
                              contracts." price. Cards marked BARE METAL are bare metal, the rest VM
    hotaisle_notion           Dec 2025 - Feb 2026: a Notion page: "$x/GPU/hr, no-contract, on-demand" then
                              "1x / 2x, 4x / 8x 192GB MI300x Virtual Machine (on-demand)" items
"""
from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace

from backfill.parsers import text
from backfill.sources import live_page
from models import ComputeListing

_PRICE = r"\$(?P<price>\d+(?:\.\d+)?)\s*/GPU/hr"


def _strip(page: str) -> str:
    return text(re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", page, flags=re.S))


def _normalize(cards: list[dict], page: str, at: datetime) -> list[ComputeListing]:
    from providers.hotaisle import ENDPOINT, HotAisleProvider

    payload = {"cards": cards, "michigan_facility": "Michigan facility" in page}
    return HotAisleProvider.normalize({ENDPOINT: [SimpleNamespace(endpoint=ENDPOINT, payload=payload, fetched_at=at)]})


_CARD = re.compile(r"\b(?P<kind>VM|Bare metal) (?P<plan>Small|Medium|Large) (?P<counts>\d+x(?:\s*(?:&|,|and)\s*\d+x)*) "
                   r"(?P<gpu>MI\d+X?)\s+" + _PRICE, re.I)


def hotaisle_minified_cards(page: str, at: datetime) -> list[ComputeListing]:
    cards = [{"kind": m["kind"], "plan": m["plan"], "counts": m["counts"], "gpu": m["gpu"],
              "price": f"${m['price']}/GPU/hr"} for m in _CARD.finditer(_strip(page))]
    if not cards:
        raise ValueError("hotaisle(archive): no '<VM|Bare metal> <plan> <counts> <gpu> $x/GPU/hr' cards")
    return _normalize(cards, page, at)


_HEADLINE = re.compile(r"Transparent Pricing " + _PRICE + r" \. Self-service\. No contracts\.")
_PLAN = re.compile(r"\b(?P<plan>Small|Medium|Large) (?:POPULAR )?(?P<counts>\d+(?:\s*-\s*\d+)?x) (?P<gpu>MI\d+X?)\b"
                   r"(?P<body>.*?)(?=\bDeploy\b)", re.I)


def hotaisle_headline_price(page: str, at: datetime) -> list[ComputeListing]:
    t = _strip(page)
    head = _HEADLINE.search(t)
    if head is None:
        raise ValueError("hotaisle(archive): no 'Transparent Pricing $x/GPU/hr. Self-service. No contracts.' headline")
    cards = []
    for m in _PLAN.finditer(t, head.end()):
        kind = "Bare metal" if "BARE METAL" in m["body"].upper() else "VM"
        # "2-4x": the printed end points (2 and 4), as the live parser reads "2x & 4x"
        cards.append({"kind": kind, "plan": m["plan"], "counts": m["counts"], "gpu": m["gpu"],
                      "price": f"${head['price']}/GPU/hr"})
    if not cards:
        raise ValueError("hotaisle(archive): headline price but no plan cards")
    return _normalize(cards, page, at)


_NOTION_PRICE = re.compile(r"\bPricing " + _PRICE + r" , no-contract, on-demand\b")
_NOTION_VM = re.compile(r"\b(?P<counts>\d+x(?:, \d+x)*) \d+GB (?P<gpu>MI\d+X?) Virtual Machine \( on-demand \)", re.I)


def hotaisle_notion(page: str, at: datetime) -> list[ComputeListing]:
    t = _strip(page)
    price = _NOTION_PRICE.search(t)
    if price is None:
        raise ValueError("hotaisle(archive): no 'Pricing $x/GPU/hr, no-contract, on-demand' line")
    cards = [{"kind": "VM", "plan": "", "counts": m["counts"], "gpu": m["gpu"], "price": f"${price['price']}/GPU/hr"}
             for m in _NOTION_VM.finditer(t, price.end())]
    if not cards:
        raise ValueError("hotaisle(archive): price line but no 'Virtual Machine (on-demand)' items")
    return _normalize(cards, page, at)


SOURCES = [live_page("hotaisle", "hotaisle", "hotaisle.xyz/pricing",
                     hotaisle_minified_cards, hotaisle_headline_price, hotaisle_notion)]
