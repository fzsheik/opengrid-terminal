"""Power storage: the market-day grid, the series catalogue, fetch records and series-day upserts.

    day_start(day, tz)                 the UTC instant the market day starts (local midnight)
    slots(day, tz, minutes)            intervals in that day: 24 hourly, 23 / 25 on DST days
    to_days(dataset, points, start, end) parsed points -> {(series_key, day): [value | None, ...]}
    sync_series(datasets)              upsert the catalogue (visibility from the registry); {key: id}
    record_fetch(dataset, f, keep)     a power_fetches row (bytes kept compressed when keep, else url + sha256)
    upsert_days(rows, fetch_id)        INSERT .. ON CONFLICT: a changed day is a revision (counted)
    done_days(dataset, start, end)     market days already stored complete (the backfill skips them)

Slot i of a day holds the interval starting at day_start + i x interval, in absolute time, so a DST day simply has
fewer or more slots and nothing is ever shifted. A value the source did not give is NULL, never 0 or carried over.
"""

from __future__ import annotations

import gzip
import hashlib
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from power import registry
from power.registry import Dataset, Fetched

UTC = timezone.utc


def _session():
    import normalize

    return normalize.SessionLocal


# --------------------------------------------------------------------------- the grid

def day_start(day: date, tz: str) -> datetime:
    return datetime.combine(day, time(0), tzinfo=ZoneInfo(tz)).astimezone(UTC)


def slots(day: date, tz: str, minutes: int) -> int:
    if minutes >= registry.DAILY:
        return 1              # a daily / weekly / monthly period: one value, dated by the source's label
    span = day_start(day + timedelta(days=1), tz) - day_start(day, tz)
    return int(span.total_seconds() // (minutes * 60))


def market_day(t: datetime, tz: str) -> date:
    return t.astimezone(ZoneInfo(tz)).date()


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def to_days(ds: Dataset, points: dict[str, dict[datetime, float | None]], start: date, end: date
            ) -> dict[tuple[str, date], list[float | None]]:
    """Points on the dataset's grid -> one array per (series, market day) in start..end. A point off the grid
    (not a whole interval from the day start) or outside the days is dropped; a key not in the dataset too."""
    keys = {s.key for s in ds.series}
    step = ds.interval_minutes * 60
    out: dict[tuple[str, date], list[float | None]] = {}
    starts: dict[date, datetime] = {}
    for key, pts in points.items():
        if key not in keys:
            continue
        for t, v in pts.items():
            d = t if isinstance(t, date) and not isinstance(t, datetime) else market_day(t, ds.tz)
            if d < start or d > end:
                continue
            if ds.interval_minutes >= registry.DAILY:   # periodic: keyed by the period's date
                out[(key, d)] = [None if v is None or not math.isfinite(float(v)) else float(v)]
                continue
            if d not in starts:
                starts[d] = day_start(d, ds.tz)
            off = (t - starts[d]).total_seconds()
            if off < 0 or off % step:
                continue
            arr = out.get((key, d))
            if arr is None:
                arr = out[(key, d)] = [None] * slots(d, ds.tz, ds.interval_minutes)
            i = int(off // step)
            if i < len(arr):
                arr[i] = None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)
    return out


def summary(vals: list[float | None]) -> dict:
    xs = [v for v in vals if v is not None]
    return {"n": len(xs), "complete": len(xs) == len(vals) and bool(vals),
            "mean": sum(xs) / len(xs) if xs else None, "min": min(xs) if xs else None, "max": max(xs) if xs else None}


# --------------------------------------------------------------------------- catalogue

def sync_series(datasets: list[Dataset], now: datetime | None = None) -> dict[str, int]:
    """Insert new series, refresh names / visibility of known ones (the registry's verdict wins every run)."""
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerSeries as P

    now = now or datetime.now(UTC)
    rows = []
    for ds in datasets:
        vis = registry.visibility(ds.source)
        for s in ds.series:
            rows.append({"key": s.key, "source": ds.source, "dataset": ds.key, "iso": s.iso, "kind": s.kind,
                         "commodity": s.commodity,
                         "market": s.market, "location": s.location, "location_type": s.location_type, "unit": s.unit,
                         "interval_minutes": ds.interval_minutes, "tz": ds.tz, "name": s.name, "visibility": vis,
                         "meta": s.meta, "created_at": now, "updated_at": now})
    if not rows:
        return {}
    out = {}
    with _session().begin() as s:
        for i in range(0, len(rows), 500):
            stmt = insert(P).values(rows[i:i + 500])
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[P.key],
                set_={c: getattr(ex, c) for c in ("source", "dataset", "iso", "kind", "commodity", "market", "location",
                                                  "location_type", "unit", "interval_minutes", "tz", "name",
                                                  "visibility", "meta", "updated_at")},
            ).returning(P.key, P.id)
            out.update(dict(s.execute(stmt).all()))
    return out


# --------------------------------------------------------------------------- raw

