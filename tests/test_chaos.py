"""Chaos: the REAL LambdaAdapter + engine + deployments state machine + tracker + reconciler against a fake
Lambda Cloud API (tests/bench_fixtures.LambdaSim behind httpx.MockTransport) that times out, answers garbage,
rate-limits, 500s, boots slowly, lists instances twice, loses the connection after creating, kills the worker
mid-call, rejects expired keys and never finishes deleting. Database failures and worker restarts are
simulated by monkeypatching the write after the provider call / discarding in-memory state.

After EVERY scenario bench_fixtures.invariants() asserts:
    provider create calls <= 1 per deployment intent; no instance alive without a live/uncertain deployment
    or an orphan record; usage slices never overlap and never bill past confirmed termination; 'running'
    only on provider evidence; 'terminated' only with provider evidence.

Scratch DB og_test_chaos; no network.
Run:  .venv/Scripts/python tests/test_chaos.py
"""

from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_fixtures as bf  # noqa: E402
from bench_fixtures import PK, SKU, LambdaSim, WorkerKilled, q, scalar  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import OPERATOR  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import adapters, deployments, engine, guards, reconcile, tracker  # noqa: E402
from sqlalchemy import text  # noqa: E402

DB = "og_test_chaos"
G = "NVIDIA H100 80GB SXM5"
KEY = "test-lambda-key-0123456789"
SIM: LambdaSim | None = None
DEPS: list[str] = []


def setup(**kw) -> LambdaSim:
    global SIM
    SIM = LambdaSim(**kw)
    adapters.TRANSPORTS["lambda"] = SIM.transport()
    DEPS.clear()
    settings.routing_max_attempts = 1
    settings.provisioning_timeout_minutes = 15
    settings.termination_retry_base_seconds = 0
    settings.termination_retry_max = 5
    bf.mode("LIVE")
    bf.flags("lambda")
    return SIM


def spec(**kw):
    base = {"gpu": G, "count": 1, "region_group": None, "max_price_per_gpu_hour": None, "duration_hours": 2,
            "deadline_hours": None, "mode": "CHEAPEST", "weights": None,
            "preferences": {"include_providers": ["lambda"]}, "launch": {"ssh_public_key": PK}, "strict_region": False}
    base.update(kw)
    return base


def launch(*faults, who=None, **kw):
    if faults:
        SIM.fault("launch", *faults)
    code, out = engine.route(spec(**kw), who or bf.key(bf.fresh_account()))
    for d in out.get("deployments") or []:
        DEPS.append(d)
    return code, out, (out["deployments"][-1] if out.get("deployments") else None)


def dep(d):
    return deployments.load_row(d)


def running(d):
    """Poll until the provider reports active; returns the deployment row."""
    for _ in range(5):
        tracker.poll(d)
        if dep(d).status == "running":
            return dep(d)
    raise AssertionError(f"{d} never reached running: {dep(d).status}")


def check():
    return bf.invariants(DEPS, sim=SIM)


def backdate(d, delta: timedelta):
    """Shift a deployment's whole history `delta` into the past (it has been running that long)."""
    with normalize.SessionLocal.begin() as s:
        p = {"d": d, "x": delta}
        s.execute(text("UPDATE deployments SET created_at = created_at - :x, provisioned_at = provisioned_at - :x, "
                       "state_changed_at = state_changed_at - :x, running_since = running_since - :x, "
                       "last_checked_at = last_checked_at - :x, approved_at = approved_at - :x "
                       "WHERE deployment_id = :d"), p)
        s.execute(text("UPDATE deployment_events SET at = at - :x WHERE deployment_id = :d"), p)
        s.execute(text("UPDATE provision_attempts SET started_at = started_at - :x, finished_at = finished_at - :x "
                       "WHERE deployment_id = :d"), p)
        s.execute(text("UPDATE deployment_watch SET last_observed_at = last_observed_at - :x, "
                       "last_running_at = last_running_at - :x WHERE deployment_id = :d"), p)


def age_attempt(d, minutes=30):
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE provision_attempts SET started_at = started_at - make_interval(mins => :m) "
                       "WHERE deployment_id = :d"), {"d": d, "m": minutes})


class Alerts:
    def __enter__(self):
        from alerts import ops
        self.ops, self.orig, self.sent = ops, ops.alert, []
        ops.alert = lambda kind, title, **kw: self.sent.append(kind) or {}
        return self

    def __exit__(self, *a):
        self.ops.alert = self.orig


