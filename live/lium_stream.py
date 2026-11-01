"""Lium's public executor WebSocket (wss://lium.io/api/executors/ws), ingested as it changes. No key.

Frames (docs.lium.io, executor-availability-websocket; schema_version 1; checked live 2026-11-01):
    snapshot   always first on a connection: every executor; replace local state
    upsert     one executor added or replaced (`executor_id`, `executor`: the whole record in practice)
    delete     one executor removed (`executor_id`)
    heartbeat  connection alive; carries the current seq (it does not advance it)
Every frame has `seq` (increments by one per state frame) and `ts` (server time, Unix seconds).

What is kept, and when it is written (raw first, change-only, batched):
    * The Book holds each executor reduced with providers.lium._keep, the exact fields the Lium normalizer reads.
      A frame that changes none of them (Lium re-sends executors as their uptime counter ticks) changes nothing
      and is only counted.
    * Each real change becomes one stream_events row: add / change (with the changed fields) / remove, the
      provider's `ts` and `seq`, the kept view and the executor as sent. basis "stream" for a live frame;
      basis "resnapshot" for differences found when a fresh snapshot is diffed against the book after a
      reconnect or a sequence gap (then the change happened between prev_seen_at and provider_ts).
    * Every FLUSH_SECONDS, if anything changed (or KEEPALIVE_SECONDS passed), the whole book is written as ONE
      raw snapshot in exactly the poller's shape (provider "lium", endpoint providers.lium.ENDPOINT, method "WS",
      request {"source": "websocket", ...}), and Lium is re-normalized. listing_observations stay change-only
      because normalize records an observation only when a listing moved; listings and capacity refresh within a
      minute of a change instead of fifteen. The poller's silence watch sees these successful snapshots.
    * Nothing is written from a stale book: no frame (heartbeats included) for STALE_SECONDS -> reconnect.

Fallback: the Lium REST poller keeps running unchanged (900 s). If the socket drops, the newest raw snapshot is
simply the poller's again; when the socket returns, its first snapshot is diffed against the book. A sequence gap
(an upsert or delete whose seq is not last + 1, or a heartbeat ahead of the book) closes the connection and
resnapshots. Close code 1013 and any error back off exponentially (1 s to 5 min, jittered).

Integration with provider change times (the data/freshness branch's provider_changed_at / changed_at_basis):
latest_changes(item_ids) returns, per Lium listing_id, the newest stream_events row: provider_ts is the provider's
change time. Wired in: each stream snapshot's request carries stream_change_times() (listing_id -> provider ts of
its newest live change), which providers.lium.normalize copies to provider_changed_at with changed_at_basis "exact";
"resnapshot" differences get no provider time and are recorded as "poll_window" with the usual bracket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone

from providers import lium

log = logging.getLogger(__name__)

PROVIDER = "lium"
STREAM = "lium_ws"
URL = "wss://lium.io/api/executors/ws"
USER_AGENT = "opengrid-terminal/0.1 (+live feed)"
SCHEMA_VERSION = 1
FLUSH_SECONDS = 60          # batch window: changes are written at most once a minute
KEEPALIVE_SECONDS = 300     # write the (unchanged) book this often so freshness reflects the live feed
STALE_SECONDS = 180         # no frame for this long: the socket is dead, reconnect
BACKOFF_MIN, BACKOFF_MAX = 1.0, 300.0
MAX_BUFFER = 2000           # flush early if this many changes are waiting
MAX_FRAME_BYTES = 32 * 1024 * 1024


class Reshaped(ValueError):
    """A frame OpenGrid cannot read: the stream changed shape. Reconnect with backoff, never guess."""


def _ts(v) -> datetime:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise Reshaped(f"lium ws: ts {v!r} is not a Unix time")
    return datetime.fromtimestamp(float(v), timezone.utc)


def _keep(executor: dict) -> dict:
    return lium._keep(executor)


class Book:
    """Executors by id (kept view + raw), the last seq and when the book was last known to be right."""

    def __init__(self, items: dict[str, dict] | None = None, seen_at: datetime | None = None):
        self.kept: dict[str, dict] = dict(items or {})
        self.raw: dict[str, dict] = {}
        self.seq: int | None = None
        self.seen_at = seen_at          # provider time of the newest frame applied (or the seed's fetch time)
        self.seeded = items is not None
        self.unchanged = 0              # frames that changed nothing OpenGrid reads

    def payload(self) -> list[dict]:
        """The book in the poller's stored shape: a list of kept executors."""
        return [self.kept[k] for k in sorted(self.kept)]

    def _event(self, kind, item_id, frame, ts, received_at, conn, basis, changed=None, prev=None):
        return {"provider": PROVIDER, "stream": STREAM, "connection_id": conn, "seq": frame.get("seq"),
                "provider_ts": ts, "received_at": received_at, "prev_seen_at": prev, "kind": kind, "basis": basis,
                "item_id": str(item_id), "changed_fields": changed,
                "kept": self.kept.get(item_id) if kind != "remove" else None,
                "raw": self.raw.get(item_id) if kind != "remove" else None}

    def apply(self, frame: dict, received_at: datetime, conn: str) -> tuple[list[dict], str | None]:
        """Apply one frame. Returns (change events, action); action "resnapshot" means: close and reconnect."""
        if not isinstance(frame, dict):
            raise Reshaped("lium ws: frame is not an object")
        kind = frame.get("type")
        if frame.get("schema_version") != SCHEMA_VERSION:
            raise Reshaped(f"lium ws: schema_version {frame.get('schema_version')!r}, expected {SCHEMA_VERSION}")
        seq = frame.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise Reshaped(f"lium ws: seq {seq!r} is not an integer")
        ts = _ts(frame.get("ts"))
        if kind == "snapshot":
            return self._snapshot(frame, seq, ts, received_at, conn), None
        if self.seq is None:
            return [], "resnapshot"     # a state frame before any snapshot on this book: start over
        if kind == "heartbeat":
            if seq > self.seq:
                return [], "resnapshot"  # frames were sent that we never saw
            self.seen_at = max(self.seen_at or ts, ts)
            return [], None
        if kind not in ("upsert", "delete"):
            raise Reshaped(f"lium ws: unknown frame type {kind!r}")
        if seq <= self.seq:
            return [], None             # already applied (duplicate)
        if seq != self.seq + 1:
            return [], "resnapshot"
        self.seq, self.seen_at = seq, ts
        item = frame.get("executor_id")
        if not item:
            raise Reshaped(f"lium ws: {kind} without executor_id")
        item = str(item)
        if kind == "delete":
            if item not in self.kept:
                return [], None
            ev = self._event("remove", item, frame, ts, received_at, conn, "stream")
            self.kept.pop(item, None)
            self.raw.pop(item, None)
            return [ev], None
        ex = frame.get("executor")
        if not isinstance(ex, dict):
            raise Reshaped("lium ws: upsert without an executor object")
        merged = {**self.raw.get(item, {}), **ex}  # a partial upsert (as the docs' example) updates fields
        kept = _keep(merged)
        kept["id"] = kept.get("id") or item
        old = self.kept.get(item)
        self.raw[item] = merged
        if old == kept:
            self.unchanged += 1
            return [], None
        self.kept[item] = kept
        if old is None:
            return [self._event("add", item, frame, ts, received_at, conn, "stream")], None
        changed = sorted(k for k in set(old) | set(kept) if old.get(k) != kept.get(k))
        return [self._event("change", item, frame, ts, received_at, conn, "stream", changed)], None

    def _snapshot(self, frame, seq, ts, received_at, conn) -> list[dict]:
        execs = frame.get("executors")
        if not isinstance(execs, list):
            raise Reshaped("lium ws: snapshot without an executors list")
        new_kept, new_raw = {}, {}
        for e in execs:
            if isinstance(e, dict) and e.get("id"):
                new_kept[str(e["id"])] = _keep(e)
                new_raw[str(e["id"])] = e
        compare = self.seeded or self.seq is not None
        prev = self.seen_at
        old = self.kept
        self.kept, self.raw, self.seq, self.seen_at, self.seeded = new_kept, new_raw, seq, ts, True
        if not compare:
            return []   # the very first state: nothing to compare with
        events = []
        for item in sorted(set(old) | set(new_kept)):
            o, n = old.get(item), new_kept.get(item)
            if o == n:
                continue
            if n is None:
                ev = self._event("remove", item, frame, ts, received_at, conn, "resnapshot", prev=prev)
            elif o is None:
                ev = self._event("add", item, frame, ts, received_at, conn, "resnapshot", prev=prev)
            else:
                ev = self._event("change", item, frame, ts, received_at, conn, "resnapshot",
                                 sorted(k for k in set(o) | set(n) if o.get(k) != n.get(k)), prev)
            events.append(ev)
        return events


