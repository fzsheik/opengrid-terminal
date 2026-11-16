"""Physical sites (power/sites/; methodology/power-markets.md "Physical sites"): the registry's verdicts, parsing of
REAL saved answers, capacity factors by hand, the generator change-only history (status, capacity, retirement,
leaving the inventory), re-runs storing nothing new, queue editions and county totals, the admin-only reads, and
migration 0045 up and down. No network: httpx.MockTransport.

Fixtures (tests/fixtures_power/sites/, captured 2026-11-15; TRIMMED as stated):
    eia860m_meta.json                     operating-generator-capacity route metadata (the real answer, trimmed
                                          to its id / frequency / start / endPeriod 2026-08 by hand)
    eia860m_api_2026-07.json, _2026-08    API v2 operable inventory for three plants only (9 Copper TX, 298
                                          Limestone TX, 1998 Mountain Lake MN), as answered (DEMO_KEY, facets)
    eia923_meta.json                      facility-fuel route metadata, as answered
    eia923_annual_2025.json               facility-fuel annual 2025 for the same three plants, every fuel row
    eia860m_index.html                    the EIA-860M page's file table, cut to Dec 2026..Aug 2026 (the
                                          not-yet-posted months inside the page's own HTML comment)
    eia860m_september_generator2026.xlsx  the September 2026 workbook cut to plants 9, 54, 298, 1998, 63798:
                                          real title / header rows and data rows, re-written as inline strings
    lbnl_queue_thru2025.xlsx              Queued Up 2026 edition, sheet "03. Complete Queue Data" cut to 17 rows
                                          (Pecos / Reeves TX, one APS "not assigned", Duke's queue id 1 listed twice)
    ercot_gis_list.json                   the PG7-200-ER document list cut to 3 documents
    ercot_gis_2026-10.xlsx                GIS_Report_October2026 cut to 10 large-generator rows (Pecos / Reeves)
                                          with the real title, notes and 5-row header, and 2 small-generator rows

Run:  .venv/bin/python tests/test_power_sites.py
"""

import asyncio
import base64
import json
import os
import sys
from datetime import date
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import httpx  # noqa: E402

from power import http as phttp  # noqa: E402
from power.registry import Fetched  # noqa: E402
from power.sites import eia860m, eia923, ercot_gis, lbnl, reads, registry, runner, xlsx  # noqa: E402

FX = ROOT / "tests" / "fixtures_power" / "sites"
DB = "og_test_power_sites"


def _b(name):
    return (FX / name).read_bytes()


def _f(name):
    return Fetched("u", 200, 1, _b(name))


# --------------------------------------------------------------------------- no database

def test_registry():
    assert registry.visibility("eia860m") == "public" and registry.visibility("eia923") == "public"
    assert registry.visibility("ercot_gis") == "public"
    assert registry.visibility("lbnl_queues", "2025-12-31") == "public", "CC BY 4.0 edition"
    assert registry.visibility("lbnl_queues", "2024-12-31") == "internal", "no licence stated for that edition"
    assert registry.visibility("lbnl_queues", "2019-12-31") == "internal", "an unlisted edition is internal"
    assert registry.visibility("nope") == "internal"
    lst = {r["source"]: r for r in registry.as_list()}
    assert set(lst) == {"eia860m", "eia923", "lbnl_queues", "ercot_gis"}
    assert lst["eia860m"]["requires"] == ["eia_api_key"] and lst["ercot_gis"]["requires"] == []
    assert "CC BY 4.0" in lst["lbnl_queues"]["reason"] and "large-load" in lst["ercot_gis"]["reason"]
    assert {e for e in lbnl.EDITIONS} == {date.fromisoformat(e) for e in registry.source("lbnl_queues").editions}
    print("test_registry ok")


