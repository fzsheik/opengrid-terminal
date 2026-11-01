"""Lium WebSocket ingester (live/lium_stream.py): the book over REAL captured frames (snapshot, upserts that change a
price, a new executor, a heartbeat), change-only events with the provider's timestamp, unchanged frames counted not
stored, duplicates, sequence gaps and reshaped frames, the batched writer (one compacted snapshot in the poller's
shape, never from a stale book), reconnect with backoff, and the database path: events stored, Lium listings
re-normalized from the stream's snapshot, observations still change-only, the silence watch seeing Lium as fed,
the book reseeded after a restart. No network: frames are a fixture and the socket is a fake.

Fixture: fixtures_live/lium_ws_frames.json, 90 s captured from wss://lium.io/api/executors/ws on 2026-11-01, the
snapshot cut to the 3 executors the upserts touch + 4 others.

Run:  .venv/bin/python tests/test_live_lium.py
"""

import asyncio
import copy
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
from live import lium_stream as ls  # noqa: E402
from live import locks  # noqa: E402
from providers import lium  # noqa: E402

FRAMES = json.loads((ROOT / "tests" / "fixtures_live" / "lium_ws_frames.json").read_text())
SNAP, REST = FRAMES[0], FRAMES[1:]
T = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
NEW_ID = "63243c2c-a318-440b-b0a2-b24699e8f763"            # first upsert: not in the snapshot
CHANGED = ["925dcca1-a369-41cb-a645-161426a03582", "c06a6fde-1933-48b6-a78e-886e9b86c143",
           "1ae25ea8-a420-4214-bddc-c9e73e16e659"]          # price_per_gpu changed in the capture


def fresh_book():
    b = ls.Book()
    ev, act = b.apply(copy.deepcopy(SNAP), T, "c1")
    assert ev == [] and act is None
    return b


def frame(**kw):
    base = {"schema_version": 1, "ts": SNAP["ts"] + 100}
    base.update(kw)
    return base


def test_book_over_real_frames():
    b = fresh_book()
    assert b.seq == SNAP["seq"] and len(b.kept) == len(SNAP["executors"]) and b.seen_at.timestamp() == SNAP["ts"]
    assert b.payload() == sorted((lium._keep(e) for e in SNAP["executors"]), key=lambda e: e["id"])
    events = []
    for f in REST:
        ev, act = b.apply(copy.deepcopy(f), T, "c1")
        assert act is None, f
        events += ev
    kinds = {(e["kind"], e["item_id"]) for e in events}
    assert kinds == {("add", NEW_ID)} | {("change", i) for i in CHANGED}, kinds
    for e in events:
        assert e["basis"] == "stream" and e["provider"] == "lium" and e["stream"] == "lium_ws"
        src = next(f for f in REST if f.get("executor_id") == e["item_id"])
        assert e["seq"] == src["seq"] and e["provider_ts"] == datetime.fromtimestamp(src["ts"], timezone.utc)
        assert e["kept"] == lium._keep(src["executor"]) and e["raw"]["id"] == e["item_id"]
        if e["kind"] == "change":
            assert e["changed_fields"] == ["price_per_gpu"]
    assert b.seq == REST[-1]["seq"]
    print("test_book_over_real_frames ok")


