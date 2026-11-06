"""Reads of the power tables. public_only=True is the ONLY mode a public route may use: it keeps series whose
stored visibility AND current registry verdict are both "public" (a verdict tightened in power/registry.py hides
the data at once, before any re-run rewrites power_series.visibility).

    catalog(public_only, source)                      the series with their depth and latest day
    days(keys, since, until, public_only, intervals)  day rows (mean / min / max / n), optionally the values
"""

from __future__ import annotations

from datetime import date, timedelta

from power import registry, storage

NOT_CAUSAL = "Context, not cause: power series are shown beside GPU data; nothing here says one moved the other."


def _allowed(source: str, stored: str, public_only: bool) -> bool:
    return not public_only or (stored == registry.PUBLIC and registry.visibility(source) == registry.PUBLIC)


def catalog(public_only: bool = True, source: str | None = None) -> list[dict]:
    from sqlalchemy import text

    with storage._session()() as s:
        rows = s.execute(text("""
            SELECT p.key, p.source, p.dataset, p.iso, p.kind, p.market, p.location, p.location_type, p.unit,
                   p.interval_minutes, p.tz, p.name, p.visibility,
                   count(d.day) AS days, min(d.day) AS earliest, max(d.day) AS latest
            FROM power_series p LEFT JOIN power_days d ON d.series_id = p.id
            WHERE (CAST(:src AS text) IS NULL OR p.source = :src)
            GROUP BY p.id ORDER BY p.key"""), {"src": source}).all()
    out = []
    for r in rows:
        if not _allowed(r.source, r.visibility, public_only):
            continue
        d = dict(r._mapping)
        d["visibility"] = registry.visibility(r.source) if r.visibility == registry.PUBLIC else r.visibility
        out.append(d)
    return out


def days(keys: list[str], since: date | None = None, until: date | None = None, public_only: bool = True,
         intervals: bool = False) -> list[dict]:
    from sqlalchemy import text

    with storage._session()() as s:
        rows = s.execute(text(f"""
            SELECT p.key, p.source, p.visibility, p.interval_minutes, p.tz, d.day, d.n, d.complete, d.mean, d.min,
                   d.max, d.revisions, d.first_seen_at{", d.vals" if intervals else ""}
            FROM power_days d JOIN power_series p ON p.id = d.series_id
            WHERE p.key = ANY(:keys) AND (CAST(:a AS date) IS NULL OR d.day >= :a)
              AND (CAST(:b AS date) IS NULL OR d.day <= :b)
            ORDER BY p.key, d.day"""), {"keys": keys, "a": since, "b": until}).all()
    out = []
    for r in rows:
        if not _allowed(r.source, r.visibility, public_only):
            continue
        d = {"key": r.key, "day": r.day, "n": r.n, "complete": r.complete, "mean": r.mean, "min": r.min,
             "max": r.max, "revisions": r.revisions, "first_seen_at": r.first_seen_at}
        if intervals:
            d["start"] = storage.day_start(r.day, r.tz)
            d["interval_minutes"] = r.interval_minutes
            d["values"] = list(r.vals)
        out.append(d)
    return out


def daily_means(key: str, since: date, until: date | None = None) -> dict[date, tuple[float, float]]:
    """{day: (mean, max)} of one series, any tier: for the derived cross-analysis only (power/cross.py)."""
    from sqlalchemy import text

    with storage._session()() as s:
        rows = s.execute(text("""SELECT d.day, d.mean, d.max FROM power_days d JOIN power_series p ON p.id = d.series_id
                                 WHERE p.key = :k AND d.day >= :a AND d.day <= :b AND d.mean IS NOT NULL"""),
                         {"k": key, "a": since, "b": until or since + timedelta(days=36500)}).all()
    return {r.day: (float(r.mean), float(r.max)) for r in rows}
