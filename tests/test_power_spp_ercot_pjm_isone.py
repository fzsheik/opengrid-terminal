"""Power sources SPP, ERCOT, PJM and ISO-NE (power/sources/{spp,ercot,pjm,isone}.py): parsing over REAL saved
answers (trimmed), the DST days, series keys, empty and malformed answers, REQUIRES gating (off without
credentials; credentials never in stored URLs or errors), the SPP yearly-zip Range reads (zip64 too), ERCOT's
archive / daily routing, and an end-to-end runner backfill per source on a scratch DB. No network: MockTransport.

Fixtures (captured 2026-11-05; trimmed to the kept locations plus a few other rows):
    spp_da_2026-11-01_hubs.csv        SPP DA by settlement location, 2026-11-01 (DST end: 25 hours; re-posted file
                                      with "11/1/2026 1:00" times)
    spp_da_2026-11-03_hubs.csv        SPP DA, 2026-11-03 ("11/03/2026 01:00:00" times)
    spp_rt_2025-11-02_hubs.csv        SPP RTBM daily, 2025-11-02 (DST end: 300 five-minute intervals)
    ercot_dam_hist_2025_trim.zip      ERCOT 13060 DAMLZHBSPP_2025 (xlsx in zip), rows of 03-09 (23 h), 11-01..03
    ercot_rtm_hist_2025_trim.zip      ERCOT 13061 RTMLZHBSPP_2025, rows of 11-02 (100 intervals, LZ and LZEW)
    ercot_np4190_2026-11-06.zip       ERCOT NP4-190 DAM settlement point prices for 2026-11-06
    ercot_np6905_2026-11-05_1845.zip  ERCOT NP6-905 one 15-minute file (the test re-stamps it per interval)
    ercot_list_1233{1}.json, ercot_list_1306{0,1}.json   MIS document lists (12331 trimmed to 4 documents)
    pjm_da_2026-11-01_western_hub_p1.json  a real da_hrl_lmps answer (3 of 25 rows), pjm_rt_... a real rt_hrl_lmps
                                      answer, pjm_da_2000-06-01_archived_hubs.json a real archived (type=HUB) answer
    isone_{da,rt}_2025-11-02_*.json   REAL ISO-NE prices for 2025-11-02 (public CSV reports) in the web services'
                                      documented HourlyLmps shape (no JSON example is published: unverified live)

Run:  .venv/bin/python tests/test_power_spp_ercot_pjm_isone.py
"""

import asyncio
import base64
import dataclasses
import io
import json
import os
import struct
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
from power.sources import ercot, isone, pjm, spp  # noqa: E402

FX = ROOT / "tests" / "fixtures_power"
DB = "og_test_power_b"
UTC = timezone.utc
PJM_KEY = "pjm-SECRET-key-0123"
ISO_USER, ISO_PASS = "ops@example.test", "isoSECRETpass99"


def _b(name):
    return (FX / name).read_bytes()


def _f(body, url="u"):
    return Fetched(url, 200, 1, body)


def _days(ds, f, a, b=None):
    return storage.to_days(ds, ds.parse(f), a, b or a)


def _raises(fn, needle=""):
    try:
        fn()
    except ValueError as exc:
        assert needle in str(exc), exc
        return
    raise AssertionError("expected ValueError")


# --------------------------------------------------------------------------- registry, keys, gating

