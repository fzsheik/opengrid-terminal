"""Azure "effective since" anchors: one archived point per GPU VM size, region and market, from the Retail Prices
API's effectiveStartDate (source azure_retail, basis effective_since).

    fetch(sizes)          -> [item]              every Consumption row of each size, all regions (one query per size)
    points(items, at)     -> [ComputeListing]    the CURRENT price of each (size, region, market), dated at its
                                                 effectiveStartDate
    run(dry_run)          -> report              fetch, then store with load.store (idempotent)

Azure returns only current prices, but each row states the date that price took effect. A current price whose
effectiveStartDate is 2021-03-01 is therefore evidence that the same price applied on 2021-03-01, and nothing more:
it is ONE point, not a series. It says nothing about what the price was before that date, and the archived reader
does not stretch it to today (valid_to stays null). Each point keeps the meter's own query URL
(`$filter=meterId eq '<id>'`) so anyone can re-check it, and the note says when OpenGrid read it.

Rows are read with the live poller's own rules (providers/azure.py): GPU_SIZES for the GPU and count, market()
for on-demand / spot (Windows, Low Priority and non-hourly meters dropped), whole-VM price / GPU count, and of two
rows for the same meter the later effectiveStartDate is the current one. Polite: one request at a time, at least
MIN_INTERVAL apart (the API answers about 10 a minute per caller), each response cached on disk for the day.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

import canonical
from backfill.wayback import DEFAULT_CACHE, USER_AGENT
from models import ComputeListing
from providers.azure import BASE_URL, GPU_SIZES, PATH, market
from providers.base import to_decimal

SOURCE = "azure_retail"
BASIS = "effective_since"
LICENCE = "Azure Retail Prices API (public)"
PARSER = "backfill.azure_since.points (providers/azure.py GPU_SIZES + market)"
MIN_INTERVAL = 6.5
MAX_PAGES = 10


def size_filter(size: str) -> str:
    return f"serviceName eq 'Virtual Machines' and priceType eq 'Consumption' and armSkuName eq '{size}'"


def meter_url(meter_id: str) -> str:
    """The checkable query for one meter (what a reader opens to confirm the price and its start date)."""
    return f"{BASE_URL}{PATH}?" + urllib.parse.urlencode({"$filter": f"meterId eq '{meter_id}'"},
                                                          quote_via=urllib.parse.quote)


def fetch(sizes=None, *, client: httpx.Client | None = None, cache_dir: Path | None = None, log=print,
          min_interval: float = MIN_INTERVAL) -> list[dict]:
    c = client or httpx.Client(headers={"user-agent": USER_AGENT, "accept": "application/json"}, timeout=60)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    cache = Path(cache_dir or DEFAULT_CACHE) / f"azure-retail-{day}"
    cache.mkdir(parents=True, exist_ok=True)
    items, last = [], 0.0
    for size in sizes or sorted(GPU_SIZES):
        path = cache / f"{size}.json"
        if path.exists():
            items += json.loads(path.read_text())
            continue
        got, url, params = [], BASE_URL + PATH, {"$filter": size_filter(size)}
        for _ in range(MAX_PAGES):
            wait = last + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            r = c.get(url, params=params)
            if r.status_code == 429:
                time.sleep(60)
                r = c.get(url, params=params)
            r.raise_for_status()
            body = r.json()
            if not isinstance(body.get("Items"), list):
                raise ValueError(f"azure_since: {size}: response has no Items list - API reshaped")
            got += body["Items"]
            url, params = body.get("NextPageLink"), None
            if not url:
                break
        else:
            raise ValueError(f"azure_since: more than {MAX_PAGES} pages for {size}")
        path.write_text(json.dumps(got))
        log(f"azure_since: {size}: {len(got)} rows")
        items += got
    return items


def points(items: list[dict], read_at: datetime) -> tuple[list[tuple[ComputeListing, dict]], list[str]]:
    """(listing, the Azure row it came from) per (size, region, market): the current price dated at its
    effectiveStartDate. Rows with no start date, no price or a non-USD currency are reported and skipped."""
    chosen: dict[tuple[str, str, str], dict] = {}
    problems = []
    for row in items:
        size, mkt, region = row.get("armSkuName"), market(row), row.get("armRegionName")
        if size not in GPU_SIZES or mkt is None or not region:
            continue
        if (row.get("currencyCode") or "USD") != "USD":
            problems.append(f"{size} {region}: currency {row.get('currencyCode')}")
            continue
        key = (size, region, mkt)
        prev = chosen.get(key)
        if prev is None or (row.get("effectiveStartDate") or "") > (prev.get("effectiveStartDate") or ""):
            chosen[key] = row
    out = []
    for (size, region, mkt), row in sorted(chosen.items()):
        price = to_decimal(row.get("retailPrice"))
        start = row.get("effectiveStartDate")
        if price is None or price <= 0 or not start:
            problems.append(f"{size} {region} {mkt}: no price or no effectiveStartDate; skipped")
            continue
        since = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(timezone.utc)
        count, raw_name = GPU_SIZES[size]
        out.append((ComputeListing(
            provider="azure", sku=size,
            listing_id=f"{size}:{region}" + (":spot" if mkt == "spot" else "") + f":since:{since:%Y-%m-%d}",
            raw_gpu_name=raw_name, canonical_gpu_name=canonical.canonical_gpu_name(raw_name), gpu_count=count,
            region=region, country=None, price_per_gpu_hour=price / count, price_per_instance_hour=price,
            currency="USD", market_type=mkt, provider_tier="hyperscaler", interruptible=mkt == "spot",
            available=None, capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None,
            observed_at=since), row))
    return out, problems


def run(*, dry_run: bool = False, items: list[dict] | None = None, read_at: datetime | None = None, log=print,
        **fetch_kw) -> dict:
    from backfill import load

    read_at = read_at or datetime.now(timezone.utc)
    items = fetch(log=log, **fetch_kw) if items is None else items
    pts, problems = points(items, read_at)
    rows, rejected = [], []
    for listing, row in pts:
        got, rej = load._rows(
            [listing], source=SOURCE, source_url=meter_url(row["meterId"]),
            sha256=hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(), parser=PARSER,
            basis=BASIS, licence=LICENCE,
            note=(f"Azure states ${Decimal(str(row['retailPrice']))}/h for {row.get('skuName')} "
                  f"({row.get('productName')}) has been effective since {row['effectiveStartDate'][:10]}; "
                  f"read {read_at:%Y-%m-%d}. One point: the price before that date is unknown."))
        rows += got
        rejected += rej
    inserted = 0 if dry_run else load.store(rows)
    by_market: dict[str, int] = {}
    for r in rows:
        by_market[r["market_type"]] = by_market.get(r["market_type"], 0) + 1
    report = {"provider": "azure", "source": SOURCE, "items": len(items), "rows": len(rows), "inserted": inserted,
              "by_market": by_market, "problems": problems, "rejected": rejected,
              "earliest": min((r["observed_at"] for r in rows), default=None)}
    log(f"azure_since: {len(items)} Azure rows -> {len(rows)} points ({inserted} new) {by_market}")
    return report