# --------------------------------------------------------------------------- persistence (blocking; run in a thread)

def seed_book() -> Book:
    """The book as the newest stored Lium snapshot (poller or stream) left it, so a restart diffs, not forgets."""
    import normalize
    from providers.base import find_endpoint

    with normalize.SessionLocal() as s:
        rows = find_endpoint(normalize._latest_raw(s, PROVIDER), lium.ENDPOINT)
    if not rows or not isinstance(rows[0].payload, list):
        return Book()
    items = {str(e["id"]): e for e in rows[0].payload if isinstance(e, dict) and e.get("id")}
    return Book(items, rows[0].fetched_at)


def write(events: list[dict], book_payload: list[dict] | None, meta: dict, now: datetime,
          normalize_after: bool = True) -> dict:
    """Insert the buffered change events, then store the book as one raw snapshot and re-normalize Lium."""
    import normalize
    from fetch import save
    from live.locks import refresh_provider
    from models import RawResponse
    from store.live import StreamEvent

    if events:
        with normalize.SessionLocal.begin() as s:
            s.add_all([StreamEvent(**e) for e in events])
    out = {"events": len(events), "snapshot": False}
    if book_payload is not None:
        meta = {**meta, "changed_at": stream_change_times(events)}
        save([RawResponse(provider=PROVIDER, endpoint=lium.ENDPOINT, method="WS", fetched_at=now, status_code=200,
                          ok=True, payload=book_payload, request={"source": "websocket", "stream": STREAM, **meta})])
        out["snapshot"] = True
        if normalize_after:
            out["normalize"] = refresh_provider(PROVIDER)
    return out


