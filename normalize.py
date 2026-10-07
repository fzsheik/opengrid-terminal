"""Orchestration around the per-provider normalizers.

Each provider owns its own `normalize()` in providers/<name>.py; this module
only decides what raw data to hand it, and what to do with the result:
current state, change history, and reference prices.

Normalizers read stored raw snapshots rather than the network, so the whole
pipeline can be re-run over history whenever a mapping improves.
"""

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

import canonical
import quality  # lazy inside: its submodules import this module
from db import SessionLocal
from models import ComputeListing
from providers import PROVIDERS, find_endpoint, to_decimal
from tables import ComputeListingRow, ListingObservation, RawSnapshot, ReferencePrice

log = logging.getLogger(__name__)

# Values whose change is worth a new observation row.
TRACKED = (
    "price_per_gpu_hour",
    "price_per_instance_hour",
    "available",
    "capacity",
    "capacity_unit",
)

# Each endpoint's newest successful fetch time. The recursive CTE walks distinct
# endpoints through ix_raw_provider_endpoint_time (a loose index scan) instead of
# grouping every row; the lateral LIMIT 1 then reads one index entry per endpoint.
_NEWEST_PER_ENDPOINT = text(
    """
    WITH RECURSIVE eps AS (
        (SELECT endpoint FROM raw_snapshots WHERE provider = :p ORDER BY endpoint LIMIT 1)
        UNION ALL
        SELECT (SELECT r.endpoint FROM raw_snapshots r
                WHERE r.provider = :p AND r.endpoint > eps.endpoint ORDER BY r.endpoint LIMIT 1)
        FROM eps WHERE eps.endpoint IS NOT NULL
    )
    SELECT eps.endpoint, n.fetched_at
    FROM eps
    CROSS JOIN LATERAL (
        SELECT fetched_at FROM raw_snapshots r
        WHERE r.provider = :p AND r.endpoint = eps.endpoint AND r.ok
        ORDER BY r.fetched_at DESC LIMIT 1
    ) n
    WHERE eps.endpoint IS NOT NULL
    """
)


def _latest_raw(session, provider: str) -> dict[str, list[RawSnapshot]]:
    """Most recent successful snapshot per endpoint.

    Endpoints called many times per fetch (Salad's per-class availability) keep
    every row from that newest fetch, not just one.

    Reads only the newest round: a skip-scan finds the provider's endpoints, a LIMIT 1
    per endpoint finds its newest ok fetch, and only rows within 120s of that are loaded,
    rather than streaming every snapshot (and payload) the provider ever produced.
    """
    newest = session.execute(_NEWEST_PER_ENDPOINT, {"p": provider}).all()
    if not newest:
        return defaultdict(list)  # same type as before: a missing endpoint reads as []
    window = timedelta(seconds=120)
    rows = session.execute(
        select(RawSnapshot)
        .where(
            RawSnapshot.provider == provider,
            RawSnapshot.ok.is_(True),
            or_(*(
                and_(RawSnapshot.endpoint == ep, RawSnapshot.fetched_at >= t - window, RawSnapshot.fetched_at <= t)
                for ep, t in newest
            )),
        )
        .order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc())
    ).scalars()

    by_endpoint: dict[str, list[RawSnapshot]] = defaultdict(list)
    newest_seen: dict[str, datetime] = {}
    for row in rows:
        # Rows arrive newest first; keep only the newest fetch round per endpoint.
        first = newest_seen.setdefault(row.endpoint, row.fetched_at)
        if (first - row.fetched_at).total_seconds() > 120:
            continue
        by_endpoint[row.endpoint].append(row)
    return by_endpoint





# --------------------------------------------------------------------------
# Run and store
# --------------------------------------------------------------------------


def normalize_all(only: list[str] | None = None) -> list[ComputeListing]:
    """Normalize the newest raw snapshot of each provider.

    `only` restricts the work to named providers, so one provider's poll does
    not re-read everyone else's raw data.
    """
    listings: list[ComputeListing] = []
    with SessionLocal() as s:
        seen = s.execute(select(RawSnapshot.provider).distinct()).scalars().all()
        for provider in seen:
            if only and provider not in only:
                continue
            cls = PROVIDERS.get(provider)
            if cls is None:
                log.warning("raw data for %r but no provider registered", provider)
                continue
            try:
                listings.extend(cls.normalize(_latest_raw(s, provider)))
            except Exception as exc:
                # A response that changed shape must not take the previous state (or the
                # other providers) down with it: save nothing for this one, record why.
                log.exception("%s: normalizer failed; keeping previous state", provider)
                quality.normalizer_failed(provider, exc)
    return listings