# --------------------------------------------------------------------------
# Provision chaos
# --------------------------------------------------------------------------

def test_provision_timeout_instance_created_is_adopted():
    sim = setup()
    code, out, d = launch("timeout_after")
    assert code == 202 and out["status"] == "provider_timeout" and dep(d).provider_instance_id is None
    assert len(sim.creates()) == 1 and len(sim.alive()) == 1
    tracker.track()                      # no instance id: the tracker leaves it to reconciliation
    assert dep(d).status == "provider_timeout"
    reconcile.run_once()
    row = dep(d)
    assert row.provider_instance_id in sim.instances and row.status == "provisioning", row.status
    running(d)
    reconcile.run_once()
    assert len(sim.creates()) == 1, "reconciliation never launches"
    check()


def test_provision_timeout_nothing_created_closes_after_timeout():
    sim = setup()
    code, out, d = launch("timeout_before")
    assert out["status"] == "provider_timeout" and not sim.instances
    r = reconcile.run_once()
    assert dep(d).status == "provider_timeout" and any(f["kind"] == "unresolved_waiting" for f in r["findings"])
    age_attempt(d)
    reconcile.run_once()
    assert dep(d).status == "provision_failed" and len(sim.creates()) == 1
    check()


def test_malformed_response_after_create():
    sim = setup()
    code, out, d = launch("garbage_after")
    att = deployments.public(d)["provision_attempts"][0]
    assert out["status"] == "launch_unknown" and att["outcome"] == "unknown" and att["error_kind"] == "parse", att
    reconcile.run_once()
    running(d)
    check()


def test_http_429_rate_limited_is_a_clean_rejection():
    sim = setup()
    code, out, d = launch("http429")
    assert out["status"] == "provider_rejected" and not sim.alive(), out["status"]
    assert deployments.public(d)["provision_attempts"][0]["error_kind"] == "rate_limit"
    reconcile.run_once()
    assert dep(d).status == "provider_rejected"
    check()


def test_http_500_with_and_without_an_instance():
    sim = setup()
    code, out, d1 = launch("http500")
    assert out["status"] == "launch_unknown" and not sim.instances
    code, out, d2 = launch("http500_after")
    assert out["status"] == "launch_unknown" and len(sim.alive()) == 1
    reconcile.run_once()
    assert dep(d2).provider_instance_id and dep(d1).status == "launch_unknown"
    age_attempt(d1)
    reconcile.run_once()
    assert dep(d1).status == "provision_failed" and dep(d2).status in ("provisioning", "running")
    assert len(sim.creates()) == 2
    check()


def test_connect_error_and_capacity_are_definitive():
    sim = setup()
    code, out, d1 = launch("connect_error")
    code, out2, d2 = launch("capacity")
    assert dep(d1).status == "provision_failed" and dep(d2).status == "provision_failed" and not sim.instances
    assert deployments.public(d2)["provision_attempts"][0]["error_kind"] == "capacity"
    check()


def test_delayed_provisioning_never_reports_running_early():
    sim = setup(boot_polls=3)
    code, out, d = launch()
    assert out["status"] == "provisioned" and dep(d).status == "provisioning"
    for i in range(3):
        tracker.poll(d)
        assert dep(d).status == "provisioning" and sim.reported[-1][1] == "booting", (i, dep(d).status)
    tracker.poll(d)
    assert sim.reported[-1][1] == "active" and dep(d).status == "running"
    check()


def test_duplicate_entries_in_the_instance_list():
    sim = setup()
    code, out, d = launch("timeout_after")
    sim.fault("list", "duplicate", "duplicate")
    r = reconcile.run_once()
    row = dep(d)
    assert row.status == "provisioning" and row.provider_instance_id in sim.instances, (row.status, r["findings"])
    assert scalar("SELECT count(*) FROM orphan_resources") == 0, "one instance listed twice is not a duplicate launch"
    running(d)
    sim.fault("list", "duplicate")
    reconcile.run_once()
    assert dep(d).status == "running" and scalar("SELECT count(*) FROM orphan_resources") == 0
    check()


