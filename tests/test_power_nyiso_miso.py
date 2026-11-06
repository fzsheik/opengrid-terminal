"""NYISO and MISO power sources (power/sources/nyiso.py, miso.py): parsing over REAL saved answers (DST days for
NYISO's unlabelled Eastern stamps, MISO's all-node files filtered to the hubs, monthly zips and daily CSVs), empty
and malformed answers, the daily-vs-monthly fetch choice and the monthly-zip reuse across chunks, and end-to-end
runner backfills / forwards on a scratch DB. No network: httpx.MockTransport.

Fixtures (real answers, captured 2026-11-05; "trimmed" = rows of other zones / nodes removed, nothing else changed):
    nyiso_da_202511_01_03.zip          damlbmp monthly zip, members 2025-11-01..03 (11-02: DST end, 25 hours)
    nyiso_rt_202511_02.zip             realtime monthly zip, member 2025-11-02 (300 five-minute intervals), 15 zones
    nyiso_rt_202503_09_capitl_nyc.zip  realtime, 2025-03-09 (DST start, 276 intervals), trimmed to CAPITL + N.Y.C.
    nyiso_da_20261103.csv              damlbmp daily CSV, 2026-11-03, whole
    nyiso_rt_20261104_capitl_nyc.csv   realtime daily CSV, 2026-11-04, trimmed to CAPITL + N.Y.C. (has off-cycle rows)
    miso_da_20261103_hubs.csv          da_expost_lmp daily CSV, 2026-11-03, trimmed to the 8 hubs + 3 other rows
    miso_rt_20261031_hubs.csv          rt_lmp_final daily CSV, 2026-10-31, trimmed the same way
    miso_da_202101_04_05_hubs.zip      202101_da_expost_lmp_csv.zip, members 2021-01-04 / 05, trimmed
    miso_rt_202101_04_05_hubs.zip      202101_rt_lmp_final_csv.zip, members 2021-01-04 / 05, trimmed

Run:  .venv/bin/python tests/test_power_nyiso_miso.py
"""

import asyncio
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
from power.sources import miso, nyiso  # noqa: E402

FX = ROOT / "tests" / "fixtures_power"
DB = "og_test_power_nyiso_miso"
UTC = timezone.utc


def _b(name):
    return (FX / name).read_bytes()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in members.items():
            z.writestr(n, data)
    return buf.getvalue()


def _raises(fn, *needles):
    try:
        fn()
    except ValueError as exc:
        assert all(n in str(exc) for n in needles), exc
        return
    raise AssertionError("expected ValueError")


def _days(ds, name_or_body, a, b):
    body = _b(name_or_body) if isinstance(name_or_body, str) else name_or_body
    f = Fetched("u", 200, 1, body)
    return f, storage.to_days(ds, ds.parse(f), a, b)


# --------------------------------------------------------------------------- registry / grid

