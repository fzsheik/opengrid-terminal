"""Reads over the raw snapshots."""

from sqlalchemy import func, select

from db import SessionLocal
from tables import RawSnapshot


def snapshots(limit: int = 200, q: str | None = None) -> list[dict]:
    """Recent fetches, newest first."""
    stmt = select(
        RawSnapshot.id,
        RawSnapshot.provider,
        RawSnapshot.endpoint,
        RawSnapshot.method,
        RawSnapshot.status_code,
        RawSnapshot.duration_ms,
        RawSnapshot.ok,
        RawSnapshot.fetched_at,
        func.length(RawSnapshot.payload.cast(__import__("sqlalchemy").Text)).label("bytes"),
    ).order_by(RawSnapshot.fetched_at.desc(), RawSnapshot.id.desc()).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(RawSnapshot.provider.ilike(like) | RawSnapshot.endpoint.ilike(like))
    with SessionLocal() as s:
        return [dict(r._mapping) for r in s.execute(stmt)]


def endpoint_summary() -> list[dict]:
    """One row per provider+endpoint: how many fetches, how many distinct bodies."""
    stmt = (
        select(
            RawSnapshot.provider,
            RawSnapshot.endpoint,
            func.count().label("fetches"),
            func.count(func.distinct(RawSnapshot.sha256)).label("distinct_bodies"),
            func.max(RawSnapshot.fetched_at).label("last_fetch"),
            func.round(func.avg(RawSnapshot.duration_ms)).label("avg_ms"),
            func.count().filter(RawSnapshot.ok.is_(False)).label("failures"),
        )
        .group_by(RawSnapshot.provider, RawSnapshot.endpoint)
        .order_by(RawSnapshot.provider, RawSnapshot.endpoint)
    )
    with SessionLocal() as s:
        return [dict(r._mapping) for r in s.execute(stmt)]


def latest_payload(provider: str, endpoint_like: str):
    stmt = (
        select(RawSnapshot.payload)
        .where(RawSnapshot.provider == provider, RawSnapshot.endpoint.ilike(f"%{endpoint_like}%"))
        .order_by(RawSnapshot.fetched_at.desc())
        .limit(1)
    )
    with SessionLocal() as s:
        return s.execute(stmt).scalar_one_or_none()
