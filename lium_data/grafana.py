"""Lium's public Grafana (job `lium_grafana`, hourly): what renters pay, read from the dashboards Lium publishes.

Lium documents grafana.lium.io as public ("No account is needed", docs.lium.io/providers/grafana). Its "Marketplace
prices" dashboard plots one point per rental (start time, GPU model, price paid) and its "Lium network status"
dashboard plots renter spend and the fleet per hour and GPU model. This module reads four of those panels
(feeds.GRAFANA_PANELS).

OpenGrid never writes a query. For every read it

    1. fetches the dashboard (GET /api/dashboards/uid/<uid>) and finds the panel by its exact title;
    2. takes the query the dashboard holds for that panel at that moment;
    3. fills the dashboard's own variables with values the dashboard lists (gpu_type "All"; tier "All", "spot" or
       "secure"), the way the page does when a visitor picks them; a variable this module does not know, or a value
       the dashboard does not list, stops the read before anything is sent;
    4. asks for a time range (POST /api/ds/query), as the dashboard's time picker does.

A panel that was renamed, removed or reshaped is therefore a failed read, recorded once (raw.store), never a guess.

Stored like every other Lium feed: the answer raw first and change-only (lium_feed_snapshots; the body is the
answer's non-empty cells, with the window left out of the "changed?" comparison), then parsed:

    lium_grafana_rentals   one row per rental start. The key is a hash of the start time to the nanosecond, the
                           GPU model and the price, so an overlapping window never counts a rental twice. The panel
                           is read three times, once per tier value: "spot" and "secure" store each rental with
                           its tier, then "All" adds the rentals neither tier read returned (tier NULL).
    lium_grafana_hourly    one row per UTC hour and GPU model: gpus_listed, gpus_rented, spend_usd_per_hour. The
                           running hour and the hours before it are re-read each poll and updated in place. A zero
                           spend is written only on a row the fleet panels made: the spend panel lists about 100
                           models and most of them, never listed, would be rows of nothing.

Every row is INTERNAL (provider_meta.INTERNAL_SOURCES). poll() runs hourly; backfill() walks the history back in
closed chunks, resumable. One request every feeds.POLITE_GAP_SECONDS at most.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from lium_data import feeds as F
from lium_data import raw
from store.lium import LiumFeedSnapshot, LiumGrafanaHourly, LiumGrafanaRental

log = logging.getLogger(__name__)

POLL_SECONDS = 3600
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HOUR_MS = 3_600_000
SIX_DP = Decimal("0.000001")

OVERLAP = timedelta(hours=1)          # a poll starts this long before the newest stored point
MIN_WINDOW = timedelta(hours=3)
FIRST_WINDOW = timedelta(hours=6)     # nothing stored yet
MAX_WINDOW = timedelta(hours=72)      # after a longer outage the backfill fills the rest
RENTAL_CHUNK_DAYS = 3                 # backfill: about 3,500 rentals per answer
SERIES_CHUNK_DAYS = 14                # backfill: under 1,000 hours, so the dashboard's own step stays one hour
EMPTY_CHUNKS_STOP = 2                 # backfill without --since: stop after this many empty chunks in a row
ERROR_CHUNKS_STOP = 3

DEFAULT_MAX_POINTS = 1000
_UNITS_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": HOUR_MS, "d": 24 * HOUR_MS}
# ${name}, ${name:format}, [[name]] and $name; Grafana's own macros ($__timeFilter, $__interval_ms) are left to it
_VARIABLE = re.compile(r"\$\{(\w+)(?::\w+)?\}|\[\[(\w+)(?::\w+)?\]\]|\$(?!__)([A-Za-z_]\w*)")


def _session():
    import normalize

    return normalize.SessionLocal


class BoardError(Exception):
    """The dashboard could not be read."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PanelError(Exception):
    """The dashboard was read, but the panel is not there in the shape this module can run. `code` is stable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Query:
    sql: str
    datasource: dict
    fmt: str
    min_interval_ms: int
    max_points: int

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()

    def interval_ms(self, start: datetime, end: datetime) -> int:
        """The step Grafana computes for a panel: the range over its point budget, never under its own minimum."""
        span = max(1, int((end - start).total_seconds() * 1000))
        return max(self.min_interval_ms, math.ceil(span / self.max_points), 1)


# --------------------------------------------------------------------------
# The dashboard's own query
# --------------------------------------------------------------------------


def _walk(panels):
    for p in panels or []:
        if isinstance(p, dict):
            if p.get("type") == "row":
                yield from _walk(p.get("panels"))
            else:
                yield p


def _duration_ms(text) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*(ms|s|m|h|d)\s*", text) if isinstance(text, str) else None
    return int(m.group(1)) * _UNITS_MS[m.group(2)] if m else 0


def _listed_values(variable: dict) -> set[str]:
    out = {str(o.get("value")) for o in variable.get("options") or [] if isinstance(o, dict)}
    current = (variable.get("current") or {}).get("value")
    if isinstance(current, str):
        out.add(current)
    return out


def resolve(board: dict, panel: F.Panel, values: dict[str, str]) -> Query:
    """The query the dashboard holds for `panel`, with its variables set to `values`. Raises PanelError when the
    panel is missing, has no single SQL query, uses a variable `values` does not name, or when a value is not one
    the dashboard lists for that variable. A value other than "All" must actually be used by the query: reading
    "spot" from a query with no tier filter would label every rental spot."""
    dash = board.get("dashboard") if isinstance(board, dict) else None
    if not isinstance(dash, dict):
        raise PanelError("no_dashboard", "the answer has no dashboard")
    hits = [p for p in _walk(dash.get("panels")) if p.get("title") == panel.title]
    if len(hits) != 1:
        raise PanelError("panel_not_found", f"{len(hits)} panels titled {panel.title!r} on {panel.dashboard}")
    p = hits[0]
    targets = [t for t in p.get("targets") or [] if isinstance(t, dict) and not t.get("hide")]
    if len(targets) != 1 or not isinstance(targets[0].get("rawSql"), str) or not targets[0]["rawSql"].strip():
        raise PanelError("panel_reshaped", f"{panel.title!r} no longer has exactly one SQL query")
    target = targets[0]
    datasource = target.get("datasource") or p.get("datasource")
    if not isinstance(datasource, dict) or not datasource.get("uid"):
        raise PanelError("panel_reshaped", f"{panel.title!r} names no datasource")

    listed = {v.get("name"): _listed_values(v) for v in (dash.get("templating") or {}).get("list") or []
              if isinstance(v, dict)}
    used: set[str] = set()

    def fill(m: re.Match) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        if name not in values:
            raise PanelError("unknown_variable", f"{panel.title!r} uses a dashboard variable not handled: {name}")
        if values[name] not in listed.get(name, ()):
            raise PanelError("value_not_listed", f"the dashboard does not list {values[name]!r} for {name}")
        used.add(name)
        return "'" + values[name].replace("'", "''") + "'"

    sql = _VARIABLE.sub(fill, target["rawSql"])
    for name, value in values.items():
        if value != "All" and name not in used:
            raise PanelError("filter_missing", f"{panel.title!r} no longer filters by {name}")
    return Query(sql=sql, datasource=datasource, fmt=target.get("format") or "time_series",
                 min_interval_ms=_duration_ms(p.get("interval")),
                 max_points=p.get("maxDataPoints") if isinstance(p.get("maxDataPoints"), int) else DEFAULT_MAX_POINTS)


class Boards:
    """One fetch of each dashboard per run."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._seen: dict[str, dict | BoardError] = {}

    async def get(self, uid: str) -> dict:
        if uid not in self._seen:
            self._seen[uid] = await self._fetch(uid)
        hit = self._seen[uid]
        if isinstance(hit, BoardError):
            raise hit
        return hit

    async def _fetch(self, uid: str) -> dict | BoardError:
        try:
            r = await self.http.get(f"{F.GRAFANA_URL}/api/dashboards/uid/{uid}", headers={"accept": "application/json"})
        except httpx.HTTPError as exc:
            return BoardError(f"dashboard {uid}: {type(exc).__name__}: {exc}"[:300])
        if r.status_code != 200:
            return BoardError(f"dashboard {uid}: HTTP {r.status_code} {_message(r)}"[:300], r.status_code)
        try:
            return r.json()
        except ValueError:
            return BoardError(f"dashboard {uid}: the answer is not JSON", 200)


