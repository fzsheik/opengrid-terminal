"""Wasted energy (power/waste.py, power/flaring.py, power/sources/*_curtailment.py; methodology/power-waste.md):
terms verdicts (pulled and declined), parsers over REAL saved answers with values checked by hand, the runner
(backfill, re-runs storing nothing new), the flaring loads (idempotent), the admin-only reads (401 anonymous, 403
viewer, internal envelope) and migration 0046 up and down. No network: httpx.MockTransport.

Fixtures (real answers captured 2026-11-15; "trim" = rows / members / list entries dropped, kept bytes unchanged):
    caiso_curt_2024_11_02_04_trim.xlsx      production-and-curtailments-data-2024.xlsx, rows of 2024-11-02..04 only
                                            (11-03: DST end, hour-ending 2 listed twice)
    caiso_curt_2014_05_02_trim.xlsx         the 2014-05..2017-05 workbook, 2014-05-02 only (no Reason column)
    caiso_curt_bulk2025_06_14_trim.xlsx     the bulk 2025 curtailment file, 2025-06-14 only
    caiso_renewables_report_<day>.html      daily renewable reports 2025-06-14, 2026-03-08 (DST start), 2026-11-14
    spp_ver_curtailments_2026-11-14.csv     one SPP daily file (two BAA rows per interval)
    spp_ver_curtailments_2024_trim.zip      2024.zip, members 20240310 (DST), 20241103 (DST), 20241104, and the first
                                            40 lines of MONTHLY-202402 (must not be read)
    ercot_curt_<report>_2026-11-15_2155.zip NP4-732-CD / NP4-737-CD posted 2026-11-15 21:55 CT
    ercot_curt_list_<report>_trim.json      their document lists, 40 CSV entries of 11-14 / 11-15
    wb_ggfd_2021_trim.xlsx / _2019_trim     World Bank GGFD v5: first 40 (2019: 25) rows of each sheet (+ 2021: all
                                            United States rows)
    ndic_mpr_2026_0[78]_bowman_trim.xlsx    NDIC monthly production reports, header + Bowman county rows

Run:  .venv/bin/python tests/test_power_waste.py
"""

import asyncio
import base64
import dataclasses
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import httpx  # noqa: E402

from power import flaring, registry, runner, storage, waste  # noqa: E402
from power import http as phttp  # noqa: E402
from power import sources as psources  # noqa: E402
from power.registry import Fetched  # noqa: E402
from power.sources import caiso_curtailment as C, ercot_curtailment as E, spp_curtailment as S  # noqa: E402

FX = ROOT / "tests" / "fixtures_power"
DB = "og_test_power_waste"
UTC = timezone.utc
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)     # 04:00 PT / 06:00 CT


def _b(name):
    return (FX / name).read_bytes()


def _close(a, b, tol=1e-3):
    return a is not None and abs(a - b) <= tol


def _days(ds, f, a, b):
    return storage.to_days(ds, ds.parse(f), a, b)


# --------------------------------------------------------------------------- verdicts

