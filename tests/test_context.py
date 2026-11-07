"""Market context (context/, api/context.py): EIA and SEC EDGAR parsers over REAL saved responses, the quarter
derivation (reported vs derived, first filing wins, revisions), the region -> state mapping, raw-first storage with
failures, the public API, sources off until configured, and the rule that nothing statistical imports context/.
No network: every response is a fixture, served through httpx.MockTransport.

Fixtures (real responses, 2026-11-01):
    fixtures_context/eia_retail_sales.json   EIA v2 retail-sales, monthly price, IND+COM (7 states + US + 2 census
                                             divisions kept), fetched with EIA's public DEMO_KEY
    fixtures_context/{nvda,amzn}_companyfacts.json  companyfacts cut to the concepts read (+3 unrelated), since 2023
    fixtures_context/nvda_submissions.json   the newest 40 filings
    fixtures_context/nvda_instance.xml       NVIDIA's 10-Q for the quarter to 2026-07-26: the revenue facts and
                                             their contexts, as filed

Run:  .venv/bin/python tests/test_context.py
"""

import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import httpx  # noqa: E402
from context import edgar, eia, geo  # noqa: E402
from context import series as cs  # noqa: E402

FX = ROOT / "tests" / "fixtures_context"


def _j(n):
    return json.loads((FX / n).read_text())


def test_eia():
    payload = eia.reshape(_j("eia_retail_sales.json"))
    assert {r["stateid"] for r in payload["data"]} <= set(geo.STATES) | {"US"}, "census divisions dropped"
    pts = eia.points(payload)
    va = sorted((p for p in pts if p["series_id"] == "eia.retail_price.ind.VA"), key=lambda p: p["period_end"])
    assert va and va[-1]["period_start"] == date(2026, 8, 1) and va[-1]["period_end"] == date(2026, 8, 31)
    assert va[-1]["value"] == D("9.98") and va[-1]["unit"] == "cents_per_kwh" and va[-1]["published_at"] is None
    assert any(p["series_id"] == "eia.retail_price.com.US" for p in pts)
    # blank price: skipped, never 0; an unexpected unit: refused
    blank = {"data": [{"period": "2026-08", "stateid": "VA", "sectorid": "IND", "price": None, "price-units": "cents per kilowatt-hour"}]}
    assert eia.points(blank) == []
    try:
        eia.points({"data": [{"period": "2026-08", "stateid": "VA", "sectorid": "IND", "price": "1", "price-units": "dollars per MWh"}]})
        raise AssertionError("unit change accepted")
    except ValueError:
        pass
    for bad in ({}, {"response": {}}, {"response": {"data": [{"stateid": "NEW", "sectorid": "IND"}]}}):
        try:
            eia.reshape(bad)
            raise AssertionError(f"reshape accepted {bad}")
        except ValueError:
            pass
    p = dict(eia.params("k", date(2026, 11, 1)))
    assert p["start"] == "2023-11" and p["frequency"] == "monthly" and p["data[0]"] == "price"
    assert [v for k, v in eia.params("k") if k == "facets[sectorid][]"] == ["IND", "COM"]
    cat = eia.catalog()
    assert len(cat) == 2 * (len(geo.STATES) + 1) and all(c["attribution"].startswith("Source: U.S. Energy") for c in cat)
    print("test_eia ok")


