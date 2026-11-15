"""Lium market-data feeds (lium_data/; store/lium.py; methodology/lium-data.md; migration 0040_lium_data).

Fixtures (tests/fixtures_lium/):
    REAL, recorded 2026-11-15 from Lium's keyless feeds: launch_stats, requests_404 (the feature is off),
    executors_stats, spot_idle_rates, machines_capacity, shared_config, stats.
    SYNTHETIC (file names say so): every data-partner CSV and the 403 body, built from the columns Lium's OpenAPI
    documents (trades: hashed renter / provider, price per GPU-hour, GPU count, start, end, GPU model, provider
    country; hourly-gpu: its five named columns; daily-aggregate: deals, GPU-hours, VWAP, low, high), because
    OpenGrid has no data-partner key yet; and requests_SYNTHETIC (the schema of /public/v1/requests when it is on).

Covers parsing (alias spellings, rejected rows, stable row keys), raw-first change-only storage (an unchanged
answer and a repeated 403 / 404 are one row), the partner poller (off without a key, 403 reported once and
re-checked every 6 h, incremental from/to, dumps, idempotent re-reads), the dump backfill, Lium's published
throughput (revisions), trade VWAP by hand, the internal tier on every row and every admin endpoint (403 for a
data:read viewer), and migration 0040 up / down. No network: every HTTP call goes to an httpx.MockTransport.

Run:  .venv/bin/python tests/test_lium_data.py
"""

import asyncio
import base64
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import scratchdb  # noqa: E402  (isolates the process from real providers and the network)
import fixtures  # noqa: E402
import normalize  # noqa: E402
import provider_meta  # noqa: E402
from config import settings  # noqa: E402
from lium_data import feeds as F  # noqa: E402
from lium_data import partner, public, raw, reads  # noqa: E402
from sqlalchemy import func, select, text, update  # noqa: E402
from store.lium import (LiumDailyAggregate, LiumFeedSnapshot, LiumFeedValue, LiumHourlyGpu,  # noqa: E402
                        LiumModelStatsDaily, LiumThroughputDaily, LiumTrade)

FIX = Path(__file__).resolve().parent / "fixtures_lium"
DB = "og_test_lium_data"
UTC = timezone.utc
KEY = "test-partner-key"


def fx(name: str) -> str:
    return (FIX / name).read_text()


def setup():
    url = scratchdb.create(DB)
    normalize.SessionLocal = fixtures.session(url)


def wipe():
    with normalize.SessionLocal.begin() as s:
        s.execute(text("TRUNCATE lium_feed_values, lium_trades, lium_hourly_gpu, lium_daily_aggregate, "
                       "lium_throughput_daily, lium_model_stats_daily, lium_feed_snapshots, raw_snapshots, "
                       "raw_payloads RESTART IDENTITY CASCADE"))


def count(model, *where):
    with normalize.SessionLocal() as s:
        return s.execute(select(func.count()).select_from(model).where(*where)).scalar()


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def __enter__(self):
        for n in ("lium_data.raw", "lium_data.partner", "lium_data.public"):
            logging.getLogger(n).addHandler(self)
            logging.getLogger(n).setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        for n in ("lium_data.raw", "lium_data.partner", "lium_data.public"):
            logging.getLogger(n).removeHandler(self)

    def emit(self, record):
        self.records.append(record)

    def warnings(self):
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]