def save(listings: list[ComputeListing]) -> int:
    """Upsert current state. `first_seen_at` is set once and never overwritten."""
    if not listings:
        return 0
    rows = []
    for listing in listings:
        row = listing.model_dump()
        row["first_seen_at"] = listing.observed_at
        rows.append(row)
    with SessionLocal.begin() as s:
        stmt = insert(ComputeListingRow).values(rows)
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=["provider", "listing_id"],
                set_={
                    col: getattr(stmt.excluded, col)
                    for col in rows[0]
                    if col not in ("provider", "listing_id", "first_seen_at")
                },
            )
        )
    return len(rows)


def _last_observations(session, listings: list[ComputeListing]) -> dict[tuple[str, str], ListingObservation]:
    """The most recent observation for each listing, via DISTINCT ON."""
    providers = {listing.provider for listing in listings}
    rows = session.execute(
        select(ListingObservation)
        .where(ListingObservation.provider.in_(providers))
        .order_by(
            ListingObservation.provider,
            ListingObservation.listing_id,
            ListingObservation.observed_at.desc(),
        )
        .distinct(ListingObservation.provider, ListingObservation.listing_id)
    ).scalars()
    return {(r.provider, r.listing_id): r for r in rows}


def _moved(listing: ComputeListing, previous: ListingObservation | None) -> bool:
    """True when a tracked value differs from the last observation."""
    if previous is None:
        return True
    for field in TRACKED:
        new, old = getattr(listing, field), getattr(previous, field)
        if new is None or old is None:
            if (new is None) != (old is None):
                return True
        elif isinstance(new, Decimal) or isinstance(old, Decimal):
            # Decimal compares by value, so 2.0 and 2.000000 are the same price.
            if Decimal(str(new)) != Decimal(str(old)):
                return True
        elif new != old:
            return True
    return False


def record_observations(listings: list[ComputeListing]) -> int:
    """Write an observation per listing whose price or supply moved.

    Nothing is written when a listing is unchanged, so a gap in the series means
    "same as the row before it". compute_listings.observed_at separately records
    when we last looked, so a gap is never confused with a gap in coverage.
    """
    if not listings:
        return 0
    with SessionLocal.begin() as s:
        previous = _last_observations(s, listings)
        rows = [
            {
                "provider": listing.provider,
                "listing_id": listing.listing_id,
                "observed_at": listing.observed_at,
                "price_per_gpu_hour": listing.price_per_gpu_hour,
                "price_per_instance_hour": listing.price_per_instance_hour,
                "available": listing.available,
                "capacity": listing.capacity,
                "capacity_unit": listing.capacity_unit,
            }
            for listing in listings
            if _moved(listing, previous.get((listing.provider, listing.listing_id)))
        ]
        if not rows:
            return 0
        s.execute(
            insert(ListingObservation)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["provider", "listing_id", "observed_at"])
        )
    return len(rows)


# --------------------------------------------------------------------------
# Reference prices: priced things that are not listings
# --------------------------------------------------------------------------

_KIND_BY_EXACT = {
    "vcpu": "compute",
    "ram": "compute",
    "cloud-ssd": "storage",
    "objectstorage": "storage",
    "hypervisor-local-storage": "storage",
    "publicip": "network",
}


def classify_reference(name: str) -> str | None:
    """What a pricebook entry denotes. None when we genuinely cannot tell."""
    lowered = name.lower()
    if "(input)" in lowered or "(output)" in lowered:
        return "model_token"  # per-token inference pricing
    base = lowered.replace(" (cpu-only-flavors)", "").strip()
    if base in _KIND_BY_EXACT:
        return _KIND_BY_EXACT[base]
    if canonical.canonical_gpu_name(name):
        return "gpu"
    # GPU-shaped names we have not mapped, e.g. "H100-80G-SXM5-IB", "L40-sm".
    if re.match(r"^(RTX|GTX|[HABL]\d{2,3}|L\d{2,4})[-\s]", name, re.I):
        return "gpu"
    return None


def save_reference_prices(listed_names: set[tuple[str, str]]) -> int:
    """Keep every priced entry a provider publishes, listing or not.

    Hyperstack's pricebook carries GPU variants absent from flavors and stocks
    (`-sm`, `-IB`, `-k8s`), plus vCPU, RAM, storage and per-token model pricing.
    None of it is purchasable as a listing; all of it is worth preserving.
    """
    rows = []
    with SessionLocal() as s:
        snapshots = find_endpoint(_latest_raw(s, "hyperstack"), "/pricebook")
    if not snapshots:
        return 0
    snapshot = snapshots[0]
    for entry in snapshot.payload if isinstance(snapshot.payload, list) else []:
        name = entry.get("name")
        if not name:
            continue
        rows.append(
            {
                "provider": "hyperstack",
                "name": name,
                "value": to_decimal(entry.get("value")),
                "original_value": to_decimal(entry.get("original_value")),
                "discount_applied": entry.get("discount_applied"),
                "currency": "USD",
                "is_listed": ("hyperstack", name) in listed_names,
                "kind": classify_reference(name),
                "observed_at": snapshot.fetched_at,
                "first_seen_at": snapshot.fetched_at,
            }
        )
    if not rows:
        return 0
    with SessionLocal.begin() as s:
        stmt = insert(ReferencePrice).values(rows)
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=["provider", "name"],
                set_={
                    col: getattr(stmt.excluded, col)
                    for col in rows[0]
                    if col not in ("provider", "name", "first_seen_at")
                },
            )
        )
    return len(rows)


