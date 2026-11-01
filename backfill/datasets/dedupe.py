"""Never double count. A dataset point that repeats a primary source (store/archive.py PRIMARY_SOURCES) for the
same provider, listing and date is NOT stored as a second point: it is recorded as corroboration on the primary row
(archived_prices.corroboration), so the history keeps one point per (provider, listing, date) and the agreement (or
disagreement: the corroborating price is kept as stated) stays visible.

"Same listing" = same provider, market_type and GPU count, and the same canonical GPU (or, when either side has no
canonical GPU, the same sku). "Same date" = the dataset point's time falls inside the primary row's period
(an AWS price-list version: [observed_at, valid_to)) or, for a point-in-time row (a capture, an "effective since"
anchor), on the same UTC day.

    split(rows)            -> (rows to insert, [(primary row id, corroboration entry)])
    corroborate(pairs)     -> entries added (idempotent: an entry for the same source document is added once)
    store(rows)            -> {"inserted", "corroborated"}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

import normalize
from store.archive import PRIMARY_SOURCES

_EXISTING = text("""
    SELECT id, provider, market_type, gpu_count, gpu, sku, observed_at, valid_to
    FROM archived_prices
    WHERE source = ANY(:sources) AND provider = ANY(:providers)
""")


def _day(t: datetime):
    return t.astimezone(timezone.utc).date()


def _same_listing(a: dict, b) -> bool:
    if a["provider"] != b.provider or a["market_type"] != b.market_type or a["gpu_count"] != b.gpu_count:
        return False
    if a["gpu"] and b.gpu:
        return a["gpu"] == b.gpu
    return bool(a.get("sku")) and a.get("sku") == b.sku


def _same_date(t: datetime, b) -> bool:
    if b.valid_to is not None:
        return b.observed_at <= t < b.valid_to
    return _day(t) == _day(b.observed_at)


def entry(row: dict) -> dict:
    return {"source": row["source"], "source_url": row["source_url"], "listing_id": row["listing_id"],
            "observed_at": row["observed_at"].isoformat(), "price_per_gpu_hour": str(row["price_per_gpu_hour"]),
            "licence": row.get("licence")}


def split(rows: list[dict]) -> tuple[list[dict], list[tuple[int, dict]]]:
    if not rows:
        return [], []
    with normalize.SessionLocal() as s:
        existing = s.execute(_EXISTING, {"sources": list(PRIMARY_SOURCES),
                                         "providers": sorted({r["provider"] for r in rows})}).all()
    index: dict[tuple, list] = {}
    for b in existing:
        index.setdefault((b.provider, b.market_type, b.gpu_count), []).append(b)
    keep, pairs = [], []
    for r in rows:
        match = next((b for b in index.get((r["provider"], r["market_type"], r["gpu_count"]), [])
                      if _same_listing(r, b) and _same_date(r["observed_at"], b)), None)
        if match is None:
            keep.append(r)
        else:
            pairs.append((match.id, entry(r)))
    return keep, pairs


def corroborate(pairs: list[tuple[int, dict]]) -> int:
    n = 0
    with normalize.SessionLocal.begin() as s:
        for rid, e in pairs:
            key = json.dumps([{"source": e["source"], "source_url": e["source_url"], "listing_id": e["listing_id"]}])
            n += s.execute(text("""
                UPDATE archived_prices SET corroboration = coalesce(corroboration, '[]'::jsonb) || CAST(:e AS jsonb)
                WHERE id = :id AND NOT coalesce(corroboration, '[]'::jsonb) @> CAST(:key AS jsonb)
            """), {"id": rid, "e": json.dumps([e]), "key": key}).rowcount
    return n


def store(rows: list[dict], *, dry_run: bool = False) -> dict:
    from backfill import load

    keep, pairs = split(rows)
    if dry_run:
        return {"inserted": 0, "corroborated": 0, "would_insert": len(keep), "would_corroborate": len(pairs)}
    return {"inserted": load.store(keep), "corroborated": corroborate(pairs), "candidates": len(rows),
            "matched_primary": len(pairs)}
