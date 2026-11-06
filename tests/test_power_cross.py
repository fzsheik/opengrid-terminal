"""EIA grid data, EIA natural gas and the power x GPU cross-analysis (power/sources/eia.py, eia_gas.py, power/geo.py,
power/cross.py, api/power.py): parsing over REAL saved EIA answers (hour-ending UTC periods, paging, units, withheld
months), the region -> price-series mapping (documented places only), power cost per GPU-hour, the spread, lagged
correlations (a designed 2-day lag is found; gas as a slow input), the shifts hook (read-time only: market_shift_items
untouched), and visibility: EIA-930 public, gas internal by choice, every derived row internal; the public
/v1/power/series never shows an ISO or gas value. No network: httpx.MockTransport.

Fixtures (real EIA API v2 answers, captured 2026-11-05; the api_key is not in them):
    fixtures_power/eia_region_ciso_erco_2025-11-02.json   region-data CISO + ERCO, UTC day 2025-11-02 (168 rows)
    fixtures_power/eia_fuel_ciso_2025-11-02.json          fuel-type-data CISO, same day (192 rows)
    fixtures_power/gas_daily_2024-04-01_12.json           pri/fut RNGWHHD + RNGC1-4 (futures end 2024-04-05)
    fixtures_power/gas_storage_2025-10.json               stor/wkly Lower 48 + East, October 2025
    fixtures_power/gas_price_2025-q1.json                 pri/sum PEU, U.S. + TX + AR (AR withheld), 2025-01..03

Run:  .venv/bin/python tests/test_power_cross.py
"""

import asyncio
import base64
import dataclasses
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402
import httpx  # noqa: E402

from power import cross, geo, registry, runner, storage  # noqa: E402
from power.registry import Fetched  # noqa: E402
from power.sources import caiso, eia, eia_gas  # noqa: E402

FX = ROOT / "tests" / "fixtures_power"
DB = "og_test_power_cross"
UTC = timezone.utc
H100 = "NVIDIA H100 80GB SXM5"


def _f(name) -> Fetched:
    return Fetched("u", 200, 1, (FX / name).read_bytes())


def test_parse_eia():
    reg, fuel = eia.DATASETS
    f = _f("eia_region_ciso_erco_2025-11-02.json")
    pts = reg.parse(f)
    days = storage.to_days(reg, pts, date(2025, 11, 2), date(2025, 11, 2))
    d = days[("eia.actual.demand.ciso", date(2025, 11, 2))]
    assert len(d) == 24 and None not in d
    raw = {(r["respondent"], r["type"], r["period"]): float(r["value"]) for r in json.loads(f.body)["response"]["data"]
           if r["value"] is not None}
    # hour ending: period T01 is the 00:00-01:00 UTC hour (slot 0); T00 of the next day is slot 23
    assert d[0] == raw[("CISO", "D", "2025-11-02T01")] and d[23] == raw[("CISO", "D", "2025-11-03T00")]
    assert ("eia.forecast.demand_forecast.erco", date(2025, 11, 2)) in days
    assert {k[0] for k in days} <= {s.key for s in reg.series}
    fd = storage.to_days(fuel, fuel.parse(_f("eia_fuel_ciso_2025-11-02.json")), date(2025, 11, 2), date(2025, 11, 2))
    assert ("eia.actual.net_generation_sun.ciso", date(2025, 11, 2)) in fd and len(fd) == 8, sorted(fd)
    bad = Fetched("u", 200, 1, json.dumps({"response": {"total": 1, "data": [
        {"period": "2025-11-02T01", "respondent": "CISO", "type": "D", "value": "1", "value-units": "megawatts"}]}}).encode())
    try:
        reg.parse(bad)
        raise AssertionError("a changed unit must fail the parse")
    except ValueError:
        pass
    empty = Fetched("u", 200, 1, b'{"response": {"total": 0, "data": []}}')
    assert reg.parse(empty) == {} and empty.empty
    p = dict(eia._hourly_params(date(2025, 11, 2), date(2025, 11, 4)))
    assert p["start"] == "2025-11-02T01" and p["end"] == "2025-11-05T00"
    assert eia.latest(datetime(2025, 11, 5, 3, tzinfo=UTC)) == date(2025, 11, 3)
    print("test_parse_eia ok")


