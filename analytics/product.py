"""Product analytics: privacy-preserving events, the activation funnel, top pages and searches.

Events (table product_events, store/metrics.py)
    client  page_view | search | gpu_view | provider_view | compare | watchlist_create
            sent in batches to POST /v1/events/track with a random, cookie-less anon_id the page
            generates (no cookie, no fingerprint). Rate-limited per client in memory.
    server  api_call | route_preview | route_approved | route_completed, derived by the
            `product_events` job from api_key_usage and the routing tables, idempotently
            (dedupe_key), so they cannot be forged by a client and never double count.

Privacy
    No IP address is stored (the rate limiter keys on a salted hash held only in memory).
    props are sanitized: short [a-z0-9_] keys, scalar values, PII-looking keys dropped (email,
    name, phone, ip, address, token, key, password, ...), e-mail addresses and secrets
    redacted from values, query strings stripped from paths, total size capped at 2 KB.
    Operator (site login) events are tagged internal and left out of the funnel.

Funnel (by ISO week; methodology in methodology/economics.md, "Funnel")
    visitor -> market user (>= 2 market page views in the week) -> account -> API key ->
    route preview -> real deployment -> second deployment.
    Visitors and market users are anonymous counts; from "account" on it is an account cohort
    (accounts created that week, and how many of them have reached each later step by now).
    The market-user -> account conversion is therefore a ratio of two counts, not a tracked path.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

import normalize
import observability as obs
from config import settings
from jobs import job
from store.metrics import ProductEvent

CLIENT_EVENTS = ("page_view", "search", "gpu_view", "provider_view", "compare", "watchlist_create")
SERVER_EVENTS = ("api_call", "route_preview", "route_approved", "route_completed")
EVENTS = CLIENT_EVENTS + SERVER_EVENTS
MARKET_VIEWS = ("page_view", "gpu_view", "provider_view", "compare", "search")
MAX_BATCH = 50
MAX_PROPS_BYTES = 2048
_ANON = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_PII_KEY = re.compile(r"(?i)(e-?mail|name|phone|mobile|^ip$|ip_?addr|address|street|zip|postal|token|key|secret|"
                      r"password|passwd|auth|cookie|session|ssn|card|iban|account_number|birth|user(name)?$)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PATH_KEYS = ("page", "path", "url", "referrer", "from", "to")


class Rejected(ValueError):
    pass


def _clean_str(k: str, v: str) -> str:
    v = v.strip()
    if k in _PATH_KEYS:
        v = v.split("?", 1)[0].split("#", 1)[0]
        if "://" in v:  # keep only the path of a full URL (and never another site's query string)
            from urllib.parse import urlparse

            v = urlparse(v).path or "/"
    v = _EMAIL.sub("[email]", v)
    v = obs.redact(v)
    return v[: (100 if k == "q" else 200)]


def sanitize_props(props) -> dict:
    if not isinstance(props, dict):
        return {}
    out = {}
    for k, v in list(props.items())[:40]:
        if not isinstance(k, str) or not _KEY.match(k) or _PII_KEY.search(k):
            continue
        if isinstance(v, bool) or v is None or isinstance(v, int) and abs(v) < 10 ** 12:
            out[k] = v
        elif isinstance(v, float):
            out[k] = round(v, 6) if v == v and abs(v) < 1e12 else None
        elif isinstance(v, str):
            out[k] = _clean_str(k, v)
        elif isinstance(v, list):
            out[k] = [_clean_str(k, x) if isinstance(x, str) else x for x in v[:10]
                      if isinstance(x, (str, int, float, bool))]
        # nested objects are dropped: nothing structured from a client is kept
        if len(out) >= 20:
            break
    import json

    while out and len(json.dumps(out)) > MAX_PROPS_BYTES:
        out.pop(next(reversed(out)))
    return out


# ---------------------------------------------------------------- rate limit (memory only)

_salt = secrets.token_bytes(16)
_hits: dict[str, deque] = {}
_lock = threading.Lock()


def client_key(ip: str | None, anon_id: str | None) -> str:
    """A salted hash of the client address (never stored; the salt dies with the process)."""
    return hashlib.sha256(_salt + (ip or "").encode() + b"|" + (anon_id or "").encode()).hexdigest()[:24]


def allow(key: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    limit = max(1, int(settings.product_events_per_minute))
    with _lock:
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(_hits) > 50_000:  # bound memory: drop idle clients
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60][:10_000]:
                _hits.pop(k, None)
        return True


def reset_limits() -> None:
    with _lock:
        _hits.clear()


# ---------------------------------------------------------------- recording

def track(events: list, *, account_id: int | None, internal: bool = False) -> dict:
    """Validate and store a client batch. Unknown/server-only event names are rejected, not stored."""
    if not isinstance(events, list) or not events:
        raise Rejected("events must be a non-empty list")
    if len(events) > MAX_BATCH:
        raise Rejected(f"at most {MAX_BATCH} events per batch")
    now = datetime.now(timezone.utc)
    rows, rejected = [], []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            rejected.append({"index": i, "reason": "not an object"})
            continue
        name = e.get("event")
        if name not in CLIENT_EVENTS:
            rejected.append({"index": i, "reason": f"event must be one of {', '.join(CLIENT_EVENTS)}"})
            continue
        anon = e.get("anon_id")
        if anon is not None and (not isinstance(anon, str) or not _ANON.match(anon)):
            rejected.append({"index": i, "reason": "anon_id must be 8-64 chars [A-Za-z0-9_-]"})
            continue
        if anon is None and account_id is None:
            rejected.append({"index": i, "reason": "anon_id required for anonymous events"})
            continue
        props = sanitize_props(e.get("props") or {})
        if internal:
            props["internal"] = True
        ts = now
        if isinstance(e.get("ts"), str):  # client clocks are not trusted beyond a small window
            try:
                t = datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))
                t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
                if now - timedelta(hours=1) <= t <= now + timedelta(minutes=5):
                    ts = t
            except ValueError:
                pass
        rows.append({"ts": ts, "account_id": account_id, "anon_id": anon, "event": name, "props": props,
                     "source": "client"})
    if rows:
        with normalize.SessionLocal.begin() as s:
            s.execute(insert(ProductEvent), rows)
    return {"accepted": len(rows), "rejected": rejected}


def _upsert(s, rows: list[dict]) -> int:
    if not rows:
        return 0
    stmt = insert(ProductEvent).values(rows)
    s.execute(stmt.on_conflict_do_update(index_elements=["dedupe_key"],
                                         set_={"props": stmt.excluded.props, "ts": stmt.excluded.ts}))
    return len(rows)


def sync_server_events(days: float | None = 3) -> dict:
    """Derive server events from accounts usage and the routing tables. Idempotent.

    days=None (or no server events yet) backfills everything."""
    n = {}
    with normalize.SessionLocal.begin() as s:
        if not obs.has_table(s, "product_events"):
            return {"unavailable": "product_events table missing (migration 0012 not applied)"}
        if s.execute(text("SELECT NOT EXISTS (SELECT 1 FROM product_events WHERE source = 'server')")).scalar():
            days = None
        since = datetime.now(timezone.utc) - timedelta(days=days) if days else datetime(1970, 1, 1, tzinfo=timezone.utc)
        if obs.has_table(s, "api_key_usage"):
            rows = [{"ts": r.day, "account_id": r.account_id, "anon_id": None, "event": "api_call",
                     "props": {"key_id": r.key_id, "count": int(r.n), "day": r.day.date().isoformat()},
                     "source": "server", "dedupe_key": f"api:{r.account_id}:{r.key_id}:{r.day.date().isoformat()}"}
                    for r in s.execute(text(
                        "SELECT account_id, key_id, date_trunc('day', ts) AS day, count(*) AS n FROM api_key_usage "
                        "WHERE ts >= date_trunc('day', CAST(:t AS timestamptz)) GROUP BY 1, 2, 3"), {"t": since})]
            n["api_call"] = sum(_upsert(s, rows[i:i + 1000]) for i in range(0, len(rows), 1000))
        rows = [{"ts": r.created_at, "account_id": r.account_id, "anon_id": None, "event": "route_preview",
                 "props": {"route_request_id": r.id, "gpu": r.gpu, "mode": r.mode}, "source": "server",
                 "dedupe_key": f"rp:{r.id}"}
                for r in s.execute(text("SELECT id, account_id, gpu, mode, created_at FROM route_requests "
                                        "WHERE preview AND created_at >= :t"), {"t": since})]
        n["route_preview"] = sum(_upsert(s, rows[i:i + 1000]) for i in range(0, len(rows), 1000))
        cols = obs.columns(s, "deployments")
        purpose = "coalesce(d.purpose, 'customer')" if "purpose" in cols else "'customer'"
        if "approved_at" in cols:
            rows = [{"ts": r.approved_at, "account_id": r.account_id, "anon_id": None, "event": "route_approved",
                     "props": {"deployment_id": r.deployment_id, "provider": r.provider, "purpose": r.purpose},
                     "source": "server", "dedupe_key": f"ra:{r.deployment_id}"}
                    for r in s.execute(text(f"SELECT d.deployment_id, d.account_id, d.provider, d.approved_at, "
                                            f"{purpose} AS purpose FROM deployments d "
                                            "WHERE d.approved_at IS NOT NULL AND d.approved_at >= :t"), {"t": since})]
            n["route_approved"] = sum(_upsert(s, rows[i:i + 1000]) for i in range(0, len(rows), 1000))
        rows = [{"ts": r.ended_at, "account_id": r.account_id, "anon_id": None, "event": "route_completed",
                 "props": {"deployment_id": r.deployment_id, "provider": r.provider, "purpose": r.purpose},
                 "source": "server", "dedupe_key": f"rc:{r.deployment_id}"}
                for r in s.execute(text(
                    f"SELECT d.deployment_id, d.account_id, d.provider, {purpose} AS purpose, min(e.at) AS ended_at "
                    "FROM deployments d JOIN deployment_events e ON e.deployment_id = d.deployment_id "
                    "AND e.to_status = 'terminated' WHERE e.at >= :t AND EXISTS (SELECT 1 FROM deployment_events r "
                    "WHERE r.deployment_id = d.deployment_id AND r.to_status = 'running') "
                    "GROUP BY 1, 2, 3, 4"), {"t": since})]
        n["route_completed"] = sum(_upsert(s, rows[i:i + 1000]) for i in range(0, len(rows), 1000))
    return n


@job("product_events", every_seconds=settings.metrics_recompute_seconds, initial_delay_seconds=120)
def _sync_job():
    return sync_server_events()


# ---------------------------------------------------------------- reading

def _week(t: datetime) -> str:
    t = t.astimezone(timezone.utc) if t.tzinfo else t
    return (t - timedelta(days=t.weekday())).date().isoformat()


def _conv(a: int, b: int):
    return None if not b else round(a / b, 4)


def funnel(weeks: int = 8, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start = datetime.combine((now - timedelta(days=now.weekday() + 7 * (weeks - 1))).date(), datetime.min.time(),
                             tzinfo=timezone.utc)
    with normalize.SessionLocal() as s:
        vis = {r.w.date().isoformat(): (r.visitors, r.market_users) for r in s.execute(text(
            "SELECT w, count(*) AS visitors, count(*) FILTER (WHERE mv >= 2) AS market_users FROM ("
            "  SELECT date_trunc('week', ts AT TIME ZONE 'UTC') AS w, anon_id, count(*) FILTER (WHERE event = ANY(:mv)) AS mv"
            "  FROM product_events WHERE source = 'client' AND anon_id IS NOT NULL AND ts >= :t"
            "  AND NOT coalesce((props->>'internal')::boolean, false) GROUP BY 1, 2) x GROUP BY w"),
            {"t": start, "mv": list(MARKET_VIEWS)})}
        cols = obs.columns(s, "deployments")
        purpose = "AND coalesce(d.purpose, 'customer') = 'customer'" if "purpose" in cols else ""
        accts = s.execute(text(
            "SELECT a.id, a.created_at,"
            " EXISTS (SELECT 1 FROM api_keys k WHERE k.account_id = a.id) AS has_key,"
            " EXISTS (SELECT 1 FROM route_requests r WHERE r.account_id = a.id AND r.preview) AS has_preview,"
            " (SELECT count(DISTINCT d.deployment_id) FROM deployments d JOIN deployment_events e"
            "   ON e.deployment_id = d.deployment_id AND e.to_status = 'running'"
            f"  WHERE d.account_id = a.id {purpose}) AS ran"
            " FROM accounts a WHERE NOT a.is_operator AND a.created_at >= :t"), {"t": start}).all()
    out = []
    for i in range(weeks):
        wk = (start + timedelta(days=7 * i)).date().isoformat()
        v, m = vis.get(wk, (0, 0))
        cohort = [a for a in accts if _week(a.created_at) == wk]
        stage = {"visitors": v, "market_users": m, "accounts": len(cohort),
                 "api_key": sum(1 for a in cohort if a.has_key),
                 "route_preview": sum(1 for a in cohort if a.has_preview),
                 "real_deployment": sum(1 for a in cohort if a.ran >= 1),
                 "second_deployment": sum(1 for a in cohort if a.ran >= 2)}
        out.append({"week": wk, **stage, "conversion": _conversions(stage)})
    total = {k: sum(w[k] for w in out) for k in ("visitors", "market_users", "accounts", "api_key", "route_preview",
                                                 "real_deployment", "second_deployment")}
    return {"weeks": out, "total": {**total, "conversion": _conversions(total)},
            "stages": ["visitors", "market_users", "accounts", "api_key", "route_preview", "real_deployment",
                       "second_deployment"],
            "note": "visitors / market users are anonymous weekly counts; accounts onward are the cohort of accounts "
                    "created that week and how many have reached each step by now. market_users -> accounts is a "
                    "ratio of counts, not a tracked path. Weekly visitor counts sum across weeks (a returning "
                    "visitor counts once per week)."}


def _conversions(st: dict) -> dict:
    order = ["visitors", "market_users", "accounts", "api_key", "route_preview", "real_deployment", "second_deployment"]
    return {f"{a}->{b}": _conv(st[b], st[a]) for a, b in zip(order, order[1:])}


def summary(days: int = 30, limit: int = 20) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    p = {"t": since, "l": limit}
    ext = "source = 'client' AND ts >= :t AND NOT coalesce((props->>'internal')::boolean, false)"
    with normalize.SessionLocal() as s:
        pages = [{"page": r[0], "views": r[1], "visitors": r[2]} for r in s.execute(text(
            f"SELECT coalesce(props->>'page', props->>'path') AS page, count(*), count(DISTINCT anon_id) "
            f"FROM product_events WHERE event = 'page_view' AND {ext} GROUP BY 1 ORDER BY 2 DESC LIMIT :l"), p)]
        searches = [{"q": r[0], "count": r[1]} for r in s.execute(text(
            f"SELECT lower(props->>'q'), count(*) FROM product_events WHERE event = 'search' AND {ext} "
            "AND props ? 'q' GROUP BY 1 ORDER BY 2 DESC LIMIT :l"), p)]
        gpus = [{"gpu": r[0], "views": r[1]} for r in s.execute(text(
            f"SELECT props->>'gpu', count(*) FROM product_events WHERE event = 'gpu_view' AND {ext} "
            "AND props ? 'gpu' GROUP BY 1 ORDER BY 2 DESC LIMIT :l"), p)]
        by_event = {r[0]: r[1] for r in s.execute(text(
            "SELECT event, count(*) FROM product_events WHERE ts >= :t GROUP BY 1"), p)}
        rep = s.execute(text(
            f"SELECT count(*) FILTER (WHERE days >= 2), count(*) FROM (SELECT anon_id, "
            f"count(DISTINCT date_trunc('day', ts)) AS days FROM product_events WHERE {ext} AND anon_id IS NOT NULL "
            "GROUP BY 1) x"), p).first()
    return {"days": days, "top_pages": pages, "top_searches": searches, "top_gpus": gpus, "events": by_event,
            "visitors": rep[1], "repeat_visitors": rep[0],
            "repeat_rate": _conv(rep[0], rep[1]),
            "note": "repeat visitor = the same anon_id on 2+ distinct days in the window; operator traffic excluded"}
