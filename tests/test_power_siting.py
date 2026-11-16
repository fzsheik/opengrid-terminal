"""Site ranking (power/siting.py; methodology/power-siting.md): the county ranking and the point query over a
scratch database seeded from the REAL fixtures of the three site datasets, plus hand-built rows where no fixture
exists. Checks every part of one county by hand, the price grain labels, the ERCOT node -> county rule (and that it
refuses short, generic and ambiguous keys), missing parts shown as missing (never 0), weights changing the order,
the point query, and the admin-only reads (401 anonymous, 403 viewer, internal for an admin). No network.

Seeded from fixtures (tests/fixtures_power/, tests/fixtures_power/sites/; see test_power_sites.py,
test_power_nodal.py, test_power_waste.py for what each holds):
    eia860m_september_generator2026.xlsx, eia923_annual_2025.json   plants 9, 54, 298, 1998, 63798 and their 2025 MWh
    lbnl_queue_thru2025.xlsx, ercot_gis_2026-10.xlsx                 queue editions (Pecos, Reeves, ...)
    ercot_nodal_splist_trim.zip, ercot_nodal_np6905_..._trim.zip     ERCOT node catalogue and one priced interval
    spp_rt_2025-11-02_hubs.csv                                       SPP South hub, real-time, 300 intervals
    spp_ver_curtailments_2026-11-14.csv                              SPP curtailment, one day
    eia_region_ciso_erco_2025-11-02.json                             EIA-930 demand / net generation, CISO and ERCO
    ndic_mpr_2026_0[78]_bowman_trim.xlsx, wb_ggfd_2021_trim.xlsx     North Dakota and World Bank flaring
Hand-built (TEST DATA, not real plants' figures): plants 90001-90006 (Texas and Oklahoma), their 2025 MWh, four
ERCOT catalogue nodes, three days of one node's real-time prices and two days of the ERCOT hub average.

Run:  .venv/bin/python tests/test_power_siting.py
"""

import base64
import csv
import io
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

from power import flaring, nodal, siting, storage  # noqa: E402
from power.registry import Fetched  # noqa: E402
from power.sites import eia860m, eia923, ercot_gis, lbnl, store as site_store  # noqa: E402
from power.sources import eia, ercot, spp, spp_curtailment  # noqa: E402

FX = ROOT / "tests" / "fixtures_power"
SX = FX / "sites"
DB = "og_test_power_siting"
AS_OF = date(2026, 11, 16)


def _f(path: Path) -> Fetched:
    return Fetched("u", 200, 1, path.read_bytes())


def _close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


def _gen(pid, gid, name, state, county, ba, es, pm, mw, op="2001-01", group="operating", status="OP",
         retire=None, lat=None, lon=None):
    return {"plant_id": pid, "generator_id": gid, "plant_name": name, "entity_id": None, "entity_name": "Test",
            "state": state, "county": county, "latitude": lat, "longitude": lon, "ba_code": ba, "sector": None,
            "technology": None, "energy_source": es, "prime_mover": pm, "nameplate_mw": mw, "summer_mw": mw,
            "winter_mw": mw, "status": status, "status_group": group, "operating_ym": op,
            "planned_operation_ym": None, "planned_retirement_ym": retire, "retirement_ym": None}