def test_registry():
    assert registry.visibility("nyiso") == "internal" and registry.visibility("miso") == "internal"
    lst = {r["source"]: r for r in registry.as_list()}
    assert lst["nyiso"]["terms_url"] == "https://www.nyiso.com/legal-notice" and lst["nyiso"]["signup"] is None
    assert lst["miso"]["terms_url"].startswith("https://www.misoenergy.org/") and lst["miso"]["signup"] is None
    assert "reserves such rights" in nyiso.TERMS.reason and "automated means" in miso.TERMS.reason
    assert nyiso.TERMS.checked == miso.TERMS.checked == "2026-11-05"
    sel = registry.selected()
    assert "nyiso" in sel and "miso" not in sel, "MISO is off by default (automated-access clause)"
    keys = list(registry.series_index())
    assert len(keys) == len(set(keys)), "series keys are globally unique"
    assert all(k == k.lower() for k in keys)
    nda, nrt = nyiso.DATASETS
    mda, mrt = miso.DATASETS
    assert len(nda.series) == len(nrt.series) == 15 and len(mda.series) == len(mrt.series) == 8
    assert {s.key for s in nda.series} >= {f"nyiso.da.lmp.{z}" for z in ("capitl", "nyc", "hud_vl", "mhk_vl", "west",
                                                                         "longil", "hq", "npx", "oh", "pjm")}
    assert {s.key for s in mrt.series} == {f"miso.rt.lmp.{h}_hub" for h in ("arkansas", "illinois", "indiana",
                                                                          "louisiana", "michigan", "minn", "ms",
                                                                          "texas")}
    # grids: NYISO Eastern prevailing (23 / 25-hour days), MISO EST all year (always 24)
    assert storage.slots(date(2025, 11, 2), nyiso.TZ, 60) == 25 and storage.slots(date(2025, 3, 9), nyiso.TZ, 5) == 276
    assert storage.slots(date(2025, 11, 2), miso.TZ, 60) == 24 and storage.slots(date(2025, 3, 9), miso.TZ, 60) == 24
    assert storage.day_start(date(2026, 7, 1), miso.TZ) == datetime(2026, 7, 1, 5, tzinfo=UTC)
    # depth and publication
    now = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)   # 13:00 EST / ET
    assert nyiso.earliest(now) == miso.earliest(now) == date(2021, 1, 1)
    assert nyiso.latest_da(now) == date(2026, 11, 6) and nyiso.latest_rt(now) == date(2026, 11, 4)
    assert nyiso.latest_da(datetime(2026, 11, 5, 15, tzinfo=UTC)) == date(2026, 11, 5)   # 10:00 ET: not yet
    assert miso.latest_da(now) == date(2026, 11, 5) and miso.latest_da(now + timedelta(hours=3)) == date(2026, 11, 6)
    assert miso.latest_rt(now) == date(2026, 10, 28)
    assert nyiso.months(date(2025, 1, 30), date(2025, 3, 2)) == [
        (date(2025, 1, 30), date(2025, 1, 31)), (date(2025, 2, 1), date(2025, 2, 28)), (date(2025, 3, 1), date(2025, 3, 2))]
    assert nyiso.monthly_url("rt", date(2025, 11, 17)).endswith("/realtime/20251101realtime_zone_csv.zip")
    assert nyiso.daily_url("da", date(2026, 11, 3)).endswith("/damlbmp/20261103damlbmp_zone.csv")
    assert miso.monthly_url("da", date(2021, 1, 9)).endswith("/marketreports/202101_da_expost_lmp_csv.zip")
    assert miso.daily_url("rt", date(2026, 10, 31)).endswith("/marketreports/20261031_rt_lmp_final.csv")
    print("test_registry ok")


# --------------------------------------------------------------------------- NYISO parsing