def test_parse():
    # EIA API: 7 operable generators, Copper's planned retirement, coordinates rounded to 4 places
    rows = {(r["plant_id"], r["generator_id"]): r for r in eia860m.parse_api(_f("eia860m_api_2026-08.json"))}
    assert len(rows) == 7 and rows[(9, "1")]["planned_retirement_ym"] == "2030-12"
    lime = rows[(298, "1")]
    assert (lime["nameplate_mw"], lime["summer_mw"], lime["ba_code"], lime["county"]) == (893.0, 831.0, "ERCO",
                                                                                          "Limestone")
    assert (lime["latitude"], lime["longitude"], lime["status_group"]) == (31.4219, -96.2525, "operating")
    # the file: "as of" month from the title, every sheet, statuses from "(V) Under construction ..."
    data = _b("eia860m_september_generator2026.xlsx")
    assert eia860m.as_of(data) == date(2026, 9, 1)
    f = {(r["plant_id"], r["generator_id"]): r for r in eia860m.parse_file(data)}
    assert len(f) == 21
    atlas = f[(63798, "ATL01")]
    assert (atlas["status"], atlas["status_group"], atlas["planned_operation_ym"], atlas["nameplate_mw"]) == (
        "V", "planned", "2026-09", 300.0)
    ml = f[(1998, "5")]
    assert (ml["status"], ml["retirement_ym"], ml["operating_ym"]) == ("RE", "2026-09", "1959-05")
    assert f[(54, "GT11")]["status_group"] == "canceled" and f[(9, "1")]["planned_retirement_ym"] == "2030-12"
    # the index: commented-out (unposted) months ignored, the current folder preferred
    links = eia860m.file_links(_b("eia860m_index.html"))
    assert links[0] == (date(2026, 9, 1), "https://www.eia.gov/electricity/data/eia860m/xls/september_generator2026.xlsx")
    assert [d for d, _ in links] == [date(2026, 9, 1), date(2026, 8, 1)]
    # EIA-923: only the ALL / ALL row per plant
    g = eia923.parse(_f("eia923_annual_2025.json"))
    assert g == {9: (27840.0, "TX"), 298: (7523395.0, "TX"), 1998: (2599.0, "MN")}
    # LBNL: ids, the duplicate, MW sums, dates
    q = {r["project_id"]: r for r in lbnl.parse(_b("lbnl_queue_thru2025.xlsx"))}
    assert len(q) == 17 and "Duke|1" in q and "Duke|1#2" in q
    assert q["ERCOT|19INR0120"]["mw"] == -18.0 and q["ERCOT|19INR0120"]["status"] == "active"
    assert q["APS|not assigned"]["queue_date"] == date(2019, 2, 15) and q["APS|not assigned"]["fips"] == "04005"
    # ERCOT GIS: the document list keeps GIS reports only; the 5-row header; types from fuel / technology
    docs = ercot_gis.documents(_b("ercot_gis_list.json"))
    assert [(d["doc_id"], d["edition"]) for d in docs] == [("1293955078", date(2026, 10, 31)),
                                                           ("1281327706", date(2026, 9, 30))]
    p = {r["project_id"]: r for r in ercot_gis.parse(_b("ercot_gis_2026-10.xlsx"))}
    assert len(p) == 12
    assert (p["17INR0070"]["county"], p["17INR0070"]["type"], p["17INR0070"]["mw"]) == ("Pecos", "Wind", 7.2)
    assert p["22INR0365"]["type"] == "Battery" and p["22INR0365"]["proposed_cod"] == date(2028, 6, 1)
    assert p["23INR0483"]["phase"] == "small generator"
    # the xlsx helpers
    assert xlsx.serial_date("1") is None and xlsx.serial_date("46359") == date(2026, 12, 3)
    assert xlsx.header_key("Nameplate Capacity (MW)") == "nameplate_capacity_mw"
    for bad in (b"not a zip", b"PK\x03\x04garbage"):
        try:
            eia860m.parse_file(bad)
            raise AssertionError("a non-workbook must fail the parse")
        except Exception as exc:  # noqa: BLE001
            assert not isinstance(exc, AssertionError)
    print("test_parse ok")


