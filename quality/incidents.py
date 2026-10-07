"""Incidents: quality events that are not a held value.

One row per dedupe_key. Recurrence re-opens it and bumps `count` (only when it is seen
at a later time, so re-normalizing the same raw data does not inflate counts).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import literal_column, text, update
from sqlalchemy.dialects.postgresql import insert

import normalize
from store.quality import Incident

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


def key_for(kind: str, provider: str | None, listing_id: str | None = None, extra: str | None = None) -> str:
    return ":".join(x or "" for x in (kind, provider, listing_id, extra))[:512]


def record(session, kind: str, *, provider: str | None = None, listing_id: str | None = None,
           endpoint: str | None = None, severity: str = "notable", detail: dict | None = None,
           key: str | None = None, at: datetime | None = None) -> None:
    at = at or _now()
    stmt = insert(Incident).values(
        kind=kind, provider=provider, listing_id=listing_id, endpoint=endpoint, severity=severity,
        detail=detail, first_seen=at, last_seen=at, count=1, status="open",
        dedupe_key=key or key_for(kind, provider, listing_id),
    )
    ex = stmt.excluded
    session.execute(stmt.on_conflict_do_update(
        index_elements=["dedupe_key"],
        set_={
            "count": literal_column(
                "quality_incidents.count + CASE WHEN excluded.last_seen > quality_incidents.last_seen "
                "OR quality_incidents.status = 'resolved' THEN 1 ELSE 0 END"),
            "last_seen": literal_column("GREATEST(quality_incidents.last_seen, excluded.last_seen)"),
            "detail": ex.detail, "severity": ex.severity, "status": "open", "resolved_at": None,
        },
    ))


def resolve(session, kind: str, provider: str | None, listing_ids: list[str] | None = None,
            keep_keys: set[str] | None = None) -> int:
    """Close open incidents of `kind` for a provider (optionally only these listings)."""
    stmt = update(Incident).where(Incident.kind == kind, Incident.status == "open")
    if provider is not None:
        stmt = stmt.where(Incident.provider == provider)
    if listing_ids is not None:
        if not listing_ids:
            return 0
        stmt = stmt.where(Incident.listing_id.in_(listing_ids))
    if keep_keys:
        stmt = stmt.where(Incident.dedupe_key.not_in(keep_keys))
    return session.execute(stmt.values(status="resolved", resolved_at=_now())).rowcount


def record_now(kind: str, **kw) -> None:
    """Record in its own transaction, never raising: used from error paths."""
    try:
        with normalize.SessionLocal.begin() as s:
            record(s, kind, **kw)
    except Exception:
        log.exception("could not record %s incident", kind)


def recent(*, provider: str | None = None, kinds: list[str] | None = None, status: str | None = None,
           since: datetime | None = None, limit: int = 200) -> list[dict]:
    q = ["SELECT * FROM quality_incidents WHERE true"]
    params: dict = {"limit": limit}
    if provider:
        q.append("AND provider = :provider"); params["provider"] = provider
    if kinds:
        q.append("AND kind = ANY(:kinds)"); params["kinds"] = list(kinds)
    if status:
        q.append("AND status = :status"); params["status"] = status
    if since:
        q.append("AND last_seen >= :since"); params["since"] = since
    q.append("ORDER BY last_seen DESC, id DESC LIMIT :limit")
    with normalize.SessionLocal() as s:
        return [dict(r._mapping) for r in s.execute(text(" ".join(q)), params)]