def test_parse_nyiso():
    da, rt = nyiso.DATASETS
    f, d = _days(da, "nyiso_da_202511_01_03.zip", date(2025, 11, 1), date(2025, 11, 3))
    assert not f.empty and len(d) == 15 * 3
    assert all(len(v) == (25 if day == date(2025, 11, 2) else 24) and None not in v for (_, day), v in d.items())
    # fall back: the two unlabelled 01:00 rows are 01:00 EDT (05:00Z, slot 1) then 01:00 EST (06:00Z, slot 2)
    cap = d[("nyiso.da.lmp.capitl", date(2025, 11, 2))]
    assert cap[:4] == [49.96, 52.73, 51.44, 42.62], cap[:4]
    f, d = _days(da, "nyiso_da_20261103.csv", date(2026, 11, 3), date(2026, 11, 3))
    assert len(d) == 15 and all(len(v) == 24 and None not in v for v in d.values())
    assert d[("nyiso.da.lmp.capitl", date(2026, 11, 3))][0] == 31.42 and d[("nyiso.da.lmp.hq", date(2026, 11, 3))][0] == 30.06
    pts = da.parse(Fetched("u", 200, 1, _b("nyiso_da_20261103.csv")))
    assert min(pts["nyiso.da.lmp.nyc"]) == datetime(2026, 11, 3, 5, tzinfo=UTC), "hour-beginning 00:00 EST"

    f, d = _days(rt, "nyiso_rt_202511_02.zip", date(2025, 11, 2), date(2025, 11, 2))
    assert len(d) == 15 and all(len(v) == 300 and None not in v for v in d.values())
    cap = d[("nyiso.rt.lmp.capitl", date(2025, 11, 2))]
    # interval-ending stamps: "00:55" = slot 10, the EDT "01:00" = slot 11, the EST "01:00" (06:00Z) = slot 23
    assert cap[10] == 54.82 and cap[11] == 55.55 and cap[22] == 54.92 and cap[23] == 49.23 and cap[24] == 33.68
    f, d = _days(rt, "nyiso_rt_202503_09_capitl_nyc.zip", date(2025, 3, 9), date(2025, 3, 9))
    assert set(d) == {("nyiso.rt.lmp.capitl", date(2025, 3, 9)), ("nyiso.rt.lmp.nyc", date(2025, 3, 9))}
    cap = d[("nyiso.rt.lmp.capitl", date(2025, 3, 9))]
    assert len(cap) == 276 and None not in cap
    assert cap[22] == 40.97 and cap[23] == 40.80, "01:55 EST then the '03:00' EDT stamp (= 02:00 EST) end"
    f, d = _days(rt, "nyiso_rt_20261104_capitl_nyc.csv", date(2026, 11, 4), date(2026, 11, 4))
    cap = d[("nyiso.rt.lmp.capitl", date(2026, 11, 4))]
    assert len(cap) == 288 and None not in cap and cap[0] == 27.92
    # the "11/05/2026 00:00:00" row closes 11-04's last interval (23:55-24:00), not 11-05's first
    pts = rt.parse(Fetched("u", 200, 1, _b("nyiso_rt_20261104_capitl_nyc.csv")))["nyiso.rt.lmp.capitl"]
    assert max(pts) == datetime(2026, 11, 5, 4, 55, tzinfo=UTC) and len(pts) == 288, len(pts)

    # off-cycle RTD stamps: dropped, except one a few seconds late for a boundary with no on-time row
    text = _b("nyiso_rt_20261104_capitl_nyc.csv").decode()
    on_time = '"11/04/2026 08:10:00","CAPITL",61757,'
    assert on_time in text
    late = text.replace(on_time, '"11/04/2026 08:10:03","CAPITL",61757,')
    late_val = float(text.split(on_time)[1].split(",")[0])
    _, d = _days(rt, late.encode(), date(2026, 11, 4), date(2026, 11, 4))
    assert d[("nyiso.rt.lmp.capitl", date(2026, 11, 4))][97] == late_val, "08:10:03 fills the 08:05-08:10 slot"
    gone = text.replace(on_time, '"11/04/2026 08:11:30","CAPITL",61757,')
    _, d = _days(rt, gone.encode(), date(2026, 11, 4), date(2026, 11, 4))
    assert d[("nyiso.rt.lmp.capitl", date(2026, 11, 4))][97] is None, "a stamp 90 s late is not snapped"

    # empty and malformed
    hdr = _b("nyiso_da_20261103.csv").splitlines(keepends=True)[0]
    f = Fetched("u", 200, 1, hdr)
    assert da.parse(f) == {} and f.empty
    f = Fetched("u", 200, 1, _zip({"20251101damlbmp_zone.csv": hdr}))
    assert da.parse(f) == {} and f.empty
    _raises(lambda: da.parse(Fetched("u", 200, 1, b"<html><body>Not Found</body></html>")), "not a zonal LBMP CSV")
    _raises(lambda: da.parse(Fetched("u", 200, 1, b"Time Stamp,Name,PTID\n11/03/2026 00:00,CAPITL,1\n")), "lack")
    _raises(lambda: rt.parse(Fetched("u", 200, 1, _zip({"readme.txt": b"x"}))), "no CSV")
    _raises(lambda: da.parse(Fetched("u", 200, 1, hdr + b"yesterday,CAPITL,61757,1,0,0\n")), "bad time stamp")
    print("test_parse_nyiso ok")


# --------------------------------------------------------------------------- MISO parsing

