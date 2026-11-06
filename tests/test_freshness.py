"""GET /v1/freshness: per provider and stream, cadence, age, p95 age over 24 h, detection lag (freshness.py).

Unit half: the per-minute age sampler. Database half (scratch database `og_test_freshness`, dropped after): poll
logs, listings, capacity, exact history rows and a throttled fetch written at known times, then the report and the
public endpoint. No network.

Run:  .venv/bin/python tests/test_freshness.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (first: blanks credentials + blocks the network)

import freshness  # noqa: E402

UTC = timezone.utc


def test_ages_sampler():
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    # polled every 10 min for the whole day, data as new as the poll: ages 0..9 min, evenly
    rows = [(now - timedelta(minutes=m), now - timedelta(minutes=m)) for m in range(24 * 60 + 10, -1, -10)]
    a = freshness.ages(rows, now)
    assert len(a) == 24 * 60 + 1 and max(a) == 540 and min(a) == 0
    assert 500 <= freshness._pct(a, 0.95) <= 540
    # a log that starts 2 h ago: only those minutes are sampled
    late = [(now - timedelta(hours=2), now - timedelta(hours=2, minutes=1))]
    a = freshness.ages(late, now)
    assert len(a) == 121 and a[0] == 60 and a[-1] == 2 * 3600 + 60
    assert freshness.ages([], now) == []


def test_report_and_endpoint():
    import fixtures
    import normalize
    from sqlalchemy import text
    from tables import ComputeListingRow, ListingObservation, RawSnapshot
    from store.capacity import CapacityCurrent
    from store.freshness import FreshnessLog

    db = "og_test_freshness"
    url = scratchdb.create(db)
    saved = normalize.SessionLocal
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        with Session() as s:
            now = s.execute(text("SELECT now()")).scalar().astimezone(UTC).replace(microsecond=0)
        with Session.begin() as s:
            for p in ("aws", "lium"):
                s.add(ComputeListingRow(provider=p, listing_id=f"{p}:1", sku="x", raw_gpu_name="H100",
                                        canonical_gpu_name=None, gpu_count=1, currency="USD",
                                        observed_at=now - timedelta(minutes=3), first_seen_at=now - timedelta(days=2)))
            s.add(CapacityCurrent(provider="lium", sku="x", region_key="", raw_gpu_name="H100", gpu_count=1,
                                  raw_value="1", source="s", observed_at=now - timedelta(minutes=20),
                                  first_seen_at=now, changed_at=now))
            s.add(RawSnapshot(provider="aws", endpoint="/spot", fetched_at=now - timedelta(minutes=3), ok=True,
                              status_code=200, payload={}))
            s.add(RawSnapshot(provider="aws", endpoint="/spot", fetched_at=now - timedelta(minutes=8), ok=False,
                              status_code=429, throttled=True, error="slow down"))
            # aws polled every 5 min all day (data 1 min behind the poll); lium every 15 min, capacity 2 h behind
            for m in range(0, 24 * 60 + 15, 5):
                t = now - timedelta(minutes=m)
                s.add(FreshnessLog(provider="aws", stream="price", logged_at=t, data_at=t - timedelta(minutes=1)))
                if m % 15 == 0:
                    s.add(FreshnessLog(provider="lium", stream="price", logged_at=t, data_at=t))
                    s.add(FreshnessLog(provider="lium", stream="capacity", logged_at=t, data_at=t - timedelta(hours=2)))
            # exact history rows: lags of 60, 120, 180 s; one outside its bracket and one poll_window are ignored
            for i, lag in enumerate((60, 120, 180)):
                at = now - timedelta(hours=i + 1)
                s.add(ListingObservation(provider="aws", listing_id="aws:1", observed_at=at,
                                         price_per_gpu_hour=Decimal(1 + i), prev_observed_at=at - timedelta(minutes=5),
                                         provider_changed_at=at - timedelta(seconds=lag), changed_at_basis="exact"))
            at = now - timedelta(hours=5)
            s.add(ListingObservation(provider="aws", listing_id="aws:1", observed_at=at, price_per_gpu_hour=Decimal(9),
                                     prev_observed_at=at - timedelta(minutes=5),
                                     provider_changed_at=at - timedelta(days=3), changed_at_basis="exact"))
            s.add(ListingObservation(provider="aws", listing_id="aws:1", observed_at=now - timedelta(hours=6),
                                     price_per_gpu_hour=Decimal(8), changed_at_basis="poll_window"))
        rows = {(r["provider"], r["stream"]): r for r in freshness.report.__wrapped__(now)}
        assert set(rows) == {("aws", "price"), ("lium", "price"), ("lium", "capacity")}, set(rows)
        aws = rows[("aws", "price")]
        assert aws["tier"] == "medium" and aws["target_interval_seconds"] == 3600 and aws["sla_seconds"] == 7200
        assert aws["age_seconds"] == 180 and aws["samples_24h"] == 1441
        assert 300 <= aws["age_p95_seconds_24h"] <= 360 and aws["status"] == "meeting"
        assert aws["provider_timestamps"] == "exact" and aws["detection_lag_changes_7d"] == 3
        assert aws["detection_lag_p50_seconds_7d"] == 120.0
        assert aws["throttled_24h"] == 1
        assert aws["last_successful_fetch_at"] == now - timedelta(minutes=3)
        cap = rows[("lium", "capacity")]
        assert cap["target_interval_seconds"] == 900 and cap["last_refresh_at"] == now - timedelta(minutes=20)
        assert cap["age_p95_seconds_24h"] >= 7200 and cap["status"] == "breaching", cap
        assert rows[("lium", "price")]["provider_timestamps"] == "none"
        assert rows[("lium", "price")]["detection_lag_p50_seconds_7d"] is None

        # record(): one row per stream that has data, and old rows pruned
        with Session.begin() as s:
            s.add(FreshnessLog(provider="lium", stream="price", logged_at=now - timedelta(days=9), data_at=now))
        assert freshness.record("lium", now) == 2
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM freshness_log WHERE provider = 'lium' "
                                  "AND logged_at < now() - interval '8 days'")).scalar() == 0

        # the endpoint: public like /v1/capacity, filters, envelope
        from fastapi.testclient import TestClient
        from starlette.requests import Request
        import main
        from api import pages

        req = Request({"type": "http", "method": "GET", "path": "/v1/reliability/providers", "headers": [], "query_string": b""})
        assert pages.is_public(req)
        import config
        config.settings.app_password = None
        freshness.report.cache_clear()
        c = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        if True:
            # freshness is part of each provider's reliability row (GET /v1/reliability/providers)
            body = c.get("/v1/reliability/providers").json()
            assert body["meta"]["kind"] == "observed" and "p95 age" in body["meta"]["sla"]
            got = {(r["provider"], f["stream"]) for r in body["data"] for f in r["freshness"]}
            assert got >= {("aws", "price"), ("lium", "capacity")}
            assert all(f["source"] == "opengrid_feeds" for r in body["data"] for f in r["freshness"])
            lium = c.get("/v1/reliability/providers/lium").json()["data"]
            assert {f["stream"] for f in lium["freshness"]} >= {"capacity"}
            assert c.get("/v1/reliability/providers/nobody").status_code == 404
            assert c.get("/methodology/freshness").status_code == 200
    finally:
        normalize.SessionLocal = saved
        scratchdb.drop(db)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(name, "ok")
    print("all passed")
