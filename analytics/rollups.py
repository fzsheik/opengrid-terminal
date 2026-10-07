"""The hourly market rollup: one sample per (segment, gpu, provider, region) per hour.

Why: answering "30-day percentile", "share of time cheapest" or a 90-day index
from raw change-only observations means replaying every event on every request.
The rollup samples each listing's state at the top of each hour, once, using
exactly market.py's rules, and stores the per-provider summary. Readers then
aggregate across providers (lowest / median / index) from a few thousand rows.

Sampling rules (identical to market.price_at / stale_after):
    - a listing's state at hour H is its last observation at or before H
    - it counts only while H <= last seen + stale_after(provider); a listing that
      vanished records no "gone" row, so without this it would live forever
    - priced = price > 0 and not sold out; sold out = available is False

Segments:
    on_demand   market.py's eligibility (canonical, on-demand, not interruptible,
                not Vast's "cheapest" row, not a "from $x" floor)
    spot        spot or interruptible, otherwise the same exclusions

Refresh: `refresh()` recomputes from the last stored hour minus a small overlap,
so it is cheap to run every few minutes; the first run backfills everything.
`rebuild()` recomputes a window from scratch (after a mapping fix, say).

Known limit: a listing that disappeared and later returned is treated as live
across the gap, because the pipeline keeps no record of the gap (compute_listings
only remembers the latest `observed_at`). Use `first_hours()` for when each
provider began being recorded.
"""

from __future__ import annotations

import bisect
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import market
import normalize
from jobs import job
from store.analytics import MarketHourly

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
OVERLAP = timedelta(hours=3)  # late-normalized observations can land a little in the past

_EXCLUDE = "COALESCE(c.provider_tier, '') NOT IN ('cheapest', 'from_price') AND c.canonical_gpu_name IS NOT NULL"
_SEGMENT_SQL = f"""
    CASE
      WHEN NOT ({_EXCLUDE}) THEN NULL
      WHEN c.market_type = 'on_demand' AND COALESCE(c.interruptible, false) = false THEN 'on_demand'
      WHEN c.market_type = 'spot' OR c.interruptible THEN 'spot'
    END
"""

_LISTINGS = text(f"""
    SELECT * FROM (
      SELECT c.provider, c.listing_id, c.canonical_gpu_name AS gpu, COALESCE(c.region, '') AS region,
             c.country, c.observed_at AS last_seen, c.capacity_unit, {_SEGMENT_SQL} AS segment
      FROM compute_listings c
    ) x WHERE segment IS NOT NULL
""")

_EVENTS = text("""
    SELECT o.provider, o.listing_id, o.observed_at, o.price_per_gpu_hour AS price, o.available, o.capacity
    FROM listing_observations o
    WHERE o.observed_at <= :t1
    ORDER BY o.provider, o.listing_id, o.observed_at
""")

# An incremental refresh needs only the tail: each listing's last event before t0, plus everything after.
_EVENTS_SINCE = text("""
    SELECT provider, listing_id, observed_at, price, available, capacity FROM (
      SELECT o.provider, o.listing_id, o.observed_at, o.price_per_gpu_hour AS price, o.available, o.capacity,
             o.observed_at >= :t0 AS after,
             row_number() OVER (PARTITION BY o.provider, o.listing_id, o.observed_at < :t0
                                ORDER BY o.observed_at DESC) AS rn
      FROM listing_observations o WHERE o.observed_at <= :t1
    ) e WHERE after OR rn = 1
    ORDER BY provider, listing_id, observed_at
""")


def floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def hours_between(t0: datetime, t1: datetime) -> list[datetime]:
    """Every top-of-hour in [t0, t1]."""
    h = floor_hour(t0)
    if h < t0:
        h += HOUR
    out = []
    while h <= t1:
        out.append(h)
        h += HOUR
    return out


def sample(listings, events, hours) -> dict:
    """{(segment, gpu, provider, region, hour): row} over `hours`. Pure: no database."""
    acc: dict[tuple, dict] = {}
    for lst in listings:
        ev = events.get((lst["provider"], lst["listing_id"]))
        if not ev:
            continue
        times = [e[0] for e in ev]
        gone_after = lst["last_seen"] + market.stale_after(lst["provider"])
        for h in hours:
            if h > gone_after:
                break
            i = bisect.bisect_right(times, h) - 1
            if i < 0:
                continue
            _, price, available, capacity = ev[i]
            key = (lst["segment"], lst["gpu"], lst["provider"], lst["region"], h)
            r = acc.get(key)
            if r is None:
                r = acc[key] = {
                    "segment": key[0], "gpu": key[1], "provider": key[2], "region": key[3], "hour": h,
                    "country": lst["country"], "prices": [], "live_listings": 0, "available_listings": 0,
                    "unknown_listings": 0, "sold_out_listings": 0, "capacity_gpus": None,
                }
            r["live_listings"] += 1
            if available is True:
                r["available_listings"] += 1
            elif available is None:
                r["unknown_listings"] += 1
            else:
                r["sold_out_listings"] += 1
            if price is not None and price > 0 and available is not False:
                r["prices"].append(float(price))
            if lst["capacity_unit"] == "gpu" and capacity is not None:
                r["capacity_gpus"] = (r["capacity_gpus"] or 0) + int(capacity)
    for r in acc.values():
        p = r.pop("prices")
        r["priced_listings"] = len(p)
        r["min_price"] = min(p) if p else None
        r["max_price"] = max(p) if p else None
        r["avg_price"] = sum(p) / len(p) if p else None
    return acc