# TEST DATA (hand-built): Trent Wind matches node TRENT_TRENT; two "Ranger" plants in two counties make RANGER_RN
# ambiguous; "Wise" is too short a key; "Central Energy Plant" only reduces to the generic CENTRAL
HAND = [
    _gen(90001, "WT1", "Trent Wind", "TX", "Nolan", "ERCO", "WND", "WT", 150.0, lat=32.43, lon=-100.13),
    _gen(90002, "PV1", "Ranger Solar", "TX", "Eastland", "ERCO", "SUN", "PV", 50.0),
    _gen(90003, "GT1", "Ranger", "TX", "Stephens", "ERCO", "NG", "GT", 120.0),
    _gen(90004, "CT1", "Wise", "TX", "Wise", "ERCO", "NG", "CT", 400.0),
    _gen(90004, "CT2", "Wise", "TX", "Wise", "ERCO", "NG", "CT", 300.0, retire="2028-06"),
    _gen(90006, "ST1", "Central Energy Plant", "TX", "Harris", "ERCO", "NG", "ST", 50.0),
    _gen(90005, "ST1", "Mooreland", "OK", "Woodward", "SWPP", "NG", "ST", 330.0, lat=36.43, lon=-99.22),
]
HAND_MWH = {90001: (394200.0, "TX"),          # 150 MW x 8,760 h x 0.30
            90004: (306600.0, "TX"),          # 700 MW x 8,760 h x 0.05: large and rarely running
            90005: (231264.0, "OK")}          # 330 MW x 8,760 h x 0.08
HAND_NODES = {"TRENT_TRENT": {"zone": "LZ_WEST", "substation": "TRENT", "node_type": "RN"},
              "RANGER_RN": {"zone": "LZ_WEST", "substation": "RANGER", "node_type": "RN"},
              "WISE_WISE_G1": {"zone": "LZ_NORTH", "substation": "WISE", "node_type": "RN"},
              "CENTRAL_CC1": {"zone": "LZ_HOUSTON", "substation": "CENTRAL", "node_type": "RN"}}
# TRENT_TRENT real-time, 3 days x 96 intervals: 48 at -5 and 48 at 30; 96 at 15; 96 at 40
TRENT_DAYS = {date(2026, 11, 13): [-5.0] * 48 + [30.0] * 48, date(2026, 11, 14): [15.0] * 96,
              date(2026, 11, 15): [40.0] * 96}
# ERCOT hub average real-time, 2 days: 24 at -2 and 72 at 25; 96 at 35
HUBAVG_DAYS = {date(2026, 11, 14): [-2.0] * 24 + [25.0] * 72, date(2026, 11, 15): [35.0] * 96}


def seed():
    # plants: the fixture workbook plus the hand-built rows, one inventory month
    rows = eia860m.parse_file((SX / "eia860m_september_generator2026.xlsx").read_bytes()) + HAND
    site_store.apply_generators(rows, date(2026, 9, 1), "file", "all", None)
    site_store.upsert_generation(2025, {**eia923.parse(_f(SX / "eia923_annual_2025.json")), **HAND_MWH}, None)
    site_store.replace_edition("lbnl_queues", date(2025, 12, 31), lbnl.parse((SX / "lbnl_queue_thru2025.xlsx")
                                                                             .read_bytes()), "public", None)
    site_store.replace_edition("ercot_gis", date(2026, 10, 31), ercot_gis.parse((SX / "ercot_gis_2026-10.xlsx")
                                                                               .read_bytes()), "public", None)
    # nodes: ERCOT's catalogue fixture, the hand-built catalogue rows, one real priced interval, Trent's days
    nodal.sync_nodes("ERCOT", ercot.parse_node_list((FX / "ercot_nodal_splist_trim.zip").read_bytes()), None)
    nodal.sync_nodes("ERCOT", HAND_NODES, None)
    ert = ercot.NODAL_DATASETS[0]
    day = date(2026, 11, 15)
    p = ert.parse(Fetched("u", 200, 1, (FX / "ercot_nodal_np6905_2026-11-15_2200_trim.zip").read_bytes(),
                          start=day, end=day))
    sums = nodal.summaries(ert, p, day)
    ids = nodal.sync_nodes("ERCOT", {k: p.nodes.get(k, {}) for k in sums}, day)
    nodal.upsert_days("rt", day, {ids[k]: v for k, v in sums.items()})
    for d, vals in TRENT_DAYS.items():
        ids = nodal.sync_nodes("ERCOT", {"TRENT_TRENT": HAND_NODES["TRENT_TRENT"]}, d)
        nodal.upsert_days("rt", d, {ids["TRENT_TRENT"]: nodal.summarize(vals)})
    # hub series: ERCOT hub average (hand-built), SPP South (fixture)
    ds = next(d for d in ercot.DATASETS if d.key == "ercot.rt_spp")
    sid = storage.sync_series([ds])
    storage.upsert_days({("ercot.rt.lmp.hb_hubavg", d): v for d, v in HUBAVG_DAYS.items()}, sid, None)
    for ds, path, d in ((next(x for x in spp.DATASETS if x.key == "spp.rt_lmp"), FX / "spp_rt_2025-11-02_hubs.csv",
                         date(2025, 11, 2)),
                        (eia.DATASETS[0], FX / "eia_region_ciso_erco_2025-11-02.json", date(2025, 11, 2)),
                        (spp_curtailment.DATASETS[0], FX / "spp_ver_curtailments_2026-11-14.csv", date(2026, 11, 14))):
        pts = ds.parse(Fetched("u", 200, 1, path.read_bytes(), start=d, end=d))
        sid = storage.sync_series([ds])
        res = storage.upsert_days(storage.to_days(ds, pts, d, d), sid, None)
        assert res["new"] > 0, (ds.key, res)
    # flaring: North Dakota July + August 2026 (Bowman), World Bank 2021
    for m in ("07", "08"):
        flaring.load_ndic((FX / f"ndic_mpr_2026_{m}_bowman_trim.xlsx").read_bytes(), None)
    flaring.load_worldbank((FX / "wb_ggfd_2021_trim.xlsx").read_bytes(), 2021, None)