def test_verdicts():
    assert {"caiso_curtailment", "spp_curtailment", "ercot_curtailment"} <= set(psources.MODULES)
    assert registry.visibility("caiso_curtailment") == "internal", "CAISO site terms: not redistributable"
    assert registry.visibility("spp_curtailment") == "internal", "SPP: no commercial use without written consent"
    assert registry.visibility("ercot_curtailment") == "public", "ERCOT raw public data may be redistributed"
    assert "commercial publication" in S.TERMS.reason and "No material or information" in C.TERMS.reason
    assert all(s.meta["measure"] == "proxy_hsl_minus_output" for s in E.SERIES if s.kind == "curtailment")
    assert flaring.SOURCES["worldbank_ggfd"].terms.visibility == "public"
    assert "Creative Commons Attribution 4.0" in flaring.SOURCES["worldbank_ggfd"].terms.reason
    assert flaring.SOURCES["ndic"].terms.visibility == "internal"
    declined = {d.dataset: d for d in registry.DECLINED}
    assert "CC BY-NC 4.0" in declined["eog_vnf_annual"].terms.reason
    assert "non-commercial uses" in declined["worldbank_gfmr_2012_2025"].terms.reason
    assert "noncommercial use" in declined["rrc_pdq_dump"].terms.reason
    assert declined["ercot_wind_solar_archive"].terms.signup
    # nothing declined is fetched: no URL of theirs in any pulled source
    pulled = " ".join(list(flaring.WB_FILES.values()) + [flaring.ND_URL, C.BASE, S.URL, E.ercot.DOC_URL])
    assert "zenodo" not in pulled and "thedocs.worldbank.org" not in pulled and "rrc.texas.gov" not in pulled
    assert all(u.startswith(flaring.WB_BASE) for u in flaring.WB_FILES.values())
    keys = list(registry.series_index())
    assert len(keys) == len(set(keys)) and all(k == k.lower() for k in keys)
    lst = {r["source"]: r for r in registry.as_list()}
    assert lst["caiso_curtailment"]["default_on"] and lst["spp_curtailment"]["signup"] is None
    assert {d["dataset"] for d in registry.declined_list()} == set(declined)
    assert abs(flaring.MWH_PER_MCF - 0.1037) < 1e-12
    print("test_verdicts ok")


# --------------------------------------------------------------------------- CAISO

