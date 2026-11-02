"""INTERNAL equities (context/equities.py, api/context.py /v1/ops/context/equities*): Alpaca bar parsing over REAL
saved responses, the 15-minute SIP rule and incomplete-bar cut-offs, pagination, 429 / 403 handling, raw-first
storage with failures (keys never stored), idempotent backfill, corporate-action refetch, the moves() research helper,
and VISIBILITY: anonymous and keyed non-admin callers never see an equity series. No network: httpx.MockTransport.

Fixtures (real Alpaca Market Data responses, captured 2026-11-01, trimmed by the request itself):
    fixtures_equities/daily_{raw,split,all}_nvda_amd_2024-06.json   NVDA + AMD daily, 2024-06-03..14 (NVDA 10:1 split
                                                                   on 2024-06-10), SIP
    fixtures_equities/daily_split_page{1,2}.json                   the same request with limit=12: two pages
    fixtures_equities/hourly_split_nvda_2024-06-10.json            NVDA 1Hour, 04:00-20:00 ET (extended hours)
    fixtures_equities/recent_sip_forbidden.json                    the free plan's 403 for the last 15 minutes
    fixtures_equities/earliest_2016.json                           NVDA from 2016-01-04 (CRWV, ARM: no bars yet)
    fixtures_equities/bad_symbol.json                              an unknown symbol is simply absent

Run:  .venv/bin/python tests/test_equities.py
"""

import asyncio
import base64
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import httpx  # noqa: E402
from context import equities as eq  # noqa: E402

FX = ROOT / "tests" / "fixtures_equities"
KEY, SECRET = "TESTKEYID123", "TESTSECRETabc456"
NOW = datetime(2024, 6, 17, 12, 0, tzinfo=timezone.utc)   # after every fixture bar


def _j(n):
    return json.loads((FX / f"{n}.json").read_text())


def _body(n):
    return _j(n)["body"]


def test_parse():
    assert eq.parse_ts("2024-06-10T04:00:00Z") == datetime(2024, 6, 10, 4, tzinfo=timezone.utc)
    assert eq.parse_ts("2024-06-10T13:30:00.123456789Z").microsecond == 123456
    assert eq.session_date(eq.parse_ts("2016-01-04T05:00:00Z")) == date(2016, 1, 4)   # EST midnight
    raw = eq.daily_points(_body("daily_raw_nvda_amd_2024-06"), "raw", NOW)
    spl = eq.daily_points(_body("daily_split_nvda_amd_2024-06"), "split", NOW)
    tot = eq.daily_points(_body("daily_all_nvda_amd_2024-06"), "all", NOW)
    by = lambda pts: {(p["series_id"], p["period_end"]): p["value"] for p in pts}  # noqa: E731
    r, s, t = by(raw), by(spl), by(tot)
    assert len(raw) == len(spl) == 2 * 10 * 6 and len(tot) == 2 * 10, "6 fields split/raw, close only for all"
    assert r[("alpaca.nvda.close.raw", date(2024, 6, 3))] == D("1150") and s[("alpaca.nvda.close.split", date(2024, 6, 3))] == D("115")
    assert r[("alpaca.nvda.volume.raw", date(2024, 6, 3))] * 10 == s[("alpaca.nvda.volume.split", date(2024, 6, 3))]
    assert r[("alpaca.nvda.close.raw", date(2024, 6, 14))] == s[("alpaca.nvda.close.split", date(2024, 6, 14))], "after the split"
    assert t[("alpaca.nvda.close.all", date(2024, 6, 3))] < s[("alpaca.nvda.close.split", date(2024, 6, 3))], "dividends"
    assert r[("alpaca.amd.close.raw", date(2024, 6, 3))] == s[("alpaca.amd.close.split", date(2024, 6, 3))]
    assert all(p["visibility"] == "internal" and p["source"] == "alpaca" for p in raw + spl + tot)
    assert {p["unit"] for p in spl} == {"usd", "shares"}
    # a session is kept only once it is over (20:15 ET); 2024-06-14 is EDT: 00:15 UTC on the 15th
    cut = datetime(2024, 6, 15, 0, 14, tzinfo=timezone.utc)
    assert date(2024, 6, 14) not in {p["period_end"] for p in eq.daily_points(_body("daily_split_nvda_amd_2024-06"), "split", cut)}
    assert date(2024, 6, 14) in {p["period_end"] for p in eq.daily_points(_body("daily_split_nvda_amd_2024-06"), "split", cut + timedelta(minutes=2))}
    hb = _body("hourly_split_nvda_2024-06-10")
    rows = eq.hourly_rows(hb, "1Hour", "split", NOW)
    assert len(rows) == 16 and rows[0]["t"] == datetime(2024, 6, 10, 8, tzinfo=timezone.utc) and rows[0]["feed"] == "sip"
    # the last bar (23:00Z) closes at 24:00Z; kept only from 00:16Z (the 15-minute SIP rule)
    assert len(eq.hourly_rows(hb, "1Hour", "split", datetime(2024, 6, 11, 0, 15, tzinfo=timezone.utc))) == 15
    assert len(eq.hourly_rows(hb, "1Hour", "split", datetime(2024, 6, 11, 0, 16, tzinfo=timezone.utc))) == 16
    e16 = _body("earliest_2016")
    assert set(e16["bars"]) == {"NVDA"} and e16["bars"]["NVDA"][0]["t"].startswith("2016-01-04")
    assert set(_body("bad_symbol")["bars"]) == {"NVDA"}
    assert _j("recent_sip_forbidden")["status"] == 403
    assert eq.daily_points({"bars": None}, "split", NOW) == []
    try:
        eq.daily_points({"bars": []}, "split", NOW)
        raise AssertionError("list accepted")
    except ValueError:
        pass
    import config
    config.settings.equity_tickers = " nvda, AMD ,nvda"
    assert eq.tickers() == ("NVDA", "AMD")
    config.settings.equity_tickers = None
    assert len(eq.tickers()) == 20 and {"SMH", "SOXX", "CRWV", "NBIS"} <= set(eq.tickers())
    cat = eq.catalog()
    assert len(cat) == 20 * 13 and all(c["visibility"] == "internal" for c in cat)
    print("test_parse ok")


