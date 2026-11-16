"""Nodal daily summaries (power/nodal.py; ERCOT and SPP NODAL_DATASETS; migration 0047): parsing every node over
REAL saved answers (trimmed), the daily summary computed by hand for one node, the DST days, the node catalogue,
idempotent and resumable runs on a scratch DB, the ERCOT document reuse after the hub run, the ranking read
(401 anonymous, 403 viewer, internal for an admin) and the migration up and down. No network: MockTransport.

Fixtures (captured 2026-11-15; each a REAL file trimmed to a handful of nodes, rows otherwise as received):
    ercot_nodal_np6905_2026-11-15_2200_trim.zip  NP6-905 one 15-minute interval (HE22 interval 4), 8 settlement
                                      points incl. an LZEW row (skipped); the tests re-stamp it per interval
    ercot_nodal_np4190_2026-11-01_trim.zip  NP4-190 DAM prices for 2026-11-01 (DST end: 25 hours, HE02 twice),
                                      the same 7 points
    ercot_nodal_splist_trim.zip       NP4-160-SG Settlement Points List (posted 2026-11-11): Settlement_Points csv
                                      trimmed to the buses of those points, plus the hub list
    ercot_list_10008.json             the MIS document list of report 10008 (4 documents, as received)
    spp_nodal_rt_2026-11-01_trim.csv  SPP RTBM by settlement location, 2026-11-01 (DST end: 300 intervals), 5
                                      locations
    spp_nodal_rt_2026-03-08_trim.csv  the same for 2026-03-08 (DST start: 276 intervals)
    spp_nodal_da_2026-11-08_trim.csv  SPP DA by settlement location, 2026-11-08, the same 5 locations

Run:  .venv/bin/python tests/test_power_nodal.py
"""

import asyncio
import base64
import dataclasses
import io
import json
import os
import statistics
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
from power import nodal, registry, runner, storage  # noqa: E402
from power.registry import Fetched  # noqa: E402
from power.sources import ercot, spp  # noqa: E402

FX = ROOT / "tests" / "fixtures_power"
DB = "og_test_power_nodal"
UTC = timezone.utc
ERT, EDA = ercot.NODAL_DATASETS
SRT, SDA = spp.NODAL_DATASETS
NP6905 = "ercot_nodal_np6905_2026-11-15_2200_trim.zip"
NP4190 = "ercot_nodal_np4190_2026-11-01_trim.zip"
ERCOT_POINTS = {"AMOCOOIL_CC1": "LCCRN", "AMO_AMOCO_1": "PCCRN", "HB_HUBAVG": "AH", "HB_WEST": "HU", "LZ_WEST": "LZ",
                "SHANNONW_RN": "RN", "YNG_WND_ALL": "RN"}
SPP_LOCS = {"SPPSOUTH_HUB", "WAUE.BEPM.LRSE", "CSWARSENALHILL5", "AEC", "AZPS"}


def _b(name):
    return (FX / name).read_bytes()


def _f(body, url="u"):
    return Fetched(url, 200, 1, body)


def _raises(fn, needle=""):
    try:
        fn()
    except ValueError as exc:
        assert needle in str(exc), exc
        return
    raise AssertionError("expected ValueError")


def _np6905(d: date, hour: int, interval: int, flag: str = "N", bump: float = 0.0) -> bytes:
    """The real trimmed NP6-905 interval file re-stamped as (d, hour, interval): same rows, prices + bump."""
    with zipfile.ZipFile(io.BytesIO(_b(NP6905))) as z:
        name = z.namelist()[0]
        lines = z.read(name).decode().splitlines()
    out = [lines[0]]
    for ln in lines[1:]:
        c = ln.split(",")
        c[0], c[1], c[2], c[6] = f"{d:%m/%d/%Y}", str(hour), str(interval), flag
        c[5] = f"{float(c[5]) + bump:.2f}"
        out.append(",".join(c))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, "\r\n".join(out) + "\r\n")
    return buf.getvalue()