def test_caiso_parse():
    ds = C.DATASETS[0]
    a24 = next(a for a in C.ARCHIVES if a.name.endswith("data-2024.xlsx"))
    f = Fetched(C.BASE + a24.name, 200, 1, _b("caiso_curt_2024_11_02_04_trim.xlsx"),
                start=date(2024, 11, 2), end=date(2024, 11, 4))
    d = _days(ds, f, date(2024, 11, 2), date(2024, 11, 4))
    sol = d[(C.curtailment_key("solar"), date(2024, 11, 2))]
    # by hand over the raw rows: 2024-11-02 solar, every interval MW / 12
    assert _close(sum(sol), 654.84, 0.01), sum(sol)
    # hour-ending 12 (11:00-12:00 PDT, slot 11): Local 279.4929 MW-sum / 12 + System 133.79 / 12
    assert _close(d[(C.curtailment_key("solar", "local"), date(2024, 11, 2))][11], 23.2911)
    assert _close(d[(C.curtailment_key("solar", "system"), date(2024, 11, 2))][11], 11.1492)
    assert _close(sol[11], 34.4403)
    assert sol[0] == 0.0, "an hour with nothing listed is zero curtailment, not a gap"
    assert _close(sum(d[(C.curtailment_key("solar"), date(2024, 11, 3))]), 16901.149, 0.01)
    assert _close(sum(d[(C.curtailment_key("wind"), date(2024, 11, 3))]), 1069.758, 0.01)
    gen = d[(C.generation_key("solar"), date(2024, 11, 2))]
    assert _close(sum(gen), 103161.332, 0.01) and _close(gen[11], 13729.6015)
    dst = d[(C.generation_key("wind"), date(2024, 11, 3))]
    assert len(dst) == 25 and None not in dst, "hour-ending 2 twice: both 01:00 hours kept"
    assert len(d[(C.curtailment_key("solar"), date(2024, 11, 3))]) == 25
    # the 2014-2017 workbook has no Reason column: totals only
    a14 = C.ARCHIVES[0]
    f = Fetched(C.BASE + a14.name, 200, 1, _b("caiso_curt_2014_05_02_trim.xlsx"),
                start=date(2014, 5, 2), end=date(2014, 5, 2))
    d14 = _days(ds, f, date(2014, 5, 2), date(2014, 5, 2))
    assert (C.curtailment_key("wind"), date(2014, 5, 2)) in d14
    assert not any(k.endswith(("_local", "_system")) for k, _ in d14)
    assert sum(d14[(C.curtailment_key("wind"), date(2014, 5, 2))]) > 0
    # the daily report and the bulk workbook agree hour by hour (2025-06-14)
    bulk = next(a for a in C.ARCHIVES if "june-december" in a.name)
    f = Fetched(C.BASE + bulk.name, 200, 1, _b("caiso_curt_bulk2025_06_14_trim.xlsx"),
                start=date(2025, 6, 14), end=date(2025, 6, 14))
    wb = _days(ds, f, date(2025, 6, 14), date(2025, 6, 14))
    f = Fetched(C.report_url(date(2025, 6, 14)), 200, 1, _b("caiso_renewables_report_2025-06-14.html"),
                start=date(2025, 6, 14), end=date(2025, 6, 14))
    rep = _days(ds, f, date(2025, 6, 14), date(2025, 6, 14))
    for fuel in C.FUELS:
        for sc in C.SCOPES:
            k = (C.curtailment_key(fuel, sc), date(2025, 6, 14))
            assert all(abs(x - y) < 1.5 for x, y in zip(wb[k], rep[k])), (k, wb[k], rep[k])
    assert (C.curtailment_key("solar", "local", "economic"), date(2025, 6, 14)) in rep
    assert (C.curtailment_key("solar", "local", "economic"), date(2025, 6, 14)) not in wb
    # 2026-11-14: hand sums of the page's arrays
    f = Fetched(C.report_url(date(2026, 11, 14)), 200, 1, _b("caiso_renewables_report_2026-11-14.html"),
                start=date(2026, 11, 14), end=date(2026, 11, 14))
    r = _days(ds, f, date(2026, 11, 14), date(2026, 11, 14))
    day = date(2026, 11, 14)
    assert _close(sum(r[(C.curtailment_key("solar", "local", "economic"), day)]), 7878.0, 0.05)
    assert _close(sum(r[(C.curtailment_key("wind", "local", "economic"), day)]), 4221.1, 0.05)
    assert _close(sum(r[(C.curtailment_key("solar"), day)]), 7878.0 + 0.1 + 58.4, 0.1)
    assert _close(sum(r[(C.generation_key("solar", True), day)]), 1304971.8 / 12, 0.5)
    assert C.report_url(day).endswith("daily-renewable-report-nov-14-2026.html")
    # spring forward: 23 hours, the missing 02:00 dropped
    f = Fetched(C.report_url(date(2026, 3, 8)), 200, 1, _b("caiso_renewables_report_2026-03-08.html"),
                start=date(2026, 3, 8), end=date(2026, 3, 8))
    r = _days(ds, f, date(2026, 3, 8), date(2026, 3, 8))
    assert len(r[(C.curtailment_key("wind"), date(2026, 3, 8))]) == 23
    try:
        C.parse_report(_b("caiso_renewables_report_2026-11-14.html"), date(2026, 11, 13))
        raise AssertionError("a page for another day must fail")
    except ValueError as exc:
        assert "asked for" in str(exc)
    assert C.latest(NOW) == date(2026, 11, 14) and C.latest(datetime(2026, 11, 16, 20, tzinfo=UTC)) == date(2026, 11, 15)
    print("test_caiso_parse ok")


# --------------------------------------------------------------------------- SPP / ERCOT

