"""Final independent review: regressions for the problems found before the first real (Lambda) launch,
plus an end-to-end validation cycle through the REAL Lambda adapter against a simulated Lambda API
(httpx.MockTransport shaped on https://cloud.lambda.ai/api/v1/openapi.json). No real provider is called.

    1  the operator's default ssh_public_key never reaches a customer machine
    2  POST /v1/route: a crashed / stale attempt that already created a deployment is never re-run
    3  kill switch pressed while an approval re-validates: the launch is refused at the last check
    4  provision answer arriving after reconciliation adopted the instance: no 500, state kept
    5  the reconcile / tracker job crashing pages the operator (first failure, then every 30th)
    6  Lambda list_instances uses the documented non-paginated form and still follows tokens
    7  full Lambda validation cycle: start -> approve -> booting -> active -> deadline auto-terminate ->
       termination confirmed (list + status) -> billed once -> validation_report marks it validated
    8  migrations 0010-0013 on a database at 0009 holding legacy routing rows

Run:  .venv/Scripts/python tests/test_final_review.py
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fixtures  # noqa: E402
import scratchdb  # noqa: E402
import test_execution_core as ec  # noqa: E402  (harness: Fake adapter, seed, helpers; its main() is not run)

import normalize  # noqa: E402
from accounts.auth import OPERATOR  # noqa: E402
from analytics import rollups  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import (adapters, control, deployments, engine, idempotency, quotes, reconcile,  # noqa: E402
                     scoring, tracker, transactions, validation)
from routing.adapters.lambda_labs import LambdaAdapter  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from tables import ComputeListingRow  # noqa: E402

DB = "og_test_review"
MIG_DB = "og_test_review_mig"
OPKEY = "ssh-ed25519 " + "A" * 68 + " operator@opengrid"


# --------------------------------------------------------------------------
# 1. ssh key policy
# --------------------------------------------------------------------------

def test_operator_public_key_never_on_customer_launch():
    saved = settings.routing_launch_defaults
    settings.routing_launch_defaults = {"lambda": {"ssh_public_key": OPKEY, "ssh_key": "operator-key"}}
    try:
        cls = adapters.get("lambda")
        spec, problem = engine.launch_spec_for("lambda", None, purpose="customer", credential_source="opengrid",
                                               adapter_cls=cls)
        assert problem is None and spec.ssh_public_key is None and spec.ssh_key is None, spec
        assert cls({"api_key": "k"}).missing_launch(spec, None) == ["ssh_key"], "refused, not launched with our key"
        mine = ec.pubkey(3, "customer")   # a structurally valid key (0014: public keys are validated)
        spec, _ = engine.launch_spec_for("lambda", {"ssh_public_key": mine}, purpose="customer",
                                         credential_source="opengrid", adapter_cls=cls)
        assert spec.ssh_public_key == mine
        spec, _ = engine.launch_spec_for("lambda", None, purpose="validation", credential_source="opengrid",
                                         adapter_cls=cls)
        assert spec.ssh_public_key == OPKEY and spec.ssh_key == "operator-key", "validation keeps the operator key"
    finally:
        settings.routing_launch_defaults = saved


# --------------------------------------------------------------------------
# 2. route idempotency after a crash
# --------------------------------------------------------------------------

def test_route_key_not_rerun_after_crash_created_a_deployment():
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    who = ec.key(21)
    s = ec.spec("syn_a")
    chk = idempotency.deployments_since(who)

    def crashing_route():
        engine.route(s, who)            # creates a pending deployment ...
        raise RuntimeError("worker killed after the deployment was written")   # ... then the request dies

    try:
        idempotency.run(who=who, scope="route", key="crash-1", body={"b": 1}, fn=crashing_route, reclaim_check=chk)
        raise AssertionError("expected the crash")
    except RuntimeError:
        pass
    n = ec.count("deployments", "account_id = 21")
    assert n == 1
    try:
        idempotency.run(who=who, scope="route", key="crash-1", body={"b": 1}, fn=lambda: engine.route(s, who),
                        reclaim_check=chk)
        raise AssertionError("a failed key that created a deployment must not be re-run")
    except HTTPException as e:
        assert e.status_code == 409 and e.detail["code"] == "idempotency_outcome_unknown", e.detail
        assert len(e.detail["resources"]) == 1
    assert ec.count("deployments", "account_id = 21") == n, "no second deployment"
    # a failure that created nothing is still reclaimable (the client may retry with the same key)
    try:
        idempotency.run(who=who, scope="route", key="crash-2", body={"b": 2},
                        fn=lambda: (_ for _ in ()).throw(RuntimeError("db blip before anything was written")),
                        reclaim_check=chk)
    except RuntimeError:
        pass
    # the guard looks at deployments created since the key was claimed; crash-1's deployment predates crash-2
    code, payload, replayed = idempotency.run(who=who, scope="route", key="crash-2", body={"b": 2},
                                              fn=lambda: engine.route(s, who), reclaim_check=chk)
    assert code == 202 and not replayed
    # and the HTTP endpoint wires the guard in
    main, c = ec._client()
    try:
        ec._as(main, ec.key(22))
        body = {**ec.BODY, "duration_hours": 3}
        from api.routing import RouteBody
        h = idempotency.request_hash(RouteBody(**body).model_dump(mode="json"))
        assert idempotency.claim("acct:22", "route", "http-crash", h)[0] == "new"
        engine.route(ec.spec("syn_a"), ec.key(22))       # the "crashed" attempt's deployment
        with normalize.SessionLocal.begin() as ss:
            ss.execute(text("UPDATE idempotency_keys SET status = 'failed' WHERE key = 'http-crash'"))
        r = c.post("/v1/route", json=body, headers={"Idempotency-Key": "http-crash"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_outcome_unknown", r.text
        assert ec.count("deployments", "account_id = 22") == 1
    finally:
        main.app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# 3. kill switch during approval
# --------------------------------------------------------------------------

def test_kill_switch_during_approval_refuses_the_launch():
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    code, out = engine.route(ec.spec("syn_a"), ec.key(23))
    rr, qid = out["route_request_id"], out["quote"]["quote_id"]
    real = quotes.revalidate

    def revalidate_then_kill(qrow, a):
        r = real(qrow, a)
        control.kill_all("incident while the approval was re-validating", "oncall")   # pressed mid-approval
        return r

    quotes.revalidate = revalidate_then_kill
    try:
        code, res = engine.approve(rr, OPERATOR, quote_id=qid)
    finally:
        quotes.revalidate = real
    assert not ec.calls("provision"), "kill switch must win over an in-flight approval"
    assert res["launch"]["code"] == "launch_not_permitted" and res["status"] == "pending_approval", res
    d = deployments.for_request(rr)
    assert d.status == "pending_approval" and d.launch_token is None
    q = quotes.get(qid)
    assert q["status"] == "active", "the quote was not consumed"
    ec.mode("SUPERVISED")


# --------------------------------------------------------------------------
# 4. provision answer after reconciliation adopted the instance
# --------------------------------------------------------------------------

def test_late_provision_answer_after_adoption():
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    code, out = engine.route(ec.spec("syn_a"), ec.key(24))
    rr, qid = out["route_request_id"], out["quote"]["quote_id"]
    dep_id = deployments.for_request(rr).deployment_id
    ec.Fake.MODE["syn_a"] = "timeout"
    real = deployments.launch

    def adopted_meanwhile(res):
        # what reconcile._adopt does when it finds og-<dep> while the call is still in flight
        with normalize.SessionLocal.begin() as s:
            row = s.get(deployments.Deployment, dep_id, with_for_update=True)
            row.provider_instance_id = "i-adopted"
            deployments.transition(row, "running", "adopted: provider reports running",
                                   {"state": "running", "basis": "test"}, actor="reconciler", s=s)

    deployments.launch = lambda *a, **k: real(*a, **{**k, "after_provider_call": adopted_meanwhile})
    try:
        code, res = engine.approve(rr, OPERATOR, quote_id=qid)     # used to raise IllegalTransition (HTTP 500)
    finally:
        deployments.launch = real
        ec.Fake.MODE.clear()
    d = ec.dep(dep_id)
    assert d.status == "running" and d.provider_instance_id == "i-adopted", (d.status, d.provider_instance_id)
    assert len(ec.calls("provision")) == 1 and res["launch"]["launched"] is True


# --------------------------------------------------------------------------
# 5. job crash alerts
# --------------------------------------------------------------------------

def test_reconcile_and_tracker_job_crash_alerts():
    sent = []
    real_alert, real_run, real_track = tracker.alert, reconcile.run_once, tracker.track
    tracker.alert = lambda kind, subject, message, **kw: sent.append((kind, subject))
    reconcile.run_once = lambda **kw: (_ for _ in ()).throw(RuntimeError("database unavailable"))
    tracker.track = lambda: (_ for _ in ()).throw(RuntimeError("database unavailable"))
    try:
        for _ in range(31):
            try:
                reconcile._reconcile_job()
                raise AssertionError("must re-raise so jobs.py records the failure")
            except RuntimeError:
                pass
        assert sent == [("reconciliation_failed", "job:reconcile")] * 2, sent      # 1st and 30th
        try:
            tracker._track_job()
        except RuntimeError:
            pass
        assert sent[-1] == ("reconciliation_failed", "job:routing_tracker")
        reconcile.run_once = lambda **kw: {"run_id": 1, "status": "ok"}
        reconcile._reconcile_job()
        assert "reconcile" not in reconcile._JOB_FAILS, "a success resets the counter"
    finally:
        tracker.alert, reconcile.run_once, tracker.track = real_alert, real_run, real_track
        reconcile._JOB_FAILS.clear()


# --------------------------------------------------------------------------
# 6. Lambda list
# --------------------------------------------------------------------------

def test_lambda_list_unpaginated():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if req.url.params.get("page_token") == "p2":
            return httpx.Response(200, json={"data": [{"id": "b", "name": "og-x", "status": "active"}], "page_token": None})
        return httpx.Response(200, json={"data": [{"id": "a", "name": "og-y", "status": "booting"}], "page_token": "p2"})

    rows = LambdaAdapter({"api_key": "k"}, transport=httpx.MockTransport(handler)).list_instances()
    assert [r.instance_id for r in rows] == ["a", "b"] and [r.state for r in rows] == ["pending", "running"]
    assert seen[0] == {}, f"first call must not opt in to (newest-first) paging: {seen[0]}"
    assert seen[1] == {"page_token": "p2"}


# --------------------------------------------------------------------------
# 7. Lambda validation cycle against a simulated Lambda API
# --------------------------------------------------------------------------

LAMBDA_LAUNCH_FIELDS = {"region_name", "instance_type_name", "ssh_key_names", "file_system_names", "file_system_mounts",
                        "hostname", "name", "image", "user_data", "tags", "firewall_rulesets"}


class SimLambda:
    """The Lambda Cloud API surface the adapter uses, per the OpenAPI spec (field names, codes, list semantics:
    GET /instances lists only instances that still exist; a terminated instance 404s)."""

    def __init__(self):
        self.inst, self.launches, self.terminates, self.n = {}, [], [], 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        import json
        if req.headers.get("authorization") != "Bearer test-lambda-key":
            return httpx.Response(401, json={"error": {"code": "global/invalid-api-key", "message": "API key was invalid"}})
        p = req.url.path
        if req.method == "GET" and p == "/api/v1/instance-types":
            return httpx.Response(200, json={"data": {"gpu_1x_a10": {
                "instance_type": {"name": "gpu_1x_a10", "gpu_description": "A10 (24 GB PCIe)", "price_cents_per_hour": 75,
                                  "specs": {"vcpus": 30, "memory_gib": 200, "storage_gib": 1400, "gpus": 1}},
                "regions_with_capacity_available": [{"name": "us-east-1", "description": "Virginia, USA"}]}}})
        if req.method == "POST" and p == "/api/v1/instance-operations/launch":
            b = json.loads(req.content)
            assert set(b) <= LAMBDA_LAUNCH_FIELDS, f"unknown launch fields {set(b) - LAMBDA_LAUNCH_FIELDS}"
            assert {"region_name", "instance_type_name", "ssh_key_names"} <= set(b)
            assert len(b["ssh_key_names"]) == 1 and len(b.get("name", "")) <= 64
            for t in b.get("tags") or []:
                assert set(t) == {"key", "value"} and len(t["key"]) <= 55 and len(t["value"]) <= 128
            self.launches.append(b)
            self.n += 1
            iid = f"{self.n:032x}"
            self.inst[iid] = {"id": iid, "name": b.get("name"), "status": "booting", "region": {"name": b["region_name"]},
                              "instance_type": {"name": b["instance_type_name"], "price_cents_per_hour": 75,
                                                "specs": {"gpus": 1}}, "ssh_key_names": b["ssh_key_names"],
                              "tags": b.get("tags") or []}
            return httpx.Response(200, json={"data": {"instance_ids": [iid]}})
        if req.method == "GET" and p == "/api/v1/instances":
            return httpx.Response(200, json={"data": list(self.inst.values()), "page_token": None})
        if req.method == "GET" and p.startswith("/api/v1/instances/"):
            iid = p.rsplit("/", 1)[1]
            if iid not in self.inst:
                return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist",
                                                           "message": "Specified instance does not exist."}})
            return httpx.Response(200, json={"data": self.inst[iid]})
        if req.method == "POST" and p == "/api/v1/instance-operations/terminate":
            ids = json.loads(req.content)["instance_ids"]
            self.terminates.append(ids)
            missing = [i for i in ids if i not in self.inst]
            if missing:
                return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist", "message": "x"}})
            for i in ids:
                self.inst[i]["status"] = "terminating"
            return httpx.Response(200, json={"data": {"terminated_instances": [self.inst[i] for i in ids]}})
        return httpx.Response(599, json={"error": f"unmocked {req.method} {p}"})


def test_lambda_validation_cycle_end_to_end():
    ec.reset()
    sim = SimLambda()
    saved = (settings.lambda_api_key, settings.routing_launch_defaults)
    settings.lambda_api_key = "test-lambda-key"
    settings.routing_launch_defaults = {"lambda": {"ssh_key": "operator-key"}}
    adapters.TRANSPORTS["lambda"] = httpx.MockTransport(sim)
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        s.add(ComputeListingRow(
            provider="lambda", listing_id="gpu_1x_a10:us-east-1", sku="gpu_1x_a10", raw_gpu_name="A10 (24 GB PCIe)",
            canonical_gpu_name="NVIDIA A10 24GB", gpu_count=1, region="us-east-1", country="US",
            price_per_gpu_hour=Decimal("0.75"), price_per_instance_hour=Decimal("0.75"), currency="USD",
            market_type="on_demand", provider_tier=None, interruptible=False, available=True, capacity=None,
            capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=now, first_seen_at=now))
    try:
        ec.mode("SUPERVISED")
        # an unvalidated Lambda adapter can never take customer compute
        assert not control.launch_permission("lambda", purpose="customer")[0]
        ec.validation_ready("lambda")   # 0014: the validation gate needs drills, healthy workers, ops alerts
        v = validation.start_validation("lambda", by="operator:founder")
        rr, dep_id, qid = v["route_request_id"], v["deployment_id"], v["quote_id"]
        assert ec.dep(dep_id).status == "pending_approval" and not sim.launches, "nothing launches before approval"
        code, res = engine.approve(rr, OPERATOR, quote_id=qid)
        assert res["status"] == "provisioned", res
        assert len(sim.launches) == 1
        b = sim.launches[0]
        name = f"og-{dep_id}"
        assert b["name"] == name and b["instance_type_name"] == "gpu_1x_a10" and b["region_name"] == "us-east-1"
        assert b["ssh_key_names"] == ["operator-key"] and {"key": "opengrid", "value": name} in b["tags"]
        d = ec.dep(dep_id)
        assert d.status == "provisioning" and d.credential_ref == "platform:lambda" and d.terminate_deadline_at
        assert 29 <= (d.terminate_deadline_at - d.approved_at).total_seconds() / 60 <= 31
        # a second approval (double click, new key) never launches again
        engine.approve(rr, OPERATOR, quote_id=qid)
        assert len(sim.launches) == 1
        iid = d.provider_instance_id
        tracker.poll(dep_id)                                      # booting
        assert ec.dep(dep_id).status == "provisioning"
        sim.inst[iid]["status"] = "active"
        tracker.poll(dep_id)                                      # active -> running, validation probes
        assert ec.dep(dep_id).status == "running"
        r = reconcile.run_once("lambda")                          # nothing to do yet; no orphan
        assert ec.dep(dep_id).status == "running" and not r["counts"].get("orphan"), r["counts"]
        with normalize.SessionLocal.begin() as s:   # it has been running for 25 minutes
            s.execute(text("UPDATE deployment_events SET at = at - interval '25 minutes' WHERE deployment_id = :d"),
                      {"d": dep_id})
            s.execute(text("UPDATE deployments SET running_since = running_since - interval '25 minutes', "
                           "provisioned_at = provisioned_at - interval '25 minutes', "
                           "approved_at = approved_at - interval '25 minutes' WHERE deployment_id = :d"), {"d": dep_id})
        # the deadline passes -> reconciliation auto-terminates
        with normalize.SessionLocal.begin() as s:
            s.execute(text("UPDATE deployments SET terminate_deadline_at = now() - interval '1 minute' "
                           "WHERE deployment_id = :d"), {"d": dep_id})
        r = reconcile.run_once("lambda")
        assert r["counts"].get("deadline_terminated") == 1, r["counts"]
        d = ec.dep(dep_id)
        assert d.status == "terminating" and d.termination_reason == "max_runtime_exceeded" and sim.terminates
        tracker.poll(dep_id)                                      # still 'terminating' at Lambda
        assert ec.dep(dep_id).status == "terminating"
        del sim.inst[iid]                                         # Lambda finished: gone from list, 404 on get
        reconcile.run_once("lambda")
        d = ec.dep(dep_id)
        assert d.status == "terminated" and d.terminated_at is not None, d.status
        transactions.bill(dep_id)
        transactions.bill(dep_id)                                 # retried: never double-billed
        with normalize.SessionLocal() as s:
            n, total, hours = s.execute(text("SELECT count(*), coalesce(sum(provider_cost_usd), 0), "
                                             "coalesce(sum(gpu_hours), 0) FROM usage_records WHERE deployment_id = :d"),
                                        {"d": dep_id}).one()
            slices = s.execute(text("SELECT count(*) FROM usage_slices WHERE deployment_id = :d"), {"d": dep_id}).scalar()
        assert n == slices >= 1, (n, slices)
        assert 0.40 <= float(hours) <= 0.45 and abs(float(total) - 0.75 * float(hours)) < 0.001, (total, hours)
        assert transactions.metered_totals(dep_id)["usage_records"] == n
        rep = validation.validation_report(dep_id, by="operator:founder", mark=True)
        assert rep["validated"], rep["missing"]
        f = control.provider_flags("lambda")
        assert f["adapter_status"] == "validated" and not f["supervised_enabled"], "validation does not enable launches"
        assert not control.launch_permission("lambda", purpose="customer")[0]
        assert len(sim.launches) == 1, "exactly one launch for the whole cycle"
    finally:
        settings.lambda_api_key, settings.routing_launch_defaults = saved
        adapters.TRANSPORTS.pop("lambda", None)
        with normalize.SessionLocal.begin() as s:
            s.execute(text("DELETE FROM provider_execution_flags WHERE provider = 'lambda'"))


# --------------------------------------------------------------------------
# 8. migrations from 0009 with legacy rows
# --------------------------------------------------------------------------

def _alembic(url: str, rev: str) -> None:
    env = dict(os.environ, DATABASE_URL=url)
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", rev], cwd=str(ROOT), env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-3000:]


def test_migrations_from_0009_with_legacy_rows():
    url = scratchdb.create(MIG_DB)
    e = create_engine(url)
    try:
        _alembic(url, "0009_frontend")
        with e.begin() as c:
            c.execute(text("INSERT INTO accounts (id,name,status,plan,settings,is_operator) "
                           "VALUES (5,'acme','active','free','{}',false)"))
            c.execute(text("INSERT INTO provider_credentials (id,account_id,provider,secret_encrypted,created_at) "
                           "VALUES (7,5,'runpod','x','2026-01-01')"))
            legacy = [("dep_l1", "lambda", "i-1", "running", "opengrid", None),
                      ("dep_l2", "lambda", None, "routing", "opengrid", None),
                      ("dep_l3", "runpod", None, "failed", "byo", '{"needs_reconciliation": true}'),
                      ("dep_l4", "runpod", "p-4", "terminated", "byo", None),
                      ("dep_l5", "crusoe", None, "failed", "opengrid", None),
                      ("dep_l6", "lambda", "i-6", "terminating", "opengrid", None)]
            for d, p, iid, st, cs, md in legacy:
                rr = "rr_" + d
                c.execute(text("INSERT INTO route_requests (id,account_id,principal_kind,preview,mode,gpu,request,status,"
                               "created_at) VALUES (:r,5,'api_key',false,'BALANCED','NVIDIA H100','{}','provisioned',now())"),
                          {"r": rr})
                c.execute(text(
                    "INSERT INTO deployments (deployment_id,account_id,key_id,route_request_id,provider,listing_id,"
                    "provider_instance_id,gpu,gpu_count,region,quoted_price_per_gpu_hour,status,created_at,"
                    "provisioned_at,terminated_at,uptime_seconds,interruptions,credential_source,launch,provider_metadata) "
                    "VALUES (:d,5,50,:rr,:p,'L1',:iid,'NVIDIA H100',1,'us-east-1',2.5,:st,'2026-09-01T08:00:00+00',"
                    "'2026-09-01T08:01:00+00',CAST(:term AS timestamptz),0,0,:cs,'{}',CAST(:md AS jsonb))"),
                    dict(d=d, rr=rr, p=p, iid=iid, st=st, cs=cs, md=md,
                         term="2026-09-01T10:00:00+00" if st == "terminated" else None))
                c.execute(text("INSERT INTO deployment_events (deployment_id,at,from_status,to_status,detail) "
                               "VALUES (:d,'2026-09-01T08:00:00+00',NULL,'routing','{}')"), {"d": d})
                c.execute(text("INSERT INTO provision_attempts (deployment_id,route_request_id,provider,started_at,ok,"
                               "error_kind) VALUES (:d,:rr,:p,'2026-09-01T08:00:00+00',:ok,:ek)"),
                          dict(d=d, rr=rr, p=p, ok=iid is not None, ek=None if iid else ("timeout" if md else "capacity")))
        _alembic(url, "head")
        with e.connect() as c:
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            head = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
            got = dict(c.execute(text("SELECT deployment_id, status FROM deployments")).all())
            refs = dict(c.execute(text("SELECT deployment_id, credential_ref FROM deployments")).all())
        assert got == {"dep_l1": "running", "dep_l2": "launch_unknown", "dep_l3": "launch_unknown",
                       "dep_l4": "terminated", "dep_l5": "provision_failed", "dep_l6": "terminating"}, got
        assert refs["dep_l1"] == "platform:lambda" and refs["dep_l3"] == "byo:7" and refs["dep_l5"] == "platform:shadeform"
    finally:
        e.dispose()
        scratchdb.drop(MIG_DB)


# --------------------------------------------------------------------------

def main():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    ec.seed(Session)
    rollups.refresh()
    scoring.history.cache_clear()
    try:
        from accounts import accounts as acc
        acc.reset_cache()
    except Exception:  # noqa: BLE001
        pass
    for p in ec.SYN:
        adapters.register(p, ec.Fake)
    try:
        tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
        failed = 0
        for name, fn in tests:
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {exc}")
        print(f"\n{len(tests) - failed}/{len(tests)} passed")
        if failed:
            sys.exit(1)
    finally:
        for p in ec.SYN:
            adapters.unregister(p)
        settings.routing_live_provisioning = False
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)


if __name__ == "__main__":
    main()
