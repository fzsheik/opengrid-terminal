"""Akash Network: archived captures of the Akash Console's GPU market summary,
console-api.akash.network/v1/gpu-prices (captured from Jul 2025), the very response the live poller reads
(providers/akash.py on branch providers/batch-nokey).

Read exactly as live: each GPU row with a USD price summary becomes ONE listing at weightedAverage (the provider-
allocatable-weighted average of each Akash provider's chosen recent bid), provider_tier "weighted", one GPU,
listing id "<vendor>-<model>-<ram>-<interface>", raw name "<vendor> <model> <ram> <interface>". A row without a price
(no bid in 31 days) is not a listing. min / max / avg / med are not read. This is a bid-derived market summary, not
a price list; the live module's docstring states its limits, which hold for every archived copy too.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from backfill.parsers import listing
from backfill.pending_names import canonical_name
from backfill.sources import Source
from models import ComputeListing


def _key(m: dict, sep: str) -> str:
    return sep.join(str(m.get(k) or "").strip() for k in ("vendor", "model", "ram", "interface")).strip(sep)


def akash_gpu_prices(body: bytes, at: datetime) -> list[ComputeListing]:
    d = json.loads(body)
    models = d.get("models") if isinstance(d, dict) else None
    if not isinstance(models, list):
        raise ValueError("akash(archive): gpu-prices has no `models` list")
    out = []
    for m in models:
        if not isinstance(m, dict) or not m.get("model") or not isinstance(m.get("availability"), dict):
            raise ValueError(f"akash(archive): model row lost its name or availability: {str(m)[:120]}")
        price = m.get("price")
        if price is None:
            continue
        if not isinstance(price, dict) or price.get("currency") != "USD":
            raise ValueError(f"akash(archive): price for {_key(m, '-')} is not a USD summary: {str(price)[:120]}")
        try:
            weighted = Decimal(str(price.get("weightedAverage")))
        except InvalidOperation:
            raise ValueError(f"akash(archive): weightedAverage {price.get('weightedAverage')!r}")
        if not weighted.is_finite() or weighted <= 0:
            continue
        raw = _key(m, " ")
        o = listing("akash", sku=_key(m, "-"), listing_id=_key(m, "-"), raw=raw, count=1, instance_price=weighted,
                    at=at, tier="weighted")
        out.append(o.model_copy(update={"canonical_gpu_name": canonical_name(raw)}))
    if not out:
        raise ValueError("akash(archive): gpu-prices has no priced GPU row")
    return out


SOURCES = [Source("akash", "console-api.akash.network/v1/gpu-prices",
                  (("backfill.parsers.akash.akash_gpu_prices (live normalizer's rules)", akash_gpu_prices),))]