def stream_change_times(events: list[dict]) -> dict[str, str]:
    """listing_id -> the provider `ts` of its newest live ("stream") add/change in this batch, ISO 8601.

    Stored in the snapshot's request so providers.lium.normalize can hand it to the freshness pipeline as
    provider_changed_at with changed_at_basis "exact" (Lium's server time for the frame that carried the change).
    "resnapshot" differences are left out on purpose: the change happened somewhere in (prev_seen_at, provider_ts],
    which normalize already records as a poll_window bracket (prev_observed_at = the last fetch that showed the old
    value). Removals write no observation row, so they carry no time.
    """
    out: dict[str, datetime] = {}
    for e in events:
        if e.get("basis") != "stream" or e.get("kind") == "remove" or not isinstance(e.get("provider_ts"), datetime):
            continue
        item = str(e["item_id"])
        if item not in out or e["provider_ts"] > out[item]:
            out[item] = e["provider_ts"]
    return {k: v.isoformat() for k, v in out.items()}


def latest_changes(item_ids: list[str] | None = None) -> dict[str, dict]:
    """listing_id -> the newest stream_events row (provider_ts, basis, prev_seen_at, kind, changed_fields)."""
    from sqlalchemy import text

    import normalize

    sql = """SELECT DISTINCT ON (item_id) item_id, provider_ts, basis, prev_seen_at, kind, changed_fields
             FROM stream_events WHERE provider = :p {f} ORDER BY item_id, provider_ts DESC, id DESC"""
    params = {"p": PROVIDER}
    if item_ids is not None:
        sql, params["ids"] = sql.format(f="AND item_id = ANY(:ids)"), list(item_ids)
    else:
        sql = sql.format(f="")
    with normalize.SessionLocal() as s:
        return {r.item_id: dict(r._mapping) for r in s.execute(text(sql), params)}


