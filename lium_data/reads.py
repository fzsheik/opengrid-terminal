"""Reads for the admin endpoints (api/lium.py). Every value here is INTERNAL; callers are admin-only."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select

import canonical
from lium_data import feeds as F
from lium_data import partner, public
from store.lium import (LiumDailyAggregate, LiumFeedSnapshot, LiumFeedValue, LiumGrafanaHourly, LiumGrafanaRental,
                        LiumHourlyGpu, LiumModelStatsDaily, LiumThroughputDaily, LiumTrade)


def _session():
    import normalize

    return normalize.SessionLocal


def status() -> dict:
    """Per feed: the newest answer (state, when first and last seen, rows), how many distinct answers are stored,
    and whether partner ingestion is configured."""
    with _session()() as s:
        counts = dict(s.execute(select(LiumFeedSnapshot.feed, func.count()).group_by(LiumFeedSnapshot.feed)).all())
        out = []
        for feed in (*F.FEEDS.values(), *F.GRAFANA_PANELS.values()):
            last = s.execute(select(LiumFeedSnapshot).where(LiumFeedSnapshot.feed == feed.name)
                             .order_by(desc(LiumFeedSnapshot.last_seen_at), desc(LiumFeedSnapshot.id)).limit(1)).scalar()
            path = f"{feed.path} · {feed.title}" if isinstance(feed, F.Panel) else "/api" + feed.path
            out.append({"feed": feed.name, "path": path, "partner": feed.partner, "kind": feed.kind,
                        "basis": feed.basis, "what": feed.what, "stored_answers": counts.get(feed.name, 0),
                        "state": last.state if last else None, "status_code": last.status_code if last else None,
                        "first_seen_at": last.fetched_at if last else None,
                        "last_seen_at": last.last_seen_at if last else None,
                        "seen_count": last.seen_count if last else None, "rows": last.rows if last else None,
                        "error": last.error if last else None, "visibility": last.visibility if last else None})
        tables = {name: s.execute(select(func.count()).select_from(t)).scalar() for name, t in (
            ("lium_trades", LiumTrade), ("lium_hourly_gpu", LiumHourlyGpu), ("lium_daily_aggregate", LiumDailyAggregate),
            ("lium_feed_values", LiumFeedValue), ("lium_throughput_daily", LiumThroughputDaily),
            ("lium_model_stats_daily", LiumModelStatsDaily), ("lium_grafana_rentals", LiumGrafanaRental),
            ("lium_grafana_hourly", LiumGrafanaHourly))}
        span = s.execute(select(func.min(LiumTrade.end_at), func.max(LiumTrade.end_at))).one()
        starts = s.execute(select(func.min(LiumGrafanaRental.start_at), func.max(LiumGrafanaRental.start_at))).one()
    return {"partner_key_configured": partner.api_key() is not None, "feeds": out, "rows": tables,
            "trades_end_range": {"first": span[0], "last": span[1]},
            "grafana_rentals_start_range": {"first": starts[0], "last": starts[1]}}


def current(feed: str) -> dict | None:
    """The newest stored answer of a public feed, pivoted: one row per (subject, gpu_count) with its metrics."""
    with _session()() as s:
        snap = s.execute(select(LiumFeedSnapshot).where(LiumFeedSnapshot.feed == feed, LiumFeedSnapshot.ok.is_(True))
                         .order_by(desc(LiumFeedSnapshot.fetched_at), desc(LiumFeedSnapshot.id)).limit(1)).scalar()
        last = s.execute(select(LiumFeedSnapshot).where(LiumFeedSnapshot.feed == feed)
                         .order_by(desc(LiumFeedSnapshot.last_seen_at), desc(LiumFeedSnapshot.id)).limit(1)).scalar()
        vals = [] if snap is None else [v for v in public.latest_values(s, feed, snap.fetched_at).values() if v.present]
        gen = max((v.generated_at for v in vals if v.generated_at), default=None)
    if last is None:
        return None
    rows: dict[tuple, dict] = {}
    meta: dict = {}
    for v in vals:
        x = v.value if v.value is not None else (v.value_time or v.value_text)
        if v.subject == "*":
            meta[v.metric] = x
            continue
        r = rows.setdefault((v.subject, v.gpu_count), {"subject": v.subject, "gpu_count": v.gpu_count or None,
                                                       "canonical_gpu_name": canonical.canonical_gpu_name(v.subject)})
        r[v.metric] = x
    f = F.FEEDS[feed]
    return {"feed": feed, "kind": f.kind, "basis": f.basis, "what": f.what, "state": last.state,
            "error": last.error, "last_checked_at": last.last_seen_at,
            "answer_first_seen_at": snap.fetched_at if snap else None,
            "answer_last_seen_at": snap.last_seen_at if snap else None,
            "generated_at": gen, "feed_wide": meta, "rows": list(rows.values())}


def history(feed: str, subject: str, metric: str, gpu_count: int = 0, days: int = 30) -> list[dict]:
    """One metric's changes over time, newest first (the table is change-only; present false: it left the feed)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with _session()() as s:
        rows = s.execute(select(LiumFeedValue.observed_at, LiumFeedValue.generated_at, LiumFeedValue.value,
                                LiumFeedValue.value_text, LiumFeedValue.present)
                         .where(LiumFeedValue.feed == feed, LiumFeedValue.subject == subject,
                                LiumFeedValue.metric == metric, LiumFeedValue.gpu_count == gpu_count,
                                LiumFeedValue.observed_at >= since)
                         .order_by(desc(LiumFeedValue.observed_at))).all()
    return [{"observed_at": r[0], "generated_at": r[1], "value": r[2], "value_text": r[3], "present": r[4]} for r in rows]