def test_parse_gas():
    daily, stor, price = eia_gas.DATASETS
    assert all(s.commodity == "natural_gas" for d in eia_gas.DATASETS for s in d.series)
    assert registry.visibility("eia_gas") == "internal" and "BY CHOICE" in eia_gas.TERMS.reason
    assert registry.visibility("eia") == "public"
    pts = daily.parse(_f("gas_daily_2024-04-01_12.json"))
    hh = pts["eia_gas.spot.spot_price.henry_hub"]
    c1 = pts["eia_gas.futures.futures_price_c1.nymex"]
    assert max(c1) == eia_gas.FUTURES_LAST and max(hh) > eia_gas.FUTURES_LAST, "futures stop 2024-04-05, spot goes on"
    days = storage.to_days(daily, pts, date(2024, 4, 1), date(2024, 4, 12))
    assert all(len(v) == 1 for v in days.values()) and ("eia_gas.spot.spot_price.henry_hub", date(2024, 4, 6)) not in days
    s = storage.to_days(stor, stor.parse(_f("gas_storage_2025-10.json")), date(2025, 10, 1), date(2025, 10, 31))
    assert {k for k, _ in s} == {"eia_gas.actual.storage_working_gas.r48", "eia_gas.actual.storage_working_gas.r31"}
    assert all(d.weekday() == 4 for _, d in s), "week ending Friday"
    p = price.parse(_f("gas_price_2025-q1.json"))
    assert set(p) == {"eia_gas.actual.electric_power_price.us", "eia_gas.actual.electric_power_price.tx"}, \
        "Arkansas is withheld (null): no row, not a zero"
    assert set(p["eia_gas.actual.electric_power_price.us"]) == {date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)}
    print("test_parse_gas ok")


def test_geo():
    assert geo.locate("aws", "us-west-1")["price_series"] == "caiso.da.lmp.th_np15"
    assert geo.locate("x", "US, Los Angeles, CA", "US")["match"] == "zone"
    assert geo.locate("x", "US, Los Angeles, CA", "US")["price_series"] == "caiso.da.lmp.th_sp15"
    assert geo.locate("lium", "San Jose", "US") is None, "a city alone is never placed"
    assert geo.locate("x", "California, US", "US") is None, "CAISO has no single reference hub: a state is not enough"
    assert geo.locate("x", "US, Phoenix, AZ", "US") is None, "no organised market"
    assert geo.locate("x", "US, Dallas, TX", "US") is None or geo.locate("x", "US, Dallas, TX", "US")["iso"] == "ERCOT"
    assert geo.locate("aws", "us-west-1", "DE") is None
    assert geo.norm(".H.INTERNAL_HUB") == "HINTERNALHUB" and geo.norm("N.Y.C.") == "NYC"
    assert abs(cross.cost_per_gpu_hour(700, 50.0, 1.3) - 0.0455) < 1e-12   # 0.7 kW x 1.3 x $50/MWh
    print("test_geo ok")


