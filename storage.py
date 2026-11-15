"""Bound raw diagnostic storage without discarding market or billing history."""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from config import settings
from db import SessionLocal
from jobs import job
from tables import RawPayload, RawSnapshot

log = logging.getLogger(__name__)
STORAGE_LOCK = 7014630016


def content_hash(payload) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def lock_storage(session):
    # Serializes body insertion and garbage collection, preventing a body from
    # being collected between insertion and the snapshot referencing it.
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": STORAGE_LOCK})


def deduplicate_batch(batch: int = 250) -> int:
    with SessionLocal.begin() as s:
        lock_storage(s)
        rows = s.execute(select(RawSnapshot.id, RawSnapshot._inline_payload)
                         .where(RawSnapshot._inline_payload.is_not(None), RawSnapshot.payload_hash.is_(None))
                         .order_by(RawSnapshot.id).limit(batch)).all()
        bodies = {content_hash(r[1]): r[1] for r in rows if r[1] is not None}
        if bodies:
            s.execute(insert(RawPayload).values([
                {"sha256": digest, "payload": bodies[digest]} for digest in sorted(bodies)
            ]).on_conflict_do_nothing(index_elements=["sha256"]))
        if rows:
            s.execute(update(RawSnapshot), [
                {"id": rid, "_inline_payload": None,
                 "payload_hash": content_hash(payload) if payload is not None else None,
                 "sha256": content_hash(payload) if payload is not None else None}
                for rid, payload in rows
            ])
    return len(rows)


# Keep the complete newest successful round (Salad has many per-class responses),
# and the newest attempt, even for providers that have been offline for months.
# Unprocessed schema-watch rows are also protected until its cursor passes them.
# Snapshots an official index fixing was computed from are evidence: kept forever (methodology/settlement.md).
_CANDIDATES = """
WITH latest AS (
    SELECT provider, endpoint, max(fetched_at) AS last_fetch,
           max(fetched_at) FILTER (WHERE ok) AS last_ok
    FROM raw_snapshots GROUP BY provider, endpoint
)
SELECT r.id FROM raw_snapshots r JOIN latest l USING (provider, endpoint)
WHERE r.fetched_at < :cutoff
  AND r.fetched_at < l.last_fetch - interval '120 seconds'
  AND (l.last_ok IS NULL OR r.fetched_at < l.last_ok - interval '120 seconds' OR r.fetched_at > l.last_ok)
  AND r.id <= coalesce((SELECT position FROM quality_cursor WHERE name = 'schema_watch'), 9223372036854775807)
  AND NOT EXISTS (SELECT 1 FROM fixing_evidence e WHERE e.snapshot_id = r.id)
"""


def retention_batch(now: datetime, batch: int = 1000) -> dict:
    if not (1 <= settings.raw_payload_retention_days <= settings.raw_snapshot_retention_days):
        raise ValueError("raw retention must satisfy 1 <= payload days <= snapshot days")
    if settings.news_fetch_retention_days < 1:
        raise ValueError("news fetch retention must be at least one day")
    result = {}
    with SessionLocal.begin() as s:
        lock_storage(s)
        s.execute(text("SET LOCAL statement_timeout = '30s'"))
        payload_ids = _CANDIDATES + " AND (r.payload_hash IS NOT NULL OR r.payload IS NOT NULL) ORDER BY r.id LIMIT :batch"
        result["expired_bodies"] = s.execute(text(
            "UPDATE raw_snapshots SET payload = NULL, payload_hash = NULL WHERE id IN (" + payload_ids + ")"
        ), {"cutoff": now - timedelta(days=settings.raw_payload_retention_days), "batch": batch}).rowcount
        result["expired_snapshots"] = s.execute(text(
            "DELETE FROM raw_snapshots WHERE id IN (" + _CANDIDATES + " ORDER BY r.id LIMIT :batch)"
        ), {"cutoff": now - timedelta(days=settings.raw_snapshot_retention_days), "batch": batch}).rowcount
        result["expired_news_fetches"] = s.execute(text("""
            DELETE FROM news_fetch_log WHERE id IN (
                SELECT id FROM news_fetch_log WHERE fetched_at < :cutoff ORDER BY fetched_at LIMIT :batch
            )"""), {"cutoff": now - timedelta(days=settings.news_fetch_retention_days), "batch": batch}).rowcount
        result["unused_bodies"] = s.execute(text("""
            DELETE FROM raw_payloads WHERE sha256 IN (
                SELECT p.sha256 FROM raw_payloads p WHERE NOT EXISTS (
                    SELECT 1 FROM raw_snapshots r WHERE r.payload_hash = p.sha256
                ) AND NOT EXISTS (SELECT 1 FROM fixing_evidence e WHERE e.payload_sha256 = p.sha256) LIMIT :batch
            )"""), {"batch": batch}).rowcount
    return result