def test_spp_ercot_parse():
    ds = S.DATASETS[0]
    day = date(2026, 11, 14)
    f = Fetched("u", 200, 1, _b("spp_ver_curtailments_2026-11-14.csv"), start=day, end=day)
    d = _days(ds, f, day, day)
    # by hand (csv module over the raw file, MW / 12, both BAA rows): redispatch 6153.9533, manual 4628.1775
    assert _close(sum(d[(S.curtailment_key("wind", "redispatch"), day)]), 6153.9533, 0.01)
    assert _close(sum(d[(S.curtailment_key("wind", "manual"), day)]), 4628.1775, 0.01)
    assert _close(sum(d[(S.curtailment_key("wind"), day)]), 6153.9533 + 4628.1775, 0.01)
    assert _close(sum(d[(S.curtailment_key("solar"), day)]), 170.7425, 0.01)
    # 08:00 CST hour (slot 8): SPP 259.39 + SWPW 0
    assert _close(d[(S.curtailment_key("wind", "redispatch"), day)][8], 259.39, 0.01)
    f = Fetched("u", 200, 1, _b("spp_ver_curtailments_2024_trim.zip"), start=date(2024, 3, 1), end=date(2024, 11, 30))
    d = _days(ds, f, date(2024, 3, 1), date(2024, 11, 30))
    assert {x for _, x in d} == {date(2024, 3, 10), date(2024, 11, 3), date(2024, 11, 4)}, "MONTHLY is not read"
    assert len(d[(S.curtailment_key("wind"), date(2024, 3, 10))]) == 23
    assert len(d[(S.curtailment_key("wind"), date(2024, 11, 3))]) == 25
    assert _close(sum(d[(S.curtailment_key("wind", "redispatch"), date(2024, 11, 3))]), 30613.5175, 0.01)
    e = d[(S.curtailment_key("wind", "energy"), date(2024, 3, 10))]
    assert _close(sum(x for x in e if x is not None), 11697.3533, 0.01)
    assert e.count(None) == 2, "32 blank rows that day: two whole hours not given stay NULL, never 0"
    assert _close(sum(d[(S.curtailment_key("wind"), date(2024, 11, 4))]), 16336.4942 + 11490.3442, 0.01)

    eds = E.DATASETS[0]
    f = Fetched("u", 200, 1, _b("ercot_curt_13028_2026-11-15_2155.zip"), start=day, end=day)
    w = E.parse(f)
    t = datetime(2026, 11, 14, 3, tzinfo=UTC)                  # 11/13 HE22 = 21:00 CST
    assert t not in w[E.key("curtailment", "wind")], "hours outside the asked day are dropped"
    f = Fetched("u", 200, 1, _b("ercot_curt_13028_2026-11-15_2155.zip"), start=date(2026, 11, 13), end=day)
    w = E.parse(f)
    assert _close(w[E.key("curtailment", "wind")][t], 19590.45 - 18756.1), "HSL 19590.45 - GEN 18756.1"
    f = Fetched("u", 200, 1, _b("ercot_curt_13483_2026-11-15_2155.zip"), start=day, end=day)
    s = storage.to_days(eds, E.parse(f), day, day)
    sol = s[(E.key("curtailment", "solar"), day)]
    assert len(sol) == 24 and None not in sol
    assert _close(sol[17], 563.35 - 319.55), "HE18: GEN 319.55 above the 50 MW floor"
    assert sol[18] == 0.0 and sol[23] == 0.0, "night: HSL ~23 MW vs ~0.5 MW output is noise, not curtailment"
    docs = E.ercot.documents(_b("ercot_curt_list_13028_trim.json"))
    assert E.pick(docs, "WPPHRLYAVGACTNP4732_csv", day)["DocID"] == "1299174349"
    assert E.pick(docs, "WPPHRLYAVGACTNP4732_csv", date(2026, 11, 15)) is None
    assert E.earliest(NOW) == date(2026, 11, 9) and E.latest(NOW) == date(2026, 11, 15)
    print("test_spp_ercot_parse ok")


# --------------------------------------------------------------------------- flaring parse

