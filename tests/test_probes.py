"""OpenGrid's own probes: consensus rules, one measurement against a local server, raw store + rollups + scope on a
scratch database, the ingest API (scopes, validation), and a remote agent posting through the real app.

Run:  .venv/Scripts/python tests/test_probes.py
"""

import asyncio
import http.server
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from probes import consensus, measure, targets  # noqa: E402

DB = "og_test_probes"
UTC = timezone.utc


def _setup_db():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    return Session


# --------------------------------------------------------------------------- targets

def test_targets():
    assert len({t.id for t in targets.TARGETS}) == len(targets.TARGETS)
    from provider_meta import _ALL
    slugs = {m.name for m in _ALL}
    for t in targets.TARGETS:
        assert t.provider in slugs, f"{t.id}: {t.provider} is not a tracked provider"
        assert t.id.startswith(t.provider + ":") and t.url.startswith("https://") and t.accept and t.interval_s >= 60
        assert t.kind in ("api", "host", "website")
    assert len(targets.for_provider("aws")) == 4 and all(t.region for t in targets.for_provider("aws"))
    assert len({t.provider for t in targets.TARGETS}) >= 30, "nearly every tracked provider has a target"
    assert targets.BY_ID["aws:ec2-us-east-1"].accept == (400,)


# --------------------------------------------------------------------------- consensus (pure)

def _r(loc, ok, minute, ms=100, err=None):
    return {"location": loc, "ok": ok, "observed_at": datetime(2026, 10, 23, 10, minute, 0, tzinfo=UTC), "total_ms": ms if ok else None,
            "error_class": None if ok else (err or "timeout")}


def test_consensus():
    c = consensus
    assert c.classify_slot({"a": True, "b": True}) == ("ok", [])
    assert c.classify_slot({"a": True, "b": False, "c": True}) == ("regional", ["b"])
    assert c.classify_slot({"a": False, "b": False}) == ("global", ["a", "b"])
    assert c.classify_slot({"a": False}) == ("single", ["a"]), "one vantage point cannot call it global"
    assert c.slot_start(datetime(2026, 10, 23, 10, 7, 31, tzinfo=UTC), 300) == datetime(2026, 10, 23, 10, 5, tzinfo=UTC)
    assert c.percentile([100, 200, 300, 400], 0.5) == 250 and c.percentile([], 0.5) is None and c.percentile([None, 7], 0.5) == 7
    # three locations over three slots: ap fails slot 1 (regional), everyone fails slot 2 (global), slot 3 fine
    rows = [_r("eu", True, 0), _r("us", True, 0), _r("ap", False, 0, err="timeout"),
            _r("eu", False, 5, err="dns"), _r("us", False, 5, err="dns"), _r("ap", False, 5, err="dns"),
            _r("eu", True, 10), _r("us", True, 10), _r("ap", True, 10, ms=700)]
    out = c.rollup_hour(rows, 300)
    t = out["target"]
    assert (t["slots_ok"], t["slots_regional"], t["slots_global"], t["slots_single"]) == (1, 1, 1, 0)
    assert t["checks"] == 9 and t["ok_checks"] == 5 and abs(t["availability"] - 5 / 9 * 100) < 1e-9
    assert t["failing_locations"] == {"ap": 2, "eu": 1, "us": 1} and t["error_classes"] == {"timeout": 1, "dns": 3}
    assert t["locations"] == ["ap", "eu", "us"] and t["slot_minutes"] == 5
    assert out["locations"]["ap"]["latency_max_ms"] == 700 and out["locations"]["ap"]["error_classes"] == {"timeout": 1, "dns": 1}
    assert c.hour_scope(t)[0] == "global", "the worst slot wins"
    assert c.hour_scope({**t, "slots_global": 0})[0] == "regional" and "ap" in c.hour_scope({**t, "slots_global": 0})[1]
    assert c.hour_scope({**t, "slots_global": 0, "slots_regional": 0, "slots_single": 1})[0] == "unconfirmed"
    assert c.hour_scope({**t, "slots_global": 0, "slots_regional": 0, "slots_single": 0})[0] is None


# --------------------------------------------------------------------------- one measurement, against a local server

class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = {"/ok": 200, "/auth": 401, "/boom": 503, "/gone": 404}.get(self.path, 200)
        self.send_response(code)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, *a):
        pass