def _message(r: httpx.Response) -> str:
    try:
        doc = r.json()
    except ValueError:
        return (r.text or "")[:200]
    if isinstance(doc, dict):
        result = (doc.get("results") or {}).get("A") if isinstance(doc.get("results"), dict) else None
        return str((result or {}).get("error") or doc.get("message") or doc.get("error") or "")[:200]
    return ""


# --------------------------------------------------------------------------
# One read
# --------------------------------------------------------------------------


def _label(fld: dict) -> str:
    labels = fld.get("labels")
    if isinstance(labels, dict) and labels:
        return " ".join(str(v) for _, v in sorted(labels.items()) if v not in (None, ""))
    return str(fld.get("name") or "")


def compact(frames: list, *, panel: F.Panel, sha: str, start: datetime, end: datetime) -> dict:
    """The answer's non-empty cells, in a stable order: [time ms, label index, value] plus the nanoseconds past
    the millisecond when there are any. Nothing the frames say about the data is dropped."""
    cells = []
    for frame in frames:
        fields = frame["schema"]["fields"]
        values = frame["data"]["values"]
        if not fields:                      # an empty window: one frame with no fields
            continue
        t = next(i for i, f in enumerate(fields) if f.get("type") == "time")
        nanos = (frame["data"].get("nanos") or [None] * len(fields))[t] or []
        for i, fld in enumerate(fields):
            if i == t:
                continue
            label = _label(fld)
            for row, v in enumerate(values[i]):
                if v is not None:
                    cells.append((values[t][row], nanos[row] if row < len(nanos) else 0, label, v))
    labels = sorted({c[2] for c in cells})
    at = {label: i for i, label in enumerate(labels)}
    cells.sort(key=lambda c: (c[0], c[1] or 0, c[2], c[3]))
    return {"dashboard": panel.dashboard, "panel": panel.title, "sql_sha256": sha,
            "from": start.isoformat(), "to": end.isoformat(), "labels": labels,
            "cells": [[c[0], at[c[2]], c[3]] + ([c[1]] if c[1] else []) for c in cells]}