def _rt_day_files(d: date):
    """[(friendly name, publish time, body)] for every 15-minute file of market day d (92 / 96 / 100)."""
    t = datetime.combine(d, datetime.min.time()).replace(tzinfo=ercot.CT)
    out = []
    for i in range(1, storage.slots(d, ercot.TZ, 15) + 1):
        start = (t.astimezone(UTC) + timedelta(minutes=15 * (i - 1))).astimezone(ercot.CT)
        end = (t.astimezone(UTC) + timedelta(minutes=15 * i)).astimezone(ercot.CT)
        out.append((f"SPPHLZNP6905_{end:%Y%m%d_%H%M}_csv", end,
                    _np6905(d, start.hour + 1, start.minute // 15 + 1, "Y" if start.fold else "N", bump=i)))
    return out


# --------------------------------------------------------------------------- registry and summary

def test_registry():
    keys = [d.key for d in registry.nodal_datasets()]
    assert keys == ["spp.rt_nodal", "spp.da_nodal", "ercot.rt_nodal", "ercot.da_nodal"], keys
    for src, v in registry.NODAL_VERDICTS.items():
        assert v.same_terms_as_hub and v.visibility == registry.visibility(src), src
        assert v.checked == "2026-11-15" and v.reason
    assert registry.nodal_visibility("ercot") == "public" and registry.nodal_visibility("spp") == "internal"
    assert registry.nodal_visibility("caiso") == "internal" and registry.nodal_visibility("nope") == "internal"
    lst = {r["source"]: r for r in registry.as_list()}
    assert lst["ercot"]["nodal"]["visibility"] == "public" and len(lst["spp"]["nodal"]["datasets"]) == 2
    assert "nodal" not in lst["caiso"]
    assert (ERT.interval_minutes, EDA.interval_minutes, SRT.interval_minutes, SDA.interval_minutes) == (15, 60, 5, 60)
    now = datetime(2026, 11, 16, 4, tzinfo=UTC)            # 22:00 CT on 11-15
    assert ERT.earliest(now) == date(2026, 11, 9) and EDA.earliest(now) == date(2026, 10, 17)
    assert ERT.latest(now) == date(2026, 11, 14) and EDA.latest(now) == date(2026, 11, 16)
    print("test_registry ok")


def test_summary_by_hand():
    s = nodal.summarize([5.0, -2.0, 30.0, 19.99, 20.0, None, float("nan")])
    # sorted: -2, 5, 19.99, 20, 30 -> n 5, below 0: 1, below 20: 3 (20.0 is not below 20)
    assert (s["n"], s["n_neg"], s["n_lt20"]) == (5, 1, 3), s
    assert abs(s["mean"] - 72.99 / 5) < 1e-9 and s["min"] == -2.0 and s["max"] == 30.0
    assert s["p50"] == 19.99
    assert abs(s["p10"] - (-2.0 + 0.4 * 7.0)) < 1e-9, "p10 at rank 0.4: -2 + 0.4 x (5 - -2)"
    assert nodal.summarize([]) is None and nodal.summarize([None]) is None
    one = nodal.summarize([7.5])
    assert one["p10"] == one["p50"] == one["mean"] == 7.5 and one["n_lt20"] == 1
    # an even count: the median is the mean of the middle two, as percentile_cont gives
    assert nodal.summarize([1.0, 2.0, 3.0, 4.0])["p50"] == 2.5
    print("test_summary_by_hand ok")


# --------------------------------------------------------------------------- SPP

def test_spp_parse():
    p = SRT.parse(_f(_b("spp_nodal_rt_2026-11-01_trim.csv")))
    assert set(p.prices) == SPP_LOCS
    s = nodal.summaries(SRT, p, date(2026, 11, 1))
    assert {k: v["n"] for k, v in s.items()} == {k: 300 for k in SPP_LOCS}, "DST end: 300 five-minute intervals"
    # WAUE.BEPM.LRSE on 2026-11-01, by hand (the 300 LMPs sorted with `sort -g`, counted and averaged with awk):
    # 42 below $0, 280 below $20, mean 13.620852, min -29.1357, max 328.6544, median (150th + 151st) / 2 = 15.7999,
    # p10 at rank 29.9 = x30 + 0.9 x (x31 - x30) = -3.768610
    lrse = s["WAUE.BEPM.LRSE"]
    assert (lrse["n"], lrse["n_neg"], lrse["n_lt20"]) == (300, 42, 280), lrse
    assert abs(lrse["mean"] - 13.620852) < 1e-6 and lrse["min"] == -29.1357 and lrse["max"] == 328.6544, lrse
    assert abs(lrse["p50"] - 15.7999) < 1e-9 and abs(lrse["p10"] - (-3.768610)) < 1e-6, lrse
    vals = sorted(nodal.day_values(SRT, p.prices["WAUE.BEPM.LRSE"], date(2026, 11, 1)))
    assert abs(statistics.median(vals) - lrse["p50"]) < 1e-9 and abs(statistics.fmean(vals) - lrse["mean"]) < 1e-9
    # interval ENDING 00:05 CDT (GMT 05:05) is the day's first interval, starting at local midnight
    assert min(p.prices["WAUE.BEPM.LRSE"]) == storage.day_start(date(2026, 11, 1), SRT.tz)
    assert p.prices["WAUE.BEPM.LRSE"][datetime(2026, 11, 1, 5, tzinfo=UTC)] == 16.5476
    assert p.nodes["SPPSOUTH_HUB"] == {"node_type": "hub", "zone": "SPP", "meta": {"pnode": "SPPSOUTH_H"}}
    assert p.nodes["WAUE.BEPM.LRSE"]["meta"] == {"pnode": "WAUELARAMIEUNBEPM_RA"}
    assert p.nodes["AZPS"]["zone"] == "SWPW" and p.nodes["AEC"]["node_type"] == "settlement_location"
    # DST start: 276 intervals; that year's file has no BAA column (and AZPS was not yet in SPP's market)
    p = SRT.parse(_f(_b("spp_nodal_rt_2026-03-08_trim.csv")))
    s = nodal.summaries(SRT, p, date(2026, 3, 8))
    assert set(s) == SPP_LOCS - {"AZPS"} and {v["n"] for v in s.values()} == {276}
    assert p.nodes["AEC"]["zone"] is None
    p = SDA.parse(_f(_b("spp_nodal_da_2026-11-08_trim.csv")))
    s = nodal.summaries(SDA, p, date(2026, 11, 8))
    assert set(s) == SPP_LOCS and {v["n"] for v in s.values()} == {24}
    assert nodal.summaries(SDA, p, date(2026, 11, 7)) == {}, "another day's summary is empty"
    # empty and malformed
    f = _f(b"Interval,GMTIntervalEnd,BAA,Settlement Location,Pnode,LMP,MLC,MCC,MEC\n")
    assert SDA.parse(f).prices == {} and f.empty
    _raises(lambda: SDA.parse(_f(b"<html>maintenance</html>")), "not a CSV")
    _raises(lambda: SDA.parse(_f(b"")), "empty file")
    _raises(lambda: SDA.parse(_f(b"Interval,GMTIntervalEnd,Settlement Location,LMP\n1,2\n")), "short row")
    _raises(lambda: SDA.parse(_f(b"Interval,GMTIntervalEnd,Settlement Location,LMP\nx,bad,AEC,1\n")), "bad row")
    print("test_spp_parse ok")


# --------------------------------------------------------------------------- ERCOT

def _merge(ds, bodies):
    out = registry.NodalParsed()
    for b in bodies:
        p = ds.parse(_f(b))
        for k, v in p.prices.items():
            out.prices.setdefault(k, {}).update(v)
        for k, v in p.nodes.items():
            out.nodes.setdefault(k, v)
    return out


def test_ercot_parse():
    p = ERT.parse(_f(_b(NP6905)))
    t = datetime(2026, 11, 16, 3, 45, tzinfo=UTC)          # HE22 interval 4 = 21:45 CST
    assert {k: v["node_type"] for k, v in p.nodes.items()} == ERCOT_POINTS, "LZEW skipped, types as stated"
    assert p.prices["LZ_WEST"] == {t: 46.18} and p.prices["HB_WEST"] == {t: 45.89}
    # a whole day re-stamped from the real file: interval i carries price + i
    for d, n in ((date(2026, 11, 1), 100), (date(2026, 3, 8), 92), (date(2026, 11, 2), 96)):
        s = nodal.summaries(ERT, _merge(ERT, [b for _, _, b in _rt_day_files(d)]), d)
        assert set(s) == set(ERCOT_POINTS) and {v["n"] for v in s.values()} == {n}, (d, n)
        w = s["HB_WEST"]
        assert abs(w["mean"] - (45.89 + (n + 1) / 2)) < 1e-6 and abs(w["min"] - 46.89) < 1e-6
        assert abs(w["max"] - (45.89 + n)) < 1e-6 and w["n_lt20"] == 0
    # DAM on the fall-back day: 25 hours per point, HE02 twice
    p = EDA.parse(_f(_b(NP4190)))
    s = nodal.summaries(EDA, p, date(2026, 11, 1))
    assert set(s) == set(ERCOT_POINTS) and {v["n"] for v in s.values()} == {25}
    assert all(v.get("node_type") is None for v in p.nodes.values()), "NP4-190 states no type"
    # the Settlement Points List: zone, substation, voltage for resource nodes; load zones; never coordinates
    nl = ercot.parse_node_list(_b("ercot_nodal_splist_trim.zip"))
    assert nl["SHANNONW_RN"] == {"zone": "LZ_WEST", "substation": "COBSW",
                                 "meta": {"electrical_bus": "COBSW_BB2", "voltage_kv": 345.0}}
    assert nl["AMO_AMOCO_1"]["zone"] == "LZ_HOUSTON" and nl["LZ_WEST"]["zone"] == "LZ_WEST"
    assert not any(k in v for v in nl.values() for k in ("latitude", "longitude", "county"))
    _raises(lambda: ercot.parse_node_list(b"<html>"), "not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x/Other.csv", "A,B\n")
    _raises(lambda: ercot.parse_node_list(buf.getvalue()), "no Settlement_Points")
    _raises(lambda: ERT.parse(_f(b"<html>")), "not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "hello\n")
    _raises(lambda: ERT.parse(_f(buf.getvalue())), "no header")
    print("test_ercot_parse ok")


# --------------------------------------------------------------------------- end to end

def _patched():
    for m in (spp, ercot):
        m.NODAL_DATASETS = tuple(dataclasses.replace(d, pace_seconds=0) for d in m.NODAL_DATASETS)
        m.DATASETS = tuple(dataclasses.replace(d, pace_seconds=0) for d in m.DATASETS)
    phttp.BACKOFF_SECONDS = (0, 0, 0)
    spp._INDEX.clear()
    ercot._DOCS.clear()
    ercot._ARCHIVE.clear()


class ErcotMis:
    """The MIS document lists and documents: synthetic NP6-905 lists for the given days (re-stamped real files),
    one NP4-190 document for 2026-11-01, the real report-10008 list with the trimmed points list as its newest."""

    def __init__(self, rt_days, drop: dict | None = None):
        self.seen, self.fail = [], set()
        self.docs, lst = {}, []
        for d in rt_days:
            files = _rt_day_files(d)
            for i, (name, end, body) in enumerate(files):
                if i in (drop or {}).get(d, ()):
                    continue
                doc = f"9{d:%m%d}{i:04d}"
                self.docs[doc] = body
                lst.append({"Document": {"DocID": doc, "FriendlyName": name, "PublishDate": end.isoformat()}})
        self.rt_list = json.dumps({"ListDocsByRptTypeRes": {"DocumentList": lst}}).encode()
        self.docs["7000001"] = _b(NP4190)
        self.da_list = json.dumps({"ListDocsByRptTypeRes": {"DocumentList": [{"Document": {
            "DocID": "7000001", "FriendlyName": "DAMSPNP4190_csv", "PublishDate": "2026-10-31T12:33:00-05:00"}}]}}).encode()
        newest = sorted(ercot.documents(_b("ercot_list_10008.json")), key=ercot._published)[-1]["DocID"]
        self.docs[newest] = _b("ercot_nodal_splist_trim.zip")
        self.splist = newest

    def downloads(self, doc=None):
        return sum(1 for q in self.seen if "doclookupId" in q and (doc is None or q["doclookupId"] == doc))

    def __call__(self, req):
        q = dict(req.url.params)
        self.seen.append(q)
        if "reportTypeId" in q:
            body = {"12301": self.rt_list, "12331": self.da_list}.get(q["reportTypeId"])
            return httpx.Response(200, content=body if body else _b(f"ercot_list_{q['reportTypeId']}.json"))
        if q.get("doclookupId") in self.fail:
            return httpx.Response(500, text="busy")
        body = self.docs.get(q.get("doclookupId"))
        return httpx.Response(200, content=body) if body else httpx.Response(404)


class SppPortal:
    FILES = {"/2026/11/By_Day/RTBM-LMP-DAILY-SL-20261101.csv": "spp_nodal_rt_2026-11-01_trim.csv",
             "/2026/03/By_Day/RTBM-LMP-DAILY-SL-20260308.csv": "spp_nodal_rt_2026-03-08_trim.csv",
             "/2026/11/By_Day/DA-LMP-SL-202611080100.csv": "spp_nodal_da_2026-11-08_trim.csv"}

    def __init__(self):
        self.seen = []

    def __call__(self, req):
        path = req.url.params["path"]
        self.seen.append(path)
        return httpx.Response(200, content=_b(self.FILES[path])) if path in self.FILES else httpx.Response(404)


def _run(mode, handler, now, **kw):
    out = asyncio.run(nodal.run(mode, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), now=now, **kw))
    return {d["dataset"]: d for src in out["sources"] for d in src["datasets"]}, out


def _ct(y, m, d, h):
    return datetime(y, m, d, h, tzinfo=ercot.CT).astimezone(UTC)


def test_db_runs_and_reads():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    _patched()
    url = scratchdb.create(DB)
    saved = (config.settings.app_password, config.settings.public_pages, config.settings.viewer_user,
             config.settings.viewer_password, config.settings.environment)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session

        def rows(where="TRUE"):
            with Session() as s:
                return {(r.iso, r.market, r.node, r.day): r for r in s.execute(text(f"""
                    SELECT n.iso, d.market, n.node, d.day, d.n, d.n_neg, d.n_lt20, d.mean, d.p10, d.p50, d.min, d.max
                    FROM power_node_days d JOIN power_nodes n ON n.id = d.node_id WHERE {where}"""))}

        # ---- ERCOT: 2026-11-01 (DST, 100 files) and 11-02 read from re-stamped real interval files
        now = _ct(2026, 11, 3, 20)
        mis = ErcotMis([date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)],
                       drop={date(2026, 10, 31): {5}})
        dry, _ = _run("backfill", mis, now, sources=["ercot"], since=date(2026, 11, 1), until=date(2026, 11, 2),
                      dry_run=True)
        assert dry["ercot.rt_nodal"]["days_missing"] == 2 and not mis.seen, "a dry run asks nothing"
        r, out = _run("backfill", mis, now, sources=["ercot"], since=date(2026, 11, 1), until=date(2026, 11, 2))
        assert r["ercot.rt_nodal"]["new"] == 14 and r["ercot.rt_nodal"]["failed"] == 0, r
        assert r["ercot.rt_nodal"]["fetches"] == 196, "100 + 96 interval files"
        assert r["ercot.da_nodal"]["new"] == 7 and r["ercot.da_nodal"]["days_missing"] == 2, r   # 11-02 not posted
        assert out["sources"][0]["catalogue"]["node_list"] == "read" and mis.downloads(mis.splist) == 1
        got = rows()
        w = got[("ERCOT", "rt", "HB_WEST", date(2026, 11, 1))]
        assert w.n == 100 and abs(w.mean - (45.89 + 50.5)) < 1e-3 and abs(w.max - 145.89) < 1e-3
        assert got[("ERCOT", "da", "LZ_WEST", date(2026, 11, 1))].n == 25
        with Session() as s:
            cat = {r.node: r for r in s.execute(text("SELECT * FROM power_nodes WHERE iso = 'ERCOT'"))}
            assert cat["SHANNONW_RN"].node_type == "RN" and cat["SHANNONW_RN"].zone == "LZ_WEST"
            assert cat["SHANNONW_RN"].substation == "COBSW" and cat["SHANNONW_RN"].meta["voltage_kv"] == 345.0
            assert cat["SHANNONW_RN"].first_day == date(2026, 11, 1) and cat["SHANNONW_RN"].last_day == date(2026, 11, 2)
            assert cat["LZ_SOUTH"].first_day is None, "a zone from the list only: never priced here"
            assert all(c.latitude is None and c.longitude is None and c.county is None for c in cat.values())
            modes = s.execute(text("SELECT DISTINCT mode, body IS NULL FROM power_fetches")).all()
            assert modes == [("hash", True)], "url + sha256 only, bodies never kept"
        # idempotent: a re-run asks for no document and changes nothing (the list of report 10008 is re-read)
        n = mis.downloads()
        r, out = _run("backfill", mis, now, sources=["ercot"], since=date(2026, 11, 1), until=date(2026, 11, 2))
        assert mis.downloads() == n and r["ercot.rt_nodal"]["days_missing"] == 0, r
        assert out["sources"][0]["catalogue"] == {"node_list": "unchanged"}
        assert rows() == got
        # an RT day missing a file: left while it may still come, read as it is once a day old
        early = _ct(2026, 11, 1, 20)
        r, _ = _run("backfill", mis, early, sources=["ercot"], since=date(2026, 10, 31), until=date(2026, 10, 31),
                    datasets=["ercot.rt_nodal"])
        assert r["ercot.rt_nodal"]["fetches"] == 0 and not rows("d.day = '2026-10-31'"), r
        r, _ = _run("backfill", mis, now, sources=["ercot"], since=date(2026, 10, 31), until=date(2026, 10, 31),
                    datasets=["ercot.rt_nodal"])
        assert r["ercot.rt_nodal"]["new"] == 7 and {x.n for x in rows("d.day = '2026-10-31'").values()} == {95}
        # a failed file: the day's summary is kept but the day is not done; the next run completes it (a revision)
        mis.fail = {next(k for k in mis.docs if k.startswith("91103"))}
        later = _ct(2026, 11, 4, 20)
        r, _ = _run("backfill", mis, later, sources=["ercot"], since=date(2026, 11, 3), until=date(2026, 11, 3),
                    datasets=["ercot.rt_nodal"])
        assert r["ercot.rt_nodal"]["failed"] == 1 and r["ercot.rt_nodal"]["new"] == 7, r
        assert {x.n for x in rows("d.day = '2026-11-03'").values()} == {95}
        mis.fail = set()
        r, _ = _run("backfill", mis, later, sources=["ercot"], since=date(2026, 11, 3), until=date(2026, 11, 3),
                    datasets=["ercot.rt_nodal"])
        assert r["ercot.rt_nodal"]["revised"] == 7 and {x.n for x in rows("d.day = '2026-11-03'").values()} == {96}
        # a partial re-read never replaces a fuller day
        with Session() as s:
            nid = s.execute(text("SELECT id FROM power_nodes WHERE node = 'HB_WEST'")).scalar()
        assert nodal.upsert_days("rt", date(2026, 11, 3), {nid: nodal.summarize([1.0])})["revised"] == 0

        # ---- the ERCOT documents the hub run read are reused, not downloaded again (three-downloads rule)
        ercot._DOCS.clear()
        mis2 = ErcotMis([date(2026, 11, 4)])
        hub = asyncio.run(runner.run("backfill", sources=["ercot"], datasets=["ercot.rt_spp"], now=_ct(2026, 11, 5, 20),
                                     since=date(2026, 11, 4), until=date(2026, 11, 4),
                                     client=httpx.AsyncClient(transport=httpx.MockTransport(mis2))))
        assert hub["sources"][0]["datasets"][0]["new"] == 3, hub      # HB_WEST, HB_HUBAVG, LZ_WEST
        n = mis2.downloads()
        assert n == 96
        r, _ = _run("forward", mis2, _ct(2026, 11, 5, 20), sources=["ercot"], datasets=["ercot.rt_nodal"])
        assert r["ercot.rt_nodal"]["new"] == 7 and mis2.downloads() == n, "read from memory"

        # ---- SPP: DST days from the day files; a day not posted is "not published", re-asked while recent
        portal = SppPortal()
        now_s = _ct(2026, 11, 8, 21)        # RT through 11-03, DA through 11-09
        r, _ = _run("backfill", portal, now_s, sources=["spp"], since=date(2026, 11, 1), until=date(2026, 11, 1),
                    datasets=["spp.rt_nodal"])
        assert r["spp.rt_nodal"]["new"] == 5 and portal.seen == ["/2026/11/By_Day/RTBM-LMP-DAILY-SL-20261101.csv"]
        r, _ = _run("backfill", portal, now_s, sources=["spp"], since=date(2026, 3, 8), until=date(2026, 3, 8),
                    datasets=["spp.rt_nodal"])
        assert r["spp.rt_nodal"]["new"] == 4, r
        r, _ = _run("backfill", portal, now_s, sources=["spp"], since=date(2026, 11, 7), until=date(2026, 11, 8),
                    datasets=["spp.da_nodal"])
        assert r["spp.da_nodal"]["new"] == 5 and r["spp.da_nodal"]["empty"] == 1, r
        p = nodal.plan(SDA, "backfill", now_s, date(2026, 11, 7), date(2026, 11, 8))
        assert p["missing"] == [date(2026, 11, 7)], "a recent 'not published' day is asked again"
        got = rows("n.iso = 'SPP'")
        lrse = got[("SPP", "rt", "WAUE.BEPM.LRSE", date(2026, 11, 1))]
        assert (lrse.n, lrse.n_neg, lrse.n_lt20) == (300, 42, 280) and abs(lrse.p10 - (-3.76861)) < 1e-4
        assert got[("SPP", "rt", "AEC", date(2026, 3, 8))].n == 276
        # forward: SPP DA re-reads its latest day (overlap 1); an unchanged answer is counted unchanged
        portal.seen.clear()
        r, _ = _run("forward", portal, _ct(2026, 11, 7, 16), sources=["spp"], datasets=["spp.da_nodal"])
        assert r["spp.da_nodal"]["unchanged"] == 5 and r["spp.da_nodal"]["new"] == 0, r
        assert r["spp.da_nodal"]["days_missing"] == 2 and r["spp.da_nodal"]["empty"] == 1, "11-07 still unposted"

        # ---- status and the ranking
        st = nodal.status()
        dd = {d["dataset"]: d for d in st["datasets"]}
        assert dd["spp.rt_nodal"]["rows"] == 9 and dd["ercot.rt_nodal"]["nodes"] == 7 and st["total_bytes"] > 0
        cheap = nodal.cheap_nodes(1, until=date(2026, 11, 1), min_days=1)
        assert cheap[0]["node"] == "WAUE.BEPM.LRSE" and cheap[0]["market"] == "spp", cheap[:2]
        top = cheap[0]
        assert top["share_below_20"] == round(280 / 300, 4) and top["share_below_0"] == 0.14
        assert top["hours_below_0"] == 3.5 and top["median"] == 15.8 and top["days"] == 1
        assert top["latitude"] is None and top["visibility"] == "internal" and top["meta"]["pnode"]
        assert {r["market"] for r in cheap} == {"spp", "ercot"}
        assert [r["node"] for r in nodal.cheap_nodes(1, "spp", until=date(2026, 11, 1), min_days=1,
                                                     zone="SWPW")] == ["AZPS"]
        assert [r["node"] for r in nodal.cheap_nodes(1, "spp", until=date(2026, 11, 1), min_days=1,
                                                     node_type="hub")] == ["SPPSOUTH_HUB"]
        er = nodal.cheap_nodes(30, "ercot", min_days=1, until=date(2026, 11, 3))
        assert {r["node"] for r in er} == set(ERCOT_POINTS) and er[0]["visibility"] == "public"
        assert all(r["days"] == 4 for r in er), "10-31 .. 11-03"
        assert nodal.cheap_nodes(30, "ercot", min_days=5, until=date(2026, 11, 3)) == [], "min_days"
        da = nodal.cheap_nodes(30, "ercot", series="da", min_days=1, until=date(2026, 11, 1))
        assert len(da) == 7 and all(r["intervals"] == 25 for r in da)
        assert nodal.cheap_nodes(400, "spp", min_days=1)[0]["window"]["days"] == 365, "window capped at 365"

        # ---- the ops read: 401 anonymous, 403 viewer, internal for an admin
        from fastapi.testclient import TestClient

        import main
        from accounts import security

        security.reset()
        config.settings.app_password, config.settings.environment = "pw", "development"
        config.settings.viewer_user, config.settings.viewer_password = "vc", "look-only"
        config.settings.public_pages = True

        def basic(u, p):
            return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}

        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        path = "/v1/ops/power/nodes/cheap?days=1&until=2026-11-01&min_days=1"
        assert client.get(path).status_code == 401
        assert client.get(path, headers=basic("vc", "look-only")).status_code == 403
        r = client.get(path, headers=basic("opengrid", "pw"))
        assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store", r.text[:300]
        body = r.json()
        assert body["meta"]["visibility"] == "internal" and body["meta"]["kind"] == "observed"
        assert body["data"][0]["node"] == "WAUE.BEPM.LRSE" and "median of the daily medians" in body["meta"]["note"]
        r = client.get(path + "&market=spp&series=rt&zone=SWPW", headers=basic("opengrid", "pw"))
        assert [x["node"] for x in r.json()["data"]] == ["AZPS"]
        for bad in ("days=366", "market=pjm", "series=hourly", "days=0", "limit=0"):
            assert client.get(f"/v1/ops/power/nodes/cheap?{bad}", headers=basic("opengrid", "pw")).status_code == 422, bad
        assert "/v1/ops/power/nodes/cheap" not in json.dumps(main.app.openapi()["paths"]), "not in the public schema"
        with Session() as s:
            for t in ("compute_listings", "market_hourly", "archived_prices", "power_days"):
                if t == "power_days":
                    assert s.execute(text("""SELECT count(*) FROM power_days d JOIN power_series p ON p.id = d.series_id
                                             WHERE p.location NOT IN ('HB_WEST', 'HB_HUBAVG', 'LZ_WEST')""")).scalar() == 0
                else:
                    assert s.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t
    finally:
        (config.settings.app_password, config.settings.public_pages, config.settings.viewer_user,
         config.settings.viewer_password, config.settings.environment) = saved
        scratchdb.drop(DB)
    print("test_db_runs_and_reads ok")


def test_migration_0047():
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    import db as dbmod
    from config import settings
    from store.power import PowerNode, PowerNodeDay

    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1                      # later migrations may follow
        assert script.get_revision("0047_power_nodal").down_revision == "0046_power_waste"
        command.upgrade(cfg, "0047_power_nodal")
        eng = create_engine(url)
        with eng.connect() as c:
            for model in (PowerNode, PowerNodeDay):
                t = model.__tablename__
                cols = {r[0]: r[1] for r in c.execute(text(
                    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = :t"), {"t": t})}
                assert set(cols) == {col.name for col in model.__table__.columns}, t
                assert {k for k, v in cols.items() if v == "YES"} == {
                    col.name for col in model.__table__.columns if col.nullable}, t
                pk = [r[0] for r in c.execute(text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND "
                    "a.attnum = ANY(i.indkey) WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary "
                    "ORDER BY array_position(i.indkey, a.attnum)"), {"t": t})]
                assert pk == [col.name for col in model.__table__.primary_key.columns], (t, pk)
        command.downgrade(cfg, "0046_power_waste")
        with eng.connect() as c:
            for t in ("power_nodes", "power_node_days"):
                assert c.execute(text("SELECT to_regclass(:t)"), {"t": "public." + t}).scalar() is None
            assert c.execute(text("SELECT to_regclass('public.power_days')")).scalar() is not None
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.power_node_days')")).scalar() is not None
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)
    print("test_migration_0047 ok")


if __name__ == "__main__":
    test_registry()
    test_summary_by_hand()
    test_spp_parse()
    test_ercot_parse()
    test_db_runs_and_reads()
    test_migration_0047()
    print("all passed")
