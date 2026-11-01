"""How fresh each provider's data is, per stream (price, capacity): GET /v1/freshness (methodology/freshness.md).

    record(provider)   after every poll (poller.poll_once): one freshness_log row per stream with the newest stored
                       observed_at (compute_listings for price, capacity_current for capacity). Fails open.
    report()           per provider and stream: tier, target cadence, SLA, last successful price fetch, newest data,
                       its age now, p50 / p95 age over 24 h, detection lag where the provider publishes change times,
                       throttled answers in 24 h.

Age over 24 h: sampled once a minute; at each minute the age is (minute - newest data_at logged by then). Minutes
before the first log row are not sampled, so a provider with less than 24 h of log reports `samples` < 1440.
SLA: p95 age <= 2 x the stream's cadence ("meeting"), else "breaching"; "unknown" with no samples.
Detection lag (7 days): observed_at - provider_changed_at over `exact` history rows that have a bracket and whose
provider time falls inside it (changed_at.py), i.e. how late OpenGrid learned of a change the provider timed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from cache import ttl_cache

log = logging.getLogger(__name__)

STREAMS = ("price", "capacity")
WINDOW = timedelta(hours=24)
LAG_WINDOW = timedelta(days=7)
LOG_DAYS = 8
SAMPLE = timedelta(minutes=1)
SLA_FACTOR = 2
# What each provider's history rows can say about when a price changed (changed_at.py).
PROVIDER_TIMESTAMPS = {"aws": "exact", "nebius": "exact", "azure": "date_only", "gcp": "lower_bound"}

_NEWEST = text("""
    SELECT (SELECT max(observed_at) FROM compute_listings WHERE provider = :p) AS price,
           (SELECT max(observed_at) FROM capacity_current WHERE provider = :p) AS capacity
""")


def record(provider: str, now: datetime | None = None) -> int:
    """Log how new the provider's stored data is right after a poll. Never raises."""
    import normalize

    now = now or datetime.now(timezone.utc)
    try:
        with normalize.SessionLocal.begin() as s:
            newest = s.execute(_NEWEST, {"p": provider}).one()
            rows = [{"provider": provider, "stream": st, "logged_at": now, "data_at": getattr(newest, st)}
                    for st in STREAMS if getattr(newest, st) is not None]
            if rows:
                s.execute(text("INSERT INTO freshness_log (provider, stream, logged_at, data_at) "
                               "VALUES (:provider, :stream, :logged_at, :data_at)"), rows)
            s.execute(text("DELETE FROM freshness_log WHERE provider = :p AND logged_at < :cut"),
                      {"p": provider, "cut": now - timedelta(days=LOG_DAYS)})
        return len(rows)
    except Exception:  # noqa: BLE001 - freshness bookkeeping never fails a poll
        log.exception("%s: freshness log failed", provider)
        return 0


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def ages(log_rows: list[tuple[datetime, datetime]], now: datetime) -> list[float]:
    """Per-minute data ages (seconds) over the last WINDOW, from (logged_at, data_at) rows sorted by logged_at
    (the newest row before the window included). Minutes before the first row are skipped."""
    out, i, newest = [], 0, None
    t = now - WINDOW
    while t <= now:
        while i < len(log_rows) and log_rows[i][0] <= t:
            newest = log_rows[i][1] if newest is None else max(newest, log_rows[i][1])
            i += 1
        if newest is not None:
            out.append(max(0.0, (t - newest).total_seconds()))
        t += SAMPLE
    return out


def _r(x):
    return None if x is None else round(float(x), 1)


@ttl_cache(60)
def report(now: datetime | None = None) -> list[dict]:
    import normalize
    import provider_meta
    from providers import PROVIDERS

    now = now or datetime.now(timezone.utc)
    since = now - WINDOW
    with normalize.SessionLocal() as s:
        providers = sorted(set(s.execute(text(
            "SELECT DISTINCT provider FROM compute_listings UNION SELECT DISTINCT provider FROM capacity_current "
            "UNION SELECT DISTINCT provider FROM freshness_log")).scalars()) & set(PROVIDERS))
        # public report: a wholly internal provider is not named (methodology/data-visibility.md)
        providers = [p for p in providers if provider_meta.is_public_provider(p)]
        logs: dict[tuple[str, str], list] = {}
        for p, st, at, data in s.execute(text("""
                SELECT provider, stream, logged_at, data_at FROM freshness_log WHERE logged_at >= :since
                UNION ALL
                SELECT DISTINCT ON (provider, stream) provider, stream, logged_at, data_at FROM freshness_log
                WHERE logged_at < :since ORDER BY 1, 2, 3"""), {"since": since}):
            logs.setdefault((p, st), []).append((at, data))
        lag = {r.provider: r for r in s.execute(text("""
            SELECT provider, count(*) AS n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY lag) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY lag) AS p95
            FROM (SELECT provider, extract(epoch FROM observed_at - provider_changed_at) AS lag
                  FROM listing_observations
                  WHERE observed_at >= :since7 AND changed_at_basis = 'exact' AND prev_observed_at IS NOT NULL
                    AND provider_changed_at > prev_observed_at
                    AND provider_changed_at <= observed_at + interval '5 minutes') x
            GROUP BY provider"""), {"since7": now - LAG_WINDOW})}
        throttled = dict(s.execute(text("SELECT provider, count(*) FROM raw_snapshots WHERE throttled "
                                        "AND fetched_at >= :since GROUP BY provider"), {"since": since}).all())
        out = []
        for p in providers:
            pol = PROVIDERS[p].polling
            last_fetch = max((t for _, t in s.execute(normalize._NEWEST_PER_ENDPOINT, {"p": p}).all()), default=None)
            newest = s.execute(_NEWEST, {"p": p}).one()
            for st in STREAMS:
                data_at = getattr(newest, st)
                if st == "capacity" and data_at is None:
                    continue  # this provider reports no capacity
                rows = sorted(logs.get((p, st), []))
                a = ages(rows, now)
                cadence = pol.cadence(st)
                p95 = _pct(a, 0.95)
                sla = SLA_FACTOR * cadence
                row = {"provider": p, "stream": st, "tier": pol.tier, "target_interval_seconds": cadence,
                       "sla_seconds": sla, "newest_data_at": data_at,
                       "age_seconds": None if data_at is None else _r((now - data_at).total_seconds()),
                       "age_p50_seconds_24h": _r(_pct(a, 0.5)), "age_p95_seconds_24h": _r(p95),
                       "samples_24h": len(a),
                       "status": "unknown" if p95 is None else "meeting" if p95 <= sla else "breaching"}
                if st == "price":
                    lg = lag.get(p)
                    row.update({"last_successful_fetch_at": last_fetch,
                                "provider_timestamps": PROVIDER_TIMESTAMPS.get(p, "none"),
                                "detection_lag_p50_seconds_7d": None if lg is None else _r(lg.p50),
                                "detection_lag_p95_seconds_7d": None if lg is None else _r(lg.p95),
                                "detection_lag_changes_7d": 0 if lg is None else lg.n,
                                "throttled_24h": throttled.get(p, 0)})
                else:
                    row["last_refresh_at"] = data_at
                out.append(row)
    return out