def test_unchanged_duplicates_gaps_and_deletes():
    b = fresh_book()
    ex = copy.deepcopy(SNAP["executors"][0])
    eid = ex["id"]
    # the same executor re-sent with only fields OpenGrid does not read (uptime): counted, no event
    ex["uptime_in_minutes"] = (ex.get("uptime_in_minutes") or 0) + 5
    ev, act = b.apply(frame(type="upsert", seq=b.seq + 1, executor_id=eid, executor=ex), T, "c1")
    assert ev == [] and act is None and b.unchanged == 1
    # duplicate (seq already applied): ignored
    ev, act = b.apply(frame(type="upsert", seq=b.seq, executor_id=eid, executor=dict(ex, price_per_gpu=99)), T, "c1")
    assert ev == [] and act is None and b.kept[eid]["price_per_gpu"] == ex["price_per_gpu"]
    # a partial upsert (the docs' example shape) updates only what it carries
    ev, act = b.apply(frame(type="upsert", seq=b.seq + 1, executor_id=eid, executor={"id": eid, "available_gpu_count": 0}), T, "c1")
    assert [e["changed_fields"] for e in ev] == [["available_gpu_count"]] and b.kept[eid]["machine_name"] == ex["machine_name"]
    # heartbeat at the current seq: fine; ahead of it: a missed frame -> resnapshot
    assert b.apply(frame(type="heartbeat", seq=b.seq), T, "c1") == ([], None)
    assert b.apply(frame(type="heartbeat", seq=b.seq + 1), T, "c1") == ([], "resnapshot")
    # delete: one remove event; deleting something unknown changes nothing
    ev, _ = b.apply(frame(type="delete", seq=b.seq + 1, executor_id=eid), T, "c1")
    assert [(e["kind"], e["item_id"], e["kept"]) for e in ev] == [("remove", eid, None)] and eid not in b.kept
    assert b.apply(frame(type="delete", seq=b.seq + 1, executor_id="nope"), T, "c1") == ([], None)
    # an upsert that skips a seq: gap -> resnapshot, and nothing applied
    before = dict(b.kept)
    assert b.apply(frame(type="upsert", seq=b.seq + 2, executor_id="x", executor={"id": "x"}), T, "c1") == ([], "resnapshot")
    assert b.kept == before
    # a state frame before any snapshot on a book
    assert ls.Book().apply(frame(type="upsert", seq=5, executor_id="x", executor={"id": "x"}), T, "c") == ([], "resnapshot")
    # reshaped frames are refused, never guessed
    for bad in (frame(type="snapshot", seq=1, schema_version=2, executors=[]), frame(type="upsert", seq=b.seq + 1, ts="now"),
                frame(type="mystery", seq=b.seq + 1), frame(type="snapshot", seq=1, executors={}), "not json object",
                frame(type="upsert", seq=b.seq + 1, executor_id="x", executor="?")):
        try:
            b.apply(bad, T, "c1")
            raise AssertionError(f"accepted {bad!r}")
        except ls.Reshaped:
            pass
    print("test_unchanged_duplicates_gaps_and_deletes ok")


def test_resnapshot_diff():
    seeded = ls.Book({e["id"]: lium._keep(e) for e in SNAP["executors"]}, T - timedelta(minutes=15))
    snap2 = copy.deepcopy(SNAP)
    gone = snap2["executors"].pop()                       # removed while we were away
    snap2["executors"][0]["price_per_gpu"] = 1.23         # repriced while we were away
    snap2["executors"].append(next(f for f in REST if f.get("executor_id") == NEW_ID)["executor"])  # added
    snap2["seq"], snap2["ts"] = 9000, SNAP["ts"] + 600
    ev, act = seeded.apply(snap2, T, "c2")
    got = {(e["kind"], e["item_id"]) for e in ev}
    assert got == {("remove", gone["id"]), ("change", snap2["executors"][0]["id"]), ("add", NEW_ID)}
    assert all(e["basis"] == "resnapshot" and e["prev_seen_at"] == T - timedelta(minutes=15) for e in ev)
    assert all(e["provider_ts"] == datetime.fromtimestamp(snap2["ts"], timezone.utc) for e in ev)
    assert seeded.seq == 9000 and len(seeded.kept) == len(snap2["executors"])
    # the same snapshot again: nothing changed, nothing recorded
    assert seeded.apply(copy.deepcopy(snap2), T, "c3") == ([], None)
    print("test_resnapshot_diff ok")


def test_payload_is_the_pollers_shape():
    from types import SimpleNamespace

    b = fresh_book()
    for f in REST:
        b.apply(copy.deepcopy(f), T, "c1")
    snap = SimpleNamespace(payload=b.payload(), fetched_at=T, request={"source": "websocket"}, endpoint=lium.ENDPOINT)
    listings = lium.LiumProvider.normalize({lium.ENDPOINT: [snap]})
    by = {x.listing_id: x for x in listings}
    for f in REST:
        if f.get("type") == "upsert":
            assert float(by[f["executor_id"]].price_per_gpu_hour) == f["executor"]["price_per_gpu"]
    assert lium.LiumProvider.normalize_capacity({lium.ENDPOINT: [snap]})
    print("test_payload_is_the_pollers_shape ok")


# --------------------------------------------------------------------------- the ingester with a fake socket

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeConn:
    """recv() returns queued frames; a callable item runs (e.g. advances the clock); empty -> waits (timeout)."""

    def __init__(self, items, clock=None):
        self.items = list(items)
        self.clock = clock

    async def recv(self):
        while self.items:
            it = self.items.pop(0)
            if callable(it):
                it()
                continue
            if isinstance(it, Exception):
                raise it
            return json.dumps(it)
        await asyncio.sleep(3600)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def recorder():
    calls = []

    def writer(events, payload, meta, now):
        calls.append({"events": list(events), "payload": payload, "meta": meta, "now": now})
        return {"events": len(events), "snapshot": payload is not None}
    return calls, writer