def test_capacity_factor_by_hand():
    f = {(r["plant_id"], r["generator_id"]): r for r in eia860m.parse_file(_b("eia860m_september_generator2026.xlsx"))}
    by = {}
    for (pid, _), r in f.items():
        by.setdefault(pid, []).append(r)
    # Limestone 2025: 7,523,395 MWh over (893 + 956.8) MW x 8,760 h
    assert abs(reads.capacity_factor(7523395.0, by[298], 2025) - 7523395.0 / ((893 + 956.8) * 8760)) < 1e-12
    assert round(7523395.0 / ((893 + 956.8) * 8760), 4) == 0.4643
    # Copper: 27,840 MWh over 86.9 MW x 8,760 h = 3.7 %
    assert round(reads.capacity_factor(27840.0, by[9], 2025), 4) == round(27840 / (86.9 * 8760), 4) == 0.0366
    # Mountain Lake 2025: gens 5, 6, 7 (retired 2026-09), 8 (wind) and 2 (retired 2026-01) ran all year; gen 4
    # retired 2023-01 did not: 1.3 + 1.8 + 1.8 + 1.2 + 1.1 = 7.2 MW
    assert abs(reads.capacity_factor(2599.0, by[1998], 2025) - 2599 / (7.2 * 8760)) < 1e-12
    # 2026: gen 2 (retired in January) not at all; 5, 6, 7 (retired in September) January through August
    h26 = (1.3 + 1.8 + 1.8) * (31 + 28 + 31 + 30 + 31 + 30 + 31 + 31) * 24 + 1.2 * 8760
    assert abs(reads.capacity_factor(1000.0, by[1998], 2026) - 1000 / h26) < 1e-12
    # a generator that started in July counts half a year; storage never counts; a planned one never counts
    gens = [{"nameplate_mw": 100, "status_group": "operating", "operating_ym": "2025-07", "energy_source": "NG",
             "prime_mover": "CT"},
            {"nameplate_mw": 50, "status_group": "operating", "operating_ym": "2010-01", "energy_source": "MWH",
             "prime_mover": "BA"},
            {"nameplate_mw": 70, "status_group": "planned", "planned_operation_ym": "2025-01"}]
    assert reads.capacity_factor(184 * 24 * 100 * 0.5, gens, 2025) == 0.5
    assert reads.capacity_factor(None, gens, 2025) is None and reads.capacity_factor(5, gens[1:], 2025) is None
    print("test_capacity_factor_by_hand ok")


# --------------------------------------------------------------------------- with a database

class Sources:
    """Serves the fixtures by URL. Knobs: the API's latest month, per-month API bodies, the index HTML, ERCOT
    documents that fail once."""

    def __init__(self):
        self.seen: list[str] = []
        self.api_end = "2026-08"
        self.api = {m: _b(f"eia860m_api_{m}.json") for m in ("2026-07", "2026-08")}
        self.index = _b("eia860m_index.html")
        self.fail_docs = {"1281327706": 1 + len(phttp.BACKOFF_SECONDS)}   # fails through the client's retries

    def __call__(self, req: httpx.Request) -> httpx.Response:
        u, q = str(req.url), dict(req.url.params)
        self.seen.append(u)
        if "operating-generator-capacity/data" in u:
            body = self.api.get(q["start"])
            if body is None:
                return httpx.Response(200, json={"response": {"total": "0", "data": []}})
            return httpx.Response(200, content=body)
        if u.startswith("https://api.eia.gov/v2/electricity/operating-generator-capacity/"):
            meta = json.loads(_b("eia860m_meta.json"))
            meta["response"]["endPeriod"] = self.api_end
            return httpx.Response(200, json=meta)
        if "facility-fuel/data" in u:
            return httpx.Response(200, content=_b("eia923_annual_2025.json"))
        if "facility-fuel" in u:
            return httpx.Response(200, content=_b("eia923_meta.json"))
        if u == eia860m.INDEX_URL:
            return httpx.Response(200, content=self.index, headers={"content-type": "text/html"})
        if u.endswith("september_generator2026.xlsx"):
            return httpx.Response(200, content=_b("eia860m_september_generator2026.xlsx"))
        if "_generator2026.xlsx" in u:
            return httpx.Response(301, headers={"location": "https://www.eia.gov/cneaf/electricity/"})
        if "eta-publications.lbl.gov" in u:
            return httpx.Response(200, content=_b("lbnl_queue_thru2025.xlsx"))
        if "IceDocListJsonWS" in u:
            return httpx.Response(200, content=_b("ercot_gis_list.json"))
        if "mirDownload" in u:
            if self.fail_docs.get(q["doclookupId"]):
                self.fail_docs[q["doclookupId"]] -= 1
                return httpx.Response(500, text="oops")
            return httpx.Response(200, content=_b("ercot_gis_2026-10.xlsx"))
        return httpx.Response(404, text="unexpected " + u)


