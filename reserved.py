"""Reserved and committed GPU prices, computed on read from stored raw snapshots. Never listings.

Founders' rule (2026-11-01): reserved / committed prices have their own section and are NEVER mixed into on-demand
listings, indices or market statistics. This module enforces it by construction:
    * it writes nothing: no table, no listing, no observation, no rollup row;
    * nothing in normalize / analytics / indices imports it (tests/test_reserved.py checks both);
    * it reads the providers' raw snapshots through their own pure readers, which normalize() never calls.

One row per (provider, GPU, GPU count, region, term):

    effective $/GPU-hour     what one GPU costs per hour over the commitment
    term                     months (normalised) plus the provider's own label, and a column bucket (TERM_BUCKETS)
    term_price               the whole-term price of the whole machine, only where the provider states one
    on_demand_per_gpu_hour   the matching CURRENT on-demand listing (see attach_on_demand), and the saving %
    derived                  False: the provider publishes this price for this term ("quoted");
                             True: OpenGrid applied the provider's published discount % to its on-demand price

Sources (methodology/reserved-prices.md):
    aws         Compute Savings Plans, 1/3 years x No / Partial / All Upfront, every SPOT_REGIONS region: the plan's
                effective hourly rate per instance / GPUs, compared with the same file's on-demand rate
    azure       1/3/5-year reservations; retailPrice is the whole term:  term price / (8,760 h x years) / GPUs
    gcp         1/3-year commitments of the GPU SKU ALONE (vCPU, RAM, SSD on top): scope "gpu_only", compared with the
                same GPU SKU on demand, never with a whole-VM listing
    jarvislabs  reserved_pricing per GPU-hour, 1/3/6 months and 1/2 years
    latitude    plan prices per month and per year (whole server):  price / (730 or 8,760 h) / GPUs
    together    GPU Clusters reserved rates per GPU-hour for 3-30 / 31-90 / 91-180 / 181+ day commitments
    verda       a discount ladder (1 month 2% ... 2 years 25%), applied to each current on-demand listing: derived

Only USD prices are used. Anything in another currency is skipped and named in the notes (no FX in OpenGrid yet).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

log = logging.getLogger(__name__)

HOURS_PER_YEAR = Decimal(8760)  # 365 x 24; leap days ignored
DAYS_PER_MONTH = Decimal(365) / 12
STALE_AFTER_POLLS = 3  # a reserved source not refreshed within 3 of its provider's poll intervals is flagged stale

# Column buckets, by MINIMUM commitment: a term goes in the longest bucket it reaches (0.1-month tolerance, so 181
# days counts as 6 months and 91 days as 3). (key, months, label)
TERM_BUCKETS: tuple[tuple[str, int, str], ...] = (
    ("lt1m", 0, "Under 1 month"), ("1m", 1, "1 month"), ("3m", 3, "3 months"), ("6m", 6, "6 months"),
    ("1y", 12, "1 year"), ("2y", 24, "2 years"), ("3y", 36, "3 years"), ("5y", 60, "5 years"),
)
BUCKET_KEYS = tuple(k for k, _, _ in TERM_BUCKETS)
BUCKET_LABEL = {k: label for k, _, label in TERM_BUCKETS}

# provider -> (endpoint read, how the effective price is obtained). Shown with every row and on the page.
SOURCES: dict[str, dict[str, str]] = {
    "aws": {"source": "AWS Compute Savings Plans price files (b0.p.awsstatic.com), 1 and 3 years, No / Partial / All "
                      "Upfront, Linux, shared tenancy, 15 regions",
            "method": "the plan's effective hourly rate (upfront amortised by AWS) / GPUs per instance; compared with "
                      "the on-demand rate printed in the same file"},
    "azure": {"source": "Azure Retail Prices API, priceType 'Reservation' (eastus, eastus2)",
              "method": "whole-term price / (8,760 h x years) / GPUs per VM"},
    "gcp": {"source": "Cloud Billing Catalog API, Commit1Yr / Commit3Yr GPU SKUs, in the regions "
                     "Compute Engine offers the GPU (us-central1 without the service account)",
            "method": "GPU SKU only: vCPUs, memory and Local SSD are billed on top; compared with the same GPU SKU on demand"},
    "jarvislabs": {"source": "Jarvislabs misc/server_meta, reserved_pricing",
                   "method": "per GPU-hour, as quoted"},
    "latitude": {"source": "Latitude plans API, USD month and year prices per region",
                 "method": "plan price / (730 h a month or 8,760 h a year) / GPUs per server"},
    "together": {"source": "together.ai/pricing, GPU Clusters reserved columns",
                 "method": "per GPU-hour, as quoted"},
    "verda": {"source": "Verda /v1/long-term/periods/instances (discount ladder)",
              "method": "Verda's discount % applied to its current on-demand price (derived)"},
}
PROVIDERS = tuple(sorted(SOURCES))

_Q6 = Decimal("0.000001")


def _q(v: Decimal | None, exp: Decimal = _Q6) -> float | None:
    return None if v is None else float(Decimal(v).quantize(exp, rounding=ROUND_HALF_UP))


def term_bucket(months: Decimal | float) -> str:
    """The column a term goes in: the longest bucket its minimum commitment reaches."""
    m = Decimal(str(months)) + Decimal("0.1")
    for key, bm, _ in reversed(TERM_BUCKETS):
        if bm and m >= bm:
            return key
    return "lt1m"


def _row(provider: str, *, gpu: str | None, raw_gpu_name: str, gpu_count: int, region: str | None, sku: str | None,
         term_label: str, term_months: Decimal, price_per_gpu_hour: Decimal, observed_at: datetime | None,
         term_min_days: int | None = None, term_max_days: int | None = None, term_price: Decimal | None = None,
         term_hours: Decimal | None = None, derived: bool = False, discount_pct: Decimal | None = None,
         scope: str = "instance", listing_hint: str | None = None, same_source_on_demand: Decimal | None = None) -> dict:
    months = Decimal(term_months)
    return {
        "provider": provider, "gpu": gpu, "raw_gpu_name": raw_gpu_name, "gpu_count": gpu_count, "region": region,
        "sku": sku, "term_label": term_label, "term_months": _q(months, Decimal("0.01")),
        "term_min_days": term_min_days if term_min_days is not None else int((months * DAYS_PER_MONTH).to_integral_value()),
        "term_max_days": term_max_days, "term_bucket": term_bucket(months),
        "price_per_gpu_hour": price_per_gpu_hour, "term_price": term_price, "term_hours": term_hours,
        "currency": "USD", "derived": derived, "kind": "derived" if derived else "observed",
        "discount_pct": discount_pct, "scope": scope, "observed_at": observed_at,
        "source": SOURCES[provider]["source"], "method": SOURCES[provider]["method"],
        # filled by attach_on_demand
        "_listing_hint": listing_hint, "_same_source": same_source_on_demand,
    }


def _newest(by_endpoint: dict, endpoints: Iterable[str]) -> datetime | None:
    times = [s.fetched_at for ep in endpoints for s in by_endpoint.get(ep) or [] if getattr(s, "fetched_at", None)]
    return max(times) if times else None


# --------------------------------------------------------------------------- per provider (pure)

def _azure(by_endpoint: dict, listings: list[dict], notes: list[str]) -> list[dict]:
    from providers import azure

    eps = [azure.endpoint(r, azure.RESERVATION) for r in azure.REGIONS]
    at = _newest(by_endpoint, eps)
    out, foreign = [], set()
    for rp in azure.reserved_prices(by_endpoint):
        if rp.currency != "USD":
            foreign.add(rp.currency)
            continue
        out.append(_row(
            "azure", gpu=rp.canonical_gpu_name, raw_gpu_name=rp.raw_gpu_name, gpu_count=rp.gpu_count,
            region=rp.region, sku=rp.sku, term_label=f"{rp.term_years} Year" + ("s" if rp.term_years > 1 else ""),
            term_months=Decimal(12 * rp.term_years), price_per_gpu_hour=rp.price_per_gpu_hour, observed_at=at,
            term_price=rp.term_price, term_hours=rp.term_hours, listing_hint=f"{rp.sku}:{rp.region}"))
    if foreign:
        notes.append(f"azure: reservations priced in {', '.join(sorted(foreign))} skipped (no currency conversion)")
    return out


def _aws(by_endpoint: dict, listings: list[dict], notes: list[str]) -> list[dict]:
    from providers import aws

    out = []
    for sp in aws.savings_plan_prices(by_endpoint):
        out.append(_row(
            "aws", gpu=sp.canonical_gpu_name, raw_gpu_name=sp.raw_gpu_name, gpu_count=sp.gpu_count, region=sp.region,
            sku=sp.instance_type,
            term_label=f"{sp.years} Year{'s' if sp.years > 1 else ''} Compute Savings Plan, {sp.payment}",
            term_months=Decimal(12 * sp.years), price_per_gpu_hour=sp.price_per_instance_hour / sp.gpu_count,
            observed_at=sp.observed_at, listing_hint=f"{sp.instance_type}:{sp.region}",
            same_source_on_demand=(sp.on_demand_per_instance_hour / sp.gpu_count
                                   if sp.on_demand_per_instance_hour else None)))
    return out


def _gcp(by_endpoint: dict, listings: list[dict], notes: list[str]) -> list[dict]:
    from providers import gcp

    at = _newest(by_endpoint, [gcp.ENDPOINT])
    out, foreign = [], set()
    for c in gcp.commitment_prices(by_endpoint):
        if c.currency != "USD":
            foreign.add(c.currency)
            continue
        out.append(_row(
            "gcp", gpu=c.canonical_gpu_name, raw_gpu_name=c.accelerator, gpu_count=1, region=c.region,
            sku=c.description, term_label=c.usage_type, term_months=Decimal(12 * c.term_years),
            price_per_gpu_hour=c.price_per_gpu_hour, observed_at=at, scope="gpu_only",
            same_source_on_demand=c.on_demand_gpu_hour))
    if foreign:
        notes.append(f"gcp: commitments priced in {', '.join(sorted(foreign))} skipped (no currency conversion)")
    return out


def _jarvislabs(by_endpoint: dict, listings: list[dict], notes: list[str]) -> list[dict]:
    from providers import jarvislabs

    try:
        prices, currency = jarvislabs.reserved_prices(by_endpoint)
    except ValueError as exc:  # a reshaped catalogue: nothing, never a guess
        notes.append(f"jarvislabs: {exc}")
        return []
    if prices and (currency or "USD") != "USD":
        notes.append(f"jarvislabs: catalogue priced in {currency}; skipped (no currency conversion)")
        return []
    at = _newest(by_endpoint, [jarvislabs.ENDPOINT])
    return [_row("jarvislabs", gpu=p.canonical_gpu_name, raw_gpu_name=p.raw_gpu_name, gpu_count=1, region=p.region,
                 sku=f"{p.raw_gpu_name}:1x:{p.workload}", term_label=p.term, term_months=Decimal(p.months),
                 price_per_gpu_hour=p.price_per_gpu_hour, observed_at=at, listing_hint=p.listing_id)
            for p in prices]


def _latitude(by_endpoint: dict, listings: list[dict], notes: list[str]) -> list[dict]:
    from providers import shadeform

    prices, skipped = shadeform.latitude_plan_prices(by_endpoint)
    if skipped:
        notes.append(f"latitude: plan prices in {', '.join(skipped)} skipped (no currency conversion); USD kept")
    at = _newest(by_endpoint, [shadeform.LATITUDE_PLANS])
    return [_row("latitude", gpu=p.canonical_gpu_name, raw_gpu_name=p.raw_gpu_name, gpu_count=p.gpu_count,
                 region=p.region, sku=p.slug, term_label=p.term, term_months=Decimal(p.months),
                 price_per_gpu_hour=p.price_per_gpu_hour, observed_at=at, term_price=p.term_price,
                 term_hours=p.term_hours, same_source_on_demand=p.hourly_per_gpu)
            for p in prices]


def _together(by_endpoint: dict, listings: list[dict], notes: list[str]) -> list[dict]:
    import re

    from providers import together

    at = _newest(by_endpoint, [together.RESERVED_ENDPOINT])
    out = []
    for p in together.reserved_prices(by_endpoint):
        slug = re.sub(r"[^a-z0-9]+", "-", p.hardware.lower()).strip("-")  # the on-demand listing id (normalize())
        out.append(_row("together", gpu=p.canonical_gpu_name, raw_gpu_name=p.hardware, gpu_count=1, region=None,
                        sku=p.hardware, term_label=p.term, term_months=Decimal(p.min_days) / DAYS_PER_MONTH,
                        term_min_days=p.min_days, term_max_days=p.max_days, price_per_gpu_hour=p.price_per_gpu_hour,
                        observed_at=at, listing_hint=slug))
    return out


def _verda(by_endpoint: dict, listings: list[dict], notes: list[str]) -> list[dict]:
    from providers import verda

    ladder = verda.commitment_ladder(by_endpoint)
    if not ladder:
        return []
    at = _newest(by_endpoint, [verda.LADDER])
    out = []
    for lst in listings:
        if lst["provider"] != "verda" or lst.get("market_type") != "on_demand" or lst.get("currency", "USD") != "USD":
            continue
        od = lst.get("price_per_gpu_hour")
        if od is None or Decimal(od) <= 0:
            continue
        for rung in ladder:
            out.append(_row(
                "verda", gpu=lst.get("canonical_gpu_name"), raw_gpu_name=lst.get("raw_gpu_name") or lst.get("sku") or "",
                gpu_count=lst["gpu_count"], region=lst.get("region"), sku=lst.get("sku"), term_label=rung.name,
                term_months=Decimal(rung.months), price_per_gpu_hour=Decimal(od) * (1 - rung.discount_pct / 100),
                observed_at=at, derived=True, discount_pct=rung.discount_pct, listing_hint=lst["listing_id"]))
    return out


READERS = {"aws": _aws, "azure": _azure, "gcp": _gcp, "jarvislabs": _jarvislabs, "latitude": _latitude, "together": _together,
           "verda": _verda}
assert set(READERS) == set(SOURCES)


# --------------------------------------------------------------------------- on-demand match and saving (pure)

def _eligible(listings: list[dict]) -> list[dict]:
    return [lst for lst in listings
            if lst.get("market_type") == "on_demand" and not lst.get("interruptible")
            and (lst.get("currency") or "USD") == "USD" and lst.get("price_per_gpu_hour") is not None
            and Decimal(lst["price_per_gpu_hour"]) > 0]


def attach_on_demand(rows: list[dict], listings: list[dict]) -> list[dict]:
    """Give each row its on-demand comparison and saving %. Pure.

    Match order (the first that finds a CURRENT on-demand USD listing wins; `on_demand_match` says which):
        same_listing           the provider's own on-demand listing for this exact SKU (and region)
        same_source            the provider's own pay-as-you-go price for the same product in the same raw response
                               (GCP: the same GPU SKU on demand, always, because its commitments are GPU-only;
                               Latitude: the same plan's hourly price in the same region)
        same_size_region       same provider, GPU, GPUs per machine and region
        same_size              same provider, GPU and GPUs per machine, cheapest region
    No match leaves the comparison empty: a saving is never computed against a different machine size or provider.
    """
    pool = _eligible(listings)
    by_id = {(lst["provider"], lst["listing_id"]): lst for lst in pool}
    for r in rows:
        hint, same_source = r.pop("_listing_hint", None), r.pop("_same_source", None)
        match, lst = None, None
        if r["scope"] == "gpu_only":
            pass  # never against a whole-VM listing
        elif hint and (r["provider"], hint) in by_id:
            match, lst = "same_listing", by_id[(r["provider"], hint)]
        elif same_source is not None and Decimal(same_source) > 0:
            pass  # the same product's own pay-as-you-go price beats a listing that only shares the GPU
        elif r["gpu"]:
            same = [x for x in pool if x["provider"] == r["provider"] and x.get("canonical_gpu_name") == r["gpu"]
                    and x.get("gpu_count") == r["gpu_count"]]
            here = [x for x in same if r["region"] is not None and x.get("region") == r["region"]]
            if here:
                match, lst = "same_size_region", min(here, key=lambda x: Decimal(x["price_per_gpu_hour"]))
            elif same:
                match, lst = "same_size", min(same, key=lambda x: Decimal(x["price_per_gpu_hour"]))
        od = Decimal(lst["price_per_gpu_hour"]) if lst else None
        if od is None and same_source is not None and Decimal(same_source) > 0:
            match, od = "same_source", Decimal(same_source)
        r["on_demand_per_gpu_hour"] = od
        r["on_demand_match"] = match
        r["on_demand_listing_id"] = lst["listing_id"] if lst else None
        r["on_demand_observed_at"] = lst.get("observed_at") if lst else None
        r["saving_pct"] = None if od is None else (od - Decimal(r["price_per_gpu_hour"])) / od * 100
    return rows


# --------------------------------------------------------------------------- the whole model (pure)

def build(raw: dict[str, dict], listings: list[dict], now: datetime | None = None,
          intervals: dict[str, int] | None = None) -> tuple[list[dict], list[str]]:
    """raw: provider -> its newest raw snapshots by endpoint (normalize._latest_raw). listings: current on-demand
    listings as dicts (compute_listings columns). Returns (rows, notes), rows JSON-ready and sorted."""
    now = now or datetime.now(timezone.utc)
    rows, notes = [], []
    for provider, reader in READERS.items():
        by_endpoint = raw.get(provider) or {}
        try:
            got = reader(by_endpoint, listings, notes)
        except Exception as exc:  # one reshaped source never takes the others down
            log.exception("reserved: %s reader failed", provider)
            notes.append(f"{provider}: reserved source could not be read ({type(exc).__name__})")
            continue
        interval = (intervals or {}).get(provider, 900)
        for r in got:
            at = r["observed_at"]
            r["stale"] = at is None or (now - at) > timedelta(seconds=STALE_AFTER_POLLS * interval)
        rows += got
    attach_on_demand(rows, listings)
    rows.sort(key=lambda r: (r["gpu"] is None, r["gpu"] or r["raw_gpu_name"], r["term_months"] or 0,
                             Decimal(r["price_per_gpu_hour"]), r["provider"], r["region"] or "", r["sku"] or ""))
    return [_json(r) for r in rows], notes


def _json(r: dict) -> dict:
    out = dict(r)
    for k in ("price_per_gpu_hour", "on_demand_per_gpu_hour"):
        out[k] = _q(r[k])
    out["term_price"] = _q(r["term_price"], Decimal("0.01"))
    out["term_hours"] = None if r["term_hours"] is None else int(r["term_hours"])
    out["discount_pct"] = _q(r["discount_pct"], Decimal("0.01"))
    out["saving_pct"] = _q(r["saving_pct"], Decimal("0.1"))
    return out


def summary(rows: list[dict]) -> list[dict]:
    """Per GPU: the cheapest row in each term bucket, and the cheapest on-demand price the rows were compared with.

    GPU-only rows (GCP) are left out of "cheapest": they exclude the CPU, memory and disk every other row includes.
    Rows with no canonical GPU are left out too."""
    by_gpu: dict[str, dict] = {}
    for r in rows:
        if not r["gpu"] or r["scope"] != "instance":
            continue
        g = by_gpu.setdefault(r["gpu"], {"gpu": r["gpu"], "terms": {}, "on_demand_min": None, "providers": set()})
        g["providers"].add(r["provider"])
        cur = g["terms"].get(r["term_bucket"])
        if cur is None or r["price_per_gpu_hour"] < cur["price_per_gpu_hour"]:
            g["terms"][r["term_bucket"]] = {k: r[k] for k in (
                "provider", "price_per_gpu_hour", "on_demand_per_gpu_hour", "saving_pct", "derived", "term_label",
                "gpu_count", "region", "sku", "stale")}
        od = r["on_demand_per_gpu_hour"]
        if od is not None and (g["on_demand_min"] is None or od < g["on_demand_min"]):
            g["on_demand_min"] = od
    out = []
    for gpu in sorted(by_gpu):
        g = by_gpu[gpu]
        g["terms"] = {k: g["terms"][k] for k in BUCKET_KEYS if k in g["terms"]}
        g["providers"] = sorted(g["providers"])
        out.append(g)
    return out


# --------------------------------------------------------------------------- database read (no writes)

_LISTINGS_SQL = """
    SELECT provider, listing_id, sku, raw_gpu_name, canonical_gpu_name, gpu_count, region, price_per_gpu_hour,
           currency, market_type, interruptible, provider_tier, observed_at
    FROM compute_listings
    WHERE provider = ANY(:providers) AND market_type = 'on_demand' AND visibility = 'public'
"""


def load(now: datetime | None = None) -> tuple[list[dict], list[str], datetime | None]:
    """Read the newest raw snapshots and the current on-demand listings of the reserved providers; build the model.
    Read-only: one SELECT per provider plus one for listings. Returns (rows, notes, newest source time)."""
    from sqlalchemy import text

    import market
    import normalize
    from providers import PROVIDERS as REGISTRY

    now = now or datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        raw = {p: normalize._latest_raw(s, p) for p in READERS}
        listings = [dict(r._mapping) for r in s.execute(text(_LISTINGS_SQL), {"providers": list(READERS)})]
    # Only current listings: one not seen within its provider's staleness window is gone, not a comparison.
    listings = [lst for lst in listings if now - lst["observed_at"] <= market.stale_after(lst["provider"])]
    intervals = {p: REGISTRY[p].polling.interval_seconds for p in READERS if p in REGISTRY}
    rows, notes = build(raw, listings, now, intervals)
    newest = max((r["observed_at"] for r in rows if r["observed_at"]), default=None)
    return rows, notes, newest