def _world(Session, now):
    """60 days of NP15 day-ahead prices (a spike on day 45), an H100 in aws us-west-1 whose price follows the power
    price two days later, Henry Hub on weekdays, and one market shift around the spike."""
    import random

    from store.shifts import MarketShift
    from tables import ComputeListingRow, ListingObservation

    rng = random.Random(3)
    end = now.date() - timedelta(days=1)
    start = end - timedelta(days=59)
    da = caiso.DATASETS[0]
    gas = eia_gas.DATASETS[0]
    ids = storage.sync_series([da, gas])
    power, rows, hh = {}, {}, {}
    for i in range(60):
        d = start + timedelta(days=i)
        v = 40 + 15 * rng.random() + (160 if i == 45 else 0)
        power[d] = v
        rows[("caiso.da.lmp.th_np15", d)] = [v] * storage.slots(d, caiso.TZ, 60)
        if d.weekday() < 5:
            hh[("eia_gas.spot.spot_price.henry_hub", d)] = [3.0 + 0.5 * rng.random()]
    storage.upsert_days(rows, ids, None)
    storage.upsert_days(hh, ids, None)
    # GPU price two days after the power price: y(t) = 2.0 + 0.002 * x(t - 2)
    with Session.begin() as s:
        for lid, region in (("h100-a", "us-west-1"), ("h100-b", "us-west-1")):
            s.add(ComputeListingRow(provider="aws", listing_id=lid, sku=lid, raw_gpu_name="H100", canonical_gpu_name=H100,
                                    gpu_count=1, region=region, country="US", price_per_gpu_hour=Decimal("2.1"),
                                    price_per_instance_hour=Decimal("2.1"), currency="USD", market_type="on_demand",
                                    provider_tier=None, interruptible=False, available=True, capacity=None,
                                    capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=now,
                                    first_seen_at=datetime.combine(start, time(0), tzinfo=UTC), visibility="public"))
            s.flush()
            for i in range(2, 60):
                d = start + timedelta(days=i)
                y = 2.0 + 0.002 * power[d - timedelta(days=2)]
                s.add(ListingObservation(provider="aws", listing_id=lid,
                                         observed_at=datetime.combine(d, time(12), tzinfo=UTC),
                                         price_per_gpu_hour=Decimal(str(round(y, 6))),
                                         price_per_instance_hour=Decimal(str(round(y, 6))), available=i % 3 != 0))
        spike = start + timedelta(days=45)
        t = datetime.combine(spike, time(18), tzinfo=UTC)
        s.add(MarketShift(shift_key="test:spike", data_kind="observed", type="market_median", segment="on_demand",
                          gpu=H100, provider=None, index_id=None, at=t, window_start=t - timedelta(hours=72),
                          window_end=t + timedelta(hours=48), direction="up", magnitude=0.1, severity="major",
                          title="H100 up", detail={}, definition_version="1", detected_at=t, items_version=None,
                          items_computed_at=None))
    return start, end, power