def test_network_failure_after_launch_adopted_never_duplicated_http():
    sim = setup()
    main, c = _client()
    try:
        who = bf.key(bf.fresh_account())
        _as(main, who)
        body = {"gpu": "h100-80gb-sxm5", "count": 1, "mode": "cheapest", "duration_hours": 2,
                "preferences": {"include_providers": ["lambda"]}, "launch": {"ssh_public_key": PK}}
        sim.fault("launch", "reset_after")
        r1 = c.post("/v1/route", json=body, headers={"Idempotency-Key": "net-1"})
        assert r1.status_code == 202 and r1.json()["data"]["status"] == "launch_unknown", r1.text
        d = r1.json()["data"]["deployments"][0]
        DEPS.append(d)
        r2 = c.post("/v1/route", json=body, headers={"Idempotency-Key": "net-1"})   # client retry: replayed
        assert r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true"
        assert len(sim.creates()) == 1
        assert guards.usage(who.account_id)["active_deployments"] == 1, "an unresolved launch counts against guards"
    finally:
        main.app.dependency_overrides.clear()
    reconcile.run_once()
    row = running(d)
    assert row.provider_instance_id == next(iter(sim.instances)) and len(sim.creates(row.client_name)) == 1
    check()


def test_database_failure_after_provider_accepted():
    sim = setup()
    real = deployments._record_launch

    def boom(*a, **k):
        raise RuntimeError("could not write to the database (connection reset)")

    deployments._record_launch = boom
    try:
        code, out, d = launch()
    finally:
        deployments._record_launch = real
    att = q("SELECT outcome, client_name FROM provision_attempts WHERE deployment_id = :d", d=d)
    assert att == [("provisioning", f"og-{d}")] and dep(d).status == "provisioning" and dep(d).provider_instance_id is None
    r = deployments.launch(d, adapter=None, offer=None, availability=None, launch_spec=None, resolved=None)
    assert r["launched"] is False, "the launch token forbids a second provision call"
    reconcile.run_once()
    assert dep(d).provider_instance_id in sim.instances
    assert q("SELECT outcome FROM provision_attempts WHERE deployment_id = :d", d=d) == [("unknown",)]
    running(d)
    assert len(sim.creates()) == 1
    check()


def test_worker_killed_mid_provision_then_restart():
    sim = setup()
    try:
        launch("kill_after")
        raise AssertionError("the worker should have died")
    except WorkerKilled:
        pass
    d = scalar("SELECT deployment_id FROM deployments ORDER BY created_at DESC LIMIT 1")
    DEPS.append(d)
    assert dep(d).status == "provisioning" and dep(d).launch_token and len(sim.alive()) == 1
    # restart: nothing in memory survives; the jobs start fresh
    adapters.TRANSPORTS["lambda"] = sim.transport()
    tracker.track()
    reconcile.run_once()
    tracker.track()
    assert dep(d).status == "running" and len(sim.creates()) == 1
    check()


def test_terminate_requested_while_launch_unresolved():
    sim = setup()
    code, out, d = launch("timeout_after")
    who = OPERATOR
    r = deployments.terminate(d, who)
    assert r["terminate"]["provider_called"] is False and not sim.terminates()
    reconcile.run_once()                         # adopt -> terminate (requested earlier)
    assert dep(d).status == "terminating" and len(sim.terminates()) == 1
    reconcile.run_once()
    assert dep(d).status == "terminated" and not sim.alive() and len(sim.creates()) == 1
    check()


# --------------------------------------------------------------------------
# Terminate chaos
# --------------------------------------------------------------------------

def _running_dep(sim):
    code, out, d = launch()
    running(d)
    return d


def test_restart_during_terminating_after_provider_accepted():
    sim = setup()
    d = _running_dep(sim)
    real = deployments.as_terminate_result

    def die(out):
        raise WorkerKilled("restart between the provider's answer and the DB write")

    deployments.as_terminate_result = die
    try:
        deployments.terminate(d, OPERATOR)
        raise AssertionError("expected the worker to die")
    except WorkerKilled:
        pass
    finally:
        deployments.as_terminate_result = real
    assert dep(d).status == "terminating" and len(sim.terminates()) == 1
    tracker.track()                               # fresh process: status read confirms
    assert dep(d).status == "terminated" and len(sim.terminates()) == 1
    check()