def test_registry_keys_gating():
    import config

    keys = list(registry.series_index())
    assert len(keys) == len(set(keys)), "series keys are globally unique"
    for m in (spp, ercot, pjm, isone):
        for ds in m.DATASETS:
            assert ds.source == m.SOURCE and ds.key.startswith(m.SOURCE + ".")
            for s in ds.series:
                assert s.key == s.key.lower() and s.key.startswith(f"{m.SOURCE}.{s.market}.lmp."), s.key
                assert s.unit == "usd_per_mwh" and s.kind == "lmp"
        assert m.TERMS.checked == "2026-11-05" and m.TERMS.terms_url.startswith("https://")
    assert [len(d.series) for d in spp.DATASETS] == [2, 2]
    assert [len(d.series) for d in ercot.DATASETS] == [14, 14]
    assert [len(d.series) for d in pjm.DATASETS] == [35, 35]
    assert [len(d.series) for d in isone.DATASETS] == [9, 9]
    # the names power/geo.py matches day-ahead series by (iso + location)
    idx = registry.series_index()
    for key, iso, loc in (("spp.da.lmp.sppsouth_hub", "SPP", "SPPSOUTH_HUB"),
                          ("ercot.da.lmp.hb_north", "ERCOT", "HB_NORTH"),
                          ("ercot.da.lmp.hb_hubavg", "ERCOT", "HB_HUBAVG"),
                          ("pjm.da.lmp.dom", "PJM", "DOM"), ("pjm.da.lmp.comed", "PJM", "COMED"),
                          ("pjm.da.lmp.western_hub", "PJM", "WESTERN HUB"),
                          ("pjm.da.lmp.dominion_hub", "PJM", "DOMINION HUB"),
                          ("isone.da.lmp.h_internal_hub", "ISO-NE", ".H.INTERNAL_HUB")):
        s = idx[key][1]
        assert (s.iso, s.location, s.market) == (iso, loc, "da"), (key, s)
    # verdicts: ERCOT's terms allow redistributing raw public data; the others do not
    assert registry.visibility("ercot") == "public"
    assert {registry.visibility(s) for s in ("spp", "pjm", "isone")} == {"internal"}
    # gating: keyless sources on by default, key-gated ones off without their settings
    assert spp.DEFAULT_ON and ercot.DEFAULT_ON and not pjm.DEFAULT_ON and not isone.DEFAULT_ON
    assert pjm.REQUIRES == ("pjm_api_key",) and isone.REQUIRES == ("isone_username", "isone_password")
    saved = (config.settings.pjm_api_key, config.settings.isone_username, config.settings.isone_password,
             config.settings.power_sources)
    try:
        config.settings.pjm_api_key = config.settings.isone_username = config.settings.isone_password = None
        config.settings.power_sources = None
        sel = registry.selected()
        assert "spp" in sel and "ercot" in sel and "pjm" not in sel and "isone" not in sel, sel
        assert not registry.configured("pjm") and not registry.configured("isone")
        config.settings.isone_username = ISO_USER
        assert not registry.configured("isone"), "both settings are required"
        config.settings.power_sources = "pjm,isone,spp"
        assert registry.selected() == ["spp"], "named but unconfigured sources stay off"
        out = asyncio.run(runner.run("backfill", sources=["pjm", "isone"], dry_run=True,
                                     client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0))))
        assert out["sources"] == [] and {x["source"] for x in out["not_configured"]} == {"pjm", "isone"}
        assert all("https://" in x["signup"] for x in out["not_configured"])
        config.settings.pjm_api_key, config.settings.isone_password = PJM_KEY, ISO_PASS
        assert registry.configured("pjm") and registry.configured("isone")
        lst = {r["source"]: r for r in registry.as_list()}
        assert lst["pjm"]["signup"] and "apiportal.pjm.com" in lst["pjm"]["signup"]
        assert "iso-ne.com/signup" in lst["isone"]["signup"] and lst["spp"]["signup"] is None
        assert PJM_KEY not in json.dumps(lst, default=str) and ISO_PASS not in json.dumps(lst, default=str)
    finally:
        (config.settings.pjm_api_key, config.settings.isone_username, config.settings.isone_password,
         config.settings.power_sources) = saved
    print("test_registry_keys_gating ok")


# --------------------------------------------------------------------------- SPP

def _spp_zip(members: dict[str, bytes], pad: int = 5000) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("2020/", b"")
        z.writestr("2020/11/By_Interval/RTBM-LMP-SL-202011030005.csv", b"x" * pad)
        for n, data in members.items():
            z.writestr(n, data)
    return buf.getvalue()


def _ranged(body: bytes, rng: str) -> httpx.Response:
    spec = rng.split("=", 1)[1]
    a, _, b = spec.partition("-")
    if a == "":
        a, b = max(0, len(body) - int(b)), len(body) - 1
    else:
        a, b = int(a), min(int(b), len(body) - 1)
    return httpx.Response(206, content=body[a:b + 1], headers={"Content-Range": f"bytes {a}-{b}/{len(body)}"})


def _spp_2020_da() -> bytes:
    """The 2026-11-03 DA fixture re-dated to 2020-11-03 (same weekday offsets: DST ended Nov 1 in both years)."""
    return _b("spp_da_2026-11-03_hubs.csv").replace(b"/2026", b"/2020")