def test_parse_miso():
    da, rt = miso.DATASETS
    f, d = _days(da, "miso_da_20261103_hubs.csv", date(2026, 11, 3), date(2026, 11, 3))
    assert not f.empty and len(d) == 8 and all(len(v) == 24 and None not in v for v in d.values())
    ark = d[("miso.da.lmp.arkansas_hub", date(2026, 11, 3))]
    assert ark[:3] == [13.82, 13.31, 11.82], "LMP rows only (not MCC / MLC)"
    pts = da.parse(Fetched("u", 200, 1, _b("miso_da_20261103_hubs.csv")))
    assert min(pts["miso.da.lmp.minn_hub"]) == datetime(2026, 11, 3, 5, tzinfo=UTC), "HE 1 = 00:00-01:00 EST"
    assert not any(k.startswith("miso.da.lmp.aeci") for k in pts), "non-hub nodes skipped"
    f, d = _days(rt, "miso_rt_20261031_hubs.csv", date(2026, 10, 31), date(2026, 10, 31))
    assert len(d) == 8 and d[("miso.rt.lmp.texas_hub", date(2026, 10, 31))][:2] == [19.57, 16.44]
    f, d = _days(da, "miso_da_202101_04_05_hubs.zip", date(2021, 1, 1), date(2021, 1, 31))
    assert len(d) == 16 and {day for _, day in d} == {date(2021, 1, 4), date(2021, 1, 5)}
    f, d = _days(rt, "miso_rt_202101_04_05_hubs.zip", date(2021, 1, 1), date(2021, 1, 31))
    assert len(d) == 16 and all(len(v) == 24 and None not in v for v in d.values()), "'1/5/2021' unpadded date"
    # a day file is dated by its own date line, so a zip member lands on its own day
    assert d[("miso.rt.lmp.ms_hub", date(2021, 1, 5))][0] == 19.04

    # empty and malformed
    lines = _b("miso_da_20261103_hubs.csv").decode().splitlines(keepends=True)
    no_hubs = "".join(lines[:8]).encode()
    f = Fetched("u", 200, 1, no_hubs)
    assert da.parse(f) == {} and f.empty
    _raises(lambda: rt.parse(Fetched("u", 200, 1, _b("miso_da_20261103_hubs.csv"))), "Realtime Market LMPs")
    _raises(lambda: da.parse(Fetched("u", 200, 1, b"<!DOCTYPE html><html>404</html>")), "expected")
    _raises(lambda: da.parse(Fetched("u", 200, 1, "".join([lines[0], "Nov 3\n"] + lines[2:]).encode())), "date line")
    _raises(lambda: da.parse(Fetched("u", 200, 1, "".join(lines[:4]).encode())), "header")
    bad_hdr = "".join(lines[:4] + ["Node,Type,Value,HE 1,HE 2\n"] + lines[5:]).encode()
    _raises(lambda: da.parse(Fetched("u", 200, 1, bad_hdr)), "unexpected header")
    _raises(lambda: da.parse(Fetched("u", 200, 1, _zip({"x.txt": b"x"}))), "no CSV")
    print("test_parse_miso ok")


# --------------------------------------------------------------------------- runner, end to end

class Files:
    """Serves fixtures by URL path; anything else is a 404 (as both sites answer a missing file)."""

    def __init__(self, routes: dict[str, bytes]):
        self.routes, self.seen = routes, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req.url.path)
        body = self.routes.get(req.url.path)
        if body is None:
            return httpx.Response(404, text="<html>Not Found</html>")
        return httpx.Response(200, content=body)


def _patched():
    for m in (nyiso, miso):
        m.DATASETS = tuple(dataclasses.replace(d, pace_seconds=0) for d in m.DATASETS)
        m._cache.clear()
    phttp.BACKOFF_SECONDS = (0, 0, 0)