def test_flaring_parse():
    fl = flaring.parse_worldbank(_b("wb_ggfd_2021_trim.xlsx"), 2021)
    v = fl[0]
    assert (v["iso3"], v["kind"]) == ("VEN", "upstream oil") and _close(v["bcm"], 1.331804316, 1e-9)
    assert flaring.cell_key(9.652413, -63.623903) == "c+00965-06363"
    assert all(x["iso3"] for x in fl) and len({x["kind"] for x in fl}) > 1
    fl19 = flaring.parse_worldbank(_b("wb_ggfd_2019_trim.xlsx"), 2019)
    # 'all flares' repeats 'flare upstream' + 'flare downstream': read once
    assert len(fl19) == 48, len(fl19)
    m = flaring.parse_ndic(_b("ndic_mpr_2026_08_bowman_trim.xlsx"))
    assert m["period"] == date(2026, 8, 1) and len(m["wells"]) == 540
    counties, sites = flaring.ndic_aggregate(m)
    b = counties["BOW"]
    assert (round(b["flared"]), round(b["gas"]), b["wells"]) == (159584, 214976, 273)
    assert round(sites["t130r104s13"]["flared"]) == 10373 and sites["t130r104s13"]["wells"] == 2
    assert flaring.mwh(1000.0) == 103.7
    print("test_flaring_parse ok")


# --------------------------------------------------------------------------- DB: runner, loads, reads, API

class Server:
    def __init__(self):
        self.seen = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        self.seen.append(u)
        if u == C.BASE + "production-and-curtailments-data-2024.xlsx":
            return httpx.Response(200, content=_b("caiso_curt_2024_11_02_04_trim.xlsx"))
        for day in ("nov-14-2026", "mar-08-2026"):
            if u.endswith(f"daily-renewable-report-{day}.html"):
                iso = {"nov-14-2026": "2026-11-14", "mar-08-2026": "2026-03-08"}[day]
                return httpx.Response(200, content=_b(f"caiso_renewables_report_{iso}.html"))
        if u.startswith(S.URL):
            p = req.url.params.get("path")
            if p == "/2024/2024.zip":
                return httpx.Response(200, content=_b("spp_ver_curtailments_2024_trim.zip"))
            if p == "/2026/11/VER-Curtailments-20261114.csv":
                return httpx.Response(200, content=_b("spp_ver_curtailments_2026-11-14.csv"))
        if "IceDocListJsonWS" in u:
            return httpx.Response(200, content=_b(f"ercot_curt_list_{req.url.params['reportTypeId']}_trim.json"))
        if "mirDownload" in u:
            doc = req.url.params["doclookupId"]
            if doc == "1299174349":
                return httpx.Response(200, content=_b("ercot_curt_13028_2026-11-15_2155.zip"))
            if doc == "1299174340":
                return httpx.Response(200, content=_b("ercot_curt_13483_2026-11-15_2155.zip"))
        if u == flaring.WB_FILES[2021]:
            return httpx.Response(200, content=_b("wb_ggfd_2021_trim.xlsx"))
        if u == flaring.WB_FILES[2019]:
            return httpx.Response(200, content=_b("wb_ggfd_2019_trim.xlsx"))
        for mo in ("07", "08"):
            if u == flaring.ND_URL.format(y=2026, m=int(mo)):
                return httpx.Response(200, content=_b(f"ndic_mpr_2026_{mo}_bowman_trim.xlsx"))
        return httpx.Response(404, text="not found")


def _fast():
    for mod in (C, S, E):
        mod.DATASETS = tuple(dataclasses.replace(d, pace_seconds=0) for d in mod.DATASETS)
    phttp.BACKOFF_SECONDS = (0, 0, 0)
    flaring.PACE_SECONDS = 0
    C._BODIES.clear()
    C._PARSED.clear()
    S._ZIPS.clear()


