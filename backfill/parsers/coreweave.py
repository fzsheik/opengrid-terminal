"""CoreWeave: archived designs of www.coreweave.com/pricing the live parser (providers/coreweave.py) rejects."""
from __future__ import annotations

import re
from datetime import datetime

from backfill.parsers import dec, listing, slug
from backfill.sources import live_page
from models import ComputeListing

# --------------------------------------------------------------------------------------------------------------
# CoreWeave, designs before the regional tables (Nov 2025 - Feb 2026): one table, instance price only, no spot.
# --------------------------------------------------------------------------------------------------------------

_CW_SECTION = re.compile(r">On-demand GPU instances</h[24]>(?P<body>.*?)>On-demand CPU instances</h[24]>", re.S)
_CW_META = re.compile(r'class="table-meta-value[^"]*"[^>]*>(?P<v>[^<]*)</div>\s*<div[^>]*>(?P<label>[^<]+)</div>')
# "$49.24 / Hour" in one div (Dec 2025 - Feb 2026) or "$49.24</div><div>/ Hour" (Nov 2025).
_CW_PRICE = re.compile(r'class="table-meta-value[^"]*"[^>]*>\s*\$(?P<v>[\d,]+(?:\.\d+)?)\s*(?:</div>\s*<div>)?/\s*Hour')
_CW_MODEL = re.compile(r'<h3[^>]*class="table-model-name"[^>]*>(?P<m>[^<]+)</h3>')


def coreweave_single_table(page: str, at: datetime) -> list[ComputeListing]:
    from providers.coreweave import _COUNT, raw_name

    sec = _CW_SECTION.search(page)
    if sec is None:
        raise ValueError("coreweave(archive): 'On-demand GPU instances' section not found")
    out = []
    for row in re.split(r'role="listitem"', sec["body"])[1:]:
        models = {m["m"].strip() for m in _CW_MODEL.finditer(row)}
        meta = {m["label"].strip(): m["v"].strip() for m in _CW_META.finditer(row)}
        if "GPU Count" not in meta:
            continue  # a managed service row (SUNK, ...), not an instance shape
        prices = {p["v"] for p in _CW_PRICE.finditer(row)}
        if len(models) != 1 or len(prices) > 1:
            raise ValueError("coreweave(archive): a GPU row has several models or prices")
        if not prices:
            continue  # Contact sales
        cm = _COUNT.match(meta["GPU Count"])
        if cm is None or int(cm.group(1)) == 0:
            continue
        model, count, price = models.pop(), int(cm.group(1)), dec(prices.pop())
        vram = meta.get("VRAM")
        storage = dec(meta.get("Local Storage (TB)"))
        out.append(listing(
            "coreweave", sku=f"{model}:{count}x", listing_id=f"{slug(model)}:{count}x", raw=raw_name(model, vram),
            count=count, instance_price=price, at=at,
            vcpu=int(meta["vCPUs"]) if meta.get("vCPUs", "").isdigit() else None,
            ram_gb=float(meta["System RAM"].replace(",", "")) if dec(meta.get("System RAM")) else None,
            storage_gb=float(storage) * 1024 if storage else None))
    if not out:
        raise ValueError("coreweave(archive): no priced GPU rows")
    return out



SOURCES = [live_page("coreweave", "coreweave", "www.coreweave.com/pricing", coreweave_single_table)]