# --------------------------------------------------------------------------- the ingester

class LiumStream:
    """One long-lived consumer. connect(): an async context manager yielding an object with `await recv()`
    (websockets' ClientConnection, or a fake in tests). writer: write() above (patched in tests)."""

    def __init__(self, connect=None, writer=write, clock=time.monotonic, now=lambda: datetime.now(timezone.utc)):
        self.connect = connect or _ws_connect
        self.writer = writer
        self.clock, self.now = clock, now
        self.book: Book | None = None
        self.buffer: list[dict] = []
        self.dirty = False
        self.last_write = None          # clock time of the last snapshot write
        self.last_frame = None          # clock time of the last frame received
        self.status = {"stream": STREAM, "url": URL, "state": "idle", "connected": False, "connection_id": None,
                       "connections": 0, "frames": 0, "changes": 0, "unchanged": 0, "gaps": 0, "errors": 0,
                       "last_error": None, "last_frame_at": None, "last_seq": None, "last_provider_ts": None,
                       "last_write_at": None, "writes": 0, "backoff_s": 0.0}

    # -- frames
    def handle(self, raw_frame: str | bytes, conn: str) -> str | None:
        frame = json.loads(raw_frame)
        self.status["frames"] += 1
        self.last_frame = self.clock()
        self.status["last_frame_at"] = self.now().isoformat()
        events, action = self.book.apply(frame, self.now(), conn)
        if frame.get("type") == "snapshot":
            self.dirty = True           # a fresh snapshot is always worth one write
        if events:
            self.buffer += events
            self.dirty = True
            self.status["changes"] += len(events)
        self.status.update(unchanged=self.book.unchanged, last_seq=self.book.seq,
                           last_provider_ts=self.book.seen_at.isoformat() if self.book.seen_at else None)
        if action == "resnapshot":
            self.status["gaps"] += 1
        return action

    def fresh(self) -> bool:
        return self.last_frame is not None and self.clock() - self.last_frame < STALE_SECONDS

    def due(self) -> bool:
        if not self.fresh() or self.book is None or self.book.seq is None:
            return False
        if len(self.buffer) >= MAX_BUFFER:
            return True
        since = None if self.last_write is None else self.clock() - self.last_write
        return (self.dirty and (since is None or since >= FLUSH_SECONDS)) or (since is not None and since >= KEEPALIVE_SECONDS)

    async def flush(self, force: bool = False) -> dict | None:
        if not force and not self.due():
            return None
        if not self.fresh() or self.book is None or self.book.seq is None:
            # stale book: keep the events (they are true), never write a snapshot of state we cannot vouch for
            payload = None
        else:
            payload = self.book.payload()
        events, self.buffer = self.buffer, []
        if not events and payload is None:
            return None
        meta = {"seq": self.book.seq if self.book else None, "connection_id": self.status["connection_id"],
                "provider_ts": self.book.seen_at.isoformat() if self.book and self.book.seen_at else None}
        try:
            out = await asyncio.to_thread(self.writer, events, payload, meta, self.now())
        except Exception as exc:  # noqa: BLE001 - a failed write keeps the events for the next flush
            self.buffer = events + self.buffer
            self.status["errors"] += 1
            self.status["last_error"] = f"write: {type(exc).__name__}: {exc}"[:300]
            log.exception("lium stream: write failed")
            return None
        if payload is not None:
            self.dirty = False
            self.last_write = self.clock()
            self.status["writes"] += 1
            self.status["last_write_at"] = self.now().isoformat()
        return out

    # -- one connection
    async def consume(self, ws, conn: str) -> str:
        """Read frames until the socket ends, goes stale, or a gap asks for a resnapshot. Returns why."""
        while True:
            wait = STALE_SECONDS if self.last_write is None else max(1.0, min(STALE_SECONDS, FLUSH_SECONDS))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                if not self.fresh():
                    return "stale"
                await self.flush()
                continue
            action = self.handle(msg, conn)
            await self.flush()
            if action == "resnapshot":
                return "gap"

    async def run(self) -> None:
        backoff = BACKOFF_MIN
        if self.book is None:
            try:
                self.book = await asyncio.to_thread(seed_book)
            except Exception:  # noqa: BLE001 - no seed: the first snapshot is the first state
                log.exception("lium stream: could not seed the book from stored raw")
                self.book = Book()
        while True:
            conn = uuid.uuid4().hex[:12]
            self.status.update(state="connecting", connection_id=conn)
            reason = None
            try:
                async with self.connect() as ws:
                    self.status.update(state="connected", connected=True)
                    self.status["connections"] += 1
                    self.book.seq = None    # seq is per connection: wait for this connection's snapshot
                    reason = await self.consume(ws, conn)
                    if self.book.seq is not None:
                        backoff = BACKOFF_MIN   # a connection that delivered a snapshot resets the backoff
            except asyncio.CancelledError:
                self.status.update(state="stopped", connected=False)
                await self.flush(force=True)
                raise
            except Exception as exc:  # noqa: BLE001 - reshaped frames, closes (incl. 1013), network: back off
                reason = f"{type(exc).__name__}: {exc}"[:300]
                self.status["errors"] += 1
                self.status["last_error"] = reason
                log.warning("lium stream: connection %s ended: %s", conn, reason)
            self.status.update(state="backoff", connected=False)
            await self.flush(force=True)    # keep what was learned (events); a stale book is not written
            delay = backoff * (0.5 + random.random())
            self.status["backoff_s"] = round(delay, 1)
            log.info("lium stream: reconnecting in %.1fs (%s)", delay, reason)
            await asyncio.sleep(delay)
            backoff = min(BACKOFF_MAX, backoff * 2)


