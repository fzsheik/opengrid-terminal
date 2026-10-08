"""Final red-team regressions before the first supervised Lambda validation (one GPU).

Each test is a concrete counterexample found by the red-team review (or a bound the review's exposure
calculation relies on), driven through the REAL Lambda adapter against tests/test_money_invariants.MoneySim
(httpx.MockTransport), the real engine / approval / launch path and the real tracker + reconciler. No network,
no uvicorn. Scratch DB og_test_redteam.

    Q8  a quote re-validated inside the 2% tolerance but ABOVE a cap launched anyway:
          - approve() checked the customer's max_price_per_gpu_hour against the OLD quote price
          - POST /v1/route with quote_id (LIVE) never checked max_price_per_gpu_hour at all
          - the account max_price_per_gpu_hour guard (guards.gate) used the OLD quote price
          - the validation gate ($3.00/h total, not overridable) used the OLD quote price
    Q3/Q5  a platform-admin API key (cross-tenant admin) could not force-terminate a deployment of another
          account (or a validation deployment, account NULL): deployments.terminate 404'd.
    exposure  the deadline / crash bounds the validation exposure calculation uses.

Run:  .venv/Scripts/python tests/test_redteam.py
"""

from __future__ import annotations

import sys
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_fixtures as bf  # noqa: E402
import test_money_invariants as tmi  # noqa: E402
from bench_fixtures import PK, SKU, WorkerKilled  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import OPERATOR, Principal  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import control, deployments, engine, guards, reconcile, tracker  # noqa: E402

tmi.DB = "og_test_redteam"
G = tmi.G


def refused(fn, code):
    try:
        out = fn()
    except HTTPException as e:
        got = e.detail.get("code") if isinstance(e.detail, dict) else e.detail
        assert got == code, (code, e.status_code, e.detail)
        return e
    raise AssertionError(f"expected refusal {code}, got {str(out)[:300]}")


def validation_ready(provider="lambda"):
    """Every validation precondition true (drills, heartbeats, a fresh reconciliation pass, ops channel)."""
    from store.reconcile import ReconciliationRun
    settings.validation_allowed_providers = [provider]
    bf.mode("SUPERVISED")
    control.kill_all("drill", "test")
    control.set_mode("SUPERVISED", reason="drill done", by="test")
    control.kill_provider(provider, "drill", "test")
    control.unkill_provider(provider, "drill done", "test")
    control.record_job_health("reconcile", ok=True)
    control.record_job_health("routing_tracker", ok=True)
    now = tmi.CLOCK.now()
    with normalize.SessionLocal.begin() as s:
        s.add(ReconciliationRun(started_at=now, finished_at=now, trigger="test", provider=None, status="ok",
                                providers={provider: {"credentials": 1, "deployments": 0, "listed": 0,
                                                      "list_errors": 0}}, findings=[], counts={}))
    control.ops_channel_configured = lambda: True
    control.record("ops_test_alert", "alerts:ops", after={"delivered": True, "channel_configured": True},
                   reason="test", actor="test")


def supervised_pending(**kw):
    bf.mode("SUPERVISED")
    bf.flags("lambda", live=False)
    who = bf.key(bf.fresh_account())
    code, out = engine.route(tmi.spec(**kw), who)
    assert out["status"] == "pending_approval", (out["status"], out.get("reason"), out.get("considered"))
    tmi.DEPS.extend(out.get("deployments") or [])
    return who, out["route_request_id"], out["quote"]["quote_id"], out["deployments"][-1]


# --------------------------------------------------------------------------
# Q8: stale price vs caps
# --------------------------------------------------------------------------

def test_q8_approve_respects_request_max_price_after_drift():
    sim = tmi.setup()
    who, rr, qid, d = supervised_pending(max_price_per_gpu_hour=3.29)
    sim.price_cents = 335            # +1.8%: inside the 2% re-validation tolerance, over the customer's cap
    refused(lambda: engine.approve(rr, OPERATOR, quote_id=qid), "over_max_price")
    assert sim.creates() == [], "launched above the customer's max_price_per_gpu_hour"
    assert deployments.load_row(d).launch_token is None