class Server:
    """A mock lium.io: path (+ sorted query) -> (status, body). Records every request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        q = "&".join(f"{k}={v}" for k, v in sorted(request.url.params.items()))
        key = request.url.path + (f"?{q}" if q else "")
        hit = self.routes.get(key, self.routes.get(request.url.path))
        if callable(hit):
            hit = hit(request)
        status, body = hit if hit else (404, fx("requests_404_2026-11-15.json"))
        ctype = "text/csv" if body and not body.lstrip().startswith(("{", "[")) else "application/json"
        return httpx.Response(status, text=body, headers={"content-type": ctype})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def paths(self):
        return [c.url.path for c in self.calls]


def public_routes(**over):
    r = {"/api/public/v1/launch-stats": (200, fx("launch_stats_2026-11-15.json")),
         "/api/public/v1/requests": (404, fx("requests_404_2026-11-15.json")),
         "/api/executors/stats": (200, fx("executors_stats_2026-11-15.json")),
         "/api/machines/spot-idle-rates": (200, fx("spot_idle_rates_2026-11-15.json")),
         "/api/machines/capacity": (200, fx("machines_capacity_2026-11-15.json")),
         "/api/v1/shared-config": (200, fx("shared_config_2026-11-15.json"))}
    r.update(over)
    return r


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------ parsing


def test_parse_trades():
    rows, rejected = partner.parse_trades(fx("trades_SYNTHETIC_2026-11-13.csv"))
    assert len(rows) == 4 and rejected == 1, (len(rows), rejected)   # syn-0005 has no price
    a = rows[0]
    assert a["trade_id"] == "syn-0001" and a["renter_hash"] == "r_9f2c1a" and a["provider_hash"] == "p_77ab01"
    assert a["gpu_count"] == 8 and a["price_per_gpu_hour"] == D("2.100000") and a["gpu_hours"] == D("32.000000")
    assert a["start_at"] == datetime(2026, 11, 12, 22, tzinfo=UTC) and a["provider_country"] == "US"
    assert a["currency"] == "USD" and a["extra"] is None
    # the same trade in another file has the same key; different trades differ
    again, _ = partner.parse_trades(fx("trades_SYNTHETIC_window.csv"))
    assert again[0]["row_key"] == rows[3]["row_key"] and len({r["row_key"] for r in rows}) == 4
    # another header spelling, no trade id, naive times (UTC), an unknown column kept verbatim
    other, rej = partner.parse_trades(fx("trades_SYNTHETIC_other_spelling.csv"))
    o = other[0]
    assert rej == 0 and o["trade_id"] is None and o["gpu_model"] == "NVIDIA A100-SXM4-80GB" and o["gpu_count"] == 4
    assert o["price_per_gpu_hour"] == D("1.5") and o["gpu_hours"] == D("8") and o["provider_country"] == "CA"
    assert o["end_at"] == datetime(2026, 11, 10, 10, tzinfo=UTC) and o["extra"] == {"venue": "lium"}
    assert o["row_key"] == partner.parse_trades(fx("trades_SYNTHETIC_other_spelling.csv"))[0][0]["row_key"]
    assert partner.parse_trades("trade_id,price\n") == ([], 0)
    assert partner.parse_time("1762992000") == datetime(2025, 11, 13, tzinfo=UTC)
    assert partner.parse_time("1762992000000") == datetime(2025, 11, 13, tzinfo=UTC)


def test_parse_aggregates():
    h, rej = partner.parse_hourly(fx("hourly_gpu_SYNTHETIC.csv"))
    assert rej == 0 and len(h) == 3 and h[0]["hour_utc"] == datetime(2026, 11, 13, 10, tzinfo=UTC)
    assert h[1]["rentals"] == 30 and h[1]["gpu_hours"] == D("190.25") and h[1]["mean_price_per_gpu_hour_usd"] == D("2.1504")
    a, rej = partner.parse_daily_aggregate(fx("daily_aggregate_SYNTHETIC_2026-11-13.csv"))
    assert rej == 0 and len(a) == 3
    assert a[0] == {"day": date(2026, 11, 13), "gpu_model": "NVIDIA H100 80GB HBM3", "country": "US", "deals": 2,
                    "gpu_hours": D("34"), "vwap": D("2.111765"), "low": D("2.10"), "high": D("2.30"), "extra": None}, a[0]


def test_parse_public_feeds():
    gen, v = public.parse_launch_stats(json.loads(fx("launch_stats_2026-11-15.json")))
    assert gen == datetime(2026, 11, 15, 0, 3, 56, 812442, tzinfo=UTC)
    got = {(x["subject"], x["gpu_count"], x["metric"]): x["value"] for x in v}
    assert got[("NVIDIA GeForce RTX 5090", 1, "success_rate")] == D("0.9829")
    assert got[("NVIDIA H100 80GB HBM3", 8, "time_to_ssh_p90_seconds")] == D("185.1")
    assert got[("*", 0, "window_days")] == 14 and got[("*", 0, "min_providers")] == 3
    _, v = public.parse_executor_stats(json.loads(fx("executors_stats_2026-11-15.json")))
    got = {(x["subject"], x["metric"]): x["value"] for x in v}
    assert got[("NVIDIA L40S", "rented_count")] == 57 and got[("NVIDIA L40S", "all_count")] == 67
    _, v = public.parse_spot_idle_rates(json.loads(fx("spot_idle_rates_2026-11-15.json")))
    got = {(x["subject"], x["gpu_count"]): x["value"] for x in v}
    assert got[("H100", 8)] == D("0.81") and got[("B300", 1)] == D("3.1")
    _, v = public.parse_idle_capacity(json.loads(fx("machines_capacity_2026-11-15.json")))
    got = {(x["subject"], x["gpu_count"], x["metric"]): x for x in v}
    assert got[("B300", 8, "max_cap")]["value"] == 32 and got[("B300", 1, "emission_capped")]["value"] == 0
    assert got[("NVIDIA H200 NVL", 0, "base_model")]["value_text"] == "H200"
    _, v = public.parse_shared_config(json.loads(fx("shared_config_2026-11-15.json")))
    got = {(x["subject"], x["metric"]): x["value"] for x in v}
    assert got[("NVIDIA H200", "reference_price")] == D("3.65") and got[("NVIDIA B200", "reference_price_p90")] == D("6.2")
    assert got[("*", "rental_fees_rate")] == D("0.95")
    gen, v = public.parse_requests(json.loads(fx("requests_SYNTHETIC.json")))
    got = {(x["subject"], x["metric"]): x for x in v}
    assert got[("B300", "gpus_asked")]["value"] == 24 and got[("B300", "oldest_at")]["value_time"] == \
        datetime(2026, 11, 12, 8, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ public feeds


def test_public_poll_change_only():
    wipe()
    srv = Server(public_routes())
    with Logs() as logs:
        out = run(public.poll(srv.client(), gap=0))
    states = {r["feed"]: r["state"] for r in out["feeds"]}
    assert states == {"launch_stats": "ok", "requests": "not_found", "executor_stats": "ok", "spot_idle_rates": "ok",
                      "idle_capacity": "ok", "shared_config": "ok"}, states
    assert not logs.warnings(), logs.warnings()           # a switched-off public feed is not a warning
    assert count(LiumFeedSnapshot) == 6
    n_values = count(LiumFeedValue)
    assert n_values > 100
    # shared-config keeps only the price keys (no chain or driver configuration)
    with normalize.SessionLocal() as s:
        body = s.execute(select(LiumFeedSnapshot.body).where(LiumFeedSnapshot.feed == "shared_config")).scalar()
        vis = set(s.execute(select(LiumFeedSnapshot.visibility)).scalars()) | \
            set(s.execute(select(LiumFeedValue.visibility)).scalars())
    assert "collateral_contract_address" not in body and set(json.loads(body)) == set(F.FEEDS["shared_config"].keep)
    assert vis == {"internal"}
    # the same answers again, launch-stats recomputed (new generated_at) but unchanged: nothing new is stored
    doc = json.loads(fx("launch_stats_2026-11-15.json"))
    doc["generated_at"] = "2026-11-15T00:18:56Z"
    srv.routes["/api/public/v1/launch-stats"] = (200, json.dumps(doc))
    with Logs() as logs:
        out = run(public.poll(srv.client(), gap=0))
    assert not any(r["changed"] for r in out["feeds"]) and not logs.warnings()
    assert count(LiumFeedSnapshot) == 6 and count(LiumFeedValue) == n_values
    with normalize.SessionLocal() as s:
        seen = dict(s.execute(select(LiumFeedSnapshot.feed, LiumFeedSnapshot.seen_count)).all())
    assert seen == {f.name: 2 for f in F.PUBLIC_FEEDS}, seen
    # a real change: one success rate moves -> one new launch-stats answer with all its values
    doc["gpus"][0]["success_rate"] = 0.95
    srv.routes["/api/public/v1/launch-stats"] = (200, json.dumps(doc))
    # and Lium switches the demand feed on
    srv.routes["/api/public/v1/requests"] = (200, fx("requests_SYNTHETIC.json"))
    out = run(public.poll(srv.client(), gap=0))
    assert count(LiumFeedSnapshot, LiumFeedSnapshot.feed == "launch_stats") == 2
    written = {r["feed"]: r["values_written"] for r in out["feeds"]}
    assert written["launch_stats"] == 1, written            # values are change-only too
    cur = reads.current("launch_stats")
    row = next(r for r in cur["rows"] if r["subject"] == "NVIDIA GeForce RTX 5090" and r["gpu_count"] == 1)
    assert row["success_rate"] == D("0.95") and row["canonical_gpu_name"] and cur["feed_wide"]["window_days"] == 14
    assert cur["kind"] == "observed"
    hist = reads.history("launch_stats", "NVIDIA GeForce RTX 5090", "success_rate", 1)
    assert [h["value"] for h in hist] == [D("0.95"), D("0.9829")]
    req = reads.current("requests")
    assert req["state"] == "ok" and {r["subject"] for r in req["rows"]} == {"B300", "H200"}
    # a GPU type drops out of launch-stats: written once as absent, gone from the current view, kept in history
    gone = doc["gpus"].pop()
    srv.routes["/api/public/v1/launch-stats"] = (200, json.dumps(doc))
    out = run(public.poll(srv.client(), gap=0))
    assert {r["feed"]: r["values_written"] for r in out["feeds"]}["launch_stats"] == 3     # its three metrics
    cur = reads.current("launch_stats")
    assert (gone["gpu_type"], gone["gpu_count"]) not in {(r["subject"], r["gpu_count"]) for r in cur["rows"]}
    h = reads.history("launch_stats", gone["gpu_type"], "success_rate", gone["gpu_count"])
    assert [x["present"] for x in h] == [False, True], h
    # a transient error is stored (once) and does not touch the stored values
    srv.routes["/api/executors/stats"] = (500, '{"error":{"code":"internal"}}')
    with Logs() as logs:
        run(public.poll(srv.client(), gap=0))
        run(public.poll(srv.client(), gap=0))
    assert len([w for w in logs.warnings() if "executor_stats" in w]) == 1, logs.warnings()
    assert count(LiumFeedSnapshot, LiumFeedSnapshot.feed == "executor_stats") == 2
    assert reads.current("executor_stats")["state"] == "error" and reads.current("executor_stats")["rows"]


def _stats_snapshot(payload, at):
    from tables import RawSnapshot

    with normalize.SessionLocal.begin() as s:
        snap = RawSnapshot(provider="lium", endpoint="/api/public/v1/stats", fetched_at=at, ok=True, status_code=200,
                           payload=payload)
        s.add(snap)
        s.flush()
        return snap.id


def test_published_throughput():
    wipe()
    doc = json.loads(fx("stats_2026-11-15.json"))
    doc.pop("generated_at")                  # providers/lium.py stores it without generated_at
    at = datetime(2026, 11, 15, 0, 20, tzinfo=UTC)
    _stats_snapshot(doc, at)
    out = public.parse_stats()
    assert out["days"] == 31 and out["revised"] == 0 and out["models"] == len(doc["models"]), out
    with normalize.SessionLocal() as s:
        d = s.get(LiumThroughputDaily, date(2026, 10, 16))
        today = s.get(LiumThroughputDaily, date(2026, 11, 15))
        m = s.get(LiumModelStatsDaily, (date(2026, 11, 15), "RTX 5090"))
    assert d.gmv_usd == D("81700.00") and d.gpu_hours == D("39064") and d.rentals == 940 and d.visibility == "internal"
    assert today.gmv_usd is None and today.utilisation == D("1") and today.utilisation_partial is True
    assert m.median_price_30d == D("0.5") and m.gpus_listed == 467 and m.visibility == "internal"
    # parsing again changes nothing; Lium revising a closed day counts once
    assert public.parse_stats()["revised"] == 0
    doc["throughput"]["daily"][0]["gmv_usd"] = 81750.0
    _stats_snapshot(doc, at + timedelta(minutes=15))
    assert public.parse_stats()["revised"] == 1
    with normalize.SessionLocal() as s:
        d = s.get(LiumThroughputDaily, date(2026, 10, 16))
    assert d.gmv_usd == D("81750.00") and d.revisions == 1
    t = reads.throughput(days=60)
    assert t["kind"] == "transaction" and t["basis"] == "published_aggregate" and t["models_day"] == date(2026, 11, 15)
    assert public.reparse_stats() == {"snapshots": 2}


# ------------------------------------------------------------------------------------------------ partner feeds


def test_partner_off_without_key():
    wipe()
    saved = settings.lium_partner_api_key
    try:
        settings.lium_partner_api_key = None
        assert run(partner.poll(Server({}).client())) == {"state": "off", "reason": "LIUM_PARTNER_API_KEY is not set"}
        assert run(partner.backfill())["state"] == "off"
        import jobs
        from lium_data import schedule
        schedule._registered, saved_poll = False, settings.poller_enabled
        settings.poller_enabled = True
        jobs.JOBS.pop("lium_partner", None)
        assert schedule.register() == ["lium_public"] and "lium_partner" not in jobs.JOBS
        schedule._registered = False
        settings.lium_partner_api_key = KEY
        assert schedule.register() == ["lium_public", "lium_partner"] and "lium_partner" in jobs.JOBS
        settings.poller_enabled = saved_poll
        jobs.JOBS.pop("lium_partner", None)
        jobs.JOBS.pop("lium_public", None)
    finally:
        settings.lium_partner_api_key = saved
    assert count(LiumFeedSnapshot) == 0


def test_partner_forbidden_reported_once():
    wipe()
    srv = Server({p: (403, fx("forbidden_SYNTHETIC.json")) for p in (
        "/api/data-partners/trades.csv", "/api/data-partners/hourly-gpu.csv", "/api/data-partners/daily-aggregate.csv")})
    with Logs() as logs:
        out = run(partner.poll(srv.client(), key=KEY))
    assert out["state"] == "forbidden" and len(srv.calls) == 1, (out, srv.paths())   # stops at the first 403
    assert srv.calls[0].headers["x-api-key"] == KEY
    w = logs.warnings()
    assert len(w) == 2 and any("not (yet) flagged as a data partner" in x for x in w), w
    assert KEY not in " ".join(w)
    # within 6 h: no request at all
    with Logs() as logs:
        out = run(partner.poll(srv.client(), key=KEY))
    assert out["skipped"] and len(srv.calls) == 1 and not logs.warnings()
    # after 6 h: one re-check, still 403, still one stored row, no new warning
    with normalize.SessionLocal.begin() as s:
        s.execute(update(LiumFeedSnapshot).values(last_seen_at=LiumFeedSnapshot.last_seen_at - timedelta(hours=7)))
    with Logs() as logs:
        out = run(partner.poll(srv.client(), key=KEY))
    assert out["state"] == "forbidden" and len(srv.calls) == 2 and not logs.warnings(), logs.warnings()
    with normalize.SessionLocal() as s:
        rows = s.execute(select(LiumFeedSnapshot)).scalars().all()
    assert len(rows) == 1 and rows[0].seen_count == 2 and rows[0].state == "forbidden" and not rows[0].ok
    assert "forbidden" in rows[0].error and KEY not in (rows[0].body or "") + rows[0].url + json.dumps(rows[0].params)
    st = reads.status()
    trades = next(f for f in st["feeds"] if f["feed"] == "trades")
    assert trades["state"] == "forbidden" and trades["status_code"] == 403


def _partner_server(today: date):
    y = (today - timedelta(days=1)).isoformat()
    return Server({
        "/api/data-partners/trades.csv": (200, fx("trades_SYNTHETIC_window.csv")),
        f"/api/data-partners/dumps/{y}.csv": (200, fx("trades_SYNTHETIC_2026-11-13.csv")),
        f"/api/data-partners/dumps/{y}.daily-aggregate.csv": (200, fx("daily_aggregate_SYNTHETIC_2026-11-13.csv")),
        "/api/data-partners/hourly-gpu.csv": (200, fx("hourly_gpu_SYNTHETIC.csv")),
        "/api/data-partners/daily-aggregate.csv": (200, fx("daily_aggregate_SYNTHETIC_2026-11-13.csv")),
    })


def test_partner_poll_incremental_and_idempotent():
    wipe()
    today = datetime.now(UTC).date()
    srv = _partner_server(today)
    out = run(partner.poll(srv.client(), key=KEY))
    assert out["state"] == "ok", out
    first = srv.calls[0]
    assert first.url.params["from"] == (today - timedelta(days=1)).isoformat() and first.url.params["to"] == today.isoformat()
    assert all(c.headers["x-api-key"] == KEY for c in srv.calls)
    # window (syn-0004, syn-0006) + yesterday's dump (syn-0001..0004) = 5 distinct trades
    assert count(LiumTrade) == 5 and count(LiumHourlyGpu) == 3 and count(LiumDailyAggregate) == 3
    hourly = next(c for c in srv.calls if c.url.path.endswith("hourly-gpu.csv"))
    assert hourly.url.params["hours"] == "48"       # first run
    with normalize.SessionLocal() as s:
        vis = set(s.execute(select(LiumTrade.visibility)).scalars())
        dump = s.execute(select(LiumFeedSnapshot).where(LiumFeedSnapshot.feed == "trades_dump")).scalar()
    assert vis == {"internal"} and dump.rows == 4 and dump.rejected == 1 and dump.body == fx("trades_SYNTHETIC_2026-11-13.csv")
    # second run: resumes from the newest trade's end day, does not fetch yesterday's dump again, stores nothing new
    srv.calls.clear()
    run(partner.poll(srv.client(), key=KEY))
    resume = max(date(2026, 11, 14), today - timedelta(days=partner.MAX_WINDOW_DAYS - 1))  # syn-0006 ended 11-14
    assert srv.calls[0].url.params["from"] == resume.isoformat(), srv.calls[0].url
    assert not any("/dumps/" in p for p in srv.paths()), srv.paths()
    with normalize.SessionLocal() as s:
        seen = s.execute(select(func.sum(LiumFeedSnapshot.seen_count)).where(LiumFeedSnapshot.feed == "trades")).scalar()
    assert count(LiumTrade) == 5 and seen == 2   # a new row only when the window (params) moved
    # Lium revises an hourly row: updated in place
    srv.routes["/api/data-partners/hourly-gpu.csv"] = (200, fx("hourly_gpu_SYNTHETIC.csv").replace("3.4011", "3.5000"))
    run(partner.poll(srv.client(), key=KEY))
    with normalize.SessionLocal() as s:
        r = s.get(LiumHourlyGpu, (datetime(2026, 11, 13, 11, tzinfo=UTC), "NVIDIA H200"))
    assert r.mean_price_per_gpu_hour_usd == D("3.5") and count(LiumHourlyGpu) == 3
    partner.reparse()        # oldest body first: every row ends at its newest value, nothing duplicates
    with normalize.SessionLocal() as s:
        r = s.get(LiumHourlyGpu, (datetime(2026, 11, 13, 11, tzinfo=UTC), "NVIDIA H200"))
    assert r.mean_price_per_gpu_hour_usd == D("3.5") and count(LiumTrade) == 5 and count(LiumHourlyGpu) == 3


def test_trade_stats_by_hand():
    # after test_partner_poll_incremental_and_idempotent: syn-0001..0006 without 0005
    rows = partner.trade_stats(days=3650, until=date(2026, 11, 14))
    by = {(r["day"], r["gpu_model"]): r for r in rows}
    h100 = by[(date(2026, 11, 13), "NVIDIA H100 80GB HBM3")]
    # 8 GPUs x 4 h at 2.10 + 1 GPU x 2 h at 2.30: (67.2 + 4.6) / 34
    assert h100["rentals"] == 2 and h100["gpu_hours"] == D("34.000") and h100["vwap"] == D("2.1118"), h100
    assert h100["low"] == D("2.1") and h100["high"] == D("2.3") and h100["notional_usd"] == D("71.80")
    assert h100["kind"] == "transaction" and h100["basis"] == "derived_from_trades"
    assert by[(date(2026, 11, 13), "NVIDIA H200")]["vwap"] == D("3.4000")
    assert by[(date(2026, 11, 14), "NVIDIA H100 80GB HBM3")]["gpu_hours"] == D("6.000")
    assert [r["gpu_model"] for r in partner.trade_stats(3650, gpu="NVIDIA H200", until=date(2026, 11, 14))] == ["NVIDIA H200"]
    assert partner.trade_stats(3650, country="DE", until=date(2026, 11, 14))[0]["gpu_model"] == "NVIDIA GeForce RTX 4090"
    check = {(r["day"], r["gpu_model"]): r for r in reads.trade_stats_vs_published(3650)}
    c = check.get((date(2026, 11, 13), "NVIDIA H100 80GB HBM3"))
    if c:   # only when the fixture's day is inside the window (it is dated 2026-11-13)
        assert c["vwap_from_trades"] == D("2.1118") and c["vwap_published"] is not None


def test_backfill_walks_dumps():
    wipe()
    yday = datetime.now(UTC).date() - timedelta(days=1)
    d = [yday - timedelta(days=i) for i in range(8)]
    header = "trade_id,renter_id,provider_id,price_per_gpu_hour,gpu_count,start,end,gpu_model,provider_country\n"
    routes = {
        f"/api/data-partners/dumps/{d[0]}.csv": (200, fx("trades_SYNTHETIC_2026-11-13.csv")),
        f"/api/data-partners/dumps/{d[1]}.csv": (404, '{"error":{"code":"not_found"}}'),   # missed nightly task
        f"/api/data-partners/trades.csv?day={d[1]}": (200, fx("trades_SYNTHETIC_window.csv")),
        f"/api/data-partners/dumps/{d[2]}.csv": (200, fx("trades_SYNTHETIC_other_spelling.csv")),
        # d[3..] : no dumps, empty day reads -> 3 empty days stop the walk
        "/api/data-partners/trades.csv": (200, header),
        "/api/data-partners/daily-aggregate.csv": (200, "day,gpu_model,provider_country,deals,gpu_hours,vwap\n"),
    }
    srv = Server(routes)
    out = run(partner.backfill(http=srv.client(), key=KEY, gap=0))
    assert out["state"] == "ok" and out["days"] == 6 and out["last_day"] == d[0].isoformat(), out
    assert out["first_day"] == d[5].isoformat() and count(LiumTrade) == 4 + 1 + 1, count(LiumTrade)
    # resumable: a second run with --since skips stored days (no dump request for d[0..2])
    srv.calls.clear()
    out = run(partner.backfill(since=d[2], http=srv.client(), key=KEY, gap=0))
    assert not any(f"dumps/{x}.csv" in p for x in d[:3] for p in srv.paths()), srv.paths()
    # a 403 stops the walk at once
    srv2 = Server({f"/api/data-partners/dumps/{d[0]}.csv": (403, fx("forbidden_SYNTHETIC.json"))})
    out = run(partner.backfill(http=srv2.client(), key=KEY, gap=0, force=True))
    assert out["state"] == "forbidden" and len(srv2.calls) == 1


# ------------------------------------------------------------------------------------------------ tier, API, schema


def test_registry_internal():
    for f in F.FEEDS.values():
        assert provider_meta.visibility("lium", f.path, "signals") == "internal", f.name
        assert raw.visibility(f) == "internal"
    assert provider_meta.visibility("lium", "/api/public/v1/stats", "signals") == "internal"
    # Lium's prices and capacity stay public
    from providers import lium
    assert provider_meta.visibility("lium", lium.ENDPOINT) == "public"
    assert provider_meta.visibility("lium", lium.ENDPOINT, "capacity") == "public"
    assert provider_meta.is_public_provider("lium")


def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_admin_endpoints():
    from fastapi.testclient import TestClient

    import main
    from accounts import security

    client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
    saved = (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
             settings.environment)
    try:
        security.reset()
        settings.app_password, settings.environment = "pw", "development"
        settings.viewer_user, settings.viewer_password = "vc", "look-only"
        settings.public_pages = True
        op, viewer = _basic("opengrid", "pw"), _basic("vc", "look-only")
        paths = ["/v1/ops/lium/status", "/v1/ops/lium/trades/stats?days=730", "/v1/ops/lium/trades/check",
                 "/v1/ops/lium/hourly?hours=100", "/v1/ops/lium/daily-aggregate", "/v1/ops/lium/throughput",
                 "/v1/ops/lium/feeds/launch_stats"]
        for p in paths:
            assert client.get(p).status_code == 401, p
            assert client.get(p, headers=viewer).status_code == 403, p
        r = client.get("/v1/ops/lium/status", headers=op)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meta"]["visibility"] == "internal" and body["meta"]["kind"] == "observed", body["meta"]
        assert {f["feed"] for f in body["data"]["feeds"]} == set(F.FEEDS)
        r = client.get("/v1/ops/lium/trades/stats?days=730", headers=op)
        assert r.status_code == 200 and r.json()["data"] and "transaction" in r.text, r.text
        assert client.get("/v1/ops/lium/hourly?hours=2160", headers=op).status_code == 200
        assert client.get("/v1/ops/lium/feeds/trades", headers=op).status_code == 404
        assert client.get("/v1/ops/lium/feeds/nope", headers=op).status_code == 404
        from api import pages
        assert "ops" not in pages.PUBLIC_V1   # anonymous visitors never reach /v1/ops (401 above, PUBLIC_PAGES on)
    finally:
        (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
         settings.environment) = saved


def test_migration_0040():
    from alembic import command
    from sqlalchemy import create_engine

    import db as dbmod
    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        command.upgrade(cfg, "0040_lium_data")
        eng = create_engine(url)
        with eng.connect() as c:
            for t in ("lium_feed_snapshots", "lium_trades", "lium_hourly_gpu", "lium_daily_aggregate",
                      "lium_feed_values", "lium_throughput_daily", "lium_model_stats_daily"):
                assert c.execute(text(f"SELECT to_regclass('public.{t}')")).scalar() is not None, t
                d = c.execute(text("SELECT column_default FROM information_schema.columns WHERE table_name = :t "
                                   "AND column_name = 'visibility'"), {"t": t}).scalar()
                assert "internal" in d, (t, d)
            # the database refuses any other tier
            try:
                c.execute(text("INSERT INTO lium_throughput_daily (day, first_seen_at, updated_at, visibility) "
                               "VALUES ('2026-01-01', now(), now(), 'secret')"))
                raise AssertionError("CHECK did not fire")
            except Exception as exc:  # noqa: BLE001
                assert "ck_lium_throughput_daily_visibility" in str(exc), exc
                c.rollback()
            # ORM and migration agree on the columns
            from store import lium as L
            for model in (L.LiumFeedSnapshot, L.LiumTrade, L.LiumHourlyGpu, L.LiumDailyAggregate, L.LiumFeedValue,
                          L.LiumThroughputDaily, L.LiumModelStatsDaily):
                cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns "
                                                     "WHERE table_name = :t"), {"t": model.__tablename__})}
                assert cols == {col.name for col in model.__table__.columns}, model.__tablename__
        command.downgrade(cfg, "0039_monthly_reports")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.lium_trades')")).scalar() is None
            assert c.execute(text("SELECT to_regclass('public.monthly_reports')")).scalar() is not None
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)


TESTS = [test_parse_trades, test_parse_aggregates, test_parse_public_feeds, test_public_poll_change_only,
         test_published_throughput, test_partner_off_without_key, test_partner_forbidden_reported_once,
         test_partner_poll_incremental_and_idempotent, test_trade_stats_by_hand, test_backfill_walks_dumps,
         test_registry_internal, test_admin_endpoints, test_migration_0040]


def main():
    setup()
    failed = 0
    try:
        for fn in TESTS:
            try:
                fn()
                print(f"ok   {fn.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                import traceback
                print(f"FAIL {fn.__name__}")
                traceback.print_exc()
    finally:
        scratchdb.drop(DB)
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