def _patched():
    runner.PACE = {k: 0 for k in runner.PACE}
    phttp.BACKOFF_SECONDS = (0, 0, 0)
    lbnl.EDITIONS = {date(2025, 12, 31): "https://eta-publications.lbl.gov/sites/default/files/2026-05/"
                                         "lbnl_ix_queue_data_file_thru2025.xlsx"}


def _ds(out, source, i=0):
    return next(s for s in out["sources"] if s["source"] == source)["datasets"][i]


def _api_month(src: bytes, period: str, edit=None) -> bytes:
    body = json.loads(src)
    rows = body["response"]["data"]
    for r in rows:
        r["period"] = period
    if edit:
        rows[:] = edit(rows)
    body["response"]["total"] = str(len(rows))
    return json.dumps(body).encode()


def test_db_sync_history_reads_api():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    _patched()
    url = scratchdb.create(DB)
    saved = (config.settings.app_password, config.settings.public_pages, config.settings.eia_api_key)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        config.settings.eia_api_key = "test-key"
        src = Sources()
        client = httpx.AsyncClient(transport=httpx.MockTransport(src), follow_redirects=False)

        def sync(**kw):
            return asyncio.run(runner.sync(client=client, **kw))

        def count(sql):
            with Session() as s:
                return s.execute(text(sql)).scalar()

        # dry run: only the cheap metadata / listing requests
        out = sync(since=date(2026, 7, 1), dry_run=True)
        assert _ds(out, "eia860m")["months_to_fetch"] == 2 and _ds(out, "eia923")["years_to_fetch"] == [2025]
        assert _ds(out, "lbnl_queues")["editions_to_fetch"] == ["2025-12-31"]
        assert count("SELECT count(*) FROM power_generators") == 0
        assert not any("mirDownload" in u or "lbl.gov" in u or u.endswith(".xlsx") for u in src.seen)

        # ---------------- first run = the backfill (from 2026-07 here)
        out = sync(since=date(2026, 7, 1))
        api, fil = _ds(out, "eia860m", 0), _ds(out, "eia860m", 1)
        assert api["months"] == 2 and api["new"] == 7 and api["changed"] == 0 and api["left"] == 0, api
        assert fil["applied"].endswith("september_generator2026.xlsx") and fil["period"] == date(2026, 9, 1), fil
        assert fil["changed"] == 3, "Mountain Lake 5, 6, 7: operable in the API's August, retired in September"
        assert fil["new"] == 14 and fil["left"] == 0, fil
        gen = _ds(out, "eia923")
        assert gen["new"] == 3 and gen["years"] == 1
        lb = _ds(out, "lbnl_queues")["editions"][0]
        assert lb["new"] == 17 and lb["edition"] == date(2025, 12, 31)
        er = _ds(out, "ercot_gis")
        assert er["failed"] == 1 and len(er["editions"]) == 2 and er["editions"][1]["new"] == 12, er
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM power_generators")).scalar() == 21
            hist = s.execute(text("""SELECT plant_id, generator_id, period, change, status FROM power_generator_history
                                     ORDER BY plant_id, generator_id, period""")).all()
            ml5 = [h[2:] for h in hist if h[:2] == (1998, "5")]
            assert ml5 == [(date(2026, 7, 1), "new", "OP"), (date(2026, 9, 1), "changed", "RE")], ml5
            assert len(hist) == 7 + 3 + 14, "July: 7 new; August: identical, nothing; September: 3 + 14"
            row = s.execute(text("""SELECT status, retirement_ym, period, via, first_period FROM power_generators
                                    WHERE plant_id = 1998 AND generator_id = '5'""")).one()
            assert tuple(row) == ("RE", "2026-09", date(2026, 9, 1), "file", date(2026, 7, 1))
            # url + sha256 of every fetch; no body ever kept; the unposted file names recorded "not published"
            f = s.execute(text("""SELECT count(*), count(*) FILTER (WHERE body IS NOT NULL),
                                         count(*) FILTER (WHERE sha256 IS NULL AND ok AND NOT empty)
                                  FROM power_fetches WHERE dataset LIKE 'sites.%'""")).one()
            assert f[0] > 0 and f[1] == 0 and f[2] == 0, f
            vis = dict(s.execute(text("SELECT source, min(visibility) FROM power_queue_projects GROUP BY 1")).all())
            assert vis == {"lbnl_queues": "public", "ercot_gis": "public"}
            for t in ("compute_listings", "power_days", "power_series"):
                assert s.execute(text(f"SELECT count(*) FROM {t}")).scalar() == 0, t

        # ---------------- re-run: nothing new stored; only the failed ERCOT month is asked again
        n_hist, n_fetch = count("SELECT count(*) FROM power_generator_history"), len(src.seen)
        out = sync()
        assert _ds(out, "eia860m", 0)["months"] == 0 and _ds(out, "eia860m", 1)["applied"] is None
        g2 = _ds(out, "eia923")
        assert g2["new"] == 0 and g2["revised"] == 0 and g2["unchanged"] == 3, "923 re-reads the newest years"
        assert _ds(out, "lbnl_queues")["requests"] == 0
        er = _ds(out, "ercot_gis")
        assert [e["edition"] for e in er["editions"]] == [date(2026, 9, 30)] and er["editions"][0]["new"] == 12
        asked = src.seen[n_fetch:]
        assert sum("mirDownload" in u for u in asked) == 1 and not any(u.endswith(".xlsx") for u in asked)
        out = sync()
        assert _ds(out, "ercot_gis")["editions"] == [] and count("SELECT max(revisions) FROM power_plant_generation") == 0
        assert count("SELECT count(*) FROM power_generator_history") == n_hist, "re-runs add no history"
        assert count("SELECT count(*) FROM power_queue_projects") == 17 + 12 + 12

        # ---------------- a later API month: a derate, a planned retirement moved, and a generator gone
        def september(rows):    # Mountain Lake 5, 6, 7 retired in September: no longer operable
            return [r for r in rows if not (r["plantid"] == "1998" and r["generatorid"] in ("5", "6", "7"))]

        def october(rows):
            out = []
            for r in september(rows):
                if r["plantid"] == "9":
                    continue                                       # Copper 1 left the operable inventory
                if r["plantid"] == "298" and r["generatorid"] == "2":
                    r["net-summer-capacity-mw"] = "800"            # derate
                    r["planned-retirement-year-month"] = "2032-12"
                out.append(r)
            return out
        src.api["2026-09"] = _api_month(src.api["2026-08"], "2026-09", september)
        src.api["2026-10"] = _api_month(src.api["2026-08"], "2026-10", october)
        src.api_end = "2026-10"
        out = sync()
        api = _ds(out, "eia860m", 0)
        # left: Copper 1, and J K Smith's nine operable units, which only the file listed (the trimmed API answer
        # covers three plants). September changes nothing: the file already gave that month
        assert api["months"] == 2 and api["changed"] == 1 and api["left"] == 1 + 9 and api["new"] == 0, api
        with Session() as s:
            h = s.execute(text("""SELECT period, change, status, summer_mw, planned_retirement_ym FROM
                                  power_generator_history WHERE plant_id = 298 AND generator_id = '2'
                                  ORDER BY period""")).all()
            assert [tuple(x) for x in h] == [(date(2026, 7, 1), "new", "OP", 857.0, None),
                                             (date(2026, 10, 1), "changed", "OP", 800.0, "2032-12")], h
            left = s.execute(text("""SELECT change, status FROM power_generator_history WHERE plant_id = 9
                                     AND period = '2026-10-01'""")).one()
            assert tuple(left) == ("left", "RE")
            # September from the API is the file's month: the file outranks it, so Mountain Lake stays retired
            assert s.execute(text("""SELECT status FROM power_generators WHERE plant_id = 1998
                                     AND generator_id = '5'""")).scalar() == "RE"
        n_hist = count("SELECT count(*) FROM power_generator_history")
        sync()
        assert count("SELECT count(*) FROM power_generator_history") == n_hist

        # ---------------- reads (plain dicts)
        tx = {p["plant_id"]: p for p in reads.plants(state="TX", limit=None)}
        lime = tx[298]
        assert lime["year"] == 2025 and lime["capacity_factor"] == round(7523395 / ((893 + 956.8) * 8760), 4)
        assert lime["idle_mw"] == round(1849.8 * (1 - 7523395 / ((893 + 956.8) * 8760)), 3)
        assert lime["planned_retirement_ym"] == "2032-12" and lime["planned_retirement_mw"] == 956.8
        assert tx[9]["nameplate_mw"] == 0 and tx[9]["retired_mw"] == 86.9 and tx[9]["capacity_factor"] is None, \
            "Copper's generator left (retirement month unknown): not counted"
        order = [p["plant_id"] for p in reads.plants(sort="idle", limit=3)]
        assert order[0] == 298, order
        assert [p["plant_id"] for p in reads.plants(sort="retirement")] == [298]
        assert [p["plant_id"] for p in reads.plants(status="planned")] == [63798]
        assert [p["plant_id"] for p in reads.plants(technology="coal")] == [298]
        near = reads.plants_near(31.40, -96.25, km=10)
        assert [p["plant_id"] for p in near] == [298] and 2.0 < near[0]["distance_km"] < 2.6
        mn = reads.plants_in_county("MN", "Cottonwood County")
        assert [p["plant_id"] for p in mn] == [1998] and mn[0]["retired_mw"] == round(1.3 + 1.8 + 1.8 + 1.1 + 2.0, 3)
        # queued MW per county, active requests only, never summed across sources
        er = {(t["county"], t["kind"]): t for t in reads.queued_mw_by_county(source="ercot_gis")}
        pecos = 7.2 + 9.3 + 0 + 0 - 18 + 308.98 + 129.57 + 185.2 + 54.99
        assert abs(er[("Pecos", "generation")]["mw"] - pecos) < 1e-6 and er[("Pecos", "generation")]["projects"] == 9
        assert er[("Pecos", "generation")]["by_type"]["Battery"] == round(308.98 + 129.57 + 54.99, 3)
        assert er[("Reeves", "generation")]["mw"] == 307.0
        assert {t["edition"] for t in reads.queued_mw_by_county(source="ercot_gis")} == {date(2026, 10, 31)}
        lbt = {t["county"]: t for t in reads.queued_mw_by_county(source="lbnl_queues", state="TX")}
        assert abs(lbt["Pecos"]["mw"] - (7.2 + 9.3 - 18)) < 1e-9, "withdrawn requests are not queued MW"
        both = reads.queued_mw_by_county(state="TX")
        assert {t["source"] for t in both} == {"lbnl_queues", "ercot_gis"}, "one row per source, never summed"
        assert not reads.queued_mw_by_county(kind="load"), "no public source lists large loads by project yet"
        assert len(reads.queue_projects(source="ercot_gis", edition=date(2026, 9, 30), county="pecos")) == 9

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
            paths = ("/v1/ops/power/plants?state=TX", "/v1/ops/power/queues?county=Pecos",
                     "/v1/ops/power/sites/status")
            for p in paths:
                r = anon.get(p)
                assert r.status_code == 401 and "Limestone" not in r.text and "17INR0070" not in r.text, (p, r.text)
                r = keyed.get(p)
                assert r.status_code == 403 and "Limestone" not in r.text, (p, r.status_code)
                r = op.get(p)
                assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store", (p, r.text[:300])
            body = op.get("/v1/ops/power/plants?state=TX&sort=idle").json()
            assert body["data"][0]["plant_name"] == "Limestone" and body["meta"]["year"] == 2025
            assert op.get("/v1/ops/power/plants?sort=nope").status_code == 422
            body = op.get("/v1/ops/power/queues?source=ercot_gis&county=Pecos").json()
            assert len(body["data"]) == 9 and body["meta"]["county_totals"]["load"] == []
            assert body["meta"]["county_totals"]["generation"][0]["county"] == "Pecos"
            st = op.get("/v1/ops/power/sites/status").json()
            assert st["data"]["power_generators"]["rows"] == 21 and st["meta"]["total_bytes"] > 0
        finally:
            keys.verify = real
    finally:
        config.settings.app_password, config.settings.public_pages, config.settings.eia_api_key = saved
        scratchdb.drop(DB)
    print("test_db_sync_history_reads_api ok")


