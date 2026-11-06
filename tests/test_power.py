"""Power markets foundation (power/; methodology/power-markets.md): the registry and its visibility verdicts, the
market-day grid (DST days), CAISO OASIS parsing over REAL saved answers, storage (series-days as real[], revisions,
partial days never overwrite complete ones), the runner (backfill resumes and skips stored days, "no data" days are
recorded and skipped, failures are recorded and retried, forward keeps bytes only where the dataset says so,
OASIS 1015 throttling is retried), and the admin-only /v1/ops/power reads. No network: httpx.MockTransport.

Fixtures (real CAISO OASIS answers, captured 2026-11-05):
    fixtures_power/caiso_da_2025-11-01_03.zip           PRC_LMP DAM, 7 nodes, 2025-11-01..03 PT (11-02: DST end)
    fixtures_power/caiso_rt_2025-11-02_np15_sp15.zip    PRC_INTVL_LMP RTM, NP15 + SP15, 2025-11-02 (300 intervals)
    fixtures_power/caiso_no_data.zip                    the "no data" answer (ERR_CODE 1000) for 2021-01-05

Run:  .venv/bin/python tests/test_power.py
"""

import asyncio
import base64
import dataclasses
import io
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import httpx  # noqa: E402

from power import http as phttp  # noqa: E402
from power import registry, runner, storage  # noqa: E402
from power.registry import Fetched  # noqa: E402
from power.sources import caiso  # noqa: E402

FX = ROOT / "tests" / "fixtures_power"
DB = "og_test_power"
UTC = timezone.utc
NOW = datetime(2025, 11, 5, 12, 0, tzinfo=UTC)   # 04:00 PT: DA complete through 11-05, RT through 11-04


def _b(name):
    return (FX / name).read_bytes()