def _encode(body: bytes) -> tuple[bytes, str]:
    if body[:4] == b"PK\x03\x04" or body[:2] == b"\x1f\x8b":
        return body, "identity"           # already compressed (OASIS zips): kept exactly as received
    return gzip.compress(body, compresslevel=9, mtime=0), "gzip"


def decode(body: bytes, encoding: str | None) -> bytes:
    return gzip.decompress(body) if encoding == "gzip" else body


def record_fetch(ds: Dataset, f: Fetched, keep: bool, days_stored: int = 0, fetched_at: datetime | None = None) -> int:
    from store.power import PowerFetch

    kept = keep and f.body is not None and f.ok
    body, enc = _encode(f.body) if kept else (None, None)
    with _session().begin() as s:
        row = PowerFetch(source=ds.source, dataset=ds.key, url=f.url[:1000], period_start=f.start, period_end=f.end,
                         fetched_at=fetched_at or datetime.now(UTC), status_code=f.status, duration_ms=f.ms,
                         ok=f.ok, empty=f.empty, error=(f.error or None) and f.error[:2000],
                         bytes=None if f.body is None else len(f.body),
                         sha256=None if f.body is None else hashlib.sha256(f.body).hexdigest(),
                         content_type=(f.content_type or None) and f.content_type[:80],
                         mode="body" if kept else "hash", encoding=enc, body=body, days_stored=days_stored)
        s.add(row)
        s.flush()
        return row.id


def set_days_stored(fetch_id: int, n: int) -> None:
    from sqlalchemy import update

    from store.power import PowerFetch

    with _session().begin() as s:
        s.execute(update(PowerFetch).where(PowerFetch.id == fetch_id).values(days_stored=n))


# --------------------------------------------------------------------------- series-days

def upsert_days(rows: dict[tuple[str, date], list[float | None]], ids: dict[str, int], fetch_id: int | None,
                now: datetime | None = None) -> dict:
    """A new (series, day) is inserted; a known one is replaced only when its values differ, counted as a
    revision. A partial re-read never overwrites a more complete stored day (n would drop)."""
    from sqlalchemy import literal_column
    from sqlalchemy.dialects.postgresql import insert

    from store.power import PowerDay as D

    now = now or datetime.now(UTC)
    vals = []
    for (key, d), arr in rows.items():
        if key not in ids:
            continue
        sm = summary(arr)
        if not sm["n"]:
            continue
        vals.append({"series_id": ids[key], "day": d, "vals": arr, **sm, "fetch_id": fetch_id,
                     "first_seen_at": now, "updated_at": now, "revisions": 0})
    new = revised = 0
    if not vals:
        return {"new": 0, "revised": 0, "unchanged": 0}
    with _session().begin() as s:
        for i in range(0, len(vals), 1000):
            stmt = insert(D).values(vals[i:i + 1000])
            ex = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[D.series_id, D.day],
                set_={"vals": ex.vals, "n": ex.n, "complete": ex.complete, "mean": ex.mean, "min": ex.min,
                      "max": ex.max, "fetch_id": ex.fetch_id, "updated_at": ex.updated_at,
                      "revisions": D.revisions + 1},
                where=(D.vals.is_distinct_from(ex.vals)) & (ex.n >= D.n),
            ).returning(literal_column("(xmax = 0)").label("inserted"))
            for r in s.execute(stmt):
                if r.inserted:
                    new += 1
                else:
                    revised += 1
    return {"new": new, "revised": revised, "unchanged": len(vals) - new - revised}


def done_days(ds: Dataset, start: date, end: date, empty_before: date | None = None) -> set[date]:
    """Days with at least one stored row for the dataset and no incomplete one; plus, before `empty_before`, every
    day a wholly successful chunk covered (days_stored > 0) or the source answered "no data" for: the source's
    own gaps (an EIA hour never reported) are final there and not re-asked on every run. Days from `empty_before`
    on are re-read until complete (a recent day may still fill)."""
    from sqlalchemy import text

    with _session()() as s:
        rows = s.execute(text("""
            SELECT d.day, bool_and(d.complete) FROM power_days d JOIN power_series p ON p.id = d.series_id
            WHERE p.dataset = :ds AND d.day BETWEEN :a AND :b GROUP BY d.day"""),
                         {"ds": ds.key, "a": start, "b": end}).all()
        out = {r[0] for r in rows if r[1]}
        if empty_before:
            for a, b in s.execute(text("""SELECT period_start, period_end FROM power_fetches
                                          WHERE dataset = :ds AND ok AND (empty OR days_stored > 0)
                                          AND period_start IS NOT NULL
                                          AND period_end >= :a AND period_start <= :b"""),
                                  {"ds": ds.key, "a": start, "b": end}).all():
                out.update(d for d in days(max(a, start), min(b, end)) if d < empty_before)
    return out


def latest_day(ds: Dataset) -> date | None:
    from sqlalchemy import text

    with _session()() as s:
        return s.execute(text("""SELECT max(d.day) FROM power_days d JOIN power_series p ON p.id = d.series_id
                                 WHERE p.dataset = :ds"""), {"ds": ds.key}).scalar()
