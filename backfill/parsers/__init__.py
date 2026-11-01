"""Parsers for archived pages the live parsers cannot read: older designs of pages OpenGrid scrapes today, and
pages OpenGrid does not scrape live at all. One module per provider (backfill/parsers/<provider>.py); each
defines SOURCES, a list of backfill.sources.Source, and sources() collects every module's SOURCES.

Each parser reads ONLY labelled values (a price next to its own "/ Hour" or column header, a count next to its
"GPU Count" label) and raises when a row it recognises lacks them, so a design it does not know is reported as a
failure, never read as wrong numbers. Raw GPU names go through canonical.py exactly as live ones do.

Shared helpers here: text(fragment) (tags stripped, entities unescaped, whitespace collapsed), dec(s)
("$1,234.56" -> Decimal, else None), slug(s), listing(...) (one ComputeListing, per-GPU price derived).
"""
from __future__ import annotations

import html as htmllib
import re
from datetime import datetime
from decimal import Decimal

import canonical
from models import ComputeListing


def text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def dec(s: str | None) -> Decimal | None:
    if not s:
        return None
    m = re.fullmatch(r"\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)", s.strip())
    return Decimal(m.group(1).replace(",", "")) if m else None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def listing(provider: str, *, sku: str, listing_id: str, raw: str, count: int, instance_price: Decimal,
            at: datetime, region: str | None = None, country: str | None = None, market: str = "on_demand",
            tier: str | None = None, vcpu=None, ram_gb=None, storage_gb=None) -> ComputeListing:
    return ComputeListing(
        provider=provider, sku=sku, listing_id=listing_id, raw_gpu_name=raw,
        canonical_gpu_name=canonical.canonical_gpu_name(raw), gpu_count=count, region=region, country=country,
        price_per_gpu_hour=instance_price / count, price_per_instance_hour=instance_price, currency="USD",
        market_type=market, provider_tier=tier, interruptible=market == "spot", available=None, capacity=None,
        capacity_unit=None, vcpu=vcpu, ram_gb=ram_gb, storage_gb=storage_gb, observed_at=at)