def test_edgar_facts_and_quarters():
    nv = edgar.reshape_facts(_j("nvda_companyfacts.json"), edgar.REVENUE + edgar.CAPEX)
    assert set(nv["facts"]) <= set(edgar.REVENUE) | set(edgar.CAPEX), "unrelated concepts dropped"
    pts = {(p["series_id"], p["period_end"]): p for p in edgar.facts_points("NVDA", nv)}
    q2 = pts[("sec.nvda.revenue_total", date(2026, 7, 26))]
    assert q2["value"] == D("96221000000") and q2["basis"] == "reported" and q2["meta"]["form"] == "10-Q"
    assert q2["published_at"] == datetime(2026, 8, 26, 23, 59, 59, tzinfo=timezone.utc)
    q4 = pts[("sec.nvda.revenue_total", date(2026, 1, 25))]   # NVIDIA reports no Q4 three-month figure
    assert q4["basis"] == "derived" and q4["value"] == D("68127000000") and "minus" in q4["meta"]["derived_from"]
    nv_capex = [p for p in pts.values() if p["series_id"] == "sec.nvda.capex"]
    assert nv_capex and {p["meta"]["concept"] for p in nv_capex} == {"PaymentsToAcquireProductiveAssets"}, "the concept in use now"
    am = edgar.reshape_facts(_j("amzn_companyfacts.json"), edgar.CAPEX)
    cap = {p["period_end"]: p for p in edgar.facts_points("AMZN", am)}
    assert cap[date(2026, 9, 30)]["value"] == D("77259000000") and cap[date(2025, 12, 31)]["basis"] == "derived"
    assert all(p["series_id"] == "sec.amzn.capex" for p in cap.values())
    # quarterize rules on synthetic facts
    f = [{"start": "2026-01-01", "end": "2026-03-31", "val": 10, "filed": "2026-04-30", "accn": "a"},
         {"start": "2026-01-01", "end": "2026-06-30", "val": 25, "filed": "2026-07-30", "accn": "b"},
         {"start": "2026-01-01", "end": "2026-09-30", "val": 45, "filed": "2026-10-30", "accn": "c"},
         {"start": "2026-01-01", "end": "2026-03-31", "val": 11, "filed": "2027-04-30", "accn": "d"}]  # restated later
    q = {(r["start"].isoformat(), r["end"].isoformat()): r for r in edgar.quarterize(f)}
    assert q[("2026-01-01", "2026-03-31")]["val"] == 10 and q[("2026-01-01", "2026-03-31")]["revised"]["val"] == "11"
    assert q[("2026-04-01", "2026-06-30")]["val"] == 15 and q[("2026-04-01", "2026-06-30")]["filed"] == "2026-07-30"
    assert q[("2026-07-01", "2026-09-30")]["val"] == 20 and q[("2026-07-01", "2026-09-30")]["basis"] == "derived"
    assert edgar.quarterize(f, since=date(2026, 7, 1)) and all(r["end"] >= date(2026, 7, 1) for r in edgar.quarterize(f, since=date(2026, 7, 1)))
    print("test_edgar_facts_and_quarters ok")


def test_edgar_instance_segments():
    xml = (FX / "nvda_instance.xml").read_bytes()
    facts = edgar.parse_instance(xml, edgar.REVENUE, edgar.SEGMENTS["NVDA"])
    assert {(f["start"], f["end"], f["val"]) for f in facts} == {
        ("2026-04-27", "2026-07-26", "89023000000"), ("2025-04-28", "2025-07-27", "41096000000"),
        ("2026-01-26", "2026-07-26", "164269000000"), ("2025-01-27", "2025-07-27", "80208000000")}
    # the same facts under the segment axis (Compute & Networking) are a different thing: never matched
    assert edgar.parse_instance(xml, edgar.REVENUE, {"us-gaap:StatementBusinessSegmentsAxis": "nvda:DataCenterMember"}) == []
    pts = {p["period_end"]: p for p in edgar.segment_points("NVDA", [{"accn": "0001045810-26-000075", "filed": "2026-08-26",
                                                                      "form": "10-Q", "facts": facts}])}
    assert pts[date(2026, 7, 26)]["value"] == D("89023000000") and pts[date(2026, 7, 26)]["basis"] == "reported"
    assert pts[date(2026, 4, 26)]["value"] == D("75246000000") and pts[date(2026, 4, 26)]["basis"] == "derived"  # H1 - Q2
    assert pts[date(2025, 4, 27)]["value"] == D("39112000000")
    subs = edgar.reshape_submissions(_j("nvda_submissions.json"))
    assert subs["filings"] and all(f["form"] in ("10-Q", "10-K") for f in subs["filings"]) and len(subs["filings"]) <= edgar.MAX_FILINGS
    assert edgar.instance_doc("nvda-20260726.htm") == "nvda-20260726_htm.xml"
    try:
        edgar.instance_doc("x.pdf")
        raise AssertionError("non-htm accepted")
    except ValueError:
        pass
    assert {c["series_id"] for c in edgar.catalog()} >= {"sec.nvda.revenue_data_center", "sec.amd.revenue_data_center",
                                                         "sec.msft.capex", "sec.crwv.capex", "sec.nvda.revenue_total"}
    print("test_edgar_instance_segments ok")