def test_unposted_file_names_are_not_published():
    """With the page's comment removed (EIA posting next month's link early), an unposted name answers a 301: it
    is recorded as "not published" and the newest real workbook is still applied."""
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    _patched()
    url = scratchdb.create(DB)
    try:
        normalize.SessionLocal = fixtures.session(url)
        config.settings.eia_api_key = "test-key"
        src = Sources()
        src.index = src.index.replace(b"<!--", b"").replace(b"-->", b"")
        client = httpx.AsyncClient(transport=httpx.MockTransport(src), follow_redirects=False)
        out = asyncio.run(runner.sync(["eia860m"], since=date(2026, 8, 1), client=client))
        fil = _ds(out, "eia860m", 1)
        assert fil["applied"].endswith("september_generator2026.xlsx") and fil["requests"] == 5, fil
        with normalize.SessionLocal() as s:
            rows = s.execute(text("""SELECT status_code, empty, ok FROM power_fetches
                                     WHERE dataset = 'sites.eia860m_file' AND url LIKE '%generator2026.xlsx'
                                     ORDER BY id""")).all()
        assert [tuple(r) for r in rows] == [(301, True, True)] * 3 + [(200, False, True)], rows
    finally:
        config.settings.eia_api_key = None
        scratchdb.drop(DB)
    print("test_unposted_file_names_are_not_published ok")