@job("raw_storage", every_seconds=60, initial_delay_seconds=45)
def maintain(batch: int = 250, max_batches: int = 100) -> dict:
    """Gradual legacy backfill and bounded cleanup, with a commit after each batch."""
    result = {"deduplicated_snapshots": 0}
    for _ in range(max_batches):
        n = deduplicate_batch(batch)
        result["deduplicated_snapshots"] += n
        if n < batch:
            break
    result.update(retention_batch(datetime.now(timezone.utc)))
    log.info("raw storage maintenance: %s", result)
    return result


# ---------------------------------------------------------------------------
# Volume watch: a daily ops alert before the Postgres volume fills. A full volume stops every write (prices,
# indices, fixings) and live data not recorded cannot be backfilled.
# ---------------------------------------------------------------------------

CRITICAL_SHARE = 0.95


def volume_usage(session) -> dict:
    """Bytes Postgres reports (database + WAL), the estimated volume use and its share of DB_VOLUME_MB."""
    db_bytes = session.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    try:
        with session.begin_nested():
            wal = session.execute(text("SELECT coalesce(sum(size), 0) FROM pg_ls_waldir()")).scalar_one()
    except Exception:  # pg_ls_waldir needs pg_monitor; without it the overhead factor carries the WAL too
        wal = 0
    est = (db_bytes + int(wal)) * settings.db_volume_overhead
    limit = settings.db_volume_mb * 1024 * 1024
    top = [dict(r._mapping) for r in session.execute(text("""
        SELECT relname AS name, pg_total_relation_size(relid) AS bytes
        FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 3"""))]
    return {"database_bytes": int(db_bytes), "wal_bytes": int(wal), "estimated_volume_bytes": int(est),
            "limit_bytes": limit, "share": est / limit, "top": top}


def volume_message(u: dict) -> dict:
    from alerts.channels import _base, _embed

    mb = lambda b: f"{b / 1024 / 1024:,.0f} MB"  # noqa: E731
    critical = u["share"] >= CRITICAL_SHARE
    title = (f"Database volume {u['share']:.0%} full" + (": writes stop when it fills" if critical else ""))
    desc = ("The Postgres volume is close to full. When it fills, price polling, indices and fixings stop "
            "writing, and live data that is not recorded cannot be backfilled. Grow the volume in Railway "
            "(Postgres service, Volume) or move raw payloads to object storage.")
    fields = [("Estimated used", f"{mb(u['estimated_volume_bytes'])} of {mb(u['limit_bytes'])}", True),
              ("Database + WAL", f"{mb(u['database_bytes'])} + {mb(u['wal_bytes'])}", True),
              ("Largest tables", ", ".join(f"{t['name']} {mb(t['bytes'])}" for t in u["top"]), False)]
    return {**_base(), "embeds": [_embed(title, desc, fields, url=None, at=datetime.now(timezone.utc),
                                         footer="storage.volume_check · daily")]}


@job("db_volume_watch", every_seconds=6 * 3600, initial_delay_seconds=600)
def volume_check(now: datetime | None = None) -> dict:
    """Post to the ops channel at most once a day while the volume is past DB_VOLUME_ALERT_SHARE (and once more
    the same day if it crosses 95%)."""
    from alerts import channels

    now = now or datetime.now(timezone.utc)
    with SessionLocal() as s:
        u = volume_usage(s)
    out = {"share": round(u["share"], 4), "posted": False}
    if u["share"] < settings.db_volume_alert_share:
        return out
    level = "critical" if u["share"] >= CRITICAL_SHARE else "warn"
    key = f"db_volume:{now.date().isoformat()}:{level}"
    with SessionLocal.begin() as s:
        if not channels._claim(s, "ops", key):
            return out
    if channels.send("ops", volume_message(u)):
        out["posted"] = True
    else:
        with SessionLocal.begin() as s:
            channels._unclaim(s, "ops", key)
    log.warning("database volume %.0f%% of %s MB (posted=%s)", u["share"] * 100, settings.db_volume_mb, out["posted"])
    return out


def status() -> dict:
    with SessionLocal() as s:
        size = s.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        tables = [dict(r._mapping) for r in s.execute(text("""
            SELECT relname AS name, pg_total_relation_size(relid) AS bytes,
                   n_live_tup AS estimated_rows, n_dead_tup AS estimated_dead_rows, last_autovacuum
            FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 12
        """))]
        counts = dict(s.execute(text("""
            SELECT count(*) AS snapshots,
                   count(*) FILTER (WHERE payload IS NOT NULL AND payload_hash IS NULL) AS inline_pending,
                   count(payload_hash) AS shared_references,
                   (SELECT count(*) FROM raw_payloads) AS unique_bodies
            FROM raw_snapshots
        """)).one()._mapping)
    return {"database_bytes": size, "tables": tables, "raw": counts,
            "retention_days": {"payloads": settings.raw_payload_retention_days,
                               "fetch_metadata": settings.raw_snapshot_retention_days,
                               "news_fetch_logs": settings.news_fetch_retention_days}}