def throughput(days: int = 60) -> dict:
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    with _session()() as s:
        daily = s.execute(select(LiumThroughputDaily).where(LiumThroughputDaily.day >= since)
                          .order_by(desc(LiumThroughputDaily.day))).scalars().all()
        last_day = s.execute(select(func.max(LiumModelStatsDaily.day))).scalar()
        models = [] if last_day is None else s.execute(select(LiumModelStatsDaily).where(
            LiumModelStatsDaily.day == last_day).order_by(LiumModelStatsDaily.gpu_model)).scalars().all()
    cols = ("day", "gmv_usd", "gpu_hours", "rentals", "utilisation", "utilisation_partial", "revisions", "updated_at")
    mcols = ("gpu_model", "median_price_30d", "utilisation_30d", "from_price", "gpus_listed", "gpus_rented", "observed_at")
    return {"kind": F.TRANSACTION, "basis": F.PUBLISHED_AGGREGATE, "source": "lium.io/api/public/v1/stats",
            "daily": [{c: getattr(r, c) for c in cols} for r in daily],
            "models_day": last_day,
            "models": [{**{c: getattr(r, c) for c in mcols}, "canonical_gpu_name": canonical.canonical_gpu_name(r.gpu_model)}
                       for r in models]}


def model_price_history(gpu_model: str, days: int = 90) -> list[dict]:
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    with _session()() as s:
        rows = s.execute(select(LiumModelStatsDaily).where(LiumModelStatsDaily.gpu_model == gpu_model,
                                                            LiumModelStatsDaily.day >= since)
                         .order_by(desc(LiumModelStatsDaily.day))).scalars().all()
    return [{"day": r.day, "median_price_30d": r.median_price_30d, "utilisation_30d": r.utilisation_30d,
             "from_price": r.from_price} for r in rows]


def hourly(hours: int = 48, gpu: str | None = None) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = select(LiumHourlyGpu).where(LiumHourlyGpu.hour_utc >= since)
    if gpu:
        stmt = stmt.where(LiumHourlyGpu.gpu_model == gpu)
    with _session()() as s:
        rows = s.execute(stmt.order_by(desc(LiumHourlyGpu.hour_utc), LiumHourlyGpu.gpu_model)).scalars().all()
    return [{"hour_utc": r.hour_utc, "gpu_model": r.gpu_model, "rentals": r.rentals, "gpu_hours": r.gpu_hours,
             "mean_price_per_gpu_hour_usd": r.mean_price_per_gpu_hour_usd} for r in rows]