def test_geo():
    cases = [(("aws", "us-east-1", None), "VA"), (("gcp", "us-central1", None), "IA"), (("azure", "eastus2", None), "VA"),
             (("oracle", "us-phoenix-1", None), "AZ"), (("crusoe", "US, Culpeper, VA", None), "VA"),
             (("vast", "Arizona, US", None), "AZ"), (("aws", "eu-west-2", None), None), (("aws", "us-east-1", "DE"), None),
             (("crusoe", "NL, Amsterdam; US, Ashburn, VA", None), None), (("lium", "Columbus", None), None),
             (("lambda", "us-east-1", None), None), (("x", None, "US"), None)]
    for args, want in cases:
        assert geo.us_state(*args) == want, (args, geo.us_state(*args), want)
    assert geo.market_area("VA") == "PJM" and geo.market_area("TX") == "ERCOT" and geo.market_area("IL") is None
    print("test_geo ok")


def test_never_imported_by_statistics():
    pat = re.compile(r"^\s*(import context\b|from context\b)", re.M)
    users = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                   if ".venv" not in p.parts and "tests" not in p.parts and p.parts[len(ROOT.parts)] != "context"
                   and pat.search(p.read_text(encoding="utf-8", errors="ignore")))
    # power/geo.py reads context/geo.py's documented region -> state tables (data, not context series).
    assert users == ["api/context.py", "power/geo.py"], users
    print("test_never_imported_by_statistics ok")


# --------------------------------------------------------------------------- database + API

DB = "og_test_context"