def test_restart_during_terminating_before_provider_call():
    sim = setup()
    d = _running_dep(sim)
    real = deployments.adapter_for

    def die(*a, **k):
        raise WorkerKilled("restart before the delete was sent")

    deployments.adapter_for = die
    try:
        deployments.terminate(d, OPERATOR)
    except WorkerKilled:
        pass
    finally:
        deployments.adapter_for = real
    assert dep(d).status == "terminating" and not sim.terminates() and len(sim.alive()) == 1
    tracker.track()
    assert dep(d).status == "terminating", "alive instance: still terminating, never terminated"
    reconcile.run_once()                         # instance still listed -> re-issue the delete
    assert len(sim.terminates()) == 1
    reconcile.run_once()
    assert dep(d).status == "terminated"
    check()


def test_expired_credentials_never_terminated():
    sim = setup()
    d = _running_dep(sim)
    for op in ("status", "terminate", "list"):
        sim.always[op] = "http401"
    tracker.poll(d)
    assert dep(d).status == "credentials_unavailable", dep(d).status
    try:
        deployments.terminate(d, OPERATOR)
    except HTTPException:
        pass
    assert dep(d).status == "credentials_unavailable" and dep(d).terminate_requested_at is not None
    for _ in range(2):
        reconcile.run_once()
        tracker.track()
    row = dep(d)
    assert row.status == "credentials_unavailable" and row.terminated_at is None and len(sim.alive()) == 1
    check()
    sim.always.clear()                           # the key works again
    for _ in range(4):
        reconcile.run_once()
        tracker.track()
        if dep(d).status == "terminated":
            break
    assert dep(d).status == "terminated" and not sim.alive(), dep(d).status
    check()


def test_ssh_key_registration_failure_rejected_before_create():
    sim = setup()
    sim.fault("ssh", "http500")
    code, out, d = launch()
    assert dep(d).status == "provision_failed" and not sim.creates() and not sim.instances
    sim.fault("ssh", "http422")
    code, out, d = launch()
    assert dep(d).status == "provision_failed" and not sim.creates()
    check()


def test_termination_timeout_retries_then_termination_failed():
    sim = setup()
    settings.termination_retry_max = 2
    d = _running_dep(sim)
    sim.always["terminate"] = "timeout"
    with Alerts() as al:
        r = deployments.terminate(d, OPERATOR)
        assert r["terminate"]["outcome"] == "unknown" and dep(d).status == "terminating"
        for _ in range(3):
            reconcile.run_once()
            tracker.track()
            assert dep(d).status != "terminated"
        assert dep(d).status == "termination_failed", dep(d).status
        assert "termination_failed" in al.sent, al.sent
    assert len(sim.alive()) == 1
    check()
    sim.always.clear()
    reconcile.run_once()
    reconcile.run_once()
    assert dep(d).status == "terminated" and not sim.alive()
    check()


def test_duplicate_terminate_requests_one_provider_call():
    sim = setup()
    d = _running_dep(sim)
    sim.always["terminate"] = "noop"            # accepted, still deleting: the window for duplicates stays open
    main, c = _client()
    try:
        _as(main, OPERATOR)
        r1 = c.post(f"/v1/deployments/{d}/terminate", headers={"Idempotency-Key": "t-1"})
        r2 = c.post(f"/v1/deployments/{d}/terminate", headers={"Idempotency-Key": "t-1"})
        assert r1.status_code == r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true"
        r3 = c.post(f"/v1/deployments/{d}/terminate", headers={"Idempotency-Key": "t-2"})
        assert r3.json()["data"]["terminate"]["provider_called"] is False
    finally:
        main.app.dependency_overrides.clear()
    ts = [threading.Thread(target=deployments.terminate, args=(d, OPERATOR)) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(sim.terminates()) == 1, len(sim.terminates())
    sim.always.clear()
    check()


def test_duplicate_approve_concurrent_one_create():
    sim = setup()
    bf.mode("SUPERVISED")
    code, out, d = launch()
    assert out["status"] == "pending_approval"
    rr, qid = out["route_request_id"], out["quote"]["quote_id"]
    sim.delay = 0.3
    res = []

    def go():
        try:
            res.append(engine.approve(rr, OPERATOR, quote_id=qid)[1]["status"])
        except HTTPException as e:
            res.append(e.status_code)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    sim.delay = 0.0
    assert len(sim.creates()) == 1 and scalar("SELECT count(*) FROM provision_attempts WHERE deployment_id = :d", d=d) == 1
    assert "provisioned" in res, res
    check()


# --------------------------------------------------------------------------
# Concurrency and billing
# --------------------------------------------------------------------------

def test_tracker_and_reconcile_concurrently_no_double_billing():
    sim = setup()
    d = _running_dep(sim)
    backdate(d, timedelta(hours=2, minutes=30))
    tracker.track()                              # meters the closed hours so far
    iid = dep(d).provider_instance_id
    sim.instances[iid]["status"] = "terminated"  # the provider ended it (preempted)
    errors = []

    def worker(fn):
        try:
            for _ in range(3):
                fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=worker, args=(f,)) for f in (tracker.track, reconcile.run_once) * 3]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    row = dep(d)
    assert row.status == "terminated" and row.terminated_at is not None
    sl = q("SELECT running_seconds, billable_seconds, usage_record_id FROM usage_slices WHERE deployment_id = :d", d=d)
    run = sum(x[0] for x in sl)
    assert abs(run - 9000) < 180, run
    assert scalar("SELECT count(*) FROM usage_records WHERE deployment_id = :d", d=d) == len([x for x in sl if x[1] > 0])
    assert scalar("SELECT count(*) FROM deployment_events WHERE deployment_id = :d AND to_status = 'terminated' "
                  "AND from_status <> 'terminated'", d=d) == 1
    check()