def test_measure():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def go():
        async with measure.client() as c:
            mk = lambda path, accept=(200,): targets.Target("t:x", "t", "x", f"http://127.0.0.1:{port}{path}", accept=accept)  # noqa: E731
            ok = await measure.measure(c, mk("/ok"), "here")
            assert ok["ok"] and ok["status_code"] == 200 and ok["error_class"] is None and ok["location"] == "here"
            assert ok["resolved_ip"] == "127.0.0.1" and isinstance(ok["total_ms"], int) and isinstance(ok["connect_ms"], int)
            assert ok["tls_ms"] is None, "plain http has no TLS phase"
            assert ok["observed_at"].tzinfo is not None
            assert (await measure.measure(c, mk("/auth", (401,)), "here"))["ok"], "a declared 401 counts as up"
            r = await measure.measure(c, mk("/boom"), "here")
            assert not r["ok"] and r["error_class"] == "http_5xx" and "503" in r["error"]
            r = await measure.measure(c, mk("/gone"), "here")
            assert not r["ok"] and r["error_class"] == "http_4xx"
            r = await measure.measure(c, mk("/ok", (201,)), "here")
            assert not r["ok"] and r["error_class"] == "http_status"
            r = await measure.measure(c, targets.Target("t:y", "t", "y", "http://127.0.0.1:1/"), "here")
            assert not r["ok"] and r["error_class"] in ("connect_failed", "connection_refused") and r["status_code"] is None
            r = await measure.measure(c, targets.Target("t:z", "t", "z", "https://no-such-host.invalid/"), "here")
            assert not r["ok"] and r["error_class"] == "dns" and r["dns_ms"] is None
    try:
        asyncio.run(go())
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- raw store, rollups, scope

def _res(tid, loc, when, ok=True, err="timeout", ms=120, status=None):
    return {"target_id": tid, "observed_at": when, "ok": ok, "status_code": status or (200 if ok else None),
            "error_class": None if ok else err, "error": None if ok else f"{err} talking to {tid}", "total_ms": ms if ok else None,
            "dns_ms": 3, "connect_ms": 10, "tls_ms": 20, "ttfb_ms": 50, "resolved_ip": "203.0.113.9"}


