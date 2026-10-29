"""UptimeProject ingestion: parsing saved responses, the poll on a scratch database (idempotence, one failing
service, a failing list), and the /v1/uptime endpoints.

Run:  .venv/Scripts/python tests/test_uptime.py
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from uptime import fetch, sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_uptime"
DB = "og_test_uptime"


def _load(name: str):
    return json.loads((FIX / name).read_text())


def _setup_db():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    return Session


PERIODS: list[tuple[str, str]] = []  # every (service, period) the poller asked for


def _transport(fail_ids=(), list_status=200):
    """Serves the saved responses for every service id; `fail_ids` get a 503 on their detail call."""
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/v1/services":
            if list_status != 200:
                return httpx.Response(list_status, text="down")
            return httpx.Response(200, json=_load("services.json"))
        sid = path.split("/")[3]
        if sid in fail_ids:
            return httpx.Response(503, text="nope")
        if path.endswith("/uptime"):
            period = req.url.params["period"]
            PERIODS.append((sid, period))
            fix = "digitalocean_uptime_7d.json" if period == "7d" else "digitalocean_uptime_90d_daily.json"
            return httpx.Response(200, json={**_load(fix), "service_id": sid, "period": period})
        return httpx.Response(200, json=_load("digitalocean_detail.json"))
    return httpx.MockTransport(handler)


def _patch_client(**kw):
    def make():
        return httpx.AsyncClient(base_url="https://api.uptimeproject.org", transport=_transport(**kw))
    fetch.client = make


# --------------------------------------------------------------------------- parsing

def test_parse_services_keeps_only_tracked_providers():
    rows = fetch.parse_services(_load("services.json"))
    ids = {r["id"] for r in rows}
    assert "aws-ec2-us-east-1" in ids and "digitalocean" in ids and "ovh-cloud" in ids and "linode" in ids
    for gone in ("anthropic", "hetzner-cloud", "cloudflare-cdn", "stripe", "github"):
        assert gone not in ids, f"{gone} is not a provider OpenGrid tracks"
    assert {r["provider"] for r in rows} == {"aws", "azure", "gcp", "digitalocean", "linode", "ovhcloud", "scaleway"}
    ec2 = next(r for r in rows if r["id"] == "aws-ec2-eu-west-1")
    assert ec2["category"] == "iaas" and ec2["region"] == "eu-west-1" and ec2["provider"] == "aws"
    assert isinstance(ec2["availability_30d"], (int, float))
    try:
        fetch.parse_services({"not": "a list"})
        raise AssertionError("a non-list body must be refused")
    except ValueError:
        pass
    assert sources.provider_for("Hetzner") is None and sources.provider_for(None) is None


def test_parse_points_and_detail():
    pts = fetch.parse_points(_load("digitalocean_uptime_7d.json"))
    assert len(pts) == 169 and all(p["bucket"].tzinfo is not None for p in pts)
    assert pts[0]["latency_p50_ms"] == 133 and pts[0]["availability_pct"] == 100 and pts[0]["down_minutes"] == 0
    d = fetch.parse_detail(_load("digitalocean_detail.json"))
    assert len(d["probes"]) == 5 and {p["probe_id"] for p in d["probes"]} >= {"do-nyc3", "vlr-sgp"}
    assert d["checks"][0]["check_type"] == "http" and d["checks"][0]["interval_sec"] == 60
    # junk points are dropped, not stored with a made-up time
    assert fetch.parse_points({"points": [{"bucket": "not a time", "latency_p50_ms": 5}, "x", {"bucket": "2026-10-01T00:00:00Z"}]}) \
        == [{"bucket": fetch._ts("2026-10-01T00:00:00Z"), "availability_pct": None, "status": None,
             "down_minutes": None, "degraded_minutes": None, "latency_p50_ms": None}]


# --------------------------------------------------------------------------- poll + db

def test_poll_idempotent_and_partial_failure():
    Session = _setup_db()
    real = fetch.client
    try:
        from sqlalchemy import func, select
        from store.uptime import UptimeDaily, UptimeFetchLog, UptimeHourly, UptimeProbeSample, UptimeService
        from uptime import poll, store

        n_services = len(fetch.parse_services(_load("services.json")))
        _patch_client(fail_ids=("linode",))
        PERIODS.clear()
        r = poll.run()
        assert r["ok"] and r["services"] == n_services and r["failed"] == 1, r
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(UptimeService)) == n_services
            assert s.scalar(select(func.count()).select_from(UptimeHourly).where(UptimeHourly.service_id == "linode")) == 0
            ok_hours = s.scalar(select(func.count()).select_from(UptimeHourly))
            assert ok_hours == (n_services - 1) * 169
            # a service with no daily history is backfilled from the long period; daily buckets are their own table
            assert {p for _, p in PERIODS} == {"7d", "1y"} and s.scalar(select(func.count()).select_from(UptimeDaily)) == (n_services - 1) * 90
            probes = s.scalar(select(func.count()).select_from(UptimeProbeSample))
            assert probes == (n_services - 1) * 5
            log = s.scalars(select(UptimeFetchLog)).one()
            assert log.ok and "linode" in (log.error or "") and log.points == ok_hours + (n_services - 1) * 90
        # the same buckets again: hourly rows are rewritten in place, probe samples (a new observation) are appended
        _patch_client()
        PERIODS.clear()
        r2 = poll.run()
        assert r2["ok"] and r2["failed"] == 0, r2
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(UptimeHourly)) == n_services * 169
            assert s.scalar(select(func.count()).select_from(UptimeDaily)) == n_services * 90
            # already-backfilled services only refresh the recent month; linode (no daily yet) is backfilled now
            assert ("digitalocean", "30d") in PERIODS and ("digitalocean", "1y") not in PERIODS and ("linode", "1y") in PERIODS
            assert s.scalar(select(func.count()).select_from(UptimeProbeSample)) == probes + n_services * 5
            assert s.get(UptimeService, "digitalocean").checks[0]["target"].startswith("https://api.digitalocean.com")
        h = store.health()
        assert h["services"] == n_services and h["last_ok"] is True and h["history_since"] is not None
        assert h["daily_since"] is not None and h["daily_since"] < h["history_since"], "daily history reaches further back"
        # a failing service list is logged as a failed poll and changes nothing
        _patch_client(list_status=500)
        r3 = poll.run()
        assert r3["ok"] is False and "HTTP 500" in r3["error"]
        assert store.health()["last_ok"] is False and store.health()["last_ok_at"] is not None
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(UptimeService)) == n_services
    finally:
        fetch.client = real
        scratchdb.drop(DB)


def test_no_data_is_none_not_100():
    Session = _setup_db()
    try:
        from uptime import store
        assert store.services() == [] and store.latency_stats(["aws-ec2-us-east-1"], 24) == {}
        assert store.history("aws-ec2-us-east-1", 24) == [] and store.probes("aws-ec2-us-east-1") == []
        h = store.health()
        assert h["services"] == 0 and h["last_fetch_at"] is None and h["history_since"] is None
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------- endpoints

def test_endpoints():
    Session = _setup_db()
    real = fetch.client
    try:
        import config
        config.settings.app_password = None  # open local-dev operator for the smoke test
        import main
        from fastapi.testclient import TestClient
        from uptime import poll

        _patch_client()
        poll.run()
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})  # no context manager: no lifespan
        r = client.get("/v1/uptime/providers")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meta"]["methodology"] == "/methodology/uptime" and body["meta"]["source"] == "uptimeproject"
        assert "CC BY 4.0" in body["meta"]["attribution"]
        provs = {p["provider"]: p for p in body["data"]}
        measured = {k for k, p in provs.items() if p["measured"]}
        assert measured == {"aws", "azure", "gcp", "digitalocean", "linode", "ovhcloud", "scaleway"}
        assert len(provs) == body["meta"]["tracked_providers"] >= 32, "every tracked provider has a row"
        assert "lambda" in body["meta"]["not_covered"] and "aws" not in body["meta"]["not_covered"]
        lam = provs["lambda"]
        assert lam["measured"] is False and lam["status"] is None and lam["services"] == [] \
            and all(v is None for v in lam["availability"].values()) and lam["latency_p50_ms_24h"] is None, "no data, not zeros"
        do = provs["digitalocean"]
        assert do["availability"]["7d"] is not None and do["latency_p50_ms_24h"] is not None
        assert len(provs["aws"]["services"]) == 3, "three EC2 regions roll up into one provider row"
        assert len(client.get("/v1/uptime/services", params={"provider": "aws"}).json()["data"]) > 3
        assert client.get("/v1/uptime/services", params={"provider": "nobody"}).status_code == 404
        d = client.get("/v1/uptime/services/digitalocean", params={"hours": 24 * 30}).json()["data"]
        assert len(d["history"]) == 169 and len(d["probes"]) == 5 and d["checks"]
        assert len(d["daily"]) >= 29, "a 30-day window returns the daily buckets too"
        assert client.get("/v1/uptime/services/nope").status_code == 404
        assert client.get("/v1/uptime/services/digitalocean", params={"hours": 0}).status_code == 422
        h = client.get("/v1/uptime/health").json()["data"]
        assert h["last_ok"] is True
        assert client.get("/uptime").status_code == 200, "the page route"
    finally:
        fetch.client = real
        scratchdb.drop(DB)

# --------------------------------------------------------------------------- scope: regional vs global

def test_scope_pure():
    from uptime import scope as sc
    assert sc.regions_in("EU-NORTH1 - Identity and Access Management") == ["eu-north1"]
    assert sc.regions_in("Canada 1 Datacentre", "US 1 DataCenter") == ["canada-1", "us-1"]
    assert sc.regions_in("NYC1 droplet create", "ams3") == ["ams-3", "nyc-1"]
    assert sc.regions_in("us-east-1 and eu-west-1", "us-based customers") == ["eu-west-1", "us-east-1"]
    assert sc.regions_in("Disruption to Cluster Management Services in us-southcentral1", "in us-southcentral") == ["us-southcentral", "us-southcentral1"]
    assert sc.regions_in("CA1 - Storage backend maintenance", "NO1 and US1") == ["canada-1", "norway-1", "us-1"]
    assert sc.regions_in("ca1 lowercase is not shorthand", "Notice 1") == [], "shorthand needs the upper-case code"
    assert sc.regions_in(None, "") == []
    # reported scope
    assert sc.classify_incident("Partial IAM unavailability", ["EU-NORTH1 - IAM"], 207)[:2] == ("regional", ["eu-north1"])
    assert sc.classify_incident("Global API outage", [], 7)[0] == "global"
    assert sc.classify_incident("Slow", ["Global"], 7)[0] == "global", "a component literally named Global"
    assert sc.classify_incident("Portal slow", ["A", "B", "C", "D"], 6)[0] == "global", "touched 4 of 6 components"
    many = [f"{r} - API" for r in ("EU-NORTH1", "EU-NORTH2", "EU-SOUTH1", "EU-WEST1", "EU-WEST2")]
    assert sc.classify_incident("CLI login issues", many, 200)[0] == "global", "five named regions is not one region's problem"
    assert sc.classify_incident("CLI login issues", many[:4], 200)[0] == "regional"
    assert sc.classify_incident("Portal slow", ["Portal"], 7)[0] == "unspecified"
    assert sc.classify_incident("Portal slow", ["A", "B", "C"], 3)[0] == "unspecified", "tiny pages never trigger the share rule"
    assert sc.classify_gcp_incident(["global", "us-west1"]) [:2] == ("global", ["us-west1"])
    assert sc.classify_gcp_incident(["europe-west2"])[:2] == ("regional", ["europe-west2"])
    assert sc.classify_gcp_incident([])[0] == "unspecified"
    # measured scope
    regions = [{"id": "e1", "region": "us-east-1"}, {"id": "e2", "region": "eu-west-1"}, {"id": "e3", "region": "eu-central-1"}]
    down = {"down_minutes": 60, "degraded_minutes": 0}
    assert sc.classify_dip({"id": "e1", "region": "us-east-1", **down}, {"e1": down}, regions)[0] == "regional"
    assert sc.classify_dip({"id": "e1", "region": "us-east-1", **down}, {"e1": down, "e2": down, "e3": down}, regions)[0] == "global"
    assert sc.classify_dip({"id": "e1", "region": "us-east-1", **down}, {"e1": down, "e2": down}, regions)[0] == "regional"
    deg = {"down_minutes": 0, "degraded_minutes": 12}
    assert sc.classify_dip({"id": "e1", "region": "us-east-1", **deg}, {"e1": deg}, regions)[0] == "partial", \
        "some probes failing is never called regional"
    one = [{"id": "d", "region": None}]
    assert sc.classify_dip({"id": "d", "region": None, **down}, {"d": down}, one)[0] == "global"
    assert sc.classify_dip({"id": "d", "region": None, **deg}, {"d": deg}, one)[0] == "partial"
    assert sc.dip_kind(5, 0) == "down" and sc.dip_kind(0, 5) == "degraded" and sc.dip_kind(5, 5) == "mixed"


def test_parse_incident_fixtures():
    from uptime import incidents
    comps = _load("nebius_components.json")["components"]
    total = len([c for c in comps if not c.get("group")])
    rows = incidents.parse_statuspage("nebius", _load("nebius_incidents.json"), total, "https://status.nebius.com")
    assert len(rows) == 50 and all(r["provider"] == "nebius" and r["started_at"].tzinfo for r in rows)
    iam = next(r for r in rows if r["external_id"] == "9qs4wqcmjjgq")
    assert iam["scope"] == "regional" and iam["regions"] == ["eu-north1"], iam["scope_basis"]
    assert iam["title"] == "Partial IAM and console unavailability" and iam["updates"] and "10 minutes" in iam["updates"][-1]["body"]
    assert iam["url"].startswith("https://stspg.io/")
    assert {r["scope"] for r in rows} <= {"global", "regional", "unspecified"} and any(r["scope"] == "regional" for r in rows)
    g = incidents.parse_gcp("gcp", _load("gcp_incidents.json"), "https://status.cloud.google.com")
    assert len(g) == 8
    eu = next(r for r in g if r["regions"] == ["europe-west2"])
    assert eu["scope"] == "regional" and eu["resolved_at"] is not None and eu["components"]
    glob = next(r for r in g if r["scope"] == "global")
    assert "us-west1" in glob["regions"] and "global" not in glob["regions"]
    for bad in ({"incidents": "x"}, [], None):
        try:
            incidents.parse_statuspage("x", bad, None, "u")
            raise AssertionError("a malformed statuspage body must be refused")
        except ValueError:
            pass


def _svc(store, sid, provider, region, name="x", category="iaas"):
    from datetime import datetime, timezone
    store.upsert_service({"id": sid, "provider": provider, "name": name, "upstream_provider": provider, "category": category,
                          "region": region, "homepage_url": None, "status_page_url": None, "current_status": "up",
                          "checks_count": 2, "availability_24h": 100, "availability_7d": 100, "availability_30d": 100,
                          "availability_90d": 100}, datetime.now(timezone.utc))


def _pt(bucket, down=0, deg=0, lat=100):
    return {"bucket": bucket, "availability_pct": 100 - (down + deg) / 0.6, "status": "down" if down else "degraded" if deg else "up",
            "down_minutes": down, "degraded_minutes": deg, "latency_p50_ms": lat}


def test_dips_scope_and_reported_cause():
    from datetime import datetime, timedelta, timezone
    Session = _setup_db()
    try:
        from uptime import store
        now = datetime.now(timezone.utc)
        h = now.replace(minute=0, second=0, microsecond=0)
        t_reg, t_glob, t_part = h - timedelta(hours=5), h - timedelta(hours=3), h - timedelta(hours=1)
        for sid, reg in (("aws-ec2-us-east-1", "us-east-1"), ("aws-ec2-eu-west-1", "eu-west-1"), ("aws-ec2-eu-central-1", "eu-central-1")):
            _svc(store, sid, "aws", reg, "AWS EC2")
        _svc(store, "aws-s3-us-east-1", "aws", "us-east-1", "AWS S3", category="object-storage")
        base = [_pt(h - timedelta(hours=k)) for k in range(8)]
        def with_dips(sid, dips):
            pts = [dips.get(p["bucket"], p) for p in base]
            store.save_points(sid, pts, now)
        with_dips("aws-ec2-us-east-1", {t_reg: _pt(t_reg, down=30), t_glob: _pt(t_glob, down=60), t_part: _pt(t_part, deg=8)})
        with_dips("aws-ec2-eu-west-1", {t_glob: _pt(t_glob, down=60)})
        with_dips("aws-ec2-eu-central-1", {t_glob: _pt(t_glob, down=45)})
        store.save_points("aws-s3-us-east-1", [_pt(t_reg, down=50)], now)  # not a compute service: never a dip row
        # a regional incident for us-east-1 that overlaps t_reg, and one for another region that must not attach
        inc = lambda i, regions, scope, start: {  # noqa: E731
            "provider": "aws", "external_id": i, "source": "statuspage", "title": f"Incident {i}", "impact": "major", "status": "resolved",
            "started_at": start, "resolved_at": start + timedelta(hours=1), "url": "https://x", "scope": scope, "scope_basis": "t",
            "regions": regions, "components": [], "updates": []}
        store.upsert_incidents([inc("a", ["us-east-1"], "regional", t_reg), inc("b", ["ap-south-1"], "regional", t_reg),
                                inc("c", [], "global", t_glob)], now)
        d = store.dips("aws", 7)
        key = {(x["service_id"], x["bucket"]): x for x in d}
        assert all(x["service_id"].startswith("aws-ec2") for x in d), "only compute services produce dips"
        reg = key[("aws-ec2-us-east-1", t_reg)]
        assert reg["scope"] == "regional" and reg["kind"] == "down" and reg["resolution"] == "hour"
        assert [r["id"] for r in reg["reported"]] == ["a"], "a regional incident for another region is not the cause"
        assert key[("aws-ec2-us-east-1", t_glob)]["scope"] == "global" and key[("aws-ec2-eu-west-1", t_glob)]["scope"] == "global"
        assert [r["id"] for r in key[("aws-ec2-eu-west-1", t_glob)]["reported"]] == ["c"]
        part = key[("aws-ec2-us-east-1", t_part)]
        assert part["scope"] == "partial" and part["kind"] == "degraded" and part["reported"] == []
        assert len(d) == 5 and d[0]["bucket"] >= d[-1]["bucket"], "newest first"
        assert len(store.dips("aws", 7, service_id="aws-ec2-eu-west-1")) == 1
        assert store.dips("lambda", 7) == [] and store.dips("aws", 90) == [], "the 90-day view reads the (empty) daily table"
    finally:
        scratchdb.drop(DB)


def _incident_transport():
    def handler(req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        if host == "status.cloud.google.com":
            return httpx.Response(200, json=_load("gcp_incidents.json"))
        if host == "status.lambda.ai":
            return httpx.Response(503, text="down")  # one page down: the others still load
        if path.endswith("/incidents.json"):
            return httpx.Response(200, json=_load("nebius_incidents.json"))
        if path.endswith("/components.json"):
            return httpx.Response(200, json=_load("nebius_components.json"))
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_incident_poll_and_endpoints():
    Session = _setup_db()
    from uptime import incidents
    real = incidents._client
    try:
        import config
        config.settings.app_password = None
        import main
        from fastapi.testclient import TestClient
        from uptime import poll, store

        incidents._client = lambda: httpx.AsyncClient(transport=_incident_transport())
        r = incidents.run()
        assert set(r["failed"]) == {"lambda"} and r["incidents"] == 4 * 50 + 8, r
        again = incidents.run()  # idempotent: same rows, updated in place
        from sqlalchemy import func, select
        from store.uptime import ProviderIncident
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(ProviderIncident)) == 4 * 50 + 8
        assert again["incidents"] == r["incidents"]
        _patch_client()
        poll.run()
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        inc = client.get("/v1/uptime/incidents", params={"provider": "gcp", "days": 730}).json()
        assert inc["meta"]["reported_not_measured"] is True and len(inc["data"]) == 8
        assert {x["scope"] for x in inc["data"]} >= {"global", "regional"}
        assert client.get("/v1/uptime/incidents", params={"scope": "bogus"}).status_code == 422
        assert client.get("/v1/uptime/incidents", params={"provider": "nobody"}).status_code == 404
        eu = client.get("/v1/uptime/incidents", params={"provider": "gcp", "days": 730, "scope": "regional"}).json()["data"]
        assert eu and all(x["scope"] == "regional" for x in eu) and "updates" in eu[0]
        provs = {p["provider"]: p for p in client.get("/v1/uptime/providers").json()["data"]}
        assert provs["nebius"]["measured"] is False and provs["nebius"]["incidents_30d"] is not None, \
            "a provider with no probe still shows the incidents it reported"
        assert provs["aws"]["incidents_30d"] is None and provs["lambda"]["incidents_30d"] is None, "no feed is None, not zero"
        d = client.get("/v1/uptime/dips", params={"provider": "digitalocean", "days": 7})
        assert d.status_code == 200 and "scope" in d.json()["meta"]["scope_note"]
        assert client.get("/v1/uptime/dips", params={"provider": "digitalocean", "days": 91}).status_code == 422
        assert client.get("/v1/uptime/dips").status_code == 422
        assert client.get("/v1/uptime/dips", params={"provider": "nobody"}).status_code == 404
    finally:
        incidents._client = real
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_parse_services_keeps_only_tracked_providers, test_parse_points_and_detail,
              test_poll_idempotent_and_partial_failure, test_no_data_is_none_not_100, test_endpoints,
              test_scope_pure, test_parse_incident_fixtures, test_dips_scope_and_reported_cause,
              test_incident_poll_and_endpoints):
        t(); print(t.__name__, "ok")