def test_db():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    _fast()
    url = scratchdb.create(DB)
    saved = (config.settings.app_password, config.settings.public_pages, config.settings.viewer_user,
             config.settings.viewer_password, config.settings.environment)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        srv = Server()
        client = httpx.AsyncClient(transport=httpx.MockTransport(srv))

        def run(src, **kw):
            out = asyncio.run(runner.run("backfill", sources=[src], client=client, now=NOW, **kw))
            return out["sources"][0]["datasets"][0]

        # ---------------- curtailment: CAISO workbook + reports, SPP, ERCOT
        r = run("caiso_curtailment", since=date(2024, 11, 2), until=date(2024, 11, 4))
        assert r["failed"] == 0 and r["fetches"] == 1 and r["new"] == 3 * 8, r   # 6 curtailment + 2 output series
        r = run("caiso_curtailment", since=date(2026, 11, 13), until=date(2026, 11, 14))
        assert r["failed"] == 0 and r["empty"] == 1 and r["new"] == 20, r     # 2 fuels x (total, 2 scopes, 6 types, telemetry); 11-13: 404 = not posted
        r = run("spp_curtailment", since=date(2024, 11, 3), until=date(2024, 11, 4))
        assert r["failed"] == 0 and r["fetches"] == 1 and r["new"] == 4 + 8, r   # 11-03: solar columns blank (not given)
        r = run("spp_curtailment", since=date(2026, 11, 14), until=date(2026, 11, 14))
        assert r["new"] == 8, r
        r = run("ercot_curtailment", since=date(2026, 11, 14), until=date(2026, 11, 14))
        assert r["failed"] == 0 and r["new"] == 6, r
        # re-runs store nothing new and do not even ask again
        n = len(srv.seen)
        for src, a, b in (("caiso_curtailment", date(2024, 11, 2), date(2024, 11, 4)),
                          ("spp_curtailment", date(2024, 11, 3), date(2024, 11, 4)),
                          ("ercot_curtailment", date(2026, 11, 14), date(2026, 11, 14))):
            r = run(src, since=a, until=b)
            assert r["new"] == r["revised"] == 0 and r["chunks"] == 0, (src, r)
        assert len(srv.seen) == n
        with Session() as s:
            vis = dict(s.execute(text("SELECT source, min(visibility) FROM power_series GROUP BY 1")).all())
            assert vis == {"caiso_curtailment": "internal", "spp_curtailment": "internal",
                           "ercot_curtailment": "public"}, vis
            assert s.execute(text("SELECT count(*) FROM power_fetches WHERE body IS NOT NULL")).scalar() == 0, \
                "the backfill keeps url + sha256 only"
            assert s.execute(text("SELECT count(*) FROM power_fetches WHERE sha256 IS NULL AND ok")).scalar() == 0

        got = waste.curtailed_mwh("CAISO", date(2024, 11, 2), date(2024, 11, 2), "solar")
        assert _close(got["curtailed_mwh"], 654.84, 0.05) and _close(got["output_mwh_same_days"], 103161.332, 0.1)
        assert _close(got["share_curtailed"], 654.84 / (654.84 + 103161.332), 1e-5), got
        both = waste.curtailed_mwh("caiso", date(2024, 11, 2), date(2024, 11, 4))
        assert both["days"] == 3 and _close(both["by_fuel"]["wind"], 1069.758 + 0.27, 0.05), both
        rows = waste.curtailment(date(2024, 11, 3), date(2024, 11, 3), "SPP", "wind")
        assert {r["scope"] for r in rows} == {None, "energy"} and {r["type"] for r in rows} >= {"redispatch", "manual"}
        tot = next(r for r in rows if r["type"] is None)
        assert _close(tot["curtailed_mwh"], 30613.5175 + 21957.8808, 0.05) and tot["output_mwh"] is None
        er = waste.curtailment(date(2026, 11, 14), date(2026, 11, 14), "ERCOT", "wind")
        assert er[0]["measure"] == "proxy_hsl_minus_output" and er[0]["share_curtailed"] is not None

        # ---------------- flaring
        out = asyncio.run(flaring.run(client=client, now=NOW, since=date(2026, 7, 1)))
        wb, nd = out["sources"]
        assert [y.get("year") for y in wb["years"]] == [2017, 2018, 2019, 2020, 2021]
        assert sum(1 for y in wb["years"] if "error" in y) == 3, "the 404 years are recorded failures"
        y21 = next(y for y in wb["years"] if y["year"] == 2021)
        assert y21["flares"] == 3249 and y21["new"] == y21["sites"]
        assert [x["period"] for x in nd["loaded"]] == [date(2026, 7, 1), date(2026, 8, 1)]
        assert nd["not_posted"] == [date(2026, 9, 1), date(2026, 10, 1)]
        n = len(srv.seen)
        again = asyncio.run(flaring.run(client=client, now=NOW, since=date(2026, 7, 1)))
        wb2, nd2 = again["sources"]
        assert wb2["to_fetch"] == 3, "stored World Bank files are not fetched again"
        assert all(x["new"] == x["changed"] == 0 for x in nd2["loaded"]), nd2
        assert len(srv.seen) - n == 3 + 4, "3 failed WB years retried, ND: the last 2 months re-read + 2 not posted"
        with Session() as s:
            sid = s.execute(text("SELECT id FROM power_flare_sites WHERE source = 'ndic' "
                                 "AND site_key = 't130r104s13'")).scalar()
            v = s.execute(text("SELECT monthly_mcf, volume_mcf, months_reported, energy_mwh_derived "
                               "FROM power_flare_volumes WHERE site_id = :i AND year = 2026"), {"i": sid}).one()
            assert v.monthly_mcf[6] == 14538 and v.monthly_mcf[7] == 10373 and v.monthly_mcf[0] is None
            assert v.volume_mcf == 24911 and v.months_reported == 2 and _close(v.energy_mwh_derived, 2583.271)
            a = s.execute(text("SELECT volume_mcf, gas_produced_mcf, wells, meta FROM power_flare_areas "
                               "WHERE source = 'ndic' AND area = 'Bowman' AND period = '2026-08-01'")).one()
            assert (a.volume_mcf, a.gas_produced_mcf, a.wells) == (159584, 214976, 273)
            assert _close(a.meta["flared_share"], 159584 / 214976, 1e-5)
            vis = dict(s.execute(text("SELECT source, min(visibility) FROM power_flare_sites GROUP BY 1")).all())
            assert vis == {"worldbank_ggfd": "public", "ndic": "internal"}, vis
        top = waste.flare_sites(country="VEN", year=2021, limit=1)
        assert top[0]["site_key"] == "c+00965-06363" and _close(top[0]["volume_bcm"], 1.331804, 1e-5), top
        us = waste.flare_sites(country="USA", year=2021, limit=3, source="worldbank_ggfd")
        assert len(us) == 3 and us[0]["volume_bcm"] >= us[1]["volume_bcm"] >= us[2]["volume_bcm"]
        near = waste.flared_near(46.082112, -103.635309, 5.0)
        assert near["by_source"]["ndic"]["year"] == 2026 and near["nearest"][0]["site_key"] == "t130r104s13"
        assert near["nearest"][0]["km"] < 0.01
        cty = waste.flared_by_county("ND")
        assert cty[0]["area"] == "Bowman" and cty[0]["period"] == date(2026, 8, 1)

        # ---------------- the reads: admin only
        from fastapi.testclient import TestClient

        import main
        from accounts import security
        security.reset()
        config.settings.app_password, config.settings.environment = "pw", "development"
        config.settings.viewer_user, config.settings.viewer_password = "vc", "look-only"
        config.settings.public_pages = True

        def basic(u, p):
            return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
        op, viewer = basic("opengrid", "pw"), basic("vc", "look-only")
        tc = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        paths = ("/v1/ops/power/curtailment?iso=CAISO&since=2024-11-02&until=2024-11-04",
                 "/v1/ops/power/flaring", "/v1/ops/power/flaring?view=counties&state=ND",
                 "/v1/ops/power/flaring?lat=46.08&lon=-103.63&radius_km=20", "/v1/ops/power/sources")
        for p in paths:
            assert tc.get(p).status_code == 401, p
            r = tc.get(p, headers=viewer)
            assert r.status_code == 403 and "654.8" not in r.text, p
            r = tc.get(p, headers=op)
            assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store", (p, r.text[:300])
        r = tc.get(paths[0], headers=op).json()
        assert r["meta"]["visibility"] == "internal" and r["meta"]["count"] > 0
        assert any(x["series"] == C.curtailment_key("solar") and _close(x["curtailed_mwh"], 654.84, 0.05)
                   for x in r["data"])
        r = tc.get("/v1/ops/power/flaring?country=USA&year=2021&limit=5", headers=op).json()
        assert len(r["data"]) == 5 and "DERIVED" in r["meta"]["energy_rule"]
        r = tc.get("/v1/ops/power/sources", headers=op).json()
        assert {d["dataset"] for d in r["meta"]["declined"]} >= {"eog_vnf_annual", "rrc_pdq_dump"}
        assert tc.get("/v1/ops/power/flaring?lat=46", headers=op).status_code == 422
        assert tc.get("/v1/ops/power/curtailment?since=2025-01-02&until=2025-01-01", headers=op).status_code == 422
        assert all(not p.startswith("/v1/power") for p in main.app.openapi()["paths"])
        json.dumps(waste.curtailed_mwh("SPP", date(2024, 11, 3), date(2024, 11, 4)), default=str)   # plain dict
    finally:
        (config.settings.app_password, config.settings.public_pages, config.settings.viewer_user,
         config.settings.viewer_password, config.settings.environment) = saved
        scratchdb.drop(DB)
    print("test_db ok")