def test_q8_route_from_quote_respects_max_price_live():
    sim = tmi.setup()                # LIVE mode, validated + live-enabled lambda
    who = bf.key(bf.fresh_account())
    pv = engine.preview(tmi.spec(max_price_per_gpu_hour=3.29), who)
    qid = pv["quote_record"]["quote_id"]
    sim.price_cents = 335
    refused(lambda: engine.route(tmi.spec(max_price_per_gpu_hour=3.29, quote_id=qid), who), "over_max_price")
    assert sim.creates() == [], "LIVE launch from a quote above the request's max_price_per_gpu_hour"


def test_q8_account_max_price_guard_uses_revalidated_price():
    sim = tmi.setup()
    bf.mode("SUPERVISED")
    bf.flags("lambda", live=False)
    acct = bf.fresh_account()
    guards.set_limits(acct, {"max_price_per_gpu_hour": 3.30}, by="test", reason="red-team cap")
    who = bf.key(acct)
    code, out = engine.route(tmi.spec(), who)
    assert out["status"] == "pending_approval" and not out["deployment"]["limit_violations"], out.get("reason")
    rr, qid = out["route_request_id"], out["quote"]["quote_id"]
    tmi.DEPS.extend(out["deployments"])
    sim.price_cents = 335            # 3.35 > the account's 3.30 cap; quote said 3.29
    e = refused(lambda: engine.approve(rr, OPERATOR, quote_id=qid), "limits_exceeded")
    assert "max_price_per_gpu_hour" in [v["code"] for v in e.detail["violations"]], e.detail
    assert sim.creates() == []
    # an explicit, reasoned admin override still works (it is overridable) and launches exactly once
    code, body = engine.approve(rr, OPERATOR, quote_id=qid, override_limits=True, reason="red-team: accepted")
    assert body["launch"]["outcome"] == "accepted" and len(sim.creates()) == 1, body


def test_q8_validation_cap_uses_revalidated_price():
    sim = tmi.setup()
    row = bf.listing("lambda", 2.99, gpu=G, region="us-east-1", lid="lambda:" + SKU)
    row.sku = SKU
    bf.market(G, [row])
    sim.price_cents = 299
    old_defaults, old_cfg = settings.routing_launch_defaults, control.ops_channel_configured
    settings.routing_launch_defaults = {"lambda": {"ssh_public_key": PK}}
    try:
        validation_ready()
        rr = engine.create_validation_route("lambda", "lambda:" + SKU, by="test", max_runtime_minutes=30)
        d = deployments.for_request(rr)
        tmi.DEPS.append(d.deployment_id)
        assert d.status == "pending_approval" and d.purpose == "validation"
        sim.price_cents = 304        # $3.04/h total: +1.7% (inside tolerance), over the $3.00/h validation cap
        e = refused(lambda: engine.approve(rr, OPERATOR, quote_id=d.quote_id), "validation_preconditions_failed")
        assert "price_cap" in [f["code"] for f in e.detail["failed"]], e.detail
        assert sim.creates() == [], "validation launched above the $3.00/h cap"
        sim.price_cents = 299        # back under the cap: the same approval launches exactly once
        code, body = engine.approve(rr, OPERATOR, quote_id=d.quote_id)
        assert body["launch"]["outcome"] == "accepted" and len(sim.creates()) == 1, body
    finally:
        settings.routing_launch_defaults = old_defaults
        control.ops_channel_configured = old_cfg
        settings.validation_allowed_providers = ["lambda"]


# --------------------------------------------------------------------------
# Q3/Q5: a cross-tenant admin key can always terminate
# --------------------------------------------------------------------------

def test_platform_admin_key_can_force_terminate_any_deployment():
    sim = tmi.setup()
    code, out, d, owner = tmi.launch()
    tmi.to_running(d)
    pa = Principal(kind="api_key", account_id=bf.fresh_account(), key_id=4242,
                   scopes=frozenset({"admin", "deployments:read", "deployments:write"}), platform_admin=True)
    r = deployments.terminate(d, pa, force=True, reason="red-team: emergency stop")
    assert r["terminate"]["provider_called"] is True, r["terminate"]
    assert len(sim.terminates()) == 1
    # a NON-forced call from another tenant's key is still a 404 (tenant isolation unchanged)
    other = bf.key(bf.fresh_account())
    try:
        deployments.terminate(d, other)
        raise AssertionError("another tenant terminated a deployment it does not own")
    except HTTPException as e:
        assert e.status_code == 404
    tmi.drive(d)
    assert deployments.load_row(d).status == "terminated"


