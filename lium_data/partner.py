"""Lium's data-partner feeds: completed rentals, hourly GPU usage and daily VWAP (kind transaction).

Only an account Lium flags as a data partner may read them; OpenGrid sends the key in LIUM_PARTNER_API_KEY. Until
Lium flags the account every call answers 403: that is stored once (raw.py is change-only), logged once as a
warning, and the job then re-checks at most every FORBIDDEN_RECHECK_HOURS instead of every run.

    poll()        the `lium_partner` job (hourly, registered only with the key):
                    trades.csv          from = the end day of the newest stored trade (resume; at most 62 days
                                        back), to = today: rentals that ended since, deduplicated on the row key
                    dumps/{day}.csv     every closed day since the newest stored dump (first run: yesterday),
                                        the nightly file (written 01:00 UTC; 404 before, retried next run)
                    hourly-gpu.csv      the closed hours since the newest stored one (+1 overlap, at least 2)
                    daily-aggregate.csv from yesterday to today
    backfill()    `python -m lium_data backfill --since YYYY-MM-DD`: walks back from yesterday one day at a time,
                  the trades dump and the daily-aggregate dump of each day (trades.csv?day= when a dump is
                  missing), skipping days already stored; without --since it stops after STOP_AFTER_EMPTY
                  consecutive days with nothing (no dump and no rows). A 401/403 stops it at once.

Column names: Lium documents the trade file as "the General Index trade-data CSV format" and lists its fields
(hashed renter and provider ids, price per GPU-hour, GPU count, start and end, GPU model, provider country) but
not their header spelling, so the parser accepts the usual spellings (ALIASES) and keeps any other column verbatim
in `extra`. A row without price, GPU count, start, end or GPU model is counted as rejected; the raw body keeps it.

Row key: the feed's own trade id when it has one, else sha256 of the row's fields (hashed ids included). Lium
hashes ids with a salt; if Lium rotates it, the same rental would come back under new hashes and a new key (the
salt is Lium's; nothing here can detect that except a jump in duplicate-looking rows).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from lium_data import feeds as F
from lium_data import raw
from store.lium import LiumDailyAggregate, LiumFeedSnapshot, LiumHourlyGpu, LiumTrade

log = logging.getLogger(__name__)

POLL_SECONDS = 3600
MAX_WINDOW_DAYS = 62            # Lium's cap per trades / daily-aggregate call
FORBIDDEN_RECHECK_HOURS = 6
STOP_AFTER_EMPTY = 3
_Q6 = Decimal("0.000001")

# normalised header -> field (headers are lower-cased, spaces and dashes become underscores)
ALIASES = {
    "trade_id": ("trade_id", "id", "deal_id", "rental_id", "transaction_id", "trade_ref"),
    "renter_hash": ("renter_id", "renter_hash", "renter", "buyer_id", "buyer_hash", "buyer", "hashed_renter_id",
                    "renter_id_hash"),
    "provider_hash": ("provider_id", "provider_hash", "provider", "seller_id", "seller_hash", "seller",
                      "hashed_provider_id", "provider_id_hash"),
    "price": ("price_per_gpu_hour", "price_per_gpu_hour_usd", "price_usd_per_gpu_hour", "usd_per_gpu_hour",
              "price", "price_usd", "unit_price"),
    "gpu_count": ("gpu_count", "gpus", "num_gpus", "quantity", "qty"),
    "start": ("start", "start_time", "start_at", "started_at", "start_utc", "start_time_utc", "delivery_start"),
    "end": ("end", "end_time", "end_at", "ended_at", "end_utc", "end_time_utc", "delivery_end"),
    "gpu_model": ("gpu_model", "gpu_type", "gpu", "product", "hardware", "model"),
    "country": ("provider_country", "country", "country_code", "provider_country_code", "location"),
    "currency": ("currency", "ccy"),
}
AGG_ALIASES = {
    "day": ("day", "date", "end_day", "trade_date", "day_utc"),
    "gpu_model": ALIASES["gpu_model"],
    "country": ALIASES["country"],
    "deals": ("deals", "deal_count", "trades", "trade_count", "rentals", "count", "n"),
    "gpu_hours": ("gpu_hours", "volume_gpu_hours", "volume"),
    "vwap": ("vwap", "vwap_usd", "vwap_price_per_gpu_hour", "vwap_usd_per_gpu_hour", "vwap_price_per_gpu_hour_usd"),
    "low": ("low", "min", "low_price", "min_price", "low_price_per_gpu_hour", "min_price_per_gpu_hour",
            "low_usd_per_gpu_hour", "min_price_per_gpu_hour_usd", "low_price_per_gpu_hour_usd"),
    "high": ("high", "max", "high_price", "max_price", "high_price_per_gpu_hour", "max_price_per_gpu_hour",
             "high_usd_per_gpu_hour", "max_price_per_gpu_hour_usd", "high_price_per_gpu_hour_usd"),
}
HOURLY_COLUMNS = ("hour_utc", "gpu_model", "rentals", "gpu_hours", "mean_price_per_gpu_hour_usd")


def _session():
    import normalize  # late, like fx.py

    return normalize.SessionLocal


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------


def _norm(h: str) -> str:
    return (h or "").strip().lstrip("﻿").lower().replace(" ", "_").replace("-", "_")


def _rows(body: str) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(body))
    headers = [_norm(h) for h in (reader.fieldnames or [])]
    rows = []
    for r in reader:
        rows.append({_norm(k): (v.strip() if isinstance(v, str) else v) for k, v in r.items() if k is not None})
    return headers, rows


def _pick(row: dict, names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return n, row[n]
    return None, None


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        d = Decimal(str(v).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _int(v) -> int | None:
    d = _dec(v)
    return int(d) if d is not None and d == d.to_integral_value() else None


def parse_time(v) -> datetime | None:
    """ISO 8601 (Z or offset; a naive time is UTC) or a Unix time in seconds / milliseconds."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    if s.replace(".", "", 1).isdigit():
        x = float(s)
        return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, tz=timezone.utc)
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def parse_day(v) -> date | None:
    t = parse_time(v)
    if t is not None:
        return t.date()
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def row_key(t: dict) -> str:
    if t.get("trade_id"):
        return hashlib.sha256(f"id:{t['trade_id']}".encode()).hexdigest()
    parts = [t.get("renter_hash"), t.get("provider_hash"), t["gpu_model"], t["gpu_count"], str(t["price_per_gpu_hour"]),
             t["start_at"].isoformat(), t["end_at"].isoformat(), t.get("provider_country"),
             json.dumps(t.get("extra") or {}, sort_keys=True)]
    return hashlib.sha256("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


def parse_trades(body: str) -> tuple[list[dict], int]:
    """(trade rows, rejected count) from a trades CSV (live window, day or dump)."""
    if not body or not body.strip():
        return [], 0
    headers, rows = _rows(body)
    known = {n for names in ALIASES.values() for n in names}
    out, rejected = [], 0
    for r in rows:
        if not any(v for v in r.values()):
            continue
        got = {f: _pick(r, names)[1] for f, names in ALIASES.items()}
        price, count = _dec(got["price"]), _int(got["gpu_count"])
        start, end = parse_time(got["start"]), parse_time(got["end"])
        if price is None or price < 0 or not count or count < 1 or start is None or end is None or end < start \
                or not got["gpu_model"]:
            rejected += 1
            continue
        hours = Decimal((end - start).total_seconds()) / Decimal(3600)
        t = {"trade_id": got["trade_id"], "renter_hash": got["renter_hash"], "provider_hash": got["provider_hash"],
             "gpu_model": got["gpu_model"][:128], "gpu_count": count, "price_per_gpu_hour": price.quantize(_Q6),
             "currency": (got["currency"] or "USD").upper()[:8], "start_at": start, "end_at": end,
             "gpu_hours": (hours * count).quantize(_Q6), "provider_country": (got["country"] or None),
             "extra": {k: v for k, v in r.items() if k not in known} or None}
        t["row_key"] = row_key(t)
        out.append(t)
    return out, rejected


def parse_hourly(body: str) -> tuple[list[dict], int]:
    if not body or not body.strip():
        return [], 0
    _, rows = _rows(body)
    out, rejected = [], 0
    for r in rows:
        hour, model = parse_time(r.get("hour_utc")), r.get("gpu_model")
        if hour is None or not model:
            rejected += 1
            continue
        mean = _dec(r.get("mean_price_per_gpu_hour_usd"))
        out.append({"hour_utc": hour, "gpu_model": model[:128], "rentals": _int(r.get("rentals")),
                    "gpu_hours": None if _dec(r.get("gpu_hours")) is None else _dec(r.get("gpu_hours")).quantize(_Q6),
                    "mean_price_per_gpu_hour_usd": None if mean is None else mean.quantize(_Q6)})
    return out, rejected


def parse_daily_aggregate(body: str) -> tuple[list[dict], int]:
    if not body or not body.strip():
        return [], 0
    _, rows = _rows(body)
    known = {n for names in AGG_ALIASES.values() for n in names}
    out, rejected = [], 0
    for r in rows:
        got = {f: _pick(r, names)[1] for f, names in AGG_ALIASES.items()}
        day = parse_day(got["day"])
        if day is None or not got["gpu_model"]:
            rejected += 1
            continue
        q = {k: (None if _dec(got[k]) is None else _dec(got[k]).quantize(_Q6)) for k in ("gpu_hours", "vwap", "low", "high")}
        out.append({"day": day, "gpu_model": got["gpu_model"][:128], "country": (got["country"] or "")[:64],
                    "deals": _int(got["deals"]), **q,
                    "extra": {k: v for k, v in r.items() if k not in known} or None})
    return out, rejected


PARSERS = {"trades": parse_trades, "trades_dump": parse_trades, "hourly_gpu": parse_hourly,
           "daily_aggregate": parse_daily_aggregate, "daily_aggregate_dump": parse_daily_aggregate}


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _save_parsed(s, feed: str, rows: list[dict], snapshot_id: int, now: datetime, vis: str) -> int:
    """Upsert parsed rows; returns how many were new or changed."""
    if not rows:
        return 0
    if feed in ("trades", "trades_dump"):
        dedup = {r["row_key"]: r for r in rows}
        vals = [{**r, "snapshot_id": snapshot_id, "first_seen_at": now, "visibility": vis} for r in dedup.values()]
        n = 0
        for i in range(0, len(vals), 1000):
            res = s.execute(insert(LiumTrade).values(vals[i:i + 1000])
                            .on_conflict_do_nothing(index_elements=["row_key"]).returning(LiumTrade.row_key))
            n += len(res.all())
        return n
    table, keys, cols = ((LiumHourlyGpu, ("hour_utc", "gpu_model"), ("rentals", "gpu_hours", "mean_price_per_gpu_hour_usd"))
                         if feed == "hourly_gpu" else
                         (LiumDailyAggregate, ("day", "gpu_model", "country"),
                          ("deals", "gpu_hours", "vwap", "low", "high", "extra")))
    dedup = {tuple(r[k] for k in keys): r for r in rows}
    vals = [{**r, "snapshot_id": snapshot_id, "first_seen_at": now, "updated_at": now, "visibility": vis}
            for r in dedup.values()]
    n = 0
    for i in range(0, len(vals), 1000):
        stmt = insert(table).values(vals[i:i + 1000])
        changed = None
        for c in cols:
            cond = getattr(table, c).is_distinct_from(getattr(stmt.excluded, c))
            changed = cond if changed is None else changed | cond
        res = s.execute(stmt.on_conflict_do_update(
            index_elements=list(keys),
            set_={**{c: getattr(stmt.excluded, c) for c in cols}, "snapshot_id": stmt.excluded.snapshot_id,
                  "updated_at": stmt.excluded.updated_at},
            where=changed).returning(table.updated_at))
        n += len(res.all())
    return n


def save(f: raw.Fetched) -> dict:
    """Store one fetched answer raw (change-only) and its parsed rows. Returns a summary."""
    parsed, rejected = ([], None)
    if f.status_code == 200 and f.body is not None:
        try:
            parsed, rejected = PARSERS[f.feed.name](f.body)
        except (csv.Error, ValueError) as exc:
            f.error = f"parse: {exc}"[:500]
            parsed, rejected = [], None
    now = datetime.now(timezone.utc)
    with _session().begin() as s:
        st = raw.store(s, f, rows=len(parsed), rejected=rejected)
        added = _save_parsed(s, f.feed.name, parsed, st.snapshot.id, now, st.snapshot.visibility) if st.new else 0
    raw.log_outcome(st, f)
    return {"feed": f.feed.name, "params": f.params, "state": st.state, "status_code": f.status_code,
            "stored": st.new, "rows": len(parsed), "rejected": rejected, "new_or_changed": added,
            "error": f.error if st.state not in ("ok", "empty") else None}


# --------------------------------------------------------------------------
# Poll and backfill
# --------------------------------------------------------------------------


def api_key() -> str | None:
    from config import settings

    return (settings.lium_partner_api_key or "").strip() or None


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _latest_state() -> tuple[str | None, datetime | None]:
    """The newest partner answer's state and when it was last seen."""
    with _session()() as s:
        row = s.execute(select(LiumFeedSnapshot.state, LiumFeedSnapshot.last_seen_at)
                        .where(LiumFeedSnapshot.feed.in_([f.name for f in F.PARTNER_FEEDS]))
                        .order_by(LiumFeedSnapshot.last_seen_at.desc(), LiumFeedSnapshot.id.desc()).limit(1)).first()
    return (row[0], row[1]) if row else (None, None)


def _resume_day() -> date:
    with _session()() as s:
        last = s.execute(select(func.max(LiumTrade.end_at))).scalar()
    today = _today()
    start = last.astimezone(timezone.utc).date() if last else today - timedelta(days=1)
    return max(start, today - timedelta(days=MAX_WINDOW_DAYS - 1))


def _stored_days(feed: str) -> set[date]:
    """Closed days whose dump (or ?day= read) is stored with an answer (rows or none)."""
    with _session()() as s:
        params = s.execute(select(LiumFeedSnapshot.params).where(
            LiumFeedSnapshot.feed == feed, LiumFeedSnapshot.state.in_(("ok", "empty")))).scalars().all()
    out = set()
    for p in params:
        d = parse_day((p or {}).get("day"))
        if d:
            out.add(d)
    return out


def _last_hour() -> datetime | None:
    with _session()() as s:
        return s.execute(select(func.max(LiumHourlyGpu.hour_utc))).scalar()


def _denied(state: str | None) -> bool:
    return state in ("forbidden", "unauthorized")


async def poll(http: httpx.AsyncClient | None = None, key: str | None = None) -> dict:
    """The `lium_partner` job. Does nothing without a key; while Lium answers 403 it re-checks only every
    FORBIDDEN_RECHECK_HOURS (one cheap trades call), so a missing partner flag costs one request every 6 hours."""
    key = key or api_key()
    if not key:
        return {"state": "off", "reason": "LIUM_PARTNER_API_KEY is not set"}
    state, seen = _latest_state()
    now = datetime.now(timezone.utc)
    if _denied(state) and seen and now - seen < timedelta(hours=FORBIDDEN_RECHECK_HOURS):
        return {"state": state, "skipped": True, "recheck_after": seen + timedelta(hours=FORBIDDEN_RECHECK_HOURS)}
    own = http is None
    http = http or raw.client()
    results: list[dict] = []
    try:
        today = _today()
        first = save(await raw.fetch(F.FEEDS["trades"], http, api_key=key,
                                     params={"from": _resume_day().isoformat(), "to": today.isoformat()}))
        results.append(first)
        if _denied(first["state"]) or first["state"] == "unavailable":
            if first["stored"] or first["state"] != state:
                log.warning("lium partner feeds: %s. OpenGrid's Lium account is not (yet) flagged as a data partner "
                            "or the key lacks the read scope; re-checking every %d h. %s", first["state"],
                            FORBIDDEN_RECHECK_HOURS, first.get("error") or "")
            return {"state": first["state"], "results": results}
        # the nightly dumps of closed days not stored yet (at most a window back; first run: yesterday)
        have = _stored_days("trades_dump")
        start = max(have) + timedelta(days=1) if have else today - timedelta(days=1)
        for d in _days(start, today - timedelta(days=1))[-MAX_WINDOW_DAYS:]:
            results.append(save(await raw.fetch(F.FEEDS["trades_dump"], http, api_key=key, path={"day": d.isoformat()})))
            if not _stored_days("daily_aggregate_dump") >= {d}:
                results.append(save(await raw.fetch(F.FEEDS["daily_aggregate_dump"], http, api_key=key,
                                                    path={"day": d.isoformat()})))
        last = _last_hour()
        hours = 48 if last is None else max(2, min(720, int((now - last).total_seconds() // 3600) + 1))
        results.append(save(await raw.fetch(F.FEEDS["hourly_gpu"], http, api_key=key, params={"hours": hours})))
        results.append(save(await raw.fetch(F.FEEDS["daily_aggregate"], http, api_key=key, params={
            "from": (today - timedelta(days=1)).isoformat(), "to": today.isoformat()})))
    finally:
        if own:
            await http.aclose()
    bad = [r for r in results if r["state"] not in ("ok", "empty", "not_found")]
    return {"state": "ok" if not bad else bad[0]["state"], "results": results}


def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)] if b >= a else []


async def backfill(since: date | None = None, until: date | None = None, *, force: bool = False,
                   http: httpx.AsyncClient | None = None, key: str | None = None, gap: float | None = None) -> dict:
    """History from the nightly dumps, newest day first (see the module docstring)."""
    key = key or api_key()
    if not key:
        return {"state": "off", "reason": "LIUM_PARTNER_API_KEY is not set"}
    gap = F.POLITE_GAP_SECONDS if gap is None else gap
    day = min(until or _today() - timedelta(days=1), _today() - timedelta(days=1))
    have_t, have_a = (set(), set()) if force else (_stored_days("trades_dump") | _stored_days("trades"),
                                                    _stored_days("daily_aggregate_dump") | _stored_days("daily_aggregate"))
    own = http is None
    http = http or raw.client()
    done, empty_run, trades = [], 0, 0
    try:
        while since is None or day >= since:
            iso = day.isoformat()
            got_rows = False
            if day not in have_t:
                r = save(await raw.fetch(F.FEEDS["trades_dump"], http, api_key=key, path={"day": iso}))
                if _denied(r["state"]) or r["state"] == "unavailable":
                    log.warning("lium backfill stopped at %s: %s %s", iso, r["state"], r.get("error") or "")
                    return {"state": r["state"], "days": done, "trades": trades, "stopped_at": iso}
                if r["state"] == "not_found":     # no dump for that day: the live route serves the same rows
                    await asyncio.sleep(gap)
                    r = save(await raw.fetch(F.FEEDS["trades"], http, api_key=key, params={"day": iso}))
                trades += r["new_or_changed"] or 0
                got_rows = r["state"] == "ok"
                await asyncio.sleep(gap)
            else:
                got_rows = True
            if day not in have_a:
                a = save(await raw.fetch(F.FEEDS["daily_aggregate_dump"], http, api_key=key, path={"day": iso}))
                if a["state"] == "not_found":
                    await asyncio.sleep(gap)
                    a = save(await raw.fetch(F.FEEDS["daily_aggregate"], http, api_key=key, params={"day": iso}))
                got_rows = got_rows or a["state"] == "ok"
                await asyncio.sleep(gap)
            done.append(iso)
            empty_run = 0 if got_rows else empty_run + 1
            if since is None and empty_run >= STOP_AFTER_EMPTY:
                log.info("lium backfill: %d empty days in a row, stopping at %s", empty_run, iso)
                break
            if len(done) % 10 == 0:
                log.info("lium backfill: %d days, %d new trades, at %s", len(done), trades, iso)
            day -= timedelta(days=1)
    finally:
        if own:
            await http.aclose()
    return {"state": "ok", "days": len(done), "first_day": done[-1] if done else None,
            "last_day": done[0] if done else None, "new_trades": trades}


def reparse() -> dict:
    """Re-parse every stored partner body (after a parser change). Idempotent."""
    now = datetime.now(timezone.utc)
    n = 0
    with _session()() as s:
        snaps = s.execute(select(LiumFeedSnapshot.id, LiumFeedSnapshot.feed, LiumFeedSnapshot.body,
                                 LiumFeedSnapshot.visibility)
                          .where(LiumFeedSnapshot.feed.in_(list(PARSERS)), LiumFeedSnapshot.state == "ok")
                          .order_by(LiumFeedSnapshot.fetched_at, LiumFeedSnapshot.id)).all()
    for sid, feed, body, vis in snaps:
        rows, rejected = PARSERS[feed](body or "")
        with _session().begin() as s:
            n += _save_parsed(s, feed, rows, sid, now, vis)
            s.execute(LiumFeedSnapshot.__table__.update().where(LiumFeedSnapshot.id == sid)
                      .values(rows=len(rows), rejected=rejected))
    return {"snapshots": len(snaps), "new_or_changed": n}


# --------------------------------------------------------------------------
# Reads: OpenGrid's statistics from the stored trades (kind transaction, basis derived_from_trades)
# --------------------------------------------------------------------------


def trade_stats(days: int = 30, gpu: str | None = None, country: str | None = None,
                until: date | None = None) -> list[dict]:
    """Per UTC end day and GPU model, from the stored trades: rentals, GPU-hours, VWAP (sum of price x GPU-hours /
    GPU-hours), lowest and highest price, notional (sum of price x GPU-hours, USD). A rental counts on the day it
    ended (Lium's own axis), whole, however many days it ran. Newest day first."""
    from sqlalchemy import Date, cast

    end = until or _today()
    start = end - timedelta(days=days - 1)
    t = LiumTrade
    day = cast(func.timezone("UTC", t.end_at), Date)
    notional = func.sum(t.price_per_gpu_hour * t.gpu_hours)
    stmt = (select(day.label("day"), t.gpu_model, func.count().label("rentals"), func.sum(t.gpu_hours).label("gpu_hours"),
                   notional.label("notional"), func.min(t.price_per_gpu_hour).label("low"),
                   func.max(t.price_per_gpu_hour).label("high"), func.sum(t.gpu_count).label("gpus"))
            .where(day >= start, day <= end, t.currency == "USD")
            .group_by(day, t.gpu_model).order_by(day.desc(), t.gpu_model))
    if gpu:
        stmt = stmt.where(t.gpu_model == gpu)
    if country:
        stmt = stmt.where(t.provider_country == country)
    with _session()() as s:
        rows = s.execute(stmt).all()
    import canonical

    out = []
    for r in rows:
        gh = Decimal(r.gpu_hours or 0)
        out.append({"day": r.day, "gpu_model": r.gpu_model, "canonical_gpu_name": canonical.canonical_gpu_name(r.gpu_model),
                    "rentals": r.rentals, "gpu_hours": gh.quantize(Decimal("0.001")),
                    "vwap": (Decimal(r.notional) / gh).quantize(Decimal("0.0001")) if gh > 0 else None,
                    "low": r.low, "high": r.high, "notional_usd": Decimal(r.notional or 0).quantize(Decimal("0.01")),
                    "kind": F.TRANSACTION, "basis": F.DERIVED_FROM_TRADES})
    return out