def refresh(only: list[str] | None = None) -> dict:
    """Normalize the newest raw data, then persist state, history and references."""
    listings = normalize_all(only)
    # Suspicious values are held back (quarantined) before they reach current state or
    # history. Fails open: on its own error the listings pass through unchanged.
    listings = quality.screen(listings, only)
    saved = save(listings)
    observations = record_observations(listings)
    references = 0
    if only is None or "hyperstack" in only:
        references = save_reference_prices({(l.provider, l.raw_gpu_name) for l in listings})
    return {"listings": saved, "observations": observations, "reference_prices": references}


def history(q: str | None = None, limit: int = 200) -> list[dict]:
    """Recorded changes, newest first. A gap means nothing moved."""
    o, c = ListingObservation, ComputeListingRow
    stmt = (
        select(
            o.provider,
            o.listing_id,
            c.canonical_gpu_name,
            c.raw_gpu_name,
            c.gpu_count,
            c.region,
            c.market_type,
            c.provider_tier,
            c.sku,
            o.price_per_gpu_hour,
            o.price_per_instance_hour,
            o.available,
            o.capacity,
            o.capacity_unit,
            o.observed_at,
        )
        .join(c, (c.provider == o.provider) & (c.listing_id == o.listing_id))
        .order_by(o.observed_at.desc(), o.provider, o.listing_id)
        .limit(limit)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            c.canonical_gpu_name.ilike(like)
            | c.raw_gpu_name.ilike(like)
            | c.provider.ilike(like)
            | c.region.ilike(like)
            | c.sku.ilike(like)
        )
    with SessionLocal() as s:
        return [dict(r._mapping) for r in s.execute(stmt)]


def reference_prices(q: str | None = None, limit: int = 300) -> list[dict]:
    t = ReferencePrice
    stmt = select(t).order_by(t.provider, t.kind.nulls_last(), t.name).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(t.name.ilike(like) | t.provider.ilike(like) | t.kind.ilike(like))
    with SessionLocal() as s:
        return [
            {col.name: getattr(row, col.name) for col in t.__table__.columns}
            for row in s.execute(stmt).scalars()
        ]


def unmapped_gpus() -> list[dict]:
    """Raw GPU names with no canonical name: the mapping to-do list."""
    stmt = (
        select(
            ComputeListingRow.provider,
            ComputeListingRow.raw_gpu_name,
        )
        .where(ComputeListingRow.canonical_gpu_name.is_(None))
        .distinct()
        .order_by(ComputeListingRow.provider, ComputeListingRow.raw_gpu_name)
    )
    with SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(stmt)]
    for row in rows:
        row["reason"] = canonical.NOT_A_SINGLE_GPU.get(
            row["raw_gpu_name"], "not mapped yet"
        )
    return rows


def _previous_prices():
    """Subquery: each listing's latest observation plus the price before it.

    Observations are change-only, so the row before the newest one holds the
    last different price. LAG gives it without a self-join.
    """
    o = ListingObservation
    windowed = select(
        o.provider,
        o.listing_id,
        o.observed_at,
        o.price_per_gpu_hour,
        func.lag(o.price_per_gpu_hour)
        .over(partition_by=(o.provider, o.listing_id), order_by=o.observed_at)
        .label("previous_price_per_gpu_hour"),
        func.lag(o.observed_at)
        .over(partition_by=(o.provider, o.listing_id), order_by=o.observed_at)
        .label("previous_observed_at"),
    ).subquery()

    return (
        select(
            windowed.c.provider,
            windowed.c.listing_id,
            windowed.c.previous_price_per_gpu_hour,
            windowed.c.previous_observed_at,
            windowed.c.observed_at.label("changed_at"),
        )
        .distinct(windowed.c.provider, windowed.c.listing_id)
        .order_by(windowed.c.provider, windowed.c.listing_id, windowed.c.observed_at.desc())
        .subquery()
    )