# --------------------------------------------------------------------------
# Exposure bounds used by the validation exposure calculation
# --------------------------------------------------------------------------

def _billed_seconds(d) -> int:
    return int(bf.scalar("SELECT coalesce(sum(running_seconds), 0) FROM usage_slices WHERE deployment_id = :d", d=d))


def test_exposure_deadline_without_crash():
    """30-minute ceiling, workers every reconcile interval (120 s): the terminate call reaches Lambda no later
    than deadline + one reconcile interval, and OpenGrid bills no more than that window."""
    sim = tmi.setup()
    code, out, d, who = tmi.launch(max_runtime_minutes=30)
    row = deployments.load_row(d)
    t0 = row.terminate_deadline_at - timedelta(minutes=30)       # launch time
    for _ in range(40):
        if deployments.load_row(d).status == "terminated":
            break
        tmi.cycle(settings.reconcile_interval_seconds / 60)
    tmi.cycle(1)
    iid = deployments.load_row(d).provider_instance_id
    end = sim.true_end[iid]
    fh = sim.instances[iid]["first_healthy"]
    assert end - t0 <= timedelta(minutes=30, seconds=settings.reconcile_interval_seconds), end - t0
    assert deployments.load_row(d).termination_reason == "max_runtime_exceeded"
    billed = _billed_seconds(d)
    assert billed <= (end - fh).total_seconds() + settings.reconcile_interval_seconds, (billed, end - fh)
    print(f"  no crash: terminate at launch+{(end - t0).total_seconds() / 60:.1f} min, Lambda-billable "
          f"{(end - fh).total_seconds() / 60:.1f} min, OpenGrid billed {billed / 60:.1f} min")


def test_exposure_crash_after_accept_then_long_outage():
    """The worst moment: the process dies right after Lambda accepted the launch (nothing recorded), and stays
    down past the deadline. The first reconciliation pass after restart (jobs start 90 s after boot) adopts the
    instance by name and terminates it: Lambda bills from first_healthy to restart + 90 s + one pass."""
    sim = tmi.setup()
    try:
        tmi.launch("kill_after", max_runtime_minutes=30)
        raise AssertionError("the worker should have died")
    except WorkerKilled:
        pass
    d = bf.scalar("SELECT deployment_id FROM deployments ORDER BY created_at DESC LIMIT 1")
    tmi.DEPS.append(d)
    (iid, inst), = sim.instances.items()
    created = inst["created_at"]
    outage = timedelta(minutes=45)
    tmi.CLOCK.advance(seconds=outage.total_seconds())
    tmi.restart()
    tmi.CLOCK.advance(seconds=90)    # reconcile's initial_delay_seconds after boot
    reconcile.run_once(trigger="job")
    assert iid in sim.true_end, "first pass after restart did not terminate the adopted, past-deadline instance"
    end = sim.true_end[iid]
    assert end - created <= outage + timedelta(seconds=90 + 5), end - created
    tmi.drive(d)
    row = deployments.load_row(d)
    assert row.status == "terminated" and len(sim.creates()) == 1
    print(f"  crash+{outage.total_seconds() / 60:.0f} min outage: terminate at launch+"
          f"{(end - created).total_seconds() / 60:.1f} min (one create, adopted + terminated in the first pass)")


# --------------------------------------------------------------------------

def main():
    old_key = settings.lambda_api_key
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)
             and (len(sys.argv) < 2 or any(a in n for a in sys.argv[1:]))]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print("ok  ", name)
            except Exception:  # noqa: BLE001
                failed += 1
                traceback.print_exc()
                print("FAIL", name)
    finally:
        tmi.CLOCK.uninstall()
        tmi.ops.alert = tmi._orig_alert
        settings.lambda_api_key = old_key
        from routing import adapters
        adapters.TRANSPORTS.pop("lambda", None)
        settings.routing_live_provisioning = False
        if tmi.S is not None:
            bf.drop(tmi.S, tmi.DB)
    print(f"\n{len(tests) - failed}/{len(tests)} red-team tests passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