def daily_aggregate(days: int = 30, gpu: str | None = None) -> list[dict]:
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    stmt = select(LiumDailyAggregate).where(LiumDailyAggregate.day >= since)
    if gpu:
        stmt = stmt.where(LiumDailyAggregate.gpu_model == gpu)
    with _session()() as s:
        rows = s.execute(stmt.order_by(desc(LiumDailyAggregate.day), LiumDailyAggregate.gpu_model,
                                       LiumDailyAggregate.country)).scalars().all()
    return [{"day": r.day, "gpu_model": r.gpu_model, "country": r.country or None, "deals": r.deals,
             "gpu_hours": r.gpu_hours, "vwap": r.vwap, "low": r.low, "high": r.high} for r in rows]


def trade_stats_vs_published(days: int = 30) -> list[dict]:
    """OpenGrid's VWAP from trade records beside Lium's own daily VWAP (all countries, GPU-hour weighted), per day
    and GPU model: a cross-check of the two partner feeds."""
    ours = {(r["day"], r["gpu_model"]): r for r in partner.trade_stats(days)}
    theirs: dict[tuple, list] = defaultdict(lambda: [0, 0])
    for r in daily_aggregate(days):
        if r["vwap"] is not None and r["gpu_hours"]:
            t = theirs[(r["day"], r["gpu_model"])]
            t[0] += r["vwap"] * r["gpu_hours"]
            t[1] += r["gpu_hours"]
    out = []
    for key in sorted(set(ours) | set(theirs), reverse=True):
        o, t = ours.get(key), theirs.get(key)
        out.append({"day": key[0], "gpu_model": key[1], "vwap_from_trades": o["vwap"] if o else None,
                    "vwap_published": round(t[0] / t[1], 4) if t and t[1] else None,
                    "gpu_hours_from_trades": o["gpu_hours"] if o else None,
                    "gpu_hours_published": t[1] if t else None})
    return out


# --------------------------------------------------------------------------
# Lium's public Grafana (lium_data/grafana.py)
# --------------------------------------------------------------------------


def grafana_rental_stats(days: int = 30, gpu: str | None = None, tier: str | None = None, by: str = "day") -> list[dict]:
    """Per UTC day (or hour) of rental START, GPU model and tier: rentals, and the median, mean, lowest and highest
    price per GPU-hour paid. Every rental counts once whatever its size or length: the panel gives no GPU count or
    end time, so this is not volume-weighted. Rentals at a price of zero are left out."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    R = LiumGrafanaRental
    bucket = func.date_trunc("hour" if by == "hour" else "day", R.start_at).label("bucket")
    price = R.price_per_gpu_hour
    stmt = (select(bucket, R.gpu_model, R.tier, func.count(), func.percentile_cont(0.5).within_group(price),
                   func.avg(price), func.min(price), func.max(price))
            .where(R.start_at >= since, price > 0).group_by(bucket, R.gpu_model, R.tier))
    if gpu:
        stmt = stmt.where(R.gpu_model == gpu)
    if tier:
        stmt = stmt.where(R.tier == tier)
    with _session()() as s:
        rows = s.execute(stmt.order_by(desc(bucket), R.gpu_model, R.tier)).all()
    when = "hour_utc" if by == "hour" else "day"
    return [{when: r[0] if by == "hour" else r[0].date(), "gpu_model": r[1],
             "canonical_gpu_name": canonical.canonical_gpu_name(r[1]), "tier": r[2], "rentals": r[3],
             "median": round(float(r[4]), 4), "mean": round(float(r[5]), 4), "low": r[6], "high": r[7]} for r in rows]


def grafana_hourly(metric: str, hours: int = 48, gpu: str | None = None) -> list[dict]:
    """One hourly series (a column of lium_grafana_hourly), newest first; hours with no value are left out."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    H = LiumGrafanaHourly
    column = getattr(H, metric)
    stmt = select(H.hour_utc, H.gpu_model, column, H.updated_at).where(H.hour_utc >= since, column.is_not(None))
    if gpu:
        stmt = stmt.where(H.gpu_model == gpu)
    with _session()() as s:
        rows = s.execute(stmt.order_by(desc(H.hour_utc), H.gpu_model)).all()
    return [{"hour_utc": r[0], "gpu_model": r[1], "value": r[2], "updated_at": r[3]} for r in rows]
