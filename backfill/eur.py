"""Archived EUR prices, converted exactly as the live EUR pollers convert (providers/scaleway.py, hetzner.py, gcore.py).

    eur_listing(provider, ..., eur_instance_price, at)   one USD ComputeListing at the ECB EUR->USD rate in force at
                                                         `at` (the capture time), with its FX provenance

fx.usd_rate("EUR", at) is the newest stored ECB rate already published at the capture; none, or one more than
fx.MAX_AGE_DAYS old, raises NoRate (fail closed): the capture is reported and stores nothing, never converted at a
guess. Rates for captures older than the live FX job's history come from `python -m backfill.ecb_history`.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import fx
from backfill.pending_names import canonical_name
from models import ComputeListing


class NoRate(ValueError):
    pass


def rate(provider: str, at: datetime) -> fx.Rate:
    r = fx.usd_rate("EUR", at, context=f"{provider} archived capture {at:%Y-%m-%d %H:%M}Z")
    if r is None:
        raise NoRate(f"{provider}(archive): no usable ECB EUR->USD rate at {at:%Y-%m-%d} (fail closed; "
                     f"python -m backfill.ecb_history loads the history)")
    return r


def eur_listing(provider: str, *, sku: str, listing_id: str, raw: str, count: int, eur_instance_price: Decimal,
                at: datetime, fx_rate: fx.Rate, canonical: str | None = None, region: str | None = None,
                country: str | None = None, tier: str | None = None, vcpu=None, ram_gb=None,
                storage_gb=None) -> ComputeListing:
    return ComputeListing(
        provider=provider, sku=sku, listing_id=listing_id, raw_gpu_name=raw,
        canonical_gpu_name=canonical if canonical is not None else canonical_name(raw), gpu_count=count,
        region=region, country=country, **fx.convert(eur_instance_price, count, "EUR", fx_rate),
        market_type="on_demand", provider_tier=tier, interruptible=False, available=None, capacity=None,
        capacity_unit=None, vcpu=vcpu, ram_gb=ram_gb, storage_gb=storage_gb, observed_at=at)
