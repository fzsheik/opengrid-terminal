"""SaladCloud: archived captures of salad.com/pricing.

Live, OpenGrid reads Salad's API (providers/salad.py, which needs a key): one listing per GPU class per priority
tier (batch / low / medium / high), raw name "RTX 4090 (24 GB)", all interruptible. The archived page had three
designs:

  Oct - Nov 2025   one price per class: "RTX 4090 (24 GB) $ 0.16 per hour". The tier is not stated (it equals the
                   live batch price where both exist), so the listing's tier is recorded as "unstated".
                   -> salad_class_prices
  Dec 2025 - Sep 2026   "$/hr (Batch)" for a GPU WITH 8GB RAM and 4 vCPUs, which Salad billed separately then: a
                   bundle, not the GPU's price, so it is NOT read (it would show a bump that is only the bundle).
  Oct 2026 on      "USD per GPU-hour by priority tier" table: GPU | VRAM | High | Medium | Low | Lowest | $/mo, with
                   vCPU and RAM included, i.e. the live semantics; "Lowest" is the live API's "batch".
                   -> salad_tier_table

Both readers write the raw name in the live shape ("RTX 4090 (24 GB)"), so canonical.py treats both alike.
"""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug, text
from backfill.sources import own_page
from models import ComputeListing

_STRIP = re.compile(r"<(script|style)\b.*?</\1>", re.S)
_CLASS = re.compile(r"(?P<name>[A-Z][\w.\- ]{1,40}?) \((?P<vram>\d+ GB[^)]{0,12})\) \$ (?P<p>\d+(?:\.\d+)?) per hour")
TIER_HEADER = "GPU VRAM High Medium Low Lowest $/mo (Lowest)"
TIERS = (("high", "High"), ("medium", "Medium"), ("low", "Low"), ("batch", "Lowest"))
_TIER_ROW = re.compile(r"\s*(?P<name>[A-Z][\w.\- ]{1,40}?) (?P<vram>\d+) GB"
                       r" \$(?P<high>\d+\.\d+) \$(?P<medium>\d+\.\d+) \$(?P<low>\d+\.\d+) \$(?P<batch>\d+\.\d+)"
                       r" \$[\d,]+\.\d\d")


def _l(name: str, vram: str, price, at: datetime, tier: str) -> ComputeListing:
    raw = f"{name} ({vram})"
    out = listing("salad", sku=raw, listing_id=f"{slug(raw)}:{tier}", raw=raw, count=1, instance_price=price, at=at,
                  tier=tier)
    return out.model_copy(update={"interruptible": True})


def salad_class_prices(page: str, at: datetime) -> list[ComputeListing]:
    t = text(_STRIP.sub(" ", page))
    if "per hour" not in t or "GPU Pricing Calculator" not in t:
        raise ValueError("salad(archive): not the 2025 'GPU Pricing Calculator' design")
    # A group heading ("RTX 4000 Series Cards") runs into the first name of its group in the page text.
    out = [_l(m["name"].rsplit("Cards ", 1)[-1].strip(), m["vram"].strip(), dec(m["p"]), at, "unstated")
           for m in _CLASS.finditer(t)]
    if not out:
        raise ValueError("salad(archive): no 'Name (NN GB) $ x per hour' class price")
    return out


def salad_tier_table(page: str, at: datetime) -> list[ComputeListing]:
    t = text(_STRIP.sub(" ", page))
    i = t.find(TIER_HEADER)
    if i == -1:
        raise ValueError("salad(archive): no 'High | Medium | Low | Lowest' tier table")
    out, pos = [], i + len(TIER_HEADER)
    while True:
        m = _TIER_ROW.match(t, pos)   # rows follow each other with nothing in between; the first non-row ends it
        if m is None:
            break
        pos = m.end()
        for tier, _label in TIERS:
            out.append(_l(m["name"].strip(), f"{m['vram']} GB", dec(m[tier]), at, tier))
    if not out:
        raise ValueError("salad(archive): the tier table has no readable row")
    return out


SOURCES = [own_page("salad", "salad.com/pricing", salad_tier_table, salad_class_prices)]