def test_migration_0046():
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    import config
    import db as dbmod
    from store import power as P

    name = DB + "_mig"
    url = scratchdb.create(name)
    old = config.settings.database_url
    config.settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        script = ScriptDirectory.from_config(cfg)
        assert len(script.get_heads()) == 1                      # later migrations may follow
        assert script.get_revision("0046_power_waste").down_revision == "0045_power_sites"
        command.upgrade(cfg, "0046_power_waste")
        eng = create_engine(url)
        models = (P.PowerFlareSite, P.PowerFlareVolume, P.PowerFlareArea)
        with eng.connect() as c:
            for model in models:
                t = model.__tablename__
                cols = {r[0]: r[1] for r in c.execute(text(
                    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = :t"), {"t": t})}
                assert set(cols) == {col.name for col in model.__table__.columns}, t
                assert {k for k, v in cols.items() if v == "YES"} == \
                    {col.name for col in model.__table__.columns if col.nullable}, t
            try:
                c.execute(text("INSERT INTO power_flare_sites (source, site_key, lat, lon, visibility, created_at, "
                               "updated_at) VALUES ('x', 'y', 1, 2, 'secret', now(), now())"))
                raise AssertionError("CHECK did not fire")
            except Exception as exc:  # noqa: BLE001
                assert "ck_power_flare_sites_visibility" in str(exc), exc
                c.rollback()
        command.downgrade(cfg, "0045_power_sites")
        with eng.connect() as c:
            for model in models:
                assert c.execute(text("SELECT to_regclass(:t)"), {"t": "public." + model.__tablename__}).scalar() is None
            assert c.execute(text("SELECT to_regclass('public.power_days')")).scalar() is not None
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.power_flare_volumes')")).scalar() is not None
        eng.dispose()
    finally:
        config.settings.database_url = old
        scratchdb.drop(name)
    print("test_migration_0046 ok")


if __name__ == "__main__":
    test_verdicts()
    test_caiso_parse()
    test_spp_ercot_parse()
    test_flaring_parse()
    test_db()
    test_migration_0046()
    print("all passed")