def test_ingester_batches_and_never_writes_a_stale_book():
    clock, (calls, writer) = Clock(), recorder()
    s = ls.LiumStream(writer=writer, clock=clock, now=lambda: T)
    s.book = ls.Book()

    def tick(sec):
        return lambda: setattr(clock, "t", clock.t + sec)

    upserts = [f for f in REST if f["type"] == "upsert"]
    conn = FakeConn([SNAP, upserts[0], tick(10), upserts[1], tick(ls.FLUSH_SECONDS), upserts[2], upserts[3],
                     ValueError("socket closed")])
    try:
        asyncio.run(s.consume(conn, "c1"))
        raise AssertionError("consume should end with the socket error")
    except ValueError:
        pass
    # write 1: the snapshot itself (first state); write 2: after FLUSH_SECONDS, with the batched changes so far
    assert len(calls) == 2, [len(c["events"]) for c in calls]
    assert calls[0]["events"] == [] and len(calls[0]["payload"]) == len(SNAP["executors"])
    assert [e["item_id"] for e in calls[1]["events"]] == [upserts[0]["executor_id"], upserts[1]["executor_id"]] + [upserts[2]["executor_id"]]
    assert calls[1]["meta"]["seq"] == upserts[2]["seq"] and calls[1]["meta"]["provider_ts"]
    assert len(s.buffer) == 1, "the last change waits for the next window"
    # the socket is gone and the clock moves past STALE: the forced flush stores the event, NOT a snapshot
    clock.t += ls.STALE_SECONDS + 1
    asyncio.run(s.flush(force=True))
    assert len(calls) == 3 and calls[2]["payload"] is None and len(calls[2]["events"]) == 1 and s.buffer == []
    # a failing writer keeps the events for next time
    s.buffer = [{"x": 1}]

    def broken(*a):
        raise RuntimeError("db down")
    s.writer = broken
    asyncio.run(s.flush(force=True))
    assert s.buffer == [{"x": 1}] and "db down" in s.status["last_error"]
    print("test_ingester_batches_and_never_writes_a_stale_book ok")


def test_consume_gap_and_stale():
    clock, (calls, writer) = Clock(), recorder()
    s = ls.LiumStream(writer=writer, clock=clock, now=lambda: T)
    s.book = ls.Book()
    up = dict(REST[0], seq=SNAP["seq"] + 5)  # skips four
    assert asyncio.run(s.consume(FakeConn([SNAP, up]), "c1")) == "gap" and s.status["gaps"] == 1
    # no frame for STALE_SECONDS: "stale"
    s2 = ls.LiumStream(writer=writer, clock=clock, now=lambda: T)
    s2.book = ls.Book()
    old_stale = ls.STALE_SECONDS
    ls.STALE_SECONDS = 0.05
    try:
        assert asyncio.run(s2.consume(FakeConn([SNAP, lambda: setattr(clock, "t", clock.t + 1)]), "c2")) == "stale"
    finally:
        ls.STALE_SECONDS = old_stale
    print("test_consume_gap_and_stale ok")


def test_run_reconnects_with_backoff():
    clock, (calls, writer) = Clock(), recorder()
    s = ls.LiumStream(writer=writer, clock=clock, now=lambda: T)
    s.book = ls.Book()
    sleeps, attempts = [], []
    gap = dict(REST[0], seq=SNAP["seq"] + 9)
    snap2 = dict(copy.deepcopy(SNAP), seq=20000)
    plan = [OSError("refused (1013 try again later)"), FakeConn([SNAP, gap]), FakeConn([snap2, ValueError("closed")])]

    def connect():
        attempts.append(1)
        if not plan:
            raise asyncio.CancelledError()
        item = plan.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_sleep(d):
        sleeps.append(d)

    real_sleep = ls.asyncio.sleep
    s.connect = connect
    ls.asyncio.sleep = fake_sleep
    try:
        asyncio.run(s.run())
        raise AssertionError("run ends only by cancellation")
    except asyncio.CancelledError:
        pass
    finally:
        ls.asyncio.sleep = real_sleep
    assert len(attempts) == 4 and s.status["connections"] == 2 and s.status["gaps"] == 1 and s.status["errors"] >= 2
    assert len(sleeps) == 3 and sleeps[0] <= 1.5 * ls.BACKOFF_MIN  # first delay ~1 s (jittered 0.5-1.5x)
    assert s.book.seq == 20000, "the second connection resnapshotted"
    assert s.status["state"] == "stopped"
    print("test_run_reconnects_with_backoff ok")