def mock_client(fail_eia=False):
    nv_facts, am_facts, subs, inst = _j("nvda_companyfacts.json"), _j("amzn_companyfacts.json"), _j("nvda_submissions.json"), (FX / "nvda_instance.xml").read_bytes()
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.headers.get("user-agent")))
        u = str(req.url)
        if u.startswith(eia.URL):
            return httpx.Response(500, text="boom") if fail_eia else httpx.Response(200, json=_j("eia_retail_sales.json"))
        if "companyfacts/CIK0001045810" in u:
            return httpx.Response(200, json=nv_facts)
        if "companyfacts/CIK0001018724" in u:
            return httpx.Response(200, json=am_facts)
        if "companyfacts" in u:
            return httpx.Response(404, json={"message": "not in fixture"})
        if "submissions/CIK0001045810" in u:
            return httpx.Response(200, json=subs)
        if "submissions" in u:
            return httpx.Response(404, json={})
        if "/Archives/edgar/data/1045810/" in u and u.endswith("_htm.xml"):
            return httpx.Response(200, content=inst)   # every NVIDIA filing serves the one fixture instance
        return httpx.Response(404, text="unknown")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def test_db_and_api():
    import config
    import fixtures
    import normalize
    from sqlalchemy import text

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        # off until configured
        config.settings.eia_api_key = None
        config.settings.sec_user_agent = None
        assert cs.enabled() == {"eia": False, "sec_edgar": False}
        assert asyncio.run(cs.refresh(mock_client()[0]))["sources"] == []
        config.settings.eia_api_key = "test-key"
        config.settings.sec_user_agent = "OpenGrid Test test@example.com"
        cs.SEC_PACE_SECONDS = 0
        client, seen = mock_client(fail_eia=True)
        out = asyncio.run(cs.refresh(client))
        eia_out, sec_out = out["sources"]
        assert eia_out == {"source": "eia", "ok": False}
        assert sec_out["new"] > 0 and sec_out["failed"] == len(edgar.COMPANIES) - 2 + 1  # 6 companyfacts 404 + AMD submissions
        assert all(ua == "OpenGrid Test test@example.com" for u, ua in seen if "sec.gov" in u)
        with Session() as s:
            fails = s.execute(text("SELECT source, key, ok, error FROM context_snapshots WHERE NOT ok")).all()
            assert any(f.source == "eia" and "test-key" not in (f.error or "") for f in fails), "failure stored, key never"
        # second run: EIA OK; NVIDIA instances already stored are not fetched again
        client, seen2 = mock_client()
        out = asyncio.run(cs.refresh(client))
        assert out["sources"][0]["ok"] and out["sources"][0]["new"] > 0
        assert not [u for u, _ in seen2 if u.endswith("_htm.xml")], "instances fetched once"
        # a revision: the same period with a different value
        p = {"series_id": "eia.retail_price.ind.VA", "period_start": date(2026, 8, 1), "period_end": date(2026, 8, 31),
             "value": D("10.50"), "unit": "cents_per_kwh", "source": "eia", "basis": "reported", "published_at": None, "meta": {}}
        assert cs.upsert([p], None) == {"new": 0, "revised": 1}
        assert cs.upsert([p], None) == {"new": 0, "revised": 0}
        # listings in VA and TX for the power join; a London one is ignored
        from models import ComputeListing
        now = datetime.now(timezone.utc)
        mk = lambda prov, lid, region: ComputeListing(  # noqa: E731
            provider=prov, sku=lid, listing_id=lid, raw_gpu_name="H100", canonical_gpu_name="NVIDIA H100 80GB SXM5",
            gpu_count=8, region=region, country=None, price_per_gpu_hour=D("3"), price_per_instance_hour=D("24"),
            currency="USD", market_type="on_demand", provider_tier=None, interruptible=False, available=True,
            capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=now)
        normalize.save([mk("aws", "p5.48xlarge", "us-east-1"), mk("crusoe", "h100", "US, Dallas, TX"), mk("aws", "lon", "eu-west-2")])

        config.settings.app_password = None
        import main
        from api import context as ctx_api
        from api import pages
        from fastapi.testclient import TestClient
        assert "macro" in pages.PUBLIC_V1
        ctx_api._catalog.cache_clear()
        c = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        cat = c.get("/v1/macro/series", params={"limit": 1000}).json()
        assert "Context, not cause" in cat["meta"]["rule"] and cat["meta"]["enabled"] == {"eia": True, "sec_edgar": True}
        rows = {r["series_id"]: r for r in cat["data"]}
        assert rows["eia.retail_price.ind.VA"]["latest"]["value"] == 10.5 and rows["sec.nvda.revenue_data_center"]["points"] == 4
        assert rows["sec.msft.capex"]["points"] == 0 and rows["sec.msft.capex"]["latest"] is None   # 404 in the fixture
        nv = c.get("/v1/macro/series/sec.nvda.revenue_data_center/history?since=2026-01-01").json()
        assert {p["series_id"] for p in nv["data"]} == {"sec.nvda.revenue_data_center"}
        assert all(p["period_end"] >= "2026-01-01" for p in nv["data"]) and nv["meta"]["series"]["unit"] == "usd"
        assert c.get("/v1/macro/series/sec.nvda.revenue_total").json()["data"]["series_id"] == "sec.nvda.revenue_total"
        assert c.get("/v1/macro/series/nope").status_code == 404
        assert c.get("/v1/macro/series/nope/history").status_code == 404
        assert {r["source"] for r in c.get("/v1/macro/series?source=sec_edgar").json()["data"]} == {"sec_edgar"}
        pw = {r["state"]: r for r in c.get("/v1/macro/power").json()["data"]}
        assert set(pw) == {"VA", "TX"} and pw["VA"]["market_area"] == "PJM" and pw["VA"]["price_ind"]["value"] == 10.5
        assert pw["TX"]["providers"] == ["crusoe"] and pw["TX"]["price_ind"]["period"] == "2026-08"
    finally:
        config.settings.eia_api_key = None
        config.settings.sec_user_agent = None
        scratchdb.drop(DB)
    print("test_db_and_api ok")


if __name__ == "__main__":
    test_eia()
    test_edgar_facts_and_quarters()
    test_edgar_instance_segments()
    test_geo()
    test_never_imported_by_statistics()
    test_db_and_api()
    print("all passed")