# A move smaller than either bound is noise, not a price change: stored prices
# carry six decimals, and a marketplace median shifts by fractions of a cent.
CHANGE_MIN_PCT = Decimal("0.005")  # 0.5%
CHANGE_MIN_USD = Decimal("0.001")

_PRICE_THEN = text(
    """
    WITH cutoff AS (SELECT now() - (:hours * interval '1 hour') AS t),
    base AS (
        -- The last observation at or before the cutoff is the price in force then:
        -- observations are change-only, so no row between two changes means "same".
        SELECT DISTINCT ON (o.provider, o.listing_id)
               o.provider, o.listing_id,
               o.price_per_gpu_hour AS then_price, o.observed_at AS then_at
        FROM listing_observations o, cutoff
        WHERE o.observed_at <= cutoff.t
        ORDER BY o.provider, o.listing_id, o.observed_at DESC
    ),
    cov AS (
        -- When we first heard from each provider. Before that, "no row" means
        -- "we were not watching", not "unchanged".
        SELECT provider, min(fetched_at) AS first_fetch
        FROM raw_snapshots WHERE ok GROUP BY provider
    )
    SELECT c.provider, c.listing_id, c.price_per_gpu_hour AS now_price,
           b.then_price, b.then_at, cov.first_fetch, cutoff.t AS cutoff
    FROM compute_listings c
    CROSS JOIN cutoff
    LEFT JOIN base b ON b.provider = c.provider AND b.listing_id = c.listing_id
    LEFT JOIN cov ON cov.provider = c.provider
    """
)


def classify_change(now_price, then_price, then_at, first_fetch, cutoff) -> tuple[str, Decimal | None]:
    """(status, percent change) of one listing against its price at `cutoff`.

    up / down   moved by at least CHANGE_MIN_PCT and CHANGE_MIN_USD
    flat        moved less than that, or not at all
    new         the listing first appeared after the cutoff
    nodata      we were not recording this provider at the cutoff, or no usable price
    """
    if now_price is None or first_fetch is None or first_fetch > cutoff:
        return "nodata", None
    if then_at is None or then_price is None:
        return "new", None
    if then_price <= 0:
        return "nodata", None
    delta = Decimal(now_price) - Decimal(then_price)
    pct = delta / Decimal(then_price)
    if abs(delta) < CHANGE_MIN_USD or abs(pct) < CHANGE_MIN_PCT:
        return "flat", pct
    return ("up" if delta > 0 else "down"), pct


def price_changes(hours: float) -> dict:
    """Every listing's current per-GPU price against its price `hours` ago."""
    with SessionLocal() as s:
        rows = s.execute(_PRICE_THEN, {"hours": hours}).all()
    items, tracking_since, cutoff = [], None, None
    for r in rows:
        status, pct = classify_change(r.now_price, r.then_price, r.then_at, r.first_fetch, r.cutoff)
        cutoff = r.cutoff
        if r.first_fetch is not None and (tracking_since is None or r.first_fetch < tracking_since):
            tracking_since = r.first_fetch
        items.append(
            {
                "provider": r.provider,
                "listing_id": r.listing_id,
                "status": status,
                "pct": None if pct is None else float(pct),
                "price_now": None if r.now_price is None else float(r.now_price),
                "price_then": None if r.then_price is None else float(r.then_price),
                "then_at": r.then_at,
            }
        )
    return {"hours": hours, "cutoff": cutoff, "tracking_since": tracking_since, "items": items}


def listings(q: str | None = None, only_available: bool = False, limit: int | None = None) -> list[dict]:
    """Current listings, cheapest first, each carrying its previous price."""
    t = ComputeListingRow
    prev = _previous_prices()
    stmt = (
        select(
            t,
            prev.c.previous_price_per_gpu_hour,
            prev.c.previous_observed_at,
            prev.c.changed_at,
        )
        .outerjoin(prev, (prev.c.provider == t.provider) & (prev.c.listing_id == t.listing_id))
        .order_by(t.price_per_gpu_hour.nulls_last(), t.canonical_gpu_name, t.gpu_count)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            t.canonical_gpu_name.ilike(like)
            | t.raw_gpu_name.ilike(like)
            | t.provider.ilike(like)
            | t.region.ilike(like)
            | t.sku.ilike(like)
            | t.market_type.ilike(like)
        )
    if only_available:
        stmt = stmt.where(t.available.is_(True))
    if limit:
        stmt = stmt.limit(limit)

    rows = []
    with SessionLocal() as s:
        for row in s.execute(stmt):
            listing = row[0]
            record = {c.name: getattr(listing, c.name) for c in t.__table__.columns}
            record["previous_price_per_gpu_hour"] = row.previous_price_per_gpu_hour
            record["previous_observed_at"] = row.previous_observed_at
            record["changed_at"] = row.changed_at
            rows.append(record)
    return rows