def _write(session, rows: list[dict], t0: datetime, t1: datetime) -> int:
    session.execute(text("DELETE FROM market_hourly WHERE hour >= :t0 AND hour <= :t1"), {"t0": t0, "t1": t1})
    if rows:
        # COPY, not INSERT: ~50x faster (47k rows in ~1s versus over a minute), which matters on a full rebuild.
        cols = [c.name for c in MarketHourly.__table__.columns]
        cursor = session.connection().connection.driver_connection.cursor()
        with cursor, cursor.copy(f"COPY market_hourly ({', '.join(cols)}) FROM STDIN") as copy:
            for r in rows:
                copy.write_row([r[c] for c in cols])
    return len(rows)


# Callables run after each successful refresh (cache clears, index recompute, event detection).
AFTER_REFRESH: list = []


def rebuild(t0: datetime | None = None, t1: datetime | None = None) -> dict:
    """Recompute every hour in [t0, t1] (default: all history up to now)."""
    t1 = t1 or datetime.now(timezone.utc)
    with normalize.SessionLocal() as s:
        listings = [dict(r._mapping) for r in s.execute(_LISTINGS)]
        if t0 is None:
            rows = s.execute(_EVENTS, {"t1": t1}).all()
        else:
            rows = s.execute(_EVENTS_SINCE, {"t0": t0, "t1": t1}).all()
    events: dict[tuple, list] = defaultdict(list)
    for r in rows:
        events[(r.provider, r.listing_id)].append((r.observed_at, r.price, r.available, r.capacity))
    if t0 is None:
        t0 = min((e[0][0] for e in events.values()), default=t1)
    hours = hours_between(t0, t1)
    out = list(sample(listings, events, hours).values()) if hours else []
    if hours:
        with normalize.SessionLocal.begin() as s:
            _write(s, out, hours[0], hours[-1])
    return {"hours": len(hours), "rows": len(out),
            "from": hours[0] if hours else None, "to": hours[-1] if hours else None}


def refresh() -> dict:
    """Incremental: recompute from the newest stored hour minus OVERLAP; backfill everything if empty."""
    with normalize.SessionLocal() as s:
        last = s.execute(text("SELECT max(hour) FROM market_hourly")).scalar()
    result = rebuild(None if last is None else last - OVERLAP)
    for hook in AFTER_REFRESH:
        try:
            hook()
        except Exception:
            log.exception("rollup after-refresh hook %r failed", hook)
    return result


@job("market_hourly", every_seconds=600, initial_delay_seconds=45)
def _refresh_job():
    return refresh()


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------

def provider_hourly(segment: str = "on_demand", gpu: str | None = None, provider: str | None = None,
                    t0: datetime | None = None, t1: datetime | None = None) -> list[dict]:
    """Rows collapsed over region, one per (gpu, provider, hour): min/max over regions, summed counts.

    Ordered by gpu, hour, provider.
    """
    sql = text("""
        SELECT gpu, provider, hour,
               min(min_price) AS min_price, max(max_price) AS max_price,
               sum(live_listings) AS live_listings, sum(priced_listings) AS priced_listings,
               sum(available_listings) AS available_listings, sum(unknown_listings) AS unknown_listings,
               sum(sold_out_listings) AS sold_out_listings, sum(capacity_gpus) AS capacity_gpus,
               count(DISTINCT region) AS regions
        FROM market_hourly
        WHERE segment = :segment
          AND (CAST(:gpu AS text) IS NULL OR gpu = :gpu)
          AND (CAST(:provider AS text) IS NULL OR provider = :provider)
          AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= :t0)
          AND (CAST(:t1 AS timestamptz) IS NULL OR hour <= :t1)
        GROUP BY gpu, provider, hour
        ORDER BY gpu, hour, provider
    """)
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"segment": segment, "gpu": gpu, "provider": provider, "t0": t0, "t1": t1}).all()
    return [{**r._mapping, "min_price": _f(r.min_price), "max_price": _f(r.max_price),
             "capacity_gpus": None if r.capacity_gpus is None else int(r.capacity_gpus)} for r in rows]


def regional_hourly(segment: str = "on_demand", gpu: str | None = None, t0=None, t1=None) -> list[dict]:
    """Rollup rows with region kept, for regional analytics. Ordered by gpu, hour, provider, region."""
    sql = text("""
        SELECT * FROM market_hourly
        WHERE segment = :segment AND (CAST(:gpu AS text) IS NULL OR gpu = :gpu)
          AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= :t0)
          AND (CAST(:t1 AS timestamptz) IS NULL OR hour <= :t1)
        ORDER BY gpu, hour, provider, region
    """)
    with normalize.SessionLocal() as s:
        rows = s.execute(sql, {"segment": segment, "gpu": gpu, "t0": t0, "t1": t1}).all()
    return [{**r._mapping, "min_price": _f(r.min_price), "max_price": _f(r.max_price), "avg_price": _f(r.avg_price)}
            for r in rows]


def first_hours(segment: str = "on_demand") -> dict[tuple[str, str], datetime]:
    """{(gpu, provider): first hour in the rollup}: when we began recording that provider for that GPU."""
    sql = text("SELECT gpu, provider, min(hour) AS first FROM market_hourly WHERE segment = :s GROUP BY gpu, provider")
    with normalize.SessionLocal() as s:
        return {(r.gpu, r.provider): r.first for r in s.execute(sql, {"s": segment})}


def coverage() -> dict:
    """How much history the rollup holds."""
    with normalize.SessionLocal() as s:
        r = s.execute(text("SELECT min(hour) AS first, max(hour) AS last, count(*) AS rows FROM market_hourly")).one()
    return dict(r._mapping)


def _f(v):
    return None if v is None else float(v)
