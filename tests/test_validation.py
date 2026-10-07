"""Validation harness: start_validation goes through the core's gates; validation_report marks a provider
validated ONLY when every step of the real cycle has evidence (any missing step -> not validated).

The provider is a fake in-memory account (tests/test_reconcile.World); the cycle runs through the real
tracker, reconciler, core terminate and cost reconciliation. Scratch DB og_test_reconcile_validation.
Run:  .venv/Scripts/python tests/test_validation.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402
import test_reconcile as R  # noqa: E402

from config import settings  # noqa: E402
from routing import adapters, control, deployments, reconcile, tracker, validation  # noqa: E402
from routing.adapters.results import instance_name  # noqa: E402
from store.routing import Deployment  # noqa: E402
from tables import ComputeListingRow  # noqa: E402

R.DB = "og_test_reconcile_validation"
G = R.G


class V(R.World):
    pass


def setup():
    S = R.setup()
    adapters.register("syn_v", V)
    R.World.COST = 0.42
    return S


def flags(p="syn_v"):
    return control.provider_flags(p)


def cycle(*, run=True, find_ok=True, terminate=True, confirm=True, reconcile_cost=True, purpose="validation"):
    """Drive one deployment through the lifecycle; each flag drops one step's evidence."""
    d = R.mkdep("provisioning", provider="syn_v", iid="v-1", ref="platform:syn_v", purpose=purpose,
                max_runtime_minutes=30)
    R.inst("v-1", instance_name(d) if find_ok else "renamed-by-hand", "pending", acct="platform:syn_v")
    tracker.poll(d)
    if run:
        R.World.INST["v-1"]["state"] = "running"
        tracker.poll(d)
    if terminate:
        deployments.terminate(d, None, reason="validation complete")
    else:
        R.World.INST["v-1"]["state"] = "gone"
    if confirm:
        reconcile.run_once(provider="syn_v")      # list omits it + status not_found -> terminated
    else:
        # a single provider signal and the instance still listed: never two signals
        with R.S.begin() as s:
            row = s.get(Deployment, d, with_for_update=True)
            deployments.transition(row, "terminated", "status said terminated", {"state": "terminated"},
                                   actor="system", s=s)
        R.World.INST["v-1"] = {"state": "running", "name": instance_name(d), "acct": "platform:syn_v"}
    if not reconcile_cost:
        with R.S.begin() as s:
            row = s.get(Deployment, d, with_for_update=True)
            row.reconciled_at, row.reconciliation = None, None
    return d


def test_full_cycle_marks_validated():
    setup()
    d = cycle()
    row = R.dep(d)
    assert row.status == "terminated", row.status
    assert flags()["adapter_status"] == "simulated"
    rep = validation.validation_report(d, by="ops@test")
    assert rep["validated"] and not rep["missing"], rep
    f = flags()
    assert f["adapter_status"] == "validated" and f["validation_deployment_id"] == d
    assert not f["supervised_enabled"] and not f["live_enabled"], "validation never enables launches by itself"
    s = rep["steps"]
    assert s["find_instance"]["found_id"] == "v-1" and s["list_instances"]["ok"]
    assert s["cost_reconciled"]["provider_reported_cost"] == 0.42


def test_any_missing_step_blocks_validation():
    cases = {"observed_running": dict(run=False), "find_instance": dict(find_ok=False),
             "terminate_accepted": dict(terminate=False), "termination_confirmed": dict(confirm=False),
             "cost_reconciled": dict(reconcile_cost=False)}
    for step, kw in cases.items():
        setup()
        d = cycle(**kw)
        rep = validation.validation_report(d, by="ops@test")
        assert not rep["validated"] and step in rep["missing"], (step, rep["missing"])
        assert flags()["adapter_status"] == "simulated", step


def test_report_refuses_customer_deployments_and_dry_run():
    setup()
    d = cycle(purpose="customer")
    try:
        validation.validation_report(d, by="ops@test")
        raise AssertionError("a customer deployment never validates a provider")
    except validation.ValidationError:
        pass
    setup()
    d = cycle()
    rep = validation.validation_report(d, mark=False)
    assert not rep["missing"] and not rep["validated"] and flags()["adapter_status"] == "simulated"


def test_pick_listing_and_start_validation_gates():
    S = setup()
    now = datetime.now(timezone.utc)

    def L(lid, gpus, price, mt="on_demand"):
        return ComputeListingRow(provider="syn_v", listing_id=lid, sku=lid, raw_gpu_name="RTX 4090", canonical_gpu_name=G,
                                 gpu_count=gpus, region="us-east", country="US", price_per_gpu_hour=Decimal(str(price / gpus)),
                                 price_per_instance_hour=Decimal(str(price)), currency="USD", market_type=mt,
                                 provider_tier=None, interruptible=False, available=True, observed_at=now, first_seen_at=now)

    with S.begin() as s:
        s.add_all([L("cheap8", 8, 1.0), L("ok1", 1, 0.5), L("spot1", 1, 0.1, mt="spot"), L("pricey1", 1, 9.0)])
    assert validation.pick_listing("syn_v")["listing_id"] == "ok1", "1 GPU, on-demand, under the cap"
    settings.validation_max_price_per_hour = 0.4
    assert validation.pick_listing("syn_v") is None
    settings.validation_max_price_per_hour = 3.0
    settings.routing_live_provisioning = False
    try:
        validation.start_validation("syn_v", by="ops@test")
        raise AssertionError("PREVIEW_ONLY: the core refuses")
    except validation.ValidationError as e:
        assert "disabled" in str(e) or "PREVIEW_ONLY" in str(e), e
    try:
        validation.start_validation("syn_nobody", by="ops@test")
        raise AssertionError("no adapter")
    except validation.ValidationError:
        pass
    settings.routing_live_provisioning = True
    control.set_mode("SUPERVISED", reason="test", by="test")
    settings.routing_launch_defaults = {"syn_v": {"ssh_key": "operator-key", "image": "img"}}
    try:
        out = validation.start_validation("syn_v", by="ops@test")
    finally:
        settings.routing_live_provisioning = False
    row = R.dep(out["deployment_id"])
    assert row.purpose == "validation" and row.status == "pending_approval", row.status
    assert row.max_runtime_minutes <= settings.validation_max_runtime_minutes and row.listing_id == "ok1"
    assert not R.calls("provision"), "start_validation never launches: an admin approves through the core"


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {exc}")
    finally:
        if R.S is not None:
            R.S.kw["bind"].dispose()
        scratchdb.drop(R.DB)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