def test_db_cross_api():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    url = scratchdb.create(DB)
    saved = config.settings.app_password, config.settings.public_pages
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        now = datetime(2025, 11, 5, 12, tzinfo=UTC)

        # ---------------- EIA through the runner (MockTransport), paging included
        config.settings.eia_api_key = "EIAKEYSECRET123"
        eia.DATASETS = tuple(dataclasses.replace(d, pace_seconds=0) for d in eia.DATASETS)
        eia.PAGE = 100   # the 168-row fixture takes two pages
        body = json.loads((FX / "eia_region_ciso_erco_2025-11-02.json").read_bytes())
        seen = []

        def handler(req):
            q = dict(req.url.params)
            seen.append(str(req.url))
            data = body["response"]["data"]
            if "region-data" not in req.url.path or q["start"] != "2025-11-02T01":
                page = []
            else:
                o = int(q["offset"])
                page = data[o:o + int(q["length"])]
            return httpx.Response(200, json={"response": {"total": len(data) if page or q["offset"] != "0" else 0,
                                                          "data": page}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        out = asyncio.run(runner.run("backfill", sources=["eia"], since=date(2025, 11, 2), until=date(2025, 11, 2),
                                     datasets=["eia.region_hourly"], client=client, now=now))
        d0 = out["sources"][0]["datasets"][0]
        assert d0["fetches"] == 2 and d0["new"] == 8 and d0["failed"] == 0, d0   # 2 BAs x 4 types, merged pages
        with Session() as s:
            urls = [r[0] for r in s.execute(text("SELECT url FROM power_fetches"))]
            assert urls and all("EIAKEYSECRET123" not in u and "api_key=***" in u for u in urls), urls[:1]
            n = s.execute(text("""SELECT n FROM power_days d JOIN power_series p ON p.id = d.series_id
                                  WHERE p.key = 'eia.actual.demand.ciso'""")).scalar()
            assert n == 24, "both pages of the day merged into one row"
        assert all("EIAKEYSECRET123" in u for u in seen), "the key is sent, just never stored"

        # ---------------- cross-analysis
        start, end, power = _world(Session, now)
        res = cross.refresh(now)
        assert res["regional_rows"] > 0 and "caiso.da.lmp.th_np15" in res["price_series"], res
        rows = cross.regional(gpu=H100)
        r0 = next(r for r in rows if r["day"] == end)
        exp = 700 / 1000 * 1.3 * power[end] / 1000
        assert abs(r0["power_cost_usd_per_gpu_hour"] - exp) < 1e-9 and r0["tdp_w"] == 700 and r0["pue"] == 1.3
        assert abs(r0["spread_usd_per_gpu_hour"] - (r0["gpu_price_median"] - exp)) < 1e-9
        assert r0["match"] == "zone" and r0["iso"] == "CAISO" and r0["listings"] == 2
        assert {r["visibility"] for r in rows} == {"internal"}, "ISO prices are internal: so is every derived row"
        ll = {(r["metric"], r["lag_days"]): r for r in cross.leadlag(gpu=H100)}
        best = max((r for (m, _), r in ll.items() if m == "gpu_price_median" and r["corr"] is not None),
                   key=lambda r: r["corr"])
        assert best["lag_days"] == 2 and best["corr"] > 0.99, best
        assert abs(ll[("gpu_price_median", 0)]["corr"] or 0) < 0.5
        assert ll[("gpu_price_median", 2)]["spike_days"] >= 1
        assert ("gpu_price_vs_henry_hub", 0) in ll and ("capacity_available", 0) in ll
        assert any(r["metric"] == "power_price_vs_henry_hub" for r in cross.leadlag(gpu="*"))
        assert {r["visibility"] for r in cross.leadlag()} == {"internal"}
        # never GPU statistics
        with Session() as s:
            for t in ("market_hourly", "index_levels", "market_shift_items", "archived_prices"):
                assert s.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t
            assert s.execute(text("SELECT count(*) FROM market_shifts")).scalar() == 1
        # ---------------- shifts hook: read-time only
        sp = cross.shift_power()
        assert sp and sp[0]["power_spikes"], sp
        top = sp[0]["power_spikes"][0]
        assert top["series"] == "caiso.da.lmp.th_np15" and top["match"] == "gpu_region" and top["ratio"] > 2
        assert "not necessarily causal" in sp[0]["heading"]
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM market_shift_items")).scalar() == 0

        # ---------------- API: admin-only cross reads; the public series read is EIA-930 only
        config.settings.app_password, config.settings.public_pages = "pw", True
        import accounts.keys as keys
        import main
        from accounts import auth
        from fastapi.testclient import TestClient
        anon = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        op = TestClient(main.app, headers={"X-OpenGrid-Request": "1",
                                           "Authorization": "Basic " + base64.b64encode(b"opengrid:pw").decode()})
        real = keys.verify
        keys.verify = lambda token, request: auth.Principal(kind="api_key", account_id=1, key_id=7,
                                                            scopes=frozenset({"data:read"}))
        try:
            for p in ("/v1/ops/power/cross/regional", "/v1/ops/power/cross/leadlag", "/v1/ops/power/shifts",
                      "/v1/ops/power/map", "/v1/ops/power/series?source=eia_gas"):
                assert anon.get(p).status_code in (401, 403), p
                r = op.get(p)
                assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store", (p, r.text[:300])
            m = op.get("/v1/ops/power/map").json()
            assert m["meta"]["mapped"] >= 1
            assert op.get("/v1/ops/power/series?source=eia_gas").json()["meta"]["count"] == 5
            pub = anon.get("/v1/power/series")
            assert pub.status_code == 200, pub.text[:200]
            cat = pub.json()["data"]
            assert cat and {c["source"] for c in cat} == {"eia"} and {c["visibility"] for c in cat} == {"public"}
            r = anon.get("/v1/power/series?key=eia.actual.demand.ciso")
            assert r.status_code == 200 and r.json()["data"][0]["n"] == 24
            for k in ("caiso.da.lmp.th_np15", "eia_gas.spot.spot_price.henry_hub"):
                r = anon.get(f"/v1/power/series?key={k}")
                assert r.status_code == 404, (k, r.status_code)
                assert op.get(f"/v1/power/series?key={k}").status_code == 404, "the public read is public-only for admins too"
        finally:
            keys.verify = real
    finally:
        config.settings.eia_api_key = None
        config.settings.app_password, config.settings.public_pages = saved
        scratchdb.drop(DB)
    print("test_db_cross_api ok")


if __name__ == "__main__":
    test_parse_eia()
    test_parse_gas()
    test_geo()
    test_db_cross_api()
    print("all passed")