def test_db_runner():
    import fixtures
    import normalize
    from sqlalchemy import text

    _patched()
    url = scratchdb.create(DB)
    real_today = nyiso.today
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session

        def run(mode, source, files, now, **kw):
            client = httpx.AsyncClient(transport=httpx.MockTransport(files))
            return asyncio.run(runner.run(mode, sources=[source], client=client, now=now, **kw))

        def ds_out(out, key):
            return next(d for d in out["sources"][0]["datasets"] if d["dataset"] == key)

        # ---------------- NYISO backfill from the monthly zips (2025-11 is not recent)
        nyiso.today = lambda: date(2025, 11, 20)
        now = datetime(2025, 11, 20, 18, tzinfo=UTC)
        files = Files({"/public/csv/damlbmp/20251101damlbmp_zone_csv.zip": _b("nyiso_da_202511_01_03.zip"),
                       "/public/csv/realtime/20251101realtime_zone_csv.zip": _b("nyiso_rt_202511_02.zip")})
        out = run("backfill", "nyiso", files, now, since=date(2025, 11, 1), until=date(2025, 11, 3))
        da, rt = ds_out(out, "nyiso.da_lmp"), ds_out(out, "nyiso.rt_lmp")
        assert da["chunks"] == 1 and da["new"] == 45 and da["failed"] == 0, da
        assert rt["new"] == 15 and rt["failed"] == 0, rt
        assert files.seen == ["/public/csv/damlbmp/20251101damlbmp_zone_csv.zip",
                              "/public/csv/realtime/20251101realtime_zone_csv.zip"], files.seen
        with Session() as s:
            n = dict(s.execute(text("""SELECT p.key, array_length(vals, 1) FROM power_days d JOIN power_series p
                                       ON p.id = d.series_id WHERE day = '2025-11-02'""")).all())
            assert n["nyiso.da.lmp.capitl"] == 25 and n["nyiso.rt.lmp.nyc"] == 300, n
            assert s.execute(text("SELECT count(DISTINCT visibility) FROM power_series WHERE source = 'nyiso'")
                             ).scalar() == 1
            assert s.execute(text("SELECT visibility FROM power_series WHERE source = 'nyiso' LIMIT 1")
                             ).scalar() == "internal"
            assert s.execute(text("SELECT count(*) FROM power_fetches WHERE body IS NOT NULL")).scalar() == 0
        # resume: DA is complete, nothing re-asked
        n0 = len(files.seen)
        out = run("backfill", "nyiso", files, now, since=date(2025, 11, 1), until=date(2025, 11, 3),
                  datasets=["nyiso.da_lmp"])
        assert ds_out(out, "nyiso.da_lmp")["chunks"] == 0 and len(files.seen) == n0
        # chunks that straddle a month: each month's zip is downloaded once, the second chunk reuses it
        nyiso.DATASETS = (dataclasses.replace(nyiso.DATASETS[0], chunk_days=2), nyiso.DATASETS[1])
        nyiso._cache.clear()
        files.seen.clear()
        out = run("backfill", "nyiso", files, now, since=date(2025, 10, 30), until=date(2025, 11, 5),
                  datasets=["nyiso.da_lmp"])
        d0 = ds_out(out, "nyiso.da_lmp")
        assert d0["chunks"] == 2 and d0["failed"] == 1, d0        # 10-30..31 (Oct zip: 404), 11-04..05 (Nov zip)
        assert files.seen.count("/public/csv/damlbmp/20251101damlbmp_zone_csv.zip") == 1, files.seen
        nyiso._cache.clear()
        files.seen.clear()
        run("backfill", "nyiso", files, now, since=date(2025, 10, 31), until=date(2025, 11, 6), datasets=["nyiso.da_lmp"])
        assert files.seen.count("/public/csv/damlbmp/20251101damlbmp_zone_csv.zip") == 1, files.seen
        nyiso.DATASETS = (dataclasses.replace(nyiso.DATASETS[0], chunk_days=31), nyiso.DATASETS[1])

        # ---------------- NYISO forward from the daily CSVs (recent days)
        nyiso.today = lambda: date(2026, 11, 5)
        now = datetime(2026, 11, 5, 18, tzinfo=UTC)          # 13:00 ET: DA through 11-06, RT through 11-04
        files = Files({"/public/csv/damlbmp/20261103damlbmp_zone.csv": _b("nyiso_da_20261103.csv"),
                       "/public/csv/realtime/20261104realtime_zone.csv": _b("nyiso_rt_20261104_capitl_nyc.csv")})
        out = run("forward", "nyiso", files, now, since=date(2026, 11, 3))
        da, rt = ds_out(out, "nyiso.da_lmp"), ds_out(out, "nyiso.rt_lmp")
        assert da["new"] == 15 and da["failed"] == 3, da       # 11-04 / 05 / 06 not served here: recorded failures
        assert rt["new"] == 2 and rt["failed"] == 1, rt        # 11-03 not served
        assert all(p.startswith("/public/csv/") and p.endswith(".csv") for p in files.seen), files.seen
        with Session() as s:
            kept = s.execute(text("""SELECT dataset, mode, encoding, bytes FROM power_fetches
                                     WHERE period_start >= '2026-11-01' AND ok""")).all()
            assert ("nyiso.da_lmp", "body", "gzip", 16850) in kept, kept
            assert all(m == "hash" for ds, m, _, _ in kept if ds == "nyiso.rt_lmp"), kept

        # ---------------- MISO backfill: monthly zip before 2023, daily CSVs after
        files = Files({"/marketreports/202101_da_expost_lmp_csv.zip": _b("miso_da_202101_04_05_hubs.zip"),
                       "/marketreports/202101_rt_lmp_final_csv.zip": _b("miso_rt_202101_04_05_hubs.zip"),
                       "/marketreports/20261103_da_expost_lmp.csv": _b("miso_da_20261103_hubs.csv"),
                       "/marketreports/20261031_rt_lmp_final.csv": _b("miso_rt_20261031_hubs.csv")})
        now = datetime(2026, 11, 10, 12, tzinfo=UTC)
        out = run("backfill", "miso", files, now, since=date(2021, 1, 1), until=date(2021, 1, 10))
        da, rt = ds_out(out, "miso.da_lmp"), ds_out(out, "miso.rt_lmp")
        assert da["chunks"] == 2 and da["new"] == 16 and da["failed"] == 0, da   # 01-01..07, 01-08..10
        assert rt["new"] == 16, rt
        assert files.seen.count("/marketreports/202101_da_expost_lmp_csv.zip") == 1, files.seen
        out = run("backfill", "miso", files, now, since=date(2026, 11, 2), until=date(2026, 11, 3))
        da = ds_out(out, "miso.da_lmp")
        assert da["new"] == 8 and da["failed"] == 1 and da["fetches"] == 2, da   # one request per day; 11-02 404
        assert "/marketreports/20261102_da_expost_lmp.csv" in files.seen
        out = run("backfill", "miso", files, now, since=date(2026, 10, 30), until=date(2026, 10, 31),
                  datasets=["miso.rt_lmp"])
        assert ds_out(out, "miso.rt_lmp")["new"] == 8
        with Session() as s:
            assert s.execute(text("""SELECT count(*) FROM power_days d JOIN power_series p ON p.id = d.series_id
                                     WHERE p.source = 'miso' AND NOT d.complete""")).scalar() == 0
            assert s.execute(text("SELECT count(*) FROM power_fetches WHERE dataset LIKE 'miso.%' AND body IS NOT NULL")
                             ).scalar() == 0, "MISO never keeps bytes"
            v = s.execute(text("""SELECT vals[1] FROM power_days d JOIN power_series p ON p.id = d.series_id
                                  WHERE p.key = 'miso.da.lmp.arkansas_hub' AND day = '2026-11-03'""")).scalar()
            assert abs(v - 13.82) < 1e-6
        # bytes per series-day as stored (real[] of 24 / 288 values)
        st = {d["dataset"]: d for d in runner.status()["datasets"]}
        for k in ("nyiso.da_lmp", "nyiso.rt_lmp", "miso.da_lmp", "miso.rt_lmp"):
            d = st[k]
            print(f"  {k}: {d['series_days']} series-days, vals {d['vals_bytes'] / max(d['series_days'], 1):.0f} B each")
        assert st["nyiso.rt_lmp"]["vals_bytes"] / st["nyiso.rt_lmp"]["series_days"] < 1300
    finally:
        nyiso.today = real_today
        scratchdb.drop(DB)
    print("test_db_runner ok")


if __name__ == "__main__":
    test_registry()
    test_parse_nyiso()
    test_parse_miso()
    test_db_runner()
    print("all passed")