def test_lock_serialises_normalize():
    import normalize

    order, real = [], normalize.refresh

    def slow(only):
        order.append(("start", threading.current_thread().name))
        time.sleep(0.05)
        order.append(("end", threading.current_thread().name))
        return {}
    normalize.refresh = slow
    try:
        ts = [threading.Thread(target=locks.refresh_provider, args=("lium",), name=f"t{i}") for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        normalize.refresh = real
    assert [k for k, _ in order] == ["start", "end", "start", "end"], order
    src = (ROOT / "poller.py").read_text()
    assert "refresh_provider, provider.name" in src, "the poller normalizes through the same lock"
    print("test_lock_serialises_normalize ok")


# --------------------------------------------------------------------------- database

DB = "og_test_live_lium"


def test_db_write_normalize_silence_and_reseed():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        import fetch
        fetch.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        b = fresh_book()
        out = ls.write([], b.payload(), {"seq": b.seq, "connection_id": "c1", "provider_ts": b.seen_at.isoformat()}, now - timedelta(minutes=2))
        assert out["snapshot"] and out["normalize"]["listings"] == len(SNAP["executors"])
        events = []
        for f in REST:
            events += b.apply(copy.deepcopy(f), now, "c1")[0]
        out = ls.write(events, b.payload(), {"seq": b.seq, "connection_id": "c1", "provider_ts": b.seen_at.isoformat()}, now)
        assert out["events"] == 4 and out["normalize"]["observations"] == 4, out  # 3 repriced + 1 new: change-only
        with Session() as s:
            price = {r.listing_id: float(r.price_per_gpu_hour) for r in s.execute(text(
                "SELECT listing_id, price_per_gpu_hour FROM compute_listings WHERE provider='lium'"))}
            assert price[CHANGED[0]] == next(f for f in REST if f.get("executor_id") == CHANGED[0])["executor"]["price_per_gpu"]
            rows = s.execute(text("SELECT method, request->>'source' AS src, ok FROM raw_snapshots WHERE provider='lium'")).all()
            assert {(r.method, r.src, r.ok) for r in rows} == {("WS", "websocket", True)}
            n_ev = s.execute(text("SELECT count(*) FROM stream_events")).scalar()
            assert n_ev == 4
        # the same book again: a raw row, no new observations (still change-only)
        out = ls.write([], b.payload(), {"seq": b.seq}, now + timedelta(minutes=5))
        assert out["normalize"]["observations"] == 0
        # the silence watch reads raw_snapshots.ok: the stream's snapshots count as Lium being fed
        from alerts import silence
        with Session() as s:
            st = s.execute(silence._STATE, {"p": "lium", "live_since": now - timedelta(hours=1)}).one()
        assert st.last_ok >= now and st.listings_now == len(b.kept)
        # restart: the book is reseeded from the newest stored snapshot and the next snapshot is diffed, not forgotten
        seeded = ls.seed_book()
        assert seeded.kept == b.kept and seeded.seeded
        ch = ls.latest_changes([CHANGED[0], NEW_ID])
        assert ch[CHANGED[0]]["basis"] == "stream" and ch[CHANGED[0]]["changed_fields"] == ["price_per_gpu"]
        assert ch[NEW_ID]["kind"] == "add" and ch[CHANGED[0]]["provider_ts"].tzinfo is not None
        # API (operator: no site password locally)
        config.settings.app_password = None
        import main
        from fastapi.testclient import TestClient
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        st = client.get("/v1/live/status").json()
        assert st["data"][0]["stream"] == "lium_ws" and "fallback" in st["data"][0]
        ev = client.get("/v1/live/events?limit=10").json()["data"]
        assert len(ev) == 4 and "raw" not in ev[0]
        assert "raw" in client.get("/v1/live/events?limit=1&raw=true").json()["data"][0]
        from api import pages
        assert "live" not in pages.PUBLIC_V1, "operator reads: never public"
    finally:
        scratchdb.drop(DB)
    print("test_db_write_normalize_silence_and_reseed ok")


if __name__ == "__main__":
    test_book_over_real_frames()
    test_unchanged_duplicates_gaps_and_deletes()
    test_resnapshot_diff()
    test_payload_is_the_pollers_shape()
    test_ingester_batches_and_never_writes_a_stale_book()
    test_consume_gap_and_stale()
    test_run_reconnects_with_backoff()
    test_lock_serialises_normalize()
    test_db_write_normalize_silence_and_reseed()
    print("all passed")