def _xml_zip(code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.xml", f'<?xml version="1.0"?><m:OASISReport><m:ERR_CODE>{code}</m:ERR_CODE></m:OASISReport>')
    return buf.getvalue()


def test_registry_and_grid():
    assert registry.visibility("caiso") == "internal", "CAISO terms: not clearly redistributable"
    assert registry.visibility("nope") == "internal", "an unknown source is never public"
    assert "caiso" in registry.selected() and registry.configured("caiso")
    lst = {r["source"]: r for r in registry.as_list()}
    assert lst["caiso"]["terms_url"].startswith("https://www.caiso.com/") and lst["caiso"]["signup"] is None
    keys = list(registry.series_index())
    assert len(keys) == len(set(keys)), "series keys are globally unique"
    tz = caiso.TZ
    assert storage.slots(date(2025, 11, 1), tz, 60) == 24 and storage.slots(date(2025, 11, 2), tz, 60) == 25
    assert storage.slots(date(2025, 3, 9), tz, 60) == 23 and storage.slots(date(2025, 11, 2), tz, 5) == 300
    assert storage.day_start(date(2025, 11, 1), tz) == datetime(2025, 11, 1, 7, tzinfo=UTC)
    assert storage.day_start(date(2025, 11, 3), tz) == datetime(2025, 11, 3, 8, tzinfo=UTC)
    assert runner.chunks([date(2025, 1, d) for d in (1, 2, 3, 5, 6)], 2) == [
        (date(2025, 1, 1), date(2025, 1, 2)), (date(2025, 1, 3), date(2025, 1, 3)), (date(2025, 1, 5), date(2025, 1, 6))]
    # CAISO's depth and publication rules
    assert caiso.earliest(NOW) == date(2022, 8, 1)
    assert caiso.latest_da(NOW) == date(2025, 11, 5) and caiso.latest_rt(NOW) == date(2025, 11, 4)
    assert caiso.latest_da(datetime(2025, 11, 5, 23, tzinfo=UTC)) == date(2025, 11, 6)   # 15:00 PT: tomorrow posted
    p = caiso.params("da", date(2025, 11, 1), date(2025, 11, 3))
    assert p["startdatetime"] == "20251101T07:00-0000" and p["enddatetime"] == "20251104T08:00-0000"
    print("test_registry_and_grid ok")


def test_parse():
    da, rt = caiso.DATASETS
    f = Fetched("u", 200, 1, _b("caiso_da_2025-11-01_03.zip"))
    days = storage.to_days(da, da.parse(f), date(2025, 11, 1), date(2025, 11, 3))
    assert not f.empty and len(days) == 7 * 3, sorted(days)[:5]
    assert all(len(v) == (25 if d == date(2025, 11, 2) else 24) for (_, d), v in days.items())
    assert all(None not in v for v in days.values()), "every DA hour present"
    # the sample row: DLAP_PGAE, OPR_DT 2025-11-01 OPR_HR 19 (18:00 PDT) = 01:00Z on 11-02, $53.57017
    assert abs(days[("caiso.da.lmp.dlap_pgae", date(2025, 11, 1))][18] - 53.57017) < 1e-9
    f = Fetched("u", 200, 1, _b("caiso_rt_2025-11-02_np15_sp15.zip"))
    rdays = storage.to_days(rt, rt.parse(f), date(2025, 11, 2), date(2025, 11, 2))
    assert set(rdays) == {("caiso.rt.lmp.th_np15", date(2025, 11, 2)), ("caiso.rt.lmp.th_sp15", date(2025, 11, 2))}
    assert all(len(v) == 300 for v in rdays.values())
    f = Fetched("u", 200, 1, _b("caiso_no_data.zip"))
    assert da.parse(f) == {} and f.empty
    f = Fetched("u", 200, 1, _xml_zip("1015"))
    try:
        da.parse(f)
        raise AssertionError("an OASIS error other than 'no data' must fail the parse")
    except ValueError as exc:
        assert "1015" in str(exc)
    print("test_parse ok")


class Oasis:
    """Serves the fixtures by (queryname, start); 1015 once when asked; a 500 for a chosen start."""

    def __init__(self):
        self.seen, self.throttle_once, self.fail_start, self.da_body = [], False, None, _b("caiso_da_2025-11-01_03.zip")

    def __call__(self, req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        self.seen.append((q["queryname"], q["startdatetime"], q["enddatetime"]))
        if self.throttle_once:
            self.throttle_once = False
            return httpx.Response(200, content=_xml_zip("1015"))
        if q["startdatetime"] == self.fail_start:
            return httpx.Response(500, text="oops")
        if q["queryname"] == "PRC_LMP" and q["startdatetime"] == "20251101T07:00-0000":
            return httpx.Response(200, content=self.da_body)
        if q["queryname"] == "PRC_INTVL_LMP" and q["startdatetime"] <= "20251102T07:00-0000" < q["enddatetime"]:
            return httpx.Response(200, content=_b("caiso_rt_2025-11-02_np15_sp15.zip"))
        return httpx.Response(200, content=_b("caiso_no_data.zip"))


def _patched():
    caiso.DATASETS = tuple(dataclasses.replace(d, pace_seconds=0) for d in caiso.DATASETS)
    caiso.RATE_LIMITED_WAIT = 0
    phttp.BACKOFF_SECONDS = (0, 0, 0)


def _da_with_value(v: str) -> bytes:
    """The DA fixture with DLAP_PGAE's 11-01 hour-19 LMP changed: a revision at the source."""
    with zipfile.ZipFile(io.BytesIO(_b("caiso_da_2025-11-01_03.zip"))) as z:
        name = z.namelist()[0]
        data = z.read(name).decode()
    data = data.replace("2025-11-01,19,0,DLAP_PGAE-APND,DLAP_PGAE-APND,DLAP_PGAE-APND,DAM,LMP,LMP_PRC,"
                        "DLAP_PGAE-APND,ALL_APNODES,0,53.57017",
                        "2025-11-01,19,0,DLAP_PGAE-APND,DLAP_PGAE-APND,DLAP_PGAE-APND,DAM,LMP,LMP_PRC,"
                        f"DLAP_PGAE-APND,ALL_APNODES,0,{v}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buf.getvalue()


def test_db_runner_api():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    _patched()
    url = scratchdb.create(DB)
    saved = config.settings.app_password, config.settings.public_pages
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        oasis = Oasis()
        client = httpx.AsyncClient(transport=httpx.MockTransport(oasis))

        def run(mode, **kw):
            return asyncio.run(runner.run(mode, sources=["caiso"], client=client, now=NOW, **kw))

        # dry run: a plan, no request
        out = run("backfill", since=date(2025, 10, 30), until=date(2025, 11, 3), dry_run=True)
        assert not oasis.seen and out["sources"][0]["datasets"][0]["days_missing"] == 5
        # backfill 2025-10-30..11-03: DA one chunk (10-30..11-03 -> served as "no data" except the fixture window);
        # use since=11-01 so the DA chunk is exactly the fixture
        out = run("backfill", since=date(2025, 11, 1), until=date(2025, 11, 3))
        da, rt = out["sources"][0]["datasets"]
        assert da["chunks"] == 1 and da["new"] == 21 and da["failed"] == 0, da
        assert rt["chunks"] == 1 and rt["new"] == 2 and rt["empty"] == 0, rt   # RT chunk 11-01..03: 11-02 only
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM power_days")).scalar() == 23
            n25 = s.execute(text("""SELECT array_length(vals, 1) FROM power_days d JOIN power_series p ON p.id = d.series_id
                                    WHERE p.key = 'caiso.da.lmp.th_np15' AND day = '2025-11-02'""")).scalar()
            assert n25 == 25
            modes = s.execute(text("SELECT mode, body IS NULL, sha256 IS NOT NULL FROM power_fetches")).all()
            assert all(m == ("hash", True, True) for m in modes), "the backfill keeps url + sha256 only"
            assert s.execute(text("SELECT count(DISTINCT visibility) FROM power_series")).scalar() == 1
            assert s.execute(text("SELECT visibility FROM power_series LIMIT 1")).scalar() == "internal"
            # never GPU data
            for t in ("compute_listings", "market_hourly", "archived_prices", "index_levels"):
                assert s.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t
        # resume: everything stored is skipped (RT 11-01 / 11-03 have no rows: refetched, "no data" again)
        n = len(oasis.seen)
        out = run("backfill", since=date(2025, 11, 1), until=date(2025, 11, 3))
        da, rt = out["sources"][0]["datasets"]
        assert da["chunks"] == 0 and da["days_skipped"] == 3, da
        assert rt["days_skipped"] >= 1, rt
        # an empty older day is skipped once answered "no data" (before latest - overlap)
        run("backfill", since=date(2025, 10, 20), until=date(2025, 10, 21))
        n = len(oasis.seen)
        out = run("backfill", since=date(2025, 10, 20), until=date(2025, 10, 21))
        assert len(oasis.seen) == n, "an answered 'no data' day is not re-asked"
        # a failure is recorded and retried next run
        oasis.fail_start = "20251015T07:00-0000"
        out = run("backfill", since=date(2025, 10, 15), until=date(2025, 10, 15), datasets=["caiso.da_lmp"])
        assert out["sources"][0]["datasets"][0]["failed"] == 1
        oasis.fail_start = None
        out = run("backfill", since=date(2025, 10, 15), until=date(2025, 10, 15), datasets=["caiso.da_lmp"])
        assert out["sources"][0]["datasets"][0]["chunks"] == 1 and out["sources"][0]["datasets"][0]["failed"] == 0
        # 1015 throttling: waited and retried within the fetch
        oasis.throttle_once = True
        out = run("backfill", since=date(2025, 10, 10), until=date(2025, 10, 10), datasets=["caiso.da_lmp"])
        d0 = out["sources"][0]["datasets"][0]
        assert d0["failed"] == 0 and d0["empty"] == 1, d0
        # a revised value at the source: counted, value replaced; unchanged days untouched
        oasis.da_body = _da_with_value("60.0")
        n0 = len(oasis.seen)
        out = run("backfill", since=date(2025, 11, 1), until=date(2025, 11, 3), datasets=["caiso.da_lmp"])
        assert len(oasis.seen) == n0, "backfill never re-reads complete days"
        later = datetime(2025, 11, 3, 12, tzinfo=UTC)    # forward: latest DA 11-03, overlap re-reads 11-01..
        out = asyncio.run(runner.run("forward", sources=["caiso"], client=client, now=later,
                                     datasets=["caiso.da_lmp"]))
        d0 = out["sources"][0]["datasets"][0]
        assert d0["revised"] == 1 and d0["unchanged"] == 20, d0
        with Session() as s:
            v, rev = s.execute(text("""SELECT vals[19], revisions FROM power_days d JOIN power_series p
                                       ON p.id = d.series_id WHERE p.key = 'caiso.da.lmp.dlap_pgae'
                                       AND day = '2025-11-01'""")).one()
            assert abs(v - 60.0) < 1e-6 and rev == 1
            kept = s.execute(text("""SELECT mode, encoding, bytes, length(body) FROM power_fetches
                                     WHERE dataset = 'caiso.da_lmp' ORDER BY id DESC LIMIT 1""")).one()
            assert kept[0] == "body" and kept[1] == "identity" and kept[2] == kept[3], "forward DA keeps the zip"
        # forward RT keeps url + sha256 only (measured ~113 KB/day)
        asyncio.run(runner.run("forward", sources=["caiso"], client=client, now=later, datasets=["caiso.rt_lmp"]))
        with Session() as s:
            assert s.execute(text("""SELECT count(*) FROM power_fetches WHERE dataset = 'caiso.rt_lmp'
                                     AND body IS NOT NULL""")).scalar() == 0
        # a partial re-read never replaces a more complete day
        ds = caiso.DATASETS[1]
        ids = storage.sync_series([ds])
        part = {("caiso.rt.lmp.th_np15", date(2025, 11, 2)): [1.0] + [None] * 299}
        assert storage.upsert_days(part, ids, None)["revised"] == 0
        st = runner.status()
        dd = {d["dataset"]: d for d in st["datasets"]}
        assert dd["caiso.da_lmp"]["series_days"] >= 21 and st["total_bytes"] > 0

        # ---------------- the ops reads: admin only
        config.settings.app_password, config.settings.public_pages = "pw", True
        import accounts.keys as keys
        import main
        from accounts import auth
        from fastapi.testclient import TestClient
        anon = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        keyed = TestClient(main.app, headers={"X-OpenGrid-Request": "1", "Authorization": "Bearer opg_test"})
        op = TestClient(main.app, headers={"X-OpenGrid-Request": "1",
                                           "Authorization": "Basic " + base64.b64encode(b"opengrid:pw").decode()})
        real = keys.verify
        keys.verify = lambda token, request: auth.Principal(kind="api_key", account_id=1, key_id=7,
                                                            scopes=frozenset({"data:read"}))
        try:
            paths = ("/v1/ops/power/sources", "/v1/ops/power/status", "/v1/ops/power/series",
                     "/v1/ops/power/series?key=caiso.da.lmp.dlap_pgae&intervals=true")
            for c in (anon, keyed):
                for p in paths:
                    r = c.get(p)
                    assert r.status_code in (401, 403) and "53.57" not in r.text, (p, r.status_code)
            for p in paths:
                r = op.get(p)
                assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store", (p, r.text[:200])
            r = op.get("/v1/ops/power/series?key=caiso.da.lmp.dlap_pgae&intervals=true&since=2025-11-01&until=2025-11-01")
            row = r.json()["data"][0]
            assert row["values"][18] == 60.0 and len(row["values"]) == 24 and row["start"].startswith("2025-11-01T07:00")
        finally:
            keys.verify = real
    finally:
        config.settings.app_password, config.settings.public_pages = saved
        scratchdb.drop(DB)
    print("test_db_runner_api ok")


def test_db_per_day_files():
    """A source with one file per day (NYISO / MISO style): a 404 is "not published", not a failure, and blocks
    only its own day; a day with a real gap at the source is stored once its answer succeeded and is not re-asked;
    month-aligned chunks; fetch(now=) receives the run's clock; an unchanged forward re-read keeps no bytes."""
    import config  # noqa: F401
    import fixtures
    import normalize
    from sqlalchemy import text

    from power.http import Client
    from power.registry import Dataset, Series

    url = scratchdb.create(DB)
    try:
        normalize.SessionLocal = fixtures.session(url)
        seen_now = []
        published = {date(2025, 1, d) for d in range(1, 32)} - {date(2025, 1, 30)}   # 01-30 posted late
        gap_day = date(2025, 1, 15)

        async def fetch(get, start, end, now=None):
            seen_now.append(now)
            out = []
            for d in storage.days(start, end):
                f = await get.get(f"https://example.test/{d:%Y%m%d}.csv")
                f.start = f.end = d
                out.append(f)
            return out

        def parse(f):
            d = date.fromisoformat(f.url.rsplit("/", 1)[1][:8][:4] + "-" + f.url.rsplit("/", 1)[1][4:6] + "-"
                                   + f.url.rsplit("/", 1)[1][6:8])
            t0 = storage.day_start(d, "UTC")
            hours = [h for h in range(24) if not (d == gap_day and h == 10)]   # the source never had 10:00
            return {"t.da.lmp.x": {t0 + timedelta(hours=h): float(f.body.decode()) + h for h in hours}}

        ds = Dataset(key="t.da", source="t", series=(Series("t.da.lmp.x", "x", "lmp", "da", "X", "hub", "usd_per_mwh"),),
                     interval_minutes=60, tz="UTC", chunk_days=10, earliest=lambda now: date(2024, 12, 25),
                     latest=lambda now: date(2025, 2, 3), fetch=fetch, parse=parse, pace_seconds=0, overlap_days=2,
                     keep_raw=True, empty_statuses=(404,), align="month")
        assert runner.chunks([date(2025, 1, 30), date(2025, 1, 31), date(2025, 2, 1)], 10, "month") == [
            (date(2025, 1, 30), date(2025, 1, 31)), (date(2025, 2, 1), date(2025, 2, 1))]
        hits = []

        def handler(req):
            d = datetime.strptime(req.url.path.rsplit("/", 1)[1][:8], "%Y%m%d").date()
            hits.append(d)
            return httpx.Response(200, text="10") if d in published else httpx.Response(404, text="not found")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        now = datetime(2025, 2, 4, 12, tzinfo=UTC)

        def go(mode):
            return asyncio.run(runner.run_dataset(ds, mode, Client(client, "t", 0), now, date(2025, 1, 1),
                                                  date(2025, 1, 31)))

        r = go("backfill")
        assert r["failed"] == 0 and r["empty"] == 1 and r["new"] == 30, r   # 01-30: not published, not a failure
        assert all(n == now for n in seen_now) and seen_now
        assert all(a.month == b.month for a, b in runner.plan(ds, "backfill", now, date(2024, 12, 28))["chunks"])
        hits.clear()
        r = go("backfill")
        assert hits == [date(2025, 1, 30)], ("only the unpublished day is re-asked; the gap day is final", hits)
        published.add(date(2025, 1, 30))
        r = go("backfill")
        assert r["new"] == 1
        with normalize.SessionLocal() as s:
            n, complete = s.execute(text("SELECT n, complete FROM power_days WHERE day = :d"), {"d": gap_day}).one()
            assert n == 23 and not complete, "the source's own gap stays a NULL slot"
        # forward re-read of unchanged days keeps url + sha256 only
        hits.clear()
        asyncio.run(runner.run_dataset(ds, "forward", Client(client, "t", 0), datetime(2025, 2, 1, 12, tzinfo=UTC)))
        assert hits, "forward re-read the overlap"
        with normalize.SessionLocal() as s:
            kept = s.execute(text("""SELECT count(*) FILTER (WHERE body IS NOT NULL), count(*) FROM power_fetches
                                     WHERE fetched_at > now() - interval '1 minute' AND mode = 'hash'
                                     AND period_start >= '2025-01-29'""")).one()
            assert kept[0] == 0 and kept[1] >= 1, kept
    finally:
        scratchdb.drop(DB)
    print("test_db_per_day_files ok")


if __name__ == "__main__":
    test_registry_and_grid()
    test_parse()
    test_db_runner_api()
    test_db_per_day_files()
    print("all passed")