def test_spp_parse():
    da, rt = spp.DATASETS
    f = _f(_b("spp_da_2026-11-01_hubs.csv"))
    d = _days(da, f, date(2026, 11, 1))
    assert set(d) == {("spp.da.lmp.sppnorth_hub", date(2026, 11, 1)), ("spp.da.lmp.sppsouth_hub", date(2026, 11, 1))}
    v = d[("spp.da.lmp.sppnorth_hub", date(2026, 11, 1))]
    assert len(v) == 25 and None not in v, "DST end: 25 DA hours"
    # "11/1/2026 1:00" (HE01 CDT, GMT end 06:00) is slot 0; the two HE02 rows are slots 1 and 2 (GMT 07:00, 08:00)
    assert v[0] == 22.2121 and v[1] == 21.7039 and v[2] == 22.9337
    d = _days(da, _f(_b("spp_da_2026-11-03_hubs.csv")), date(2026, 11, 3))
    assert all(len(x) == 24 and None not in x for x in d.values()) and len(d) == 2
    f = _f(_b("spp_rt_2025-11-02_hubs.csv"))
    d = _days(rt, f, date(2025, 11, 2))
    v = d[("spp.rt.lmp.sppnorth_hub", date(2025, 11, 2))]
    assert len(v) == 300 and None not in v, "DST end: 300 RT intervals"
    assert v[0] == 18.1627 and v[-1] == -0.0238, "interval-ending 00:05 is slot 0; 24:00 is the last"
    # empty and malformed
    f = _f(b"Interval,GMTIntervalEnd,BAA,Settlement Location,Pnode,LMP,MLC,MCC,MEC\n11/03/2026 01:00:00,11/03/2026 "
           b"07:00:00,SPP,AEC,SOUC,25.0,0,0,0\n")
    assert da.parse(f) == {} and f.empty, "a file without the hubs is empty"
    _raises(lambda: da.parse(_f(b"<html>maintenance</html>")), "not a CSV")
    _raises(lambda: da.parse(_f(b"")), "empty file")
    _raises(lambda: da.parse(_f(b"Interval,Foo,Bar\n1,2,3\n")), "columns")
    _raises(lambda: da.parse(_f(b"Interval,GMTIntervalEnd,Settlement Location,LMP\nx,bad,SPPNORTH_HUB,1\n")), "bad row")
    # a zip member read by Range: local header + deflate, inflated in slices; a cut-short member fails
    name = "2020/11/By_Day/DA-LMP-SL-202011030100.csv"
    z = _spp_zip({name: _spp_2020_da()})
    off, size = spp.central_directory(z[-70000:])
    idx = spp.index_entries(z[off:off + size])
    assert set(idx) == {"DA-LMP-SL-202011030100.csv"}, "only By_Day members are indexed"
    o, cs, meth = idx["DA-LMP-SL-202011030100.csv"]
    member = z[o:o + 30 + cs + spp.SLACK_BYTES]
    d = _days(da, _f(member), date(2020, 11, 3))
    assert len(d) == 2 and all(len(x) == 24 and None not in x for x in d.values()) and meth == 8
    _raises(lambda: da.parse(_f(z[o:o + 30 + cs // 2])), "cut short")
    # zip64: a central entry whose offset overflows 32 bits carries it in the 0x0001 extra field
    nm = b"2021/03/By_Day/RTBM-LMP-DAILY-SL-20210314.csv"
    extra = struct.pack("<HHQ", 1, 8, 3778470707 + 2 ** 32)
    cd = (b"PK\x01\x02" + struct.pack("<HHHHHHIIIHHHHHII", 20, 45, 0, 8, 0, 0, 0, 5644885, 30154662, len(nm),
                                      len(extra), 0, 0, 0, 0, 0xFFFFFFFF) + nm + extra)
    assert spp.index_entries(cd) == {"RTBM-LMP-DAILY-SL-20210314.csv": (3778470707 + 2 ** 32, 5644885, 8)}
    tail = (b"junk" + b"PK\x06\x06" + struct.pack("<QHHIIQQQQ", 44, 45, 45, 0, 0, 107726, 107726, 13099851,
                                                  4602974868)
            + b"PK\x06\x07" + struct.pack("<IQI", 0, 0, 1)
            + b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, 0xFFFF, 0xFFFF, 13099851, 0xFFFFFFFF, 0))
    assert spp.central_directory(tail) == (4602974868, 13099851)
    _raises(lambda: spp.central_directory(b"no end record"), "end-of-central-directory")
    # depth and publication
    assert spp.archived(date(2024, 6, 1), date(2026, 2, 16)) and not spp.archived(date(2025, 6, 1), date(2026, 11, 5))
    assert spp.latest_da(datetime(2026, 11, 5, 21, tzinfo=UTC)) == date(2026, 11, 6)   # 15:00 CT
    assert spp.latest_da(datetime(2026, 11, 5, 18, tzinfo=UTC)) == date(2026, 11, 5)
    assert spp.latest_rt(datetime(2026, 11, 6, 1, tzinfo=UTC)) == date(2026, 10, 31)   # 19:00 CT on 11-05
    assert spp.day_url("rt", date(2025, 11, 2))[1]["path"] == "/2025/11/By_Day/RTBM-LMP-DAILY-SL-20251102.csv"
    print("test_spp_parse ok")


# --------------------------------------------------------------------------- ERCOT

def _np6905(d: date, hour: int, interval: int, flag: str = "N", bump: float = 0.0) -> bytes:
    """The real NP6-905 interval file re-stamped as (d, hour, interval): same rows, shifted prices."""
    with zipfile.ZipFile(io.BytesIO(_b("ercot_np6905_2026-11-05_1845.zip"))) as z:
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


def test_ercot_parse():
    da, rt = ercot.DATASETS
    f = _f(_b("ercot_dam_hist_2025_trim.zip"))
    d = _days(da, f, date(2025, 3, 9), date(2025, 11, 3))
    assert {k for k, _ in d} == {s.key for s in da.series}, "all 14 points from the archive"
    north = {day: v for (k, day), v in d.items() if k == "ercot.da.lmp.hb_north"}
    assert sorted(north) == [date(2025, 3, 9), date(2025, 11, 1), date(2025, 11, 2), date(2025, 11, 3)]
    assert len(north[date(2025, 3, 9)]) == 23 and None not in north[date(2025, 3, 9)], "spring: no HE03"
    v = north[date(2025, 11, 2)]
    assert len(v) == 25 and None not in v, "fall: HE02 twice"
    assert v[:4] == [54.43, 44.77, 46.18, 50.11], "HE02 N = 01:00 CDT, HE02 Y (repeated) = 01:00 CST"
    assert north[date(2025, 3, 9)][:3] == [29.31, 27.66, 26.71], "HE04 follows HE02 on the spring day"
    f = _f(_b("ercot_rtm_hist_2025_trim.zip"))
    d = _days(rt, f, date(2025, 11, 2))
    assert len(d) == 14 and all(len(x) == 100 and None not in x for x in d.values()), "100 intervals"
    v = d[("ercot.rt.lmp.hb_north", date(2025, 11, 2))]
    assert (v[0], v[3], v[4], v[7], v[8], v[11], v[12]) == (36.78, 35.86, 38.95, 28.18, 24.20, 25.98, 26.43)
    assert d[("ercot.rt.lmp.lz_aen", date(2025, 11, 2))][0] == 57.88, "type LZ kept, LZEW (57.86) skipped"
    f = _f(_b("ercot_np4190_2026-11-06.zip"))
    d = _days(da, f, date(2026, 11, 6))
    assert len(d) == 14 and all(len(x) == 24 and None not in x for x in d.values())
    assert d[("ercot.da.lmp.hb_busavg", date(2026, 11, 6))][0] == 40.81, "' 40.81' (leading space) read"
    f = _f(_b("ercot_np6905_2026-11-05_1845.zip"))
    pts = rt.parse(f)
    t = datetime(2026, 11, 6, 0, 30, tzinfo=UTC)            # HE19 interval 3 = 18:30 CST
    assert pts["ercot.rt.lmp.hb_north"] == {t: 44.01} and list(pts["ercot.rt.lmp.lz_houston"]) == [t]
    assert "ercot.rt.lmp.dc_e" not in pts and len(pts) == 14
    # the hour-ending convention, both DST days
    s = ercot.interval_start
    assert s(date(2025, 11, 2), 1, 1, 60, False) == datetime(2025, 11, 2, 5, tzinfo=UTC)
    assert s(date(2025, 11, 2), 2, 1, 60, True) == datetime(2025, 11, 2, 7, tzinfo=UTC)
    assert s(date(2025, 3, 9), 4, 1, 60, False) == datetime(2025, 3, 9, 8, tzinfo=UTC)
    assert s(date(2025, 3, 9), 24, 4, 15, False) == datetime(2025, 3, 10, 4, 45, tzinfo=UTC)
    # empty and malformed
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "DeliveryDate,HourEnding,SettlementPoint,SettlementPointPrice,DSTFlag\n"
                            "11/06/2026,01:00,7RNCHSLR_ALL, 46.91,N\n")
    f = _f(buf.getvalue())
    assert da.parse(f) == {} and f.empty
    _raises(lambda: da.parse(_f(b"<html>error</html>")), "not a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "hello\n")
    _raises(lambda: da.parse(_f(buf.getvalue())), "no header")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "DeliveryDate,HourEnding,SettlementPoint,SettlementPointPrice,DSTFlag\n"
                            "11/06/2026,xx,HB_NORTH, 1,N\n")
    _raises(lambda: da.parse(_f(buf.getvalue())), "bad row")
    _raises(lambda: ercot.documents(b"{}"), "not a document list")
    # archive coverage: a past year whole; the current year through the month before it was posted
    assert ercot.archive_covers("DAMLZHBSPP_2025", datetime.fromisoformat("2026-01-01T08:44:09-06:00")) == (
        date(2025, 1, 1), date(2025, 12, 31))
    assert ercot.archive_covers("DAMLZHBSPP_2026", datetime.fromisoformat("2026-11-01T08:02:55-06:00")) == (
        date(2026, 1, 1), date(2026, 10, 31))
    assert ercot.archive_covers("RTMLZHBSPP_2027", datetime.fromisoformat("2027-01-01T08:00:00-06:00")) is None
    docs = ercot.documents(_b("ercot_list_12331.json"))
    assert [x["DocID"] for x in ercot.daily_docs("da", date(2026, 11, 6), docs)] == ["1295106388"]
    assert ercot.daily_docs("da", date(2026, 11, 8), docs) == []
    print("test_ercot_parse ok")


# --------------------------------------------------------------------------- PJM

def _pjm_rows(market: str, day: date, pnodes: list[int]) -> list[dict]:
    """Rows in the real feed's shape (from pjm_da_2026-11-01_western_hub_p1.json) for every UTC hour of day."""
    tmpl = json.loads(_b("pjm_da_2026-11-01_western_hub_p1.json"))["items"][0]
    rows = []
    t, end = storage.day_start(day, pjm.TZ), storage.day_start(day + timedelta(days=1), pjm.TZ)
    i = 0
    while t < end:
        for p in pnodes:
            r = {k.replace("_da", f"_{market}"): v for k, v in tmpl.items()}
            r.update(datetime_beginning_utc=t.strftime("%Y-%m-%dT%H:%M:%S"), pnode_id=p,
                     pnode_name=pjm.PNODES[p][0], type=pjm.PNODES[p][1].upper())
            r[f"total_lmp_{market}"] = 30.0 + i
            rows.append(r)
        t += timedelta(hours=1)
        i += 1
    return rows


def test_pjm_parse():
    da, rt = pjm.DATASETS
    f = _f(_b("pjm_da_2026-11-01_western_hub_p1.json"))
    pts = da.parse(f)
    assert list(pts) == ["pjm.da.lmp.western_hub"] and len(pts["pjm.da.lmp.western_hub"]) == 3
    assert pts["pjm.da.lmp.western_hub"][datetime(2026, 11, 1, 4, tzinfo=UTC)] == 37.921396
    pts = rt.parse(_f(_b("pjm_rt_2026-11-01_western_hub.json")))
    assert pts["pjm.rt.lmp.western_hub"][datetime(2026, 11, 1, 4, tzinfo=UTC)] == 508.088371
    pts = da.parse(_f(_b("pjm_da_2000-06-01_archived_hubs.json")))
    assert pts["pjm.da.lmp.eastern_hub"][datetime(2000, 6, 1, 4, tzinfo=UTC)] == 11.75, "archived rows, by type"
    body = json.dumps({"items": _pjm_rows("da", date(2026, 11, 1), [51288, 34964545]), "totalRows": 50}).encode()
    d = _days(da, _f(body), date(2026, 11, 1))
    assert set(d) == {("pjm.da.lmp.western_hub", date(2026, 11, 1)), ("pjm.da.lmp.dom", date(2026, 11, 1))}
    assert all(len(v) == 25 and None not in v for v in d.values()), "DST end on UTC rows: 25 hours"
    # empty, errors, malformed
    f = _f(b'{"items": [], "totalRows": 0}')
    assert da.parse(f) == {} and f.empty
    f = _f(b'[]')
    assert da.parse(f) == {} and f.empty, "download=true: a bare list"
    _raises(lambda: da.parse(_f(b'{"errors":[{"field":"pnode_id","message":"invalid for archived data"}]}')),
            "invalid for archived")
    _raises(lambda: da.parse(_f(b"<html>")), "not JSON")
    _raises(lambda: da.parse(_f(b'{"items":[{"pnode_id":51288}]}')), "bad item")
    # windows: split at 1 January UTC and at the archive cutoff; the filter is inclusive hour starts
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    w = pjm.windows(date(2025, 12, 31), date(2025, 12, 31), now)
    assert w == [(datetime(2025, 12, 31, 5, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
                 (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 5, tzinfo=UTC))]
    cut = datetime(2024, 11, 4, tzinfo=UTC)   # 731 days before 2026-11-05
    w = pjm.windows(date(2024, 11, 1), date(2024, 11, 30), now)
    assert len(w) == 2 and w[0][1] == cut == w[1][0]
    p = pjm.params("da", datetime(2026, 11, 1, 4, tzinfo=UTC), datetime(2026, 11, 2, 5, tzinfo=UTC), "ZONE")
    assert p["datetime_beginning_utc"] == "11/1/2026 04:00to11/2/2026 04:00" and p["type"] == "ZONE"
    assert pjm.latest_da(datetime(2026, 11, 5, 19, tzinfo=UTC)) == date(2026, 11, 6)
    print("test_pjm_parse ok")


# --------------------------------------------------------------------------- ISO-NE

def test_isone_parse():
    da, rt = isone.DATASETS
    d = _days(da, _f(_b("isone_da_2025-11-02_4000.json")), date(2025, 11, 2))
    v = d[("isone.da.lmp.h_internal_hub", date(2025, 11, 2))]
    assert len(v) == 25 and None not in v and v[:4] == [32.34, 33.89, 31.96, 31.49], "01, 02, 02X, 03"
    d = _days(da, _f(_b("isone_da_2025-11-02_4004.json")), date(2025, 11, 2))
    assert list(d) == [("isone.da.lmp.z_connecticut", date(2025, 11, 2))]
    d = _days(rt, _f(_b("isone_rt_2025-11-02_4000.json")), date(2025, 11, 2))
    assert len(d[("isone.rt.lmp.h_internal_hub", date(2025, 11, 2))]) == 25
    one = json.loads(_b("isone_da_2025-11-02_4000.json"))["HourlyLmps"]["HourlyLmp"][0]
    pts = da.parse(_f(json.dumps({"HourlyLmps": {"HourlyLmp": one}}).encode()))
    assert pts == {"isone.da.lmp.h_internal_hub": {datetime(2025, 11, 2, 4, tzinfo=UTC): 32.34}}, "one record"
    for empty in (b'{"HourlyLmps": ""}', b'{"HourlyLmps": {}}', b'{"HourlyLmps": null}'):
        f = _f(empty)
        assert da.parse(f) == {} and f.empty, empty
    _raises(lambda: da.parse(_f(b"<html>401</html>")), "not JSON")
    _raises(lambda: da.parse(_f(b'{"Other": 1}')), "no HourlyLmps")
    _raises(lambda: da.parse(_f(b'{"HourlyLmps": {"HourlyLmp": [{"Location": {"@LocId": "4000"}}]}}')), "bad record")
    assert isone.url("da", date(2025, 11, 2), 4000) == (
        "https://webservices.iso-ne.com/api/v1.1/hourlylmp/da/final/day/20251102/location/4000")
    assert isone.latest_rt(datetime(2026, 11, 5, 12, tzinfo=UTC)) == date(2026, 11, 3)
    print("test_isone_parse ok")


# --------------------------------------------------------------------------- end to end

def _patched():
    for m in (spp, ercot, pjm, isone):
        m.DATASETS = tuple(dataclasses.replace(d, pace_seconds=0) for d in m.DATASETS)
    phttp.BACKOFF_SECONDS = (0, 0, 0)
    spp._INDEX.clear()
    ercot._ARCHIVE.clear()


class SppPortal:
    def __init__(self):
        self.zip = _spp_zip({"2020/11/By_Day/DA-LMP-SL-202011030100.csv": _spp_2020_da()})
        self.seen = []

    def __call__(self, req):
        path = req.url.params["path"]
        self.seen.append((path, req.headers.get("range")))
        if path == "/2020/2020.zip" and req.url.path.endswith("da-lmp-by-settlement-location"):
            return _ranged(self.zip, req.headers["range"])
        files = {"/2026/11/By_Day/DA-LMP-SL-202611010100.csv": "spp_da_2026-11-01_hubs.csv",
                 "/2025/11/By_Day/RTBM-LMP-DAILY-SL-20251102.csv": "spp_rt_2025-11-02_hubs.csv"}
        if path in files:
            return httpx.Response(200, content=_b(files[path]))
        return httpx.Response(404)


class ErcotMis:
    def __init__(self, rt_day: date, drop: int = 0):
        self.seen, self.docs = [], {"1177667469": _b("ercot_dam_hist_2025_trim.zip"),
                                    "1177737535": _b("ercot_rtm_hist_2025_trim.zip"),
                                    "1295106388": _b("ercot_np4190_2026-11-06.zip")}
        lst = []
        t = datetime.combine(rt_day, datetime.min.time()).replace(tzinfo=ercot.CT)
        for i in range(1, storage.slots(rt_day, ercot.TZ, 15) + 1):
            end = (t + timedelta(minutes=15 * i)).astimezone(UTC)   # absolute time: the DST days come out right
            loc = end.astimezone(ercot.CT)
            start = (t.astimezone(UTC) + timedelta(minutes=15 * (i - 1))).astimezone(ercot.CT)
            hour, iv = start.hour + 1, start.minute // 15 + 1
            doc = str(9000000 + i)
            self.docs[doc] = _np6905(rt_day, hour, iv, "Y" if start.fold else "N", bump=i)
            lst.append({"Document": {"DocID": doc, "FriendlyName": f"SPPHLZNP6905_{loc:%Y%m%d_%H%M}_csv",
                                     "PublishDate": loc.isoformat(), "ConstructedName": "x"}})
        self.rt_list = json.dumps({"ListDocsByRptTypeRes": {"DocumentList": lst[drop:]}}).encode()

    def __call__(self, req):
        q = dict(req.url.params)
        self.seen.append(q)
        if "reportTypeId" in q:
            if q["reportTypeId"] == "12301":
                return httpx.Response(200, content=self.rt_list)
            return httpx.Response(200, content=_b(f"ercot_list_{q['reportTypeId']}.json"))
        body = self.docs.get(q.get("doclookupId"))
        return httpx.Response(200, content=body) if body else httpx.Response(404)


class PjmApi:
    def __init__(self):
        self.seen, self.fail = [], False

    def __call__(self, req):
        self.seen.append((str(req.url), req.headers.get("ocp-apim-subscription-key")))
        if self.fail:
            return httpx.Response(401, text=f"invalid subscription key {PJM_KEY}")
        q = dict(req.url.params)
        assert q["row_is_current"] == "true" and "pnode_id" not in q
        ids = [51288, 35010337] if q["type"] == "HUB" else [34964545, 33092371]
        market = "da" if "da_hrl" in req.url.path else "rt"
        items = _pjm_rows(market, date(2026, 11, 1), ids)
        return httpx.Response(200, json={"items": items, "totalRows": len(items), "links": []})


class IsoExpress:
    def __init__(self):
        self.seen, self.fail = [], False

    def __call__(self, req):
        self.seen.append((str(req.url), req.headers.get("authorization")))
        if self.fail:
            return httpx.Response(401, text="<html>401 Unauthorized</html>")
        parts = req.url.path.split("/")
        market, day, loc = parts[-6], parts[-3], parts[-1]
        name = f"isone_{market}_2025-11-02_{loc}.json"
        if day == "20251102" and (FX / name).exists():
            return httpx.Response(200, content=_b(name))
        return httpx.Response(200, json={"HourlyLmps": ""})


def test_db_backfill():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    _patched()
    url = scratchdb.create(DB)
    saved = (config.settings.pjm_api_key, config.settings.isone_username, config.settings.isone_password)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session

        def run(source, handler, now, since, until, **kw):
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            out = asyncio.run(runner.run("backfill", sources=[source], client=client, now=now, since=since,
                                         until=until, **kw))
            return {d["dataset"]: d for d in out["sources"][0]["datasets"]} if out["sources"] else out

        def count(dataset):
            with Session() as s:
                return dict(s.execute(text("""SELECT p.key || '@' || d.day, array_length(d.vals, 1) FROM power_days d
                                              JOIN power_series p ON p.id = d.series_id WHERE p.dataset = :ds"""),
                                      {"ds": dataset}).all())

        # ---- SPP: a current-year day file, an archived year read by Range, a 404 = not published
        portal = SppPortal()
        now = datetime(2026, 11, 5, 23, tzinfo=UTC)
        r = run("spp", portal, now, date(2026, 11, 1), date(2026, 11, 2), datasets=["spp.da_lmp"])
        assert r["spp.da_lmp"]["new"] == 2 and r["spp.da_lmp"]["failed"] == 0 and r["spp.da_lmp"]["empty"] == 1, r
        spp.EARLIEST = date(2020, 1, 1)   # the archived test year (2020: DST ended Nov 1, as in 2026)
        r = run("spp", portal, now, date(2020, 11, 3), date(2020, 11, 3), datasets=["spp.da_lmp"])
        assert r["spp.da_lmp"]["new"] == 2, r
        ranges = [rg for p, rg in portal.seen if p == "/2020/2020.zip"]
        assert ranges[0] == "bytes=-70000" and len(ranges) == 3, ranges   # tail, central directory, member
        r = run("spp", portal, now, date(2025, 11, 2), date(2025, 11, 2), datasets=["spp.rt_lmp"])
        assert r["spp.rt_lmp"]["new"] == 2, r
        c = count("spp.da_lmp")
        assert c["spp.da.lmp.sppnorth_hub@2026-11-01"] == 25 and c["spp.da.lmp.sppsouth_hub@2020-11-03"] == 24
        assert count("spp.rt_lmp")["spp.rt.lmp.sppnorth_hub@2025-11-02"] == 300
        n = len(portal.seen)
        run("spp", portal, now, date(2020, 11, 3), date(2020, 11, 3), datasets=["spp.da_lmp"])
        assert len(portal.seen) == n, "a stored day is not re-read"

        # ---- ERCOT: yearly archives (old days), the NP4-190 day file, and an RT day from its 96 interval files
        now = datetime(2026, 11, 6, 2, tzinfo=UTC)   # 20:00 CT on 11-05
        mis = ErcotMis(date(2026, 11, 3))
        r = run("ercot", mis, now, date(2025, 11, 1), date(2025, 11, 3), datasets=["ercot.da_spp"])
        assert r["ercot.da_spp"]["new"] == 42 and r["ercot.da_spp"]["failed"] == 0, r
        r = run("ercot", mis, now, date(2025, 11, 2), date(2025, 11, 2), datasets=["ercot.rt_spp"])
        assert r["ercot.rt_spp"]["new"] == 14, r
        r = run("ercot", mis, now, date(2026, 11, 6), date(2026, 11, 6), datasets=["ercot.da_spp"])
        assert r["ercot.da_spp"]["new"] == 14, r
        r = run("ercot", mis, now, date(2026, 11, 3), date(2026, 11, 3), datasets=["ercot.rt_spp"])
        assert r["ercot.rt_spp"]["new"] == 14 and r["ercot.rt_spp"]["fetches"] == 96, r
        c = count("ercot.rt_spp")
        assert c["ercot.rt.lmp.hb_north@2025-11-02"] == 100 and c["ercot.rt.lmp.hb_north@2026-11-03"] == 96
        assert count("ercot.da_spp")["ercot.da.lmp.lz_raybn@2025-11-02"] == 25
        with Session() as s:
            v = s.execute(text("""SELECT vals[1], vals[96] FROM power_days d JOIN power_series p ON p.id = d.series_id
                                  WHERE p.key = 'ercot.rt.lmp.hb_north' AND day = '2026-11-03'""")).one()
            assert abs(v[0] - 45.01) < 1e-4 and abs(v[1] - 140.01) < 1e-4, ("interval i carries price + i", v)
            assert s.execute(text("SELECT DISTINCT visibility FROM power_series WHERE source = 'ercot'")).scalar() \
                == "public"
        # an RT day with an interval file missing is not read (no answer, never marked): left to the archive
        mis2 = ErcotMis(date(2026, 11, 2), drop=1)
        r = run("ercot", mis2, now, date(2026, 11, 2), date(2026, 11, 2), datasets=["ercot.rt_spp"])
        assert r["ercot.rt_spp"]["fetches"] == 0 and r["ercot.rt_spp"]["new"] == 0, r
        p = runner.plan(ercot.DATASETS[1], "backfill", now, date(2026, 11, 2), date(2026, 11, 2))
        assert p["missing"] == 1, "still missing: asked again next run"
        # the yearly archive is downloaded once per year file (month-aligned chunks read it from memory)
        ercot._ARCHIVE.clear()
        mis.seen.clear()
        r = run("ercot", mis, now, date(2025, 3, 8), date(2025, 4, 2), datasets=["ercot.da_spp"])
        assert r["ercot.da_spp"]["chunks"] == 2 and r["ercot.da_spp"]["new"] == 14, r
        assert sum(1 for q in mis.seen if q.get("doclookupId") == "1177667469") == 1

        # ---- PJM: key in a header, never in a stored URL or error; by type, filtered to the listed pnodes
        config.settings.pjm_api_key = PJM_KEY
        api = PjmApi()
        now = datetime(2026, 11, 5, 12, tzinfo=UTC)
        r = run("pjm", api, now, date(2026, 11, 1), date(2026, 11, 1), datasets=["pjm.da_lmp"])
        assert r["pjm.da_lmp"]["new"] == 4 and len(api.seen) == 2, (r, api.seen)
        assert all(k == PJM_KEY and PJM_KEY not in u for u, k in api.seen)
        assert count("pjm.da_lmp")["pjm.da.lmp.dom@2026-11-01"] == 25
        api.fail = True
        r = run("pjm", api, now, date(2026, 11, 1), date(2026, 11, 1), datasets=["pjm.rt_lmp"])
        assert r["pjm.rt_lmp"]["failed"] == 2, r

        # ---- ISO-NE: Basic auth header, per location; credentials never stored
        config.settings.isone_username, config.settings.isone_password = ISO_USER, ISO_PASS
        iso = IsoExpress()
        now = datetime(2025, 11, 6, 12, tzinfo=UTC)
        r = run("isone", iso, now, date(2025, 11, 2), date(2025, 11, 2), datasets=["isone.da_lmp"])
        assert r["isone.da_lmp"]["new"] == 2 and r["isone.da_lmp"]["empty"] == 7 and len(iso.seen) == 9, r
        want = "Basic " + base64.b64encode(f"{ISO_USER}:{ISO_PASS}".encode()).decode()
        assert all(a == want and ISO_PASS not in u for u, a in iso.seen)
        assert count("isone.da_lmp")["isone.da.lmp.h_internal_hub@2025-11-02"] == 25
        iso.fail = True
        r = run("isone", iso, now, date(2025, 11, 2), date(2025, 11, 2), datasets=["isone.rt_lmp"])
        assert r["isone.rt_lmp"]["failed"] == 9, r

        with Session() as s:
            stored = " ".join(str(x) for row in s.execute(text("SELECT url, coalesce(error, '') FROM power_fetches"))
                              for x in row)
            for secret in (PJM_KEY, ISO_PASS, ISO_USER, base64.b64encode(f"{ISO_USER}:{ISO_PASS}".encode()).decode()):
                assert secret not in stored, secret
            assert "invalid subscription key ***" in stored, "an echoed key is redacted in the stored error"
            assert s.execute(text("SELECT count(*) FROM power_fetches WHERE body IS NOT NULL")).scalar() == 0, \
                "the backfill keeps url + sha256 only"
            for t in ("compute_listings", "market_hourly", "archived_prices"):
                assert s.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t
    finally:
        (config.settings.pjm_api_key, config.settings.isone_username, config.settings.isone_password) = saved
        spp.EARLIEST = date(2021, 1, 1)
        scratchdb.drop(DB)
    print("test_db_backfill ok")


if __name__ == "__main__":
    test_registry_keys_gating()
    test_spp_parse()
    test_ercot_parse()
    test_pjm_parse()
    test_isone_parse()
    test_db_backfill()
    print("all passed")