def _ws_connect():
    import websockets

    return websockets.connect(URL, user_agent_header=USER_AGENT, max_size=MAX_FRAME_BYTES, open_timeout=30,
                              ping_interval=30, ping_timeout=30, close_timeout=5)


# --------------------------------------------------------------------------- process wiring

_INGEST: LiumStream | None = None
_TASK: asyncio.Task | None = None
_registered = False


def status() -> dict:
    s = dict(_INGEST.status) if _INGEST else {"stream": STREAM, "url": URL, "state": "off", "connected": False}
    s["running"] = bool(_TASK and not _TASK.done())
    s["fallback"] = "the Lium REST poller (every 900 s) runs whatever the stream does"
    return s


async def ensure_running() -> dict:
    """The supervisor job: start the consumer if it is not running (first run, or it crashed)."""
    global _INGEST, _TASK
    if _TASK is None or _TASK.done():
        if _TASK is not None and _TASK.done() and not _TASK.cancelled() and _TASK.exception():
            log.error("lium stream: consumer died: %r; restarting", _TASK.exception())
        _INGEST = _INGEST or LiumStream()
        _TASK = asyncio.create_task(_INGEST.run(), name="lium-stream")
    return status()


def register() -> None:
    """Idempotent: importing api.live calls this once. Off with POLLER_ENABLED or LIUM_STREAM_ENABLED off."""
    global _registered
    from config import settings
    from jobs import job

    if _registered or not settings.poller_enabled or not settings.lium_stream_enabled:
        return
    _registered = True
    job("lium_stream", every_seconds=60, initial_delay_seconds=20)(ensure_running)