def test_month_boundary_billing_during_chaos():
    sim = setup()
    d = _running_dep(sim)
    t_run = q("SELECT at FROM deployment_events WHERE deployment_id = :d AND to_status = 'running'", d=d)[0][0]
    t0 = datetime(2026, 9, 30, 23, 20, tzinfo=timezone.utc)
    backdate(d, t_run - t0)
    real = tracker._now
    clock = {"t": t0}
    tracker._now = lambda: clock["t"]
    try:
        for hh, mm, fault in ((23, 50, "http500"), (0, 30, None), (1, 10, "timeout"), (1, 12, None)):
            day = 30 if hh == 23 else 1
            clock["t"] = datetime(2026, 9 if day == 30 else 10, day, hh, mm, tzinfo=timezone.utc)
            if fault:
                sim.fault("status", fault)
            tracker.poll(d)
            tracker.meter(d)
            assert dep(d).status == "running"
        clock["t"] = datetime(2026, 10, 1, 1, 15, tzinfo=timezone.utc)
        sim.instances[dep(d).provider_instance_id]["status"] = "terminated"
        tracker.poll(d)
        tracker.meter(d)
        tracker.meter(d)
    finally:
        tracker._now = real
    row = dep(d)
    assert row.status == "terminated" and row.terminated_at == datetime(2026, 10, 1, 1, 15, tzinfo=timezone.utc)
    sl = q("SELECT period_start, period_end, running_seconds FROM usage_slices WHERE deployment_id = :d "
           "ORDER BY period_start", d=d)
    assert [(a.hour, b.hour, b.minute) for a, b, _ in sl] == [(23, 0, 0), (0, 1, 0), (1, 1, 15)], sl
    assert [r for _, _, r in sl] == [40 * 60, 3600, 15 * 60], sl
    months = q("SELECT extract(month FROM period_start)::int, sum(provider_cost_usd) FROM usage_records "
               "WHERE deployment_id = :d GROUP BY 1 ORDER BY 1", d=d)
    assert [m for m, _ in months] == [9, 10], months
    assert abs(float(months[0][1]) - 3.29 * 40 / 60) < 1e-3, months
    check()


def test_invariants_over_everything():
    """Final sweep across every deployment the suite created (and every instance at the fake provider)."""
    ids = [r[0] for r in q("SELECT deployment_id FROM deployments")]
    bf.invariants(ids)


# --------------------------------------------------------------------------

def _client():
    import main as app_main
    from fastapi.testclient import TestClient
    return app_main, TestClient(app_main.app, headers={"X-OpenGrid-Request": "1"})


def _as(app_main, who):
    from accounts.auth import principal
    app_main.app.dependency_overrides[principal] = lambda: who


def main():
    S = bf.db(DB)
    old_key = settings.lambda_api_key
    settings.lambda_api_key = KEY
    row = bf.listing("lambda", 3.29, gpu=G, region="us-east-1", lid="lambda:" + SKU)
    row.sku = SKU
    bf.market(G, [row])
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    summary = []
    try:
        for name, fn in tests:
            try:
                fn()
                summary.append(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                traceback.print_exc()
                summary.append(f"FAIL {name}: {exc}")
    finally:
        settings.lambda_api_key = old_key
        adapters.TRANSPORTS.pop("lambda", None)
        settings.routing_live_provisioning = False
        bf.drop(S, DB)
    print("\n".join(summary))
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