# --------------------------------------------------------------------------- no database

def test_pure():
    w = siting.WEIGHTS
    assert abs(sum(v for v in w.values() if v > 0) - 1.0) < 1e-9 and w["queue"] < 0
    # every part at its best, nothing queued: 1.0; a missing part adds nothing and lowers coverage
    best = {"idle_mw": 500, "retiring_mw": 200, "rarely_running_plants": 3, "share_below_20": 1.0,
            "share_below_0": 1.0, "flared_average_mw": 80, "curtailed_average_mw": 5000, "net_exporter": True,
            "queued_mw": 0}
    s = siting.score(best)
    assert s["score"] == 1.0 and s["coverage"] == 1.0
    s = siting.score({**best, "share_below_20": None})
    assert s["score"] == 0.75 and s["parts"]["price_below_20"]["contribution"] is None
    assert _close(s["coverage"], (1.15 - 0.25) / 1.15, 1e-4)
    assert siting.score({**best, "queued_mw": 1000})["score"] == round(1 - 0.15 * 0.5, 4)
    assert siting.score({**best, "net_exporter": False})["parts"]["exporter"]["contribution"] == 0.0
    # weights: parsed, merged over the defaults, unknown names refused
    assert siting.parse_weights("idle:0.5, queue:-0.3") == {"idle": 0.5, "queue": -0.3}
    assert siting.weights_of({"idle": 1})["idle"] == 1.0 and siting.weights_of({"idle": 1})["queue"] == -0.15
    for bad in ("nope:1", "idle", "idle:x", "idle:nan"):
        try:
            siting.parse_weights(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    # the node / plant keys
    assert siting.plant_keys("Trent Wind") == ({"TRENTWIND", "TRENT"}, set())
    assert siting.plant_keys("Wise") == (set(), {"WISE"}), "shorter than MIN_KEY"
    assert siting.plant_keys("Central Energy Plant") == ({"CENTRALENERGYPLANT"}, {"CENTRAL"}), "generic"
    assert siting.node_keys("LEG_LEG_G1", "LEG") == (set(), {"LEG"})
    assert siting.node_keys("SHANNONW_RN", "COBSW") == ({"SHANNONW", "COBSW"}, set())
    # hubs without a node
    assert siting.hub_for("SPP", "OK", "woodward")[0] == "SPPSOUTH_HUB"
    assert siting.hub_for("CAISO", "CA", "san diego")[0] == "TH_SP15_GEN-APND"
    assert siting.hub_for("CAISO", "CA", "kern")[0] is None, "near Path 15 / 26: no guess"
    assert siting.hub_for("MISO", "MI", "x")[0] == "MICHIGAN.HUB" and siting.hub_for("MISO", "MN", "x")[0] == "MINN.HUB"
    assert siting.hub_for("MISO", "ND", "x")[0] == "INDIANA.HUB" and siting.hub_for("NYISO", "NY", "x")[0] is None
    assert siting.hub_for(None, "TN", "x")[0] is None
    print("test_pure ok")


# --------------------------------------------------------------------------- with a database

def _spp_hub_by_hand() -> tuple[float, float, int]:
    rows = [r for r in csv.reader(io.StringIO((FX / "spp_rt_2025-11-02_hubs.csv").read_text()))][1:]
    lmp = [float(r[4]) for r in rows if r[2] == "SPPSOUTH_HUB"]
    return sum(v < 20 for v in lmp) / len(lmp), sum(v < 0 for v in lmp) / len(lmp), len(lmp)


def _erco_net_by_hand() -> tuple[float, int]:
    data = json.loads((FX / "eia_region_ciso_erco_2025-11-02.json").read_text())["response"]["data"]
    by: dict[tuple[str, str], float] = {}
    for r in data:
        if r["respondent"] == "ERCO" and r["type"] in ("NG", "D") and r["value"] is not None:
            by[(r["period"], r["type"])] = float(r["value"])
    hours = [p for (p, t) in by if t == "NG" and (p, "D") in by]
    return sum(by[(h, "NG")] - by[(h, "D")] for h in hours), len(hours)


def test_db():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    url = scratchdb.create(DB)
    saved = (config.settings.app_password, config.settings.public_pages, config.settings.viewer_user,
             config.settings.viewer_password, config.settings.environment)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        seed()

        # ---------------- the node -> county rule, and its hit rate on this data
        nm = siting.match_nodes()
        st = nm["stats"]
        assert set(nm["matches"]) == {"TRENT_TRENT"}, nm["matches"]
        m = nm["matches"]["TRENT_TRENT"]
        assert (m["county"], m["plant_ids"], m["key"], m["zone"]) == ("Nolan", [90001], "TRENT", "LZ_WEST")
        refused = {r["node"]: r for r in nm["refused"]}
        assert refused["RANGER_RN"]["reason"] == "plants in more than one county"
        assert refused["RANGER_RN"]["counties"] == ["Eastland", "Stephens"]
        wise_r = refused["WISE_WISE_G1"]
        assert wise_r["reason"] == "key too short or generic" and wise_r["keys"] == ["WISE"]
        assert refused["CENTRAL_CC1"]["keys"] == ["CENTRAL"]
        # resource nodes: the fixture's 3 (SHANNONW_RN, YNG_WND_ALL, AMO_AMOCO_1: substations COBSW, TOMSW,
        # AMOCOOIL) + the priced combined-cycle logical node AMOCOOIL_CC1 + the 4 hand-built ones
        assert (st["resource_nodes"], st["matched"], st["refused_ambiguous"], st["refused_short_or_generic"]) == \
            (8, 1, 1, 2), st
        assert st["hit_rate"] == 0.125
        with Session() as s:
            fixture_nodes = s.execute(text("""SELECT count(*) FROM power_nodes WHERE iso = 'ERCOT'
                AND (substation IS NOT NULL OR node_type IN ('RN', 'PCCRN', 'LCCRN', 'PUN'))
                AND node NOT IN ('TRENT_TRENT', 'RANGER_RN', 'WISE_WISE_G1', 'CENTRAL_CC1')""")).scalar()
        assert fixture_nodes == 4, "the fixtures alone: 0 of 4 resource nodes match a fixture plant"
        old = siting.NODE_MATCH
        siting.NODE_MATCH = False
        try:
            assert siting.match_nodes()["matches"] == {}
            nolan = next(r for r in siting.ranking(state="TX", as_of=AS_OF)["rows"] if r["county"] == "Nolan")
            assert nolan["price"]["grain"] == "hub", "the rule switched off: the county falls back to the hub"
        finally:
            siting.NODE_MATCH = old

        # ---------------- the ranking
        out = siting.ranking(as_of=AS_OF, limit=None)
        rows = {(r["state"], r["county"]): r for r in out["rows"]}
        assert set(rows) == {("TX", "Nolan"), ("TX", "Eastland"), ("TX", "Stephens"), ("TX", "Wise"),
                             ("TX", "Harris"), ("TX", "Limestone"), ("TX", "El Paso"), ("TX", "Pecos"),
                             ("TX", "Reeves"), ("TX", "Hamilton"), ("TX", "Guadalupe"), ("OK", "Woodward"),
                             ("KY", "Clark"), ("MN", "Cottonwood"), ("AZ", "La Paz"), ("ND", "Bowman")}, set(rows)
        assert out["meta"]["counties"] == len(rows) and [r["rank"] for r in out["rows"]] == list(range(1, 17))
        assert out["meta"]["node_match"]["hit_rate"] == 0.125 and "large-load" in out["meta"]["large_loads"]

        # Nolan, TX by hand: every part
        n = rows[("TX", "Nolan")]
        assert (n["iso"], n["ba"]) == ("ERCOT", "ERCO")
        sp = n["spare"]
        assert sp["idle_mw"] == 105.0 and sp["plants_with_capacity_factor"] == 1 and sp["capacity_factor_year"] == 2025
        assert sp["retiring_mw"] == 0.0 and sp["rarely_running_plants"] == 0, "known zeros, not missing"
        pr = n["price"]
        assert (pr["grain"], pr["location"], pr["market"]) == ("node", "TRENT_TRENT", "rt"), pr
        assert pr["intervals"] == 288 and pr["days"] == 3
        assert pr["share_below_20"] == round(144 / 288, 4) and pr["share_below_0"] == round(48 / 288, 4)
        assert "Trent Wind" in pr["match"] and "TRENT" in pr["match"]
        assert n["waste"]["flared"] is None and "North Dakota only" in n["waste"]["flaring_note"]
        assert n["waste"]["curtailment"] is None, "no ERCOT curtailment stored: missing, not 0"
        co = n["competition"]
        assert co["queued_mw"] == 0.0 and co["source"] == "lbnl_queues", "Queued Up loaded: no request = 0 MW"
        assert co["large_loads_mw"] is None and "large-load" in co["large_loads"]
        net, hours = _erco_net_by_hand()
        g = n["grid"]
        assert g["ba"] == "ERCO" and g["hours"] == hours and _close(g["net_mwh"], round(net, 1), 0.05)
        assert g["net_exporter"] == (net > 0)
        exp = 0.30 * (105 / 200) + 0.25 * 0.5 + 0.05 * (48 / 288) + 0.10 * (1.0 if net > 0 else 0.0)
        assert _close(n["score"]["score"], round(exp, 4), 1e-4), (n["score"]["score"], exp)
        have = 0.30 + 0.10 + 0.05 + 0.25 + 0.05 + 0.10 + 0.15     # all but flaring and curtailment
        assert _close(n["score"]["coverage"], round(have / 1.15, 4), 1e-4)
        assert n["score"]["parts"]["flaring"]["contribution"] is None
        assert n["why"].startswith(f"Nolan, TX ranks {n['rank']} of 16") and "105 idle MW" in n["why"]
        assert "TRENT_TRENT (node grain)" in n["why"] and "missing: flaring, curtailment" in n["why"]

        # the grains: zone never here, hub from series or nodal summaries, none with its reason
        lime = rows[("TX", "Limestone")]
        assert (lime["price"]["grain"], lime["price"]["location"]) == ("hub", "HB_HUBAVG")
        assert lime["price"]["series"] == "ercot.rt.lmp.hb_hubavg" and lime["price"]["intervals"] == 192
        assert lime["price"]["share_below_20"] == 0.125 and lime["price"]["share_below_0"] == 0.125
        assert _close(lime["spare"]["idle_mw"], round(1849.8 * (1 - 7523395 / (1849.8 * 8760)), 1), 0.05)
        woodward = rows[("OK", "Woodward")]
        s20, s0, nint = _spp_hub_by_hand()
        wp = woodward["price"]
        assert (wp["grain"], wp["location"], wp["series"], wp["intervals"]) == ("hub", "SPPSOUTH_HUB",
                                                                                "spp.rt.lmp.sppsouth_hub", nint)
        assert wp["share_below_20"] == round(s20, 4) and wp["share_below_0"] == round(s0, 4)
        assert "reference hub" in wp["match"]
        cu = woodward["waste"]["curtailment"]
        from power import waste
        tot = waste.curtailed_mwh("SPP", date(2026, 11, 14), date(2026, 11, 14))["curtailed_mwh"]
        assert cu["grain"] == "regional" and cu["days"] == 1 and _close(cu["curtailed_mwh"], round(tot, 1), 0.05)
        assert _close(cu["average_mw"], round(tot / 24, 1), 0.05) and cu["estimate"] is False
        assert woodward["grid"] is None, "SWPP's EIA-930 series are not stored: missing"
        assert woodward["spare"]["rarely_running_plants"] == 1 and _close(woodward["spare"]["idle_mw"], 303.6, 0.05)
        wise = rows[("TX", "Wise")]
        assert wise["spare"]["retiring_mw"] == 300.0 and wise["spare"]["retiring_last_ym"] == "2028-06"
        assert wise["spare"]["rarely_running_plants"] == 1 and wise["price"]["grain"] == "hub"
        clark = rows[("KY", "Clark")]
        assert clark["iso"] == "PJM" and clark["spare"]["idle_mw"] is None, "no EIA-923 row: idle unknown, not 0"
        assert clark["spare"]["rarely_running_plants"] is None and clark["price"]["grain"] == "none"
        assert "no prices stored for WESTERN HUB" in clark["price"]["match"]
        elp = rows[("TX", "El Paso")]
        assert elp["iso"] is None and elp["ba"] == "EPE" and elp["price"]["grain"] == "none"
        assert "EPE" in elp["price"]["match"] and "no price" in elp["why"]
        pecos = rows[("TX", "Pecos")]
        assert pecos["iso"] == "ERCOT" and "market area" in pecos["ba_basis"] and pecos["spare"]["idle_mw"] is None
        assert pecos["competition"]["source"] == "ercot_gis", "an ERCOT county: the GIS report, never summed"
        assert _close(pecos["competition"]["queued_mw"], 7.2 + 9.3 - 18 + 308.98 + 129.57 + 185.2 + 54.99, 1e-6)
        assert {b["source"] for b in pecos["competition"]["by_source"]} == {"ercot_gis", "lbnl_queues"}
        assert "held back by" in pecos["why"] and "queued (ercot_gis)" in pecos["why"]
        bow = rows[("ND", "Bowman")]
        with Session() as s:
            mcf = s.execute(text("SELECT sum(volume_mcf) FROM power_flare_areas WHERE source = 'ndic' "
                                 "AND area = 'Bowman' AND period_type = 'month'")).scalar()
        assert _close(bow["waste"]["flared"]["volume_mcf"], mcf, 0.1) and bow["waste"]["flared"]["months"] == 2
        assert _close(bow["waste"]["flared"]["average_mw_derived"], round(mcf * 0.1037 / (2 * 730), 2), 0.011)
        assert bow["iso"] == "MISO" and bow["price"]["grain"] == "none" and bow["spare"]["idle_mw"] is None
        grains = {r["price"]["grain"] for r in out["rows"]}
        assert grains == {"node", "hub", "none"} and set(out["meta"]["price_grains"]) == grains
        assert all(r["price"]["grain"] in ("node", "zone", "hub", "none") and r["why"] for r in out["rows"])

        # the zone grain: Trent's node without prices in the window falls back to its load zone (LZ_WEST, from
        # the real NP6-905 interval); the hub after that
        z = siting.ranking(state="TX", as_of=AS_OF, until=date(2026, 11, 12), window_days=1)["rows"]
        nz = next(r for r in z if r["county"] == "Nolan")
        assert nz["price"]["grain"] == "none" and "no prices stored for HB_HUBAVG" in nz["price"]["match"], \
            "nothing priced on 2026-11-12: no price rather than a price from another day"
        z = siting.ranking(state="TX", as_of=AS_OF, until=date(2026, 11, 15), window_days=1)["rows"]
        nz = next(r for r in z if r["county"] == "Nolan")
        assert nz["price"]["grain"] == "node" and nz["price"]["share_below_20"] == 0.0   # 96 at 40
        with Session() as s:
            s.execute(text("DELETE FROM power_node_days WHERE node_id = (SELECT id FROM power_nodes "
                           "WHERE node = 'TRENT_TRENT')"))
            s.commit()
        nz = next(r for r in siting.ranking(state="TX", as_of=AS_OF)["rows"] if r["county"] == "Nolan")
        assert (nz["price"]["grain"], nz["price"]["location"]) == ("zone", "LZ_WEST"), nz["price"]
        assert nz["price"]["intervals"] == 1 and nz["price"]["share_below_20"] == 0.0     # 46.18 $/MWh
        assert "load zone" in nz["price"]["match"]
        for d, vals in TRENT_DAYS.items():
            ids = nodal.sync_nodes("ERCOT", {"TRENT_TRENT": HAND_NODES["TRENT_TRENT"]}, d)
            nodal.upsert_days("rt", d, {ids["TRENT_TRENT"]: nodal.summarize(vals)})

        # ---------------- weights change the order; filters
        order = [(r["state"], r["county"]) for r in siting.ranking(as_of=AS_OF)["rows"]]
        assert order[0] != ("ND", "Bowman")
        fl = {k: 0.0 for k in siting.WEIGHTS}
        fl["flaring"] = 1.0
        order2 = [(r["state"], r["county"]) for r in siting.ranking(as_of=AS_OF, weights=fl)["rows"]]
        assert order2[0] == ("ND", "Bowman") and order2 != order
        q = siting.ranking(as_of=AS_OF, weights={"queue": -5.0})["rows"]
        assert q[-1]["county"] == "Pecos", "a heavy queue penalty sends the most-queued county last"
        assert {r["county"] for r in siting.ranking(as_of=AS_OF, iso="SPP")["rows"]} == {"Woodward"}
        big = siting.ranking(as_of=AS_OF, min_idle_mw=300)["rows"]
        assert {r["county"] for r in big} == {"Limestone", "Wise", "Woodward"}
        assert {r["state"] for r in siting.ranking(state="ok", as_of=AS_OF)["rows"]} == {"OK"}
        try:
            siting.ranking(weights={"nope": 1})
            raise AssertionError("unknown weight accepted")
        except ValueError:
            pass

        # ---------------- the point query
        nr = siting.near(32.43, -100.13, 10, as_of=AS_OF)
        assert nr["county"]["county"] == "Nolan" and "nearest plant" in nr["county"]["basis"]
        assert nr["spare"]["idle_mw"] == 105.0 and nr["price"]["grain"] == "node"
        assert nr["price"]["location"] == "TRENT_TRENT" and "within the radius" in nr["price"]["match"]
        assert nr["score"]["score"] == n["score"]["score"], "same parts as the county here"
        lm = siting.near(31.40, -96.25, 10, as_of=AS_OF)
        assert [p["plant_id"] for p in lm["spare"]["top_plants"]] == [298] and lm["price"]["grain"] == "hub"
        assert 2.0 < lm["spare"]["top_plants"][0]["distance_km"] < 2.6
        nd = siting.near(46.082112, -103.635309, 5, as_of=AS_OF)
        assert nd["county"] is None and nd["spare"]["idle_mw"] is None and nd["price"]["grain"] == "none"
        assert nd["waste"]["flared"]["source"] == "ndic" and nd["waste"]["flared"]["months"] == 2
        assert nd["waste"]["flared_near"]["nearest"][0]["site_key"] == "t130r104s13"
        us = waste.flare_sites(country="USA", year=2021, limit=1, source="worldbank_ggfd")[0]
        wb = siting.near(us["lat"], us["lon"], 2, as_of=AS_OF)
        assert wb["waste"]["flared"]["source"] == "worldbank_ggfd" and wb["waste"]["flared"]["months"] == 12
        assert _close(wb["waste"]["flared"]["average_mw_derived"],
                      round(wb["waste"]["flared"]["energy_mwh_derived"] / 8760, 2), 0.011)

        # ---------------- the reads: admin only, internal
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
        paths = ("/v1/ops/power/sites/ranking", "/v1/ops/power/sites/ranking?state=TX&weights=idle:1",
                 "/v1/ops/power/sites/near?lat=32.43&lon=-100.13&km=10")
        for p in paths:
            r = tc.get(p)
            assert r.status_code == 401 and "Nolan" not in r.text, (p, r.status_code)
            r = tc.get(p, headers=viewer)
            assert r.status_code == 403 and "Nolan" not in r.text, (p, r.status_code)
            r = tc.get(p, headers=op)
            assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store", (p, r.text[:300])
            assert r.json()["meta"]["visibility"] == "internal" and r.json()["meta"]["kind"] == "derived"
        body = tc.get("/v1/ops/power/sites/ranking?iso=ERCOT&min_idle_mw=100", headers=op).json()
        assert {d["county"] for d in body["data"]} == {"Nolan", "Limestone", "Wise"}
        assert body["meta"]["weights"] == siting.WEIGHTS and body["meta"]["node_match"]["matched"] == 1
        assert body["meta"]["doc"] == "methodology/power-siting.md"
        body = tc.get("/v1/ops/power/sites/ranking?weights=flaring:5", headers=op).json()
        assert body["data"][0]["county"] == "Bowman" and body["meta"]["weights"]["flaring"] == 5.0
        assert tc.get("/v1/ops/power/sites/ranking?weights=nope:1", headers=op).status_code == 422
        assert tc.get("/v1/ops/power/sites/ranking?state=Texas", headers=op).status_code == 422
        assert tc.get("/v1/ops/power/sites/near?lat=32", headers=op).status_code == 422
        body = tc.get("/v1/ops/power/sites/near?lat=32.43&lon=-100.13&km=10", headers=op).json()
        assert body["data"]["price"]["location"] == "TRENT_TRENT" and body["meta"]["window_days"] == 90
        assert all(not p.startswith("/v1/power") for p in main.app.openapi()["paths"])
        assert not any("sites/ranking" in p for p in main.app.openapi()["paths"]), "outside the schema"

        print("example ranking (TEST DATA):")
        for r in siting.ranking(as_of=AS_OF, limit=16)["rows"]:
            print(f"  {r['rank']:>2} {r['score']['score']:.3f} {r['price']['grain']:<4} {r['why']}")
    finally:
        (config.settings.app_password, config.settings.public_pages, config.settings.viewer_user,
         config.settings.viewer_password, config.settings.environment) = saved
        scratchdb.drop(DB)
    print("test_db ok")


if __name__ == "__main__":
    test_pure()
    test_db()
    print("all passed")