def test_static_rules():
    src = "\n".join(p.read_text() for p in (ROOT / "context").glob("*.py"))
    assert "api.alpaca.markets" not in src and "paper-api" not in src and "/v2/orders" not in src, "never trading"
    assert re.findall(r"https://[a-z.]*alpaca[a-z.]*", src) == ["https://data.alpaca.markets"]
    assert "iex" not in eq.__dict__.get("FEED", "") and eq.FEED == "sip"
    # the internal note is never a public methodology page
    from api import pages
    assert (ROOT / "methodology" / "internal" / "equities.md").exists()
    assert not any("equit" in n for n in pages.methodology_names())
    print("test_static_rules ok")


# --------------------------------------------------------------------------- mock Alpaca

def alpaca(fail=None, split_change=False, hourly_change=False):
    """Serves the fixtures by (timeframe, adjustment, page_token); records every request."""
    seen = []
    state = {"429": fail == "429", "403": fail == "403", "drop": fail == "drop"}

    def handler(req: httpx.Request):
        q = dict(req.url.params)
        seen.append((req.url.host, req.url.path, q, req.headers.get("apca-api-key-id"), req.headers.get("apca-api-secret-key")))
        if fail == "500":
            return httpx.Response(500, text=f"boom {KEY}")
        if state["drop"]:
            state["drop"] = False
            raise httpx.ReadError("connection reset", request=req)
        if state["429"]:
            state["429"] = False
            return httpx.Response(429, headers={"retry-after": "0"}, json={"message": "too many requests"})
        if state["403"]:
            state["403"] = False
            return httpx.Response(403, json=_j("recent_sip_forbidden")["body"])
        if q["timeframe"] == "1Hour":
            body = _body("hourly_split_nvda_2024-06-10")
            if hourly_change:
                for b in body["bars"]["NVDA"]:
                    b["c"] = b["c"] / 2
            return httpx.Response(200, json=body)
        adj = q["adjustment"]
        if adj == "split" and "page_token" in q:
            return httpx.Response(200, json=_body("daily_split_page2"))
        if adj == "split" and not split_change:
            return httpx.Response(200, json=_body("daily_split_page1"))
        body = _body(f"daily_{adj}_nvda_amd_2024-06")
        if split_change and adj == "split":   # a new 2:1 split: every adjusted close halves
            body = json.loads(json.dumps(body))
            for b in body["bars"]["NVDA"]:
                b["c"] = b["c"] / 2
        return httpx.Response(200, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


DB = "og_test_equities"


def test_db_api_visibility():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    url = scratchdb.create(DB)
    saved = config.settings.app_password, config.settings.public_pages
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        eq.PACE_SECONDS = 0
        syms = ("NVDA", "AMD")
        assert asyncio.run(eq.refresh(alpaca()[0], symbols=syms, now=NOW)) == {"source": "alpaca", "enabled": False}
        config.settings.alpaca_data_key_id, config.settings.alpaca_data_secret_key = KEY, SECRET

        client, seen = alpaca()
        out = asyncio.run(eq.refresh(client, full=True, symbols=syms, now=NOW))
        assert out["failed"] == 0 and out["daily"]["split"]["new"] == 120 and out["daily"]["raw"]["new"] == 120
        assert out["daily"]["all"]["new"] == 20 and out["hourly"]["split"]["new"] == 16
        assert all(h == "data.alpaca.markets" and p == "/v2/stocks/bars" for h, p, *_ in seen)
        assert all(k == KEY and s_ == SECRET and q["feed"] == "sip" for _, _, q, k, s_ in seen)
        assert any("page_token" in q for _, _, q, *_ in seen), "second page followed"
        daily = [q for _, _, q, *_ in seen if q["timeframe"] == "1Day"]
        assert all(q["start"] == "2016-01-01T00:00:00Z" and q["end"] == "2024-06-17T11:44:00Z" for q in daily), "now - 16 min"
        hq = next(q for _, _, q, *_ in seen if q["timeframe"] == "1Hour")
        assert hq["start"][:4] == "2019"
        with Session() as s:
            snaps = s.execute(text("SELECT source, key, ok, url, payload::text AS p FROM context_snapshots")).all()
            assert len(snaps) == len(seen) and all(r.ok and r.source == "alpaca" for r in snaps)
            assert not any(KEY in (r.url + r.p) or SECRET in (r.url + r.p) for r in snaps), "keys never stored"
            vis = dict(s.execute(text("SELECT visibility, count(*) FROM context_series GROUP BY 1")).all())
            assert vis == {"internal": 260}
        # idempotent: the same backfill again changes nothing
        out2 = asyncio.run(eq.refresh(alpaca()[0], full=True, symbols=syms, now=NOW))
        assert out2["daily"] == {a: {"new": 0, "revised": 0} for a in ("split", "raw", "all")} and out2["hourly"]["split"]["new"] == 0
        # forward: from the latest stored bar minus the overlap
        client, seen = alpaca()
        asyncio.run(eq.refresh(client, symbols=syms, now=NOW))
        assert {q["start"] for _, _, q, *_ in seen if q["timeframe"] == "1Day"} == {"2024-06-04T00:00:00Z"}
        # a new split in the overlap revises old adjusted closes: NVDA's split history is re-read in full
        client, seen = alpaca(split_change=True)
        out3 = asyncio.run(eq.refresh(client, symbols=syms, now=NOW, hourly=False))
        assert out3["refetched"] == ["1Day:split:NVDA"], out3
        assert [q["start"] for _, _, q, *_ in seen if q["adjustment"] == "split"][-1] == "2016-01-01T00:00:00Z"
        assert eq.points("NVDA", "close", "split", until=date(2024, 6, 3))[-1]["value"] == 57.5
        with Session() as s:
            r = s.execute(text("SELECT revisions, meta FROM context_series WHERE series_id = 'alpaca.nvda.close.split' "
                               "AND period_end = '2024-06-03'")).one()
            assert r.revisions == 1 and r.meta["previous_value"].startswith("115")
        out4 = asyncio.run(eq.refresh(alpaca(hourly_change=True)[0], symbols=("NVDA",), now=NOW))
        assert "1Hour:split:NVDA" in out4["refetched"] and out4["hourly"]["split"]["revised"] >= 15, out4
        # 429 then success; 403 (recent SIP) steps the end back an hour, never to IEX; a 500 is stored, key redacted
        eq.RETRY_SECONDS, eq.BACKOFF_SECONDS = 0, (0, 0, 0)
        client, seen = alpaca(fail="drop")   # a dropped connection is retried, not a failed fetch
        assert asyncio.run(eq.refresh(client, symbols=syms, now=NOW, hourly=False))["failed"] == 0
        client, seen = alpaca(fail="429")
        assert asyncio.run(eq.refresh(client, symbols=syms, now=NOW, hourly=False))["failed"] == 0
        client, seen = alpaca(fail="403")
        assert asyncio.run(eq.refresh(client, symbols=syms, now=NOW, hourly=False))["failed"] == 0
        assert seen[1][2]["end"] == "2024-06-17T10:44:00Z" and all(q["feed"] == "sip" for _, _, q, *_ in seen)
        client, seen = alpaca(fail="500")
        assert asyncio.run(eq.refresh(client, symbols=syms, now=NOW))["failed"] == 5   # 3 daily + 2 hourly groups (AMD has no hourly bars)
        with Session() as s:
            errs = s.execute(text("SELECT error FROM context_snapshots WHERE NOT ok")).scalars().all()
            assert len(errs) == 5 and not any(KEY in e or SECRET in e for e in errs)

        # coverage + the moves helper, beside GPU market moves
        cov = {r["symbol"]: r for r in eq.coverage()}
        assert cov["NVDA"]["daily"]["split"]["sessions"] == 10 and cov["NVDA"]["daily"]["split"]["earliest"] == date(2024, 6, 3)
        assert cov["NVDA"]["hourly"]["split"]["bars"] == 16 and cov["SMH"]["daily"]["split"] == {"sessions": 0}
        with Session.begin() as s:
            for h, med in ((datetime(2024, 6, 4, 20, tzinfo=timezone.utc), 2.0), (datetime(2024, 6, 12, 20, tzinfo=timezone.utc), 2.5)):
                s.execute(text("""INSERT INTO market_gpu_hourly (segment, gpu, hour, providers_live, providers_priced,
                    providers_available, live_listings, priced_listings, available_listings, sold_out_listings, lowest, median, highest)
                    VALUES ('on_demand', 'NVIDIA H100 80GB SXM5', :h, 5, 5, 5, 9, 9, 9, 0, :lo, :m, 4)"""),
                          {"h": h, "m": med, "lo": med - 0.5})
        mv = eq.moves(date(2024, 6, 4), date(2024, 6, 12), symbols=("NVDA", "AMD", "SMH"))
        e = {r["symbol"]: r for r in mv["equities"]}
        assert e["AMD"]["from"] == date(2024, 6, 4) and e["AMD"]["pct"] is not None and e["AMD"]["total_return_pct"] is not None
        assert e["SMH"]["pct"] is None and mv["visibility"] == "internal"
        g = mv["gpus"][0]
        assert g["median_pct"] == 25.0 and g["lowest_pct"] == round((2.0 / 1.5 - 1) * 100, 4)

        # ---------------- visibility: the public reads never carry equity data
        config.settings.app_password, config.settings.public_pages = "pw", True
        import accounts.keys as keys
        import main
        from accounts import auth
        from api import context as ctx_api
        from fastapi.testclient import TestClient
        ctx_api._catalog.cache_clear()
        anon = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        keyed = TestClient(main.app, headers={"X-OpenGrid-Request": "1", "Authorization": "Bearer opg_test"})
        op = TestClient(main.app, headers={"X-OpenGrid-Request": "1",
                                           "Authorization": "Basic " + base64.b64encode(b"opengrid:pw").decode()})
        real = keys.verify
        keys.verify = lambda token, request: auth.Principal(kind="api_key", account_id=1, key_id=7,
                                                            scopes=frozenset({"data:read"}))
        try:
            for c in (anon, keyed, op):
                cat = c.get("/v1/context/series")
                assert cat.status_code == 200 and "alpaca" not in cat.text.lower(), "catalogue never lists equities"
                assert c.get("/v1/context/series?source=alpaca").json()["data"] == []
                assert {r["source"] for r in c.get("/v1/context/series?entity=NVDA").json()["data"]} <= {"sec_edgar"}
                assert c.get("/v1/context/series?id=alpaca.nvda.close.split").status_code == 404
                pw = c.get("/v1/context/power")
                assert pw.status_code == 200 and "alpaca" not in pw.text.lower()
            for c in (anon, keyed):
                for p in ("/v1/ops/context/equities", "/v1/ops/context/equities?symbol=NVDA",
                          "/v1/ops/context/equities/moves?start=2024-06-04&end=2024-06-12"):
                    r = c.get(p)
                    assert r.status_code in (401, 403) and "115" not in r.text, (p, r.status_code)
            assert "/v1/ops/context/equities" not in json.dumps(anon.get("/openapi.json").json() if anon.get("/openapi.json").status_code == 200 else {})
            r = op.get("/v1/ops/context/equities")
            assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
            assert r.json()["meta"]["visibility"] == "internal" and len(r.json()["data"]) == 20
            r = op.get("/v1/ops/context/equities?symbol=nvda&adjustment=raw&since=2024-06-10").json()
            assert r["data"][0] == {"date": "2024-06-10", "value": 121.79} and r["meta"]["unit"] == "usd"
            assert op.get("/v1/ops/context/equities?symbol=NVDA&timeframe=1Hour").json()["meta"]["count"] == 16
            assert op.get("/v1/ops/context/equities?symbol=NVDA&adjustment=all&field=volume").status_code == 422
            assert op.get("/v1/ops/context/equities?symbol=XYZ").status_code == 404
            m = op.get("/v1/ops/context/equities/moves?start=2024-06-04&end=2024-06-12").json()
            assert m["data"]["gpus"][0]["median_pct"] == 25.0
        finally:
            keys.verify = real
    finally:
        config.settings.alpaca_data_key_id = config.settings.alpaca_data_secret_key = None
        config.settings.app_password, config.settings.public_pages = saved
        scratchdb.drop(DB)
    print("test_db_api_visibility ok")


if __name__ == "__main__":
    test_parse()
    test_static_rules()
    test_db_api_visibility()
    print("all passed")