def test_migration_0045():
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    import db as dbmod
    from config import settings
    from store import power as P

    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == ["0045_power_sites"]
        assert script.get_revision("0045_power_sites").down_revision == "0043_lium_grafana_history"
        command.upgrade(cfg, "0045_power_sites")
        eng = create_engine(url)
        models = (P.PowerGenerator, P.PowerGeneratorHistory, P.PowerPlantGeneration, P.PowerQueueProject)
        with eng.connect() as c:
            for model in models:
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
            try:
                c.execute(text("INSERT INTO power_queue_projects (source, edition, project_id, kind, status, "
                               "visibility, updated_at) VALUES ('x', '2026-01-01', 'p', 'generation', 'active', "
                               "'secret', now())"))
                raise AssertionError("CHECK did not fire")
            except Exception as exc:  # noqa: BLE001
                assert "ck_power_queue_projects_visibility" in str(exc), exc
                c.rollback()
        command.downgrade(cfg, "0043_lium_grafana_history")
        with eng.connect() as c:
            for model in models:
                assert c.execute(text("SELECT to_regclass(:t)"), {"t": "public." + model.__tablename__}).scalar() is None
            assert c.execute(text("SELECT to_regclass('public.power_fetches')")).scalar() is not None
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.power_generators')")).scalar() is not None
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)
    print("test_migration_0045 ok")


def test_cli():
    from power.sites import __main__ as cli

    assert cli.main([]) == 2 and cli.main(["nope"]) == 2
    assert cli.main(["sync", "--source", "everything"]) == 2      # refused before any request
    print("test_cli ok")


if __name__ == "__main__":
    test_registry()
    test_parse()
    test_capacity_factor_by_hand()
    test_db_sync_history_reads_api()
    test_unposted_file_names_are_not_published()
    test_migration_0045()
    test_cli()
    print("all passed")