def test_store_rollup_scope_and_reasons():
    Session = _setup_db()
    try:
        from probes import store
        from sqlalchemy import func, select
        from store.uptime import ProbeResult, ProbeTargetHourly, ProviderIncident
        now = datetime.now(UTC)
        h1 = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)   # a whole past hour: no future timestamps
        h2 = h1 - timedelta(hours=1)
        slots = lambda h: [h + timedelta(minutes=5 * k, seconds=7) for k in range(12)]   # noqa: E731
        locs = ("eu-west", "us-east", "ap-southeast")
        def feed(tid, hour, fail_at=None, fail_locs=(), err="timeout"):
            for loc in locs:
                res = [_res(tid, loc, w, ok=not (fail_at is not None and k in fail_at and loc in fail_locs), err=err)
                       for k, w in enumerate(slots(hour))]
                out = store.record(res, f"agent-{loc}", loc, now)
                assert out["stored"] == 12 and out["refused"] == 0 and out["duplicates"] == 0, out
        aws = {r: f"aws:ec2-{r}" for r in ("us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1")}
        # hour h1: us-east-1 unreachable from ap only (regional); eu-west-1 from everywhere for 2 slots (global); others fine
        feed(aws["us-east-1"], h1, fail_at={3, 4, 5}, fail_locs={"ap-southeast"}, err="timeout")
        feed(aws["eu-west-1"], h1, fail_at={6, 7}, fail_locs=set(locs), err="dns")
        feed(aws["us-west-2"], h1)
        feed(aws["ap-southeast-1"], h1)
        # hour h2: every measured AWS region dips together
        for tid in aws.values():
            feed(tid, h2, fail_at={0}, fail_locs=set(locs), err="http_5xx")
        # a provider seen from ONE vantage point only
        store.record([_res("digitalocean:api", "local", w, ok=(k != 2), err="connect_timeout") for k, w in enumerate(slots(h1))], "web", "local", now)
        # an incident that overlaps h1 in us-east-1, and one for a different region that must not attach
        inc = lambda i, reg, scope: {"provider": "aws", "external_id": i, "source": "statuspage", "title": f"AWS {i}", "impact": "major",  # noqa: E731
                                     "status": "resolved", "started_at": h1 + timedelta(minutes=10), "resolved_at": h1 + timedelta(minutes=40),
                                     "url": "https://x", "scope": scope, "scope_basis": "t", "regions": reg, "components": [], "updates": []}
        from uptime import store as ustore
        ustore.upsert_incidents([inc("use1", ["us-east-1"], "regional"), inc("apse2", ["ap-southeast-2"], "regional")], now)

        r = store.rollup(hours=4, now=now)
        assert r["target_hours"] == 5 + 4, "h1: four AWS regions + DigitalOcean; h2: four AWS regions"
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(ProbeResult)) == (4 * 3 * 12) * 2 + 12
            th = {(x.target_id, x.bucket): x for x in s.scalars(select(ProbeTargetHourly))}
        use1 = th[(aws["us-east-1"], h1)]
        assert (use1.slots_ok, use1.slots_regional, use1.slots_global) == (9, 3, 0) and use1.failing_locations == {"ap-southeast": 3}
        assert use1.error_classes == {"timeout": 3} and use1.checks == 36 and use1.ok_checks == 33
        euw1 = th[(aws["eu-west-1"], h1)]
        assert (euw1.slots_global, euw1.slots_regional) == (2, 0) and euw1.error_classes == {"dns": 6}
        assert th[(aws["us-west-2"], h1)].ok_checks == th[(aws["us-west-2"], h1)].checks
        assert store.rollup(hours=4, now=now)["target_hours"] == r["target_hours"], "rollup is idempotent"
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(ProbeTargetHourly)) == r["target_hours"]

        d = store.dips("aws", 7)
        key = {(x["target_id"], x["bucket"]): x for x in d}
        a = key[(aws["us-east-1"], h1)]
        assert a["scope"] == "regional" and a["failing_locations"] == {"ap-southeast": 3} and a["error_classes"] == {"timeout": 3}
        assert a["region_scope"] == "some_regions" and "us-east-1" in a["region_basis"] and "eu-west-1" in a["region_basis"]
        assert [i["id"] for i in a["reported"]] == ["use1"], "an incident for another region is not attached"
        b = key[(aws["eu-west-1"], h1)]
        assert b["scope"] == "global" and b["region_scope"] == "some_regions" and [i["id"] for i in b["reported"]] == []
        assert (aws["us-west-2"], h1) not in key, "a clean hour is not a dip"
        c = key[(aws["us-west-2"], h2)]
        assert c["scope"] == "global" and c["region_scope"] == "all_regions" and c["error_classes"] == {"http_5xx": 3}
        assert d[0]["bucket"] >= d[-1]["bucket"], "newest first"
        do = store.dips("digitalocean", 7)
        assert len(do) == 1 and do[0]["scope"] == "unconfirmed" and do[0]["region_scope"] is None and do[0]["locations_reporting"] == ["local"]

        # the 'right now' view and the per-provider summary
        store.record([_res(aws["us-east-1"], "eu-west", now - timedelta(minutes=1), ok=True)], "x", "eu-west", now)
        store.record([_res(aws["us-east-1"], "ap-southeast", now - timedelta(minutes=1), ok=False, err="timeout")], "y", "ap-southeast", now)
        sm = {p["provider"]: p for p in store.summary(6)}
        assert sm["aws"]["locations"] == ["ap-southeast", "eu-west", "us-east"] and sm["aws"]["availability"] < 100
        t0 = next(t for t in sm["aws"]["targets"] if t["id"] == aws["us-east-1"])
        assert t0["slots"]["regional"] == 3 and t0["error_classes"] == {"timeout": 3, "http_5xx": 3} and t0["latency_p50_ms"] == 120, t0
        assert sm["nebius"]["status"] == "unknown" and sm["nebius"]["availability"] is None, "no results: unknown, not 100%"
        ser = store.target_series(aws["us-east-1"], 6)
        assert {x["scope"] for x in ser["hourly"]} == {"regional", "global"}
        assert set(ser["by_location"]) == set(locs) and ser["current"]["ap-southeast"]["ok"] is False

        # raw guards: unknown target, implausible time, duplicates
        bad = [_res("nope:x", "eu", now), _res(aws["us-east-1"], "eu", now + timedelta(hours=1)), _res(aws["us-east-1"], "eu", now - timedelta(days=2))]
        assert store.record(bad, "x", "eu-west", now) == {"received": 3, "stored": 0, "duplicates": 0, "refused": 3}
        again = store.record([_res(aws["us-east-1"], "eu-west", slots(h1)[0])], "agent-eu-west", "eu-west", now)
        assert again["stored"] == 0 and again["duplicates"] == 1, again
        assert [a["location"] for a in store.agents()][0] == "ap-southeast" and any(a["agent_id"] == "web" for a in store.agents())
        old = now - timedelta(days=3)
        with Session.begin() as s:
            s.add(ProbeResult(target_id=aws["us-east-1"], location="eu-west", observed_at=old, agent_id="x", ok=True, received_at=old))
        assert store.prune_raw(1) == 1 and store.prune_raw(1) == 0, "only the 3-day-old raw row goes; recent rows and rollups stay"
        with Session() as s:
            assert s.scalar(select(func.count()).select_from(ProbeTargetHourly)) == r["target_hours"]
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------- API + a remote agent through the real app