async def read(http: httpx.AsyncClient, boards: Boards, panel: F.Panel, start: datetime, end: datetime, *,
               tier: str | None = None, params: dict | None = None) -> raw.Fetched:
    """One panel over [start, end]. Never raises: every outcome is a Fetched that raw.store can keep."""
    params = {**({"tier": tier} if tier else {}), **(params or {})}
    url = F.GRAFANA_URL + "/api/ds/query"
    began, at = time.perf_counter(), datetime.now(UTC)

    def out(status, body, error=None) -> raw.Fetched:
        return raw.Fetched(panel, url, params, at, status, int((time.perf_counter() - began) * 1000), body, error)

    def failed(status, code: str, message: str) -> raw.Fetched:
        # a small error object, hashed by status and code (raw.stored_body): a lasting failure is one row
        return out(status, json.dumps({"error": {"code": code, "message": message[:300]}}), f"{code}: {message}"[:500])

    try:
        board = await boards.get(panel.dashboard)
    except BoardError as exc:
        return failed(None if exc.status in (None, 200) else exc.status, "dashboard_unreadable", str(exc))
    try:
        q = resolve(board, panel, {"gpu_type": "All", "tier": tier or "All"})
    except PanelError as exc:
        return failed(None, exc.code, str(exc))

    request = {"queries": [{"refId": "A", "datasource": q.datasource, "rawSql": q.sql, "format": q.fmt,
                            "intervalMs": q.interval_ms(start, end), "maxDataPoints": q.max_points}],
               "from": str((start - EPOCH) // timedelta(milliseconds=1)),
               "to": str((end - EPOCH) // timedelta(milliseconds=1))}
    try:
        r = await http.post(url, json=request, headers={"accept": "application/json"})
    except httpx.HTTPError as exc:
        return failed(None, "network", f"{type(exc).__name__}: {exc}")
    if r.status_code != 200:
        f = failed(r.status_code, "query_refused", f"HTTP {r.status_code} {_message(r)}")
        if r.status_code == 429 and r.headers.get("retry-after"):
            f.error = f"{f.error}; retry-after {r.headers['retry-after']}s"[:500]
        return f
    try:
        result = r.json()["results"]["A"]
        if result.get("error"):
            raise ValueError(str(result["error"])[:300])
        doc = compact(result.get("frames") or [], panel=panel, sha=q.sha, start=start, end=end)
    except (ValueError, KeyError, TypeError, IndexError, StopIteration, AttributeError) as exc:
        doc = {"dashboard": panel.dashboard, "panel": panel.title, "sql_sha256": q.sha, "from": start.isoformat(),
               "to": end.isoformat(), "error": f"{type(exc).__name__}: {exc}"[:300]}
        return out(200, json.dumps(doc, sort_keys=True, separators=(",", ":")), f"answer: {doc['error']}")
    return out(200, json.dumps(doc, sort_keys=True, separators=(",", ":")))


# --------------------------------------------------------------------------
# Parse a stored body
# --------------------------------------------------------------------------


def _number(v) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        d = Decimal(repr(v))
    except InvalidOperation:
        return None
    return d.quantize(SIX_DP) if d.is_finite() else None


def _cells(doc):
    labels = doc.get("labels") if isinstance(doc, dict) else None
    if not isinstance(labels, list) or not isinstance(doc.get("cells"), list):
        raise ValueError("not a stored panel answer")
    for c in doc["cells"]:
        ok = isinstance(c, list) and len(c) >= 3 and isinstance(c[0], int) and isinstance(c[1], int) \
            and 0 <= c[1] < len(labels)
        yield (c[0], labels[c[1]], c[2], c[3] if len(c) > 3 and isinstance(c[3], int) else 0) if ok else None


def rentals_from(doc: dict) -> tuple[list[dict], int]:
    """(rental rows, cells that are not a rental). A point needs a time, a GPU model and a price of zero or more.
    Two points identical to the nanosecond are two rentals: the ordinal in the key keeps both."""
    rows, rejected, seen = [], 0, Counter()
    for c in _cells(doc):
        price = _number(c[2]) if c else None
        if c is None or price is None or price < 0 or not c[1]:
            rejected += 1
            continue
        ms, label, _, ns = c
        ident = (ms, ns, label, str(price))
        key = hashlib.sha256("|".join(map(str, (*ident, seen[ident]))).encode()).hexdigest()[:32]
        seen[ident] += 1
        rows.append({"row_key": key, "start_at": EPOCH + timedelta(milliseconds=ms, microseconds=ns // 1000),
                     "gpu_model": label[:128], "price_per_gpu_hour": price})
    return rows, rejected


def series_from(doc: dict) -> tuple[list[dict], int]:
    """(hour rows, cells that are not one). A point must sit on a UTC hour: the panel's step is one hour, and a
    different step would be a different quantity."""
    rows, rejected = {}, 0
    for c in _cells(doc):
        value = _number(c[2]) if c else None
        if c is None or value is None or not c[1] or c[0] % HOUR_MS or c[3]:
            rejected += 1
            continue
        rows[(c[0], c[1][:128])] = {"hour_utc": EPOCH + timedelta(milliseconds=c[0]), "gpu_model": c[1][:128],
                                    "value": value}
    return list(rows.values()), rejected


def _parsed(f: raw.Fetched, parse) -> tuple[list[dict], int]:
    if f.status_code != 200 or f.body is None or f.error:
        return [], 0
    try:
        return parse(json.loads(f.body))
    except (ValueError, KeyError, TypeError) as exc:
        f.error = f"parse: {exc}"[:500]
        return [], 0


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _batches(rows: list, n: int = 1000):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def write_rentals(s, rows: list[dict], tier: str | None, snapshot_id: int | None, visibility: str) -> dict:
    """Insert the rentals not stored yet; a tier read also names the tier of the ones already there."""
    new = named = 0
    for batch in _batches(rows):
        common = {"tier": tier, "snapshot_id": snapshot_id, "visibility": visibility}
        # RETURNING counts the rows really written (a multi-row insert reports no rowcount)
        new += len(s.execute(pg_insert(LiumGrafanaRental).values([{**r, **common} for r in batch])
                             .on_conflict_do_nothing(index_elements=["row_key"])
                             .returning(LiumGrafanaRental.row_key)).all())
        if tier:
            named += len(s.execute(update(LiumGrafanaRental)
                                   .where(LiumGrafanaRental.row_key.in_([r["row_key"] for r in batch]),
                                          LiumGrafanaRental.tier.is_distinct_from(tier)).values(tier=tier)
                                   .returning(LiumGrafanaRental.row_key)).all())
    return {"new": new, "tier_named": named}


ZERO_NEEDS_A_ROW = ("spend_usd_per_hour",)     # a zero of these is written only where the hour's row exists


def write_series(s, metric: str, rows: list[dict], visibility: str, at: datetime) -> int:
    """Set one column of lium_grafana_hourly. New hours are inserted; an hour whose value moved (the running hour,
    or a revision) is updated in place; an unchanged value is not rewritten. Returns the rows written."""
    H = LiumGrafanaHourly
    column = getattr(H, metric)
    zeros = [r for r in rows if metric in ZERO_NEEDS_A_ROW and r["value"] == 0]
    upserts = [r for r in rows if not (metric in ZERO_NEEDS_A_ROW and r["value"] == 0)]
    written = 0
    for batch in _batches(upserts):
        stmt = pg_insert(H).values([{"hour_utc": r["hour_utc"], "gpu_model": r["gpu_model"], metric: r["value"],
                                     "updated_at": at, "visibility": visibility} for r in batch])
        written += len(s.execute(stmt.on_conflict_do_update(
            index_elements=["hour_utc", "gpu_model"],
            set_={metric: stmt.excluded[metric], "updated_at": stmt.excluded.updated_at},
            where=column.is_distinct_from(stmt.excluded[metric])).returning(H.hour_utc)).all())
    for batch in _batches(zeros):
        written += len(s.execute(update(H).where(
            tuple_(H.hour_utc, H.gpu_model).in_([(r["hour_utc"], r["gpu_model"]) for r in batch]),
            column.is_distinct_from(0)).values({metric: 0, "updated_at": at}).returning(H.hour_utc)).all())
    return written


def _report(f: raw.Fetched, st: raw.Stored, rows: int, **more) -> dict:
    raw.log_outcome(st, f)
    return {"feed": f.feed.name, **f.params, "state": st.state, "status_code": f.status_code, "changed": st.new,
            "rows": rows, **more, "error": f.error if st.state not in ("ok", "empty") else None}


def save_rentals(f: raw.Fetched, tier: str | None) -> dict:
    """Store one answer of the rentals panel raw, then its rentals. `tier`: None for the "All" read."""
    rows, rejected = _parsed(f, rentals_from)
    wrote = {"new": 0, "tier_named": 0}
    with _session().begin() as s:
        st = raw.store(s, f, rows=len(rows), rejected=rejected)
        if st.new and st.state == "ok":
            wrote = write_rentals(s, rows, tier, st.snapshot.id, st.snapshot.visibility)
    return _report(f, st, len(rows), rejected=rejected, **wrote)


def save_series(panel: F.Panel, f: raw.Fetched) -> dict:
    rows, rejected = _parsed(f, series_from)
    written = 0
    with _session().begin() as s:
        st = raw.store(s, f, rows=len(rows), rejected=rejected)
        if st.new and st.state == "ok":
            written = write_series(s, panel.metric, rows, st.snapshot.visibility, f.fetched_at)
    return _report(f, st, len(rows), rejected=rejected, written=written)


# --------------------------------------------------------------------------
# The hourly job
# --------------------------------------------------------------------------


def _window(newest: datetime | None, now: datetime) -> tuple[datetime, datetime]:
    start = newest - OVERLAP if newest else now - FIRST_WINDOW
    return max(min(start, now - MIN_WINDOW), now - MAX_WINDOW), now


def windows(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """Per panel, the range the next poll reads: from a little before its newest stored point to now."""
    H = LiumGrafanaHourly
    with _session()() as s:
        newest = {F.RENTALS_PANEL.name: s.execute(select(func.max(LiumGrafanaRental.start_at))).scalar()}
        for p in F.SERIES_PANELS:
            newest[p.name] = s.execute(select(func.max(H.hour_utc)).where(getattr(H, p.metric).is_not(None))).scalar()
    out = {}
    for name, last in newest.items():
        start, end = _window(last, now)
        if name != F.RENTALS_PANEL.name:
            start = start.replace(minute=0, second=0, microsecond=0)     # whole hours only
        out[name] = (start, end)
    return out


async def poll(http: httpx.AsyncClient | None = None, now: datetime | None = None,
               gap: float = F.POLITE_GAP_SECONDS) -> dict:
    """The `lium_grafana` job: the rentals panel once per tier value (feeds.TIERS), then the three hourly series."""
    own = http is None
    http = http or raw.client()
    now = now or datetime.now(UTC)
    ranges = windows(now)
    boards, results = Boards(http), []
    try:
        start, end = ranges[F.RENTALS_PANEL.name]
        for tier in F.TIERS:
            f = await read(http, boards, F.RENTALS_PANEL, start, end, tier=tier)
            results.append(save_rentals(f, None if tier == "All" else tier))
            await asyncio.sleep(gap)
        for panel in F.SERIES_PANELS:
            f = await read(http, boards, panel, *ranges[panel.name])
            results.append(save_series(panel, f))
            await asyncio.sleep(gap)
    finally:
        if own:
            await http.aclose()
    return {"panels": results}


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _stored_chunk(panel: F.Panel, params: dict, closed_at: datetime) -> str | None:
    """The state of a chunk already read after it closed (ok / empty), else None."""
    with _session()() as s:
        return s.execute(select(LiumFeedSnapshot.state)
                         .where(LiumFeedSnapshot.feed == panel.name,
                                LiumFeedSnapshot.params_key == raw.params_key(params),
                                LiumFeedSnapshot.state.in_(("ok", "empty")), LiumFeedSnapshot.fetched_at >= closed_at)
                         .order_by(LiumFeedSnapshot.fetched_at.desc()).limit(1)).scalar()


async def backfill(since: date | None = None, until: date | None = None, *, only: str | None = None,
                   force: bool = False, http: httpx.AsyncClient | None = None, now: datetime | None = None,
                   gap: float = F.POLITE_GAP_SECONDS) -> dict:
    """History, newest chunk first, back to `since` (default: until EMPTY_CHUNKS_STOP empty chunks in a row).
    `only`: "rentals" or "series". A chunk read after it closed is skipped unless `force`, so a stopped run
    resumes where it left off. Stops a panel after ERROR_CHUNKS_STOP failed chunks in a row."""
    own = http is None
    http = http or raw.client()
    now = now or datetime.now(UTC)
    last_day = (until or now.date()) + timedelta(days=1)
    boards = Boards(http)
    plans = []
    if only in (None, "rentals"):
        plans.append((F.RENTALS_PANEL, RENTAL_CHUNK_DAYS, F.TIERS))
    if only in (None, "series"):
        plans += [(p, SERIES_CHUNK_DAYS, (None,)) for p in F.SERIES_PANELS]
    out = {}
    try:
        for panel, days, tiers in plans:
            tally = {"chunks": 0, "skipped": 0, "rows": 0, "written": 0, "errors": 0, "oldest": None, "stopped": None}
            out[panel.name] = tally
            hi, empty, errors = last_day, 0, 0
            while tally["stopped"] is None:
                lo = hi - timedelta(days=days)
                if since and lo < since:
                    lo = since
                if lo >= hi:
                    tally["stopped"] = "reached --since"
                    break
                chunk, states = f"{lo}/{hi}", []
                for tier in tiers:
                    params = {"chunk": chunk}
                    key = {**({"tier": tier} if tier else {}), **params}
                    done = None if force else _stored_chunk(panel, key, _midnight(hi))
                    if done:
                        tally["skipped"] += 1
                        states.append(done)
                        continue
                    f = await read(http, boards, panel, _midnight(lo), _midnight(hi), tier=tier, params=params)
                    r = save_series(panel, f) if panel.metric else save_rentals(f, None if tier == "All" else tier)
                    tally["rows"] += r["rows"]
                    tally["written"] += r.get("written", 0) + r.get("new", 0)
                    states.append(r["state"])
                    await asyncio.sleep(gap)
                tally["chunks"] += 1
                first = states[-1]                      # the unfiltered read (last) decides whether the chunk had data
                if first in ("ok", "empty"):
                    errors = 0
                    empty = empty + 1 if first == "empty" else 0
                    if first == "ok":
                        tally["oldest"] = str(lo)
                else:
                    errors += 1
                    tally["errors"] += 1
                log.info("lium grafana backfill %s %s: %s", panel.name, chunk, ", ".join(states))
                if errors >= ERROR_CHUNKS_STOP:
                    tally["stopped"] = f"{errors} failed chunks in a row"
                elif since is None and empty >= EMPTY_CHUNKS_STOP:
                    tally["stopped"] = f"{empty} empty chunks in a row"
                hi = lo
    finally:
        if own:
            await http.aclose()
    return out


# --------------------------------------------------------------------------
# Rebuild from the stored bodies
# --------------------------------------------------------------------------


def reparse() -> dict:
    """Re-parse every stored ok answer, oldest first (after a parser change)."""
    out = {p: 0 for p in F.GRAFANA_PANELS}
    with _session()() as s:
        ids = [r[0] for r in s.execute(select(LiumFeedSnapshot.id)
                                       .where(LiumFeedSnapshot.feed.in_(list(F.GRAFANA_PANELS)),
                                              LiumFeedSnapshot.state == "ok")
                                       .order_by(LiumFeedSnapshot.fetched_at, LiumFeedSnapshot.id)).all()]
    for snapshot_id in ids:
        with _session().begin() as s:
            snap = s.get(LiumFeedSnapshot, snapshot_id)
            panel = F.GRAFANA_PANELS[snap.feed]
            doc = json.loads(snap.body)
            if panel.metric:
                write_series(s, panel.metric, series_from(doc)[0], snap.visibility, snap.fetched_at)
            else:
                tier = (snap.params or {}).get("tier")
                write_rentals(s, rentals_from(doc)[0], None if tier in (None, "All") else tier, snap.id,
                              snap.visibility)
            out[snap.feed] += 1
    return {"snapshots": out}