def test_api_and_agent():
    Session = _setup_db()
    try:
        import config
        config.settings.app_password = None
        import main
        from accounts import auth
        from fastapi.testclient import TestClient
        from probes import agent, store

        def as_(*scopes):
            main.app.dependency_overrides[auth.principal] = lambda: auth.Principal(kind="api_key", account_id=1, key_id=1, scopes=frozenset(scopes))
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        now = datetime.now(UTC)
        body = lambda **kw: {"agent_id": "eu-1", "location": "eu-west", "results": [  # noqa: E731
            {"target_id": "aws:ec2-us-east-1", "observed_at": (now - timedelta(minutes=1)).isoformat(), "ok": True, "status_code": 400, "total_ms": 90}], **kw}

        as_("data:read")
        assert client.post("/v1/probes/ingest", json=body()).status_code == 403, "a read-only key cannot post"
        as_("data:read", "probes:write")
        r = client.post("/v1/probes/ingest", json=body())
        assert r.status_code == 200 and r.json()["data"] == {"received": 1, "stored": 1, "duplicates": 0, "refused": 0}, r.text
        assert client.post("/v1/probes/ingest", json=body()).json()["data"]["duplicates"] == 1
        assert client.post("/v1/probes/ingest", json=body(location="EU West!")).status_code == 422
        assert client.post("/v1/probes/ingest", json=body(extra=1)).status_code == 422
        big = body(); big["results"] = big["results"] * 501
        assert client.post("/v1/probes/ingest", json=big).status_code == 422
        junk = body(); junk["results"][0]["total_ms"] = -5
        assert client.post("/v1/probes/ingest", json=junk).status_code == 422
        unk = body(); unk["results"][0]["target_id"] = "nope:x"
        assert client.post("/v1/probes/ingest", json=unk).json()["data"]["refused"] == 1

        as_("data:read")
        tg = client.get("/v1/probes/targets").json()
        assert tg["meta"]["count"] == len(targets.TARGETS) == len(tg["data"]) and tg["meta"]["source"] == "opengrid_probes"
        assert client.get("/v1/probes/agents").json()["data"][0]["location"] == "eu-west"
        store.rollup(hours=2)
        sm = client.get("/v1/probes/summary", params={"hours": 6}).json()
        assert {p["provider"] for p in sm["data"]} >= {"aws", "lambda", "runpod"} and sm["meta"]["agents"]
        d = client.get("/v1/probes/targets/aws:ec2-us-east-1").json()["data"]
        assert d["accept"] == [400] and "hourly" in d and "by_location" in d
        assert client.get("/v1/probes/targets/nope:x").status_code == 404
        assert client.get("/v1/probes/dips", params={"provider": "aws"}).status_code == 200
        assert client.get("/v1/probes/dips", params={"provider": "nobody"}).status_code == 404
        assert client.get("/v1/probes/dips", params={"provider": "aws", "days": 31}).status_code == 422
        rw = client.get("/v1/probes/raw", params={"target": "aws:ec2-us-east-1"}).json()["data"]
        assert rw and rw[0]["location"] == "eu-west" and rw[0]["agent_id"] == "eu-1"
        assert client.get("/v1/probes/raw", params={"target": "nope"}).status_code == 404
        assert client.get("/probes").status_code == 200, "the page route"

        # a remote agent, posting through the real app (no network: every probe fails fast and is recorded as a failure)
        as_("data:read", "probes:write")
        transport = httpx.ASGITransport(app=main.app)
        asyncio.run(agent.run("http://testserver", "opg_live_test", "ap-southeast", "ap-1", once=True, transport=transport))
        with Session() as s:
            from sqlalchemy import func, select
            from store.uptime import ProbeResult
            n = s.scalar(select(func.count()).select_from(ProbeResult).where(ProbeResult.location == "ap-southeast"))
        assert n == len(targets.TARGETS), n
        assert any(a["agent_id"] == "ap-1" and a["location"] == "ap-southeast" for a in store.agents())
        assert [t.id for t in agent.parse_targets([x.public() for x in targets.TARGETS])] == [t.id for t in targets.TARGETS]
    finally:
        main.app.dependency_overrides.clear()
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_targets, test_consensus, test_measure, test_store_rollup_scope_and_reasons, test_api_and_agent):
        t(); print(t.__name__, "ok")
