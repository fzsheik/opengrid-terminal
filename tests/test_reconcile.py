"""Reconciliation, incremental metering and cost reconciliation against an in-memory fake provider account.

Scenarios: provider_timeout then the instance exists -> adopted (never a second launch); worker restart
mid-provision -> resolved by name; an unknown launch with nothing there -> provision_failed only after the
provisioning timeout; running-but-gone needs two signals; terminate confirmation and retry with backoff ->
termination_failed + alert; both orphan kinds; auto-terminate only provable og-* instances; duplicate
launches; deadline auto-terminate; credentials_unavailable; operator orphan resolution; run records.
Metering: hourly slices idempotent, split at the month boundary, stopped billing per provider (storage_only
vs full), degraded not billed where the provider says so, end time = provider-confirmed (or flagged estimate).
Cost reconciliation numbers.

Scratch DB og_test_reconcile_*; no network.
Run:  .venv/Scripts/python tests/test_reconcile.py
"""

import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import scratchdb  # noqa: E402

import normalize  # noqa: E402
from config import settings  # noqa: E402
from routing import adapters, credentials, deployments, reconcile, tracker, transactions  # noqa: E402
from routing.adapters.base import Adapter, AdapterError  # noqa: E402
from routing.adapters.results import Capabilities, CostReport, InstanceState, TerminateResult, instance_name  # noqa: E402
from routing.credentials import CredentialsUnavailable  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from store.accounts import Account, UsageRecord  # noqa: E402
from store.reconcile import DeploymentWatch, OrphanResource, ReconciliationRun, UsageSlice  # noqa: E402
from store.routing import Deployment, DeploymentEvent, ExecutionRecord, ProvisionAttempt  # noqa: E402

DB = "og_test_reconcile_main"
G = "NVIDIA RTX 4090 24GB"
REF = "platform:syn_w"
S = None


class World(Adapter):
    """A fake provider ACCOUNT per credential: INST[iid] = {state, name, acct}. Uses the base wrappers, so
    status/terminate/list/find classification is the production code's."""
    LEVEL = 3
    CREDENTIALS = ("api_key",)
    CHECK_NEEDS_CREDENTIALS = False
    CAPABILITIES = Capabilities(billing_unit=("per second", "test"), stopped_billing=("storage_only", "test"),
                                reported_cost=("YES", "test"))
    INST: dict = {}
    STATUS: dict = {}          # iid -> forced status() answer
    TERMINATE = "ok"           # ok | noop (accepted, nothing happens) | fail
    LIST_FAIL = False
    COST = None
    CALLS: list = []

    def _acct(self):
        return self.credentials.get("api_key")

    def _st(self, iid, v):
        return InstanceState(v["state"], instance_id=iid, name=v["name"], labels=list(v.get("labels", [])),
                             price_per_hour=v.get("price"), ended_at=v.get("ended_at"))

    def _status(self, iid):
        World.CALLS.append(("status", iid))
        if iid in World.STATUS:
            return InstanceState(World.STATUS[iid], instance_id=iid)
        v = World.INST.get(iid)
        if v is None or v["acct"] != self._acct() or v["state"] == "gone":
            raise AdapterError("not_found", "no such instance", 404, sent=True)
        return self._st(iid, v)

    def _terminate(self, iid):
        World.CALLS.append(("terminate", iid))
        if World.TERMINATE == "fail":
            return TerminateResult("failed", "refused", 400)
        v = World.INST.get(iid)
        if v is None or v["state"] == "gone":
            raise AdapterError("not_found", "gone", 404, sent=True)
        if World.TERMINATE == "ok":
            v["state"] = "gone"
        return TerminateResult("accepted", "deleting", 200)

    def _list(self):
        World.CALLS.append(("list",))
        if World.LIST_FAIL:
            raise AdapterError("server", "list down", 500, sent=True)
        return [self._st(k, v) for k, v in World.INST.items() if v["acct"] == self._acct() and v["state"] != "gone"]

    def provision(self, *a, **k):  # reconciliation must never launch
        World.CALLS.append(("provision",))
        raise AssertionError("reconciliation must never provision")

    def reported_cost(self, iid, start, end):
        return CostReport(World.COST, start, end, basis="test billing") if World.COST is not None else \
            CostReport(None, start, end, reason="test: no billing")


class WorldFull(World):
    """Stopped instances bill in full; billed per minute; error states not billed."""
    CAPABILITIES = Capabilities(billing_unit=("per minute", "test"), stopped_billing=("full", "test"),
                                reported_cost=("NO", "no billing api"))
    ERROR_STATE_BILLED = False
    reported_cost = Adapter.reported_cost    # no billing API: amount None + the capability's reason


def setup():
    global S
    if S is not None:
        S.kw["bind"].dispose()
    S = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = S
    with S.begin() as s:
        s.add(Account(id=7, name="acct7", status="active", plan="free", settings={}, is_operator=False))
    adapters.register("syn_w", World)
    adapters.register("syn_full", WorldFull)
    World.INST.clear()
    World.STATUS.clear()
    World.CALLS.clear()
    World.TERMINATE, World.LIST_FAIL, World.COST = "ok", False, None
    settings.provisioning_timeout_minutes = 15
    settings.termination_retry_base_seconds = 60
    settings.termination_retry_max = 5
    credentials.for_ref = _for_ref
    credentials.platform = _platform
    return S


_orig_platform = credentials.platform
BROKEN_REFS: set = set()


def _for_ref(ref, provider):
    if not ref or ref in BROKEN_REFS:
        raise CredentialsUnavailable("the BYO credential used at launch was revoked", ref=ref)
    return {"api_key": ref}


def _platform(p):
    return {"api_key": f"platform:{p}"} if p in ("syn_w", "syn_full", "syn_v") else _orig_platform(p)


def now():
    return datetime.now(timezone.utc)


def mkdep(status, *, provider="syn_w", iid=None, ref=REF, purpose="customer", age=timedelta(hours=1), events=None,
          **kw):
    dep_id = "dep-" + secrets.token_hex(6)
    t = now() - age
    with S.begin() as s:
        d = Deployment(deployment_id=dep_id, account_id=7, route_request_id="rr_t", provider=provider, listing_id="L",
                       provider_instance_id=iid, gpu=G, gpu_count=kw.pop("gpu_count", 1), status=status, created_at=t,
                       uptime_seconds=0, interruptions=0, purpose=purpose, client_name=instance_name(dep_id),
                       credential_source="opengrid", credential_ref=ref, launch_token=secrets.token_hex(8),
                       state_changed_at=t, quoted_price_per_gpu_hour=Decimal("2.0"), provider_metadata={},
                       override_limits=False, provisioned_at=t if iid else None,
                         effective_max_runtime_minutes=kw.pop("effective_max_runtime_minutes", 1440), **kw)
        s.add(d)
        for at, to in (events or [(t, status)]):
            s.add(DeploymentEvent(deployment_id=dep_id, at=at, from_status=None, to_status=to, actor="system"))
        s.add(ProvisionAttempt(deployment_id=dep_id, route_request_id="rr_t", provider=provider, listing_id="L",
                               started_at=t, ok=None, outcome="provisioning" if not iid else "accepted",
                               instance_id=iid, client_name=instance_name(dep_id), credential_ref=ref,
                               launch_token=secrets.token_hex(8)))
    return dep_id


def dep(dep_id):
    with S() as s:
        return s.get(Deployment, dep_id)


def inst(iid, name, state="running", acct=REF, **kw):
    World.INST[iid] = {"state": state, "name": name, "acct": acct, **kw}


def calls(op):
    return [c for c in World.CALLS if c[0] == op]


def watch(dep_id):
    with S() as s:
        return s.get(DeploymentWatch, dep_id)


def orphan_rows():
    with S() as s:
        return list(s.scalars(select(OrphanResource).order_by(OrphanResource.id)))


# --------------------------------------------------------------------------
# Unresolved launches
# --------------------------------------------------------------------------

def test_timeout_then_instance_exists_is_adopted_not_duplicated():
    setup()
    d = mkdep("provider_timeout")
    inst("i-77", instance_name(d), "running", price=1.25)
    r = reconcile.run_once()
    row = dep(d)
    assert row.status == "running" and row.provider_instance_id == "i-77", (row.status, r["findings"])
    assert not calls("provision"), "reconciliation never launches"
    assert float(row.actual_price_per_gpu_hour) == 1.25
    with S() as s:
        att = s.scalars(select(ProvisionAttempt).where(ProvisionAttempt.deployment_id == d)).one()
        ev = s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == d,
                                                     DeploymentEvent.to_status == "running")).one()
    assert att.instance_id == "i-77" and "adopted" in att.error
    assert ev.actor == "reconciler" and "found by name" in ev.evidence["basis"]
    assert any(a["kind"] == "adopted" for a in watch(d).alerts)
    reconcile.run_once()
    assert dep(d).provider_instance_id == "i-77" and len(World.INST) == 1 and not calls("provision")


def test_worker_restart_mid_provision_resolved_by_name():
    setup()
    d = mkdep("provisioning")           # write-ahead row, crash before the result was recorded
    inst("i-9", instance_name(d), "pending")
    reconcile.run_once()
    row = dep(d)
    assert row.provider_instance_id == "i-9" and row.status == "provisioning", row.status
    inst("i-9", instance_name(d), "running")
    tracker.poll(d)
    assert dep(d).status == "running"


def test_unknown_absent_waits_then_provision_failed():
    setup()
    d = mkdep("launch_unknown", age=timedelta(minutes=2))
    r = reconcile.run_once()
    assert dep(d).status == "launch_unknown", "never closed before the provisioning timeout"
    assert any(f["kind"] == "unresolved_waiting" for f in r["findings"])
    with S.begin() as s:
        s.execute(text("UPDATE provision_attempts SET started_at = started_at - interval '30 minutes' "
                       "WHERE deployment_id = :d"), {"d": d})
    World.LIST_FAIL = True
    reconcile.run_once()
    assert dep(d).status == "launch_unknown", "a failed list proves nothing"
    World.LIST_FAIL = False
    reconcile.run_once()
    row = dep(d)
    assert row.status == "provision_failed", row.status
    with S() as s:
        ev = s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == d,
                                                     DeploymentEvent.to_status == "provision_failed")).one()
    assert "absent from list_instances AND find_instance" in ev.evidence["basis"] and ev.actor == "reconciler"


def test_duplicate_launch_is_surfaced_never_picked():
    setup()
    d = mkdep("launch_unknown")
    inst("a", instance_name(d))
    inst("b", instance_name(d))
    reconcile.run_once()
    assert dep(d).status == "orphan_suspected" and dep(d).provider_instance_id is None
    rows = orphan_rows()
    assert {o.instance_id for o in rows} == {"a", "b"} and all(o.kind == "duplicate_launch" for o in rows)
    assert not calls("terminate"), "a duplicate of a live deployment is never auto-terminated"


# --------------------------------------------------------------------------
# Running but gone, terminate confirmation and retries
# --------------------------------------------------------------------------

def test_running_but_gone_needs_two_signals():
    setup()
    d = mkdep("running", iid="i-1")
    inst("i-1", instance_name(d))
    World.STATUS["i-1"] = "not_found"
    tracker.poll(d)
    assert dep(d).status == "running", "one not_found read is never termination"
    World.STATUS.clear()
    World.INST["i-1"]["state"] = "gone"          # list omits it ...
    World.STATUS["i-1"] = "running"              # ... but status says running: one signal only
    reconcile.run_once()
    assert dep(d).status == "running"
    World.STATUS.clear()                         # now both agree: absent from the list AND status not_found
    r = reconcile.run_once()
    row = dep(d)
    assert row.status == "terminated" and row.termination_reason == "provider_terminated", row.status
    with S() as s:
        ev = s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == d,
                                                     DeploymentEvent.to_status == "terminated")).one()
    assert "two signals" in ev.evidence["basis"] and ev.actor == "reconciler"
    assert any(f["kind"] == "provider_terminated" for f in r["findings"])


def test_terminating_retry_backoff_then_termination_failed():
    setup()
    settings.termination_retry_base_seconds = 0
    settings.termination_retry_max = 2
    d = mkdep("terminating", iid="i-2", terminate_requested_at=now() - timedelta(minutes=5))
    inst("i-2", instance_name(d))
    World.TERMINATE = "noop"                     # accepted, but the instance never goes away
    reconcile.run_once()
    reconcile.run_once()
    assert watch(d).terminate_attempts == 2 and dep(d).status == "terminating"
    reconcile.run_once()
    assert dep(d).status == "termination_failed", dep(d).status
    assert any(a["kind"] == "termination_failed" for a in watch(d).alerts)
    n = len(calls("terminate"))
    World.TERMINATE = "ok"
    reconcile.run_once()                         # keeps retrying: the instance is still billing
    assert len(calls("terminate")) == n + 1 and dep(d).status == "termination_failed",         "stays termination_failed until the provider confirms it gone"
    reconcile.run_once()
    assert dep(d).status == "terminated"
    # backoff: with a real base the next retry waits
    settings.termination_retry_base_seconds = 3600
    d2 = mkdep("terminating", iid="i-3", terminate_requested_at=now())
    inst("i-3", instance_name(d2))
    World.TERMINATE = "noop"
    reconcile.run_once()
    reconcile.run_once()
    assert watch(d2).terminate_attempts == 1, "the second pass is inside the backoff window"


def test_deadline_auto_terminate():
    setup()
    d = mkdep("running", iid="i-4", max_runtime_minutes=30, terminate_deadline_at=now() - timedelta(minutes=1))
    inst("i-4", instance_name(d))
    reconcile.run_once()
    row = dep(d)
    assert row.termination_reason == "max_runtime_exceeded" and row.terminate_requested_at is not None
    assert ("terminate", "i-4") in World.CALLS
    assert row.status == "terminated", "terminated in the same pass once the list and status both show it gone"
    with S() as s:
        ev = s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == d,
                                                     DeploymentEvent.to_status == "terminating",
                                                     DeploymentEvent.from_status != "terminating")).one()
    assert ev.actor == "system" and "terminate_deadline_at" in ev.reason


def test_credentials_unavailable_kept_not_terminated():
    setup()
    ref = "byo:42"
    BROKEN_REFS.add(ref)
    try:
        d = mkdep("running", iid="i-5", ref=ref)
        r = reconcile.run_once()
        assert dep(d).status == "credentials_unavailable" and dep(d).terminated_at is None
        assert any(f["kind"] == "credentials_unavailable" for f in r["findings"])
        assert any(a["kind"] == "credentials_unavailable" for a in watch(d).alerts)
        reconcile.run_once()
        assert dep(d).status == "credentials_unavailable"
    finally:
        BROKEN_REFS.discard(ref)
    inst("i-5", instance_name(d), acct=ref)
    reconcile.run_once()
    assert dep(d).status == "running", "credential restored: observation resumes"


# --------------------------------------------------------------------------
# Orphans
# --------------------------------------------------------------------------

def test_orphans_both_kinds_and_auto_terminate_only_provable():
    setup()
    inst("x-1", "og-dep-ffffffffffff")                    # og-* but no deployment anywhere
    inst("x-2", "someone-elses-box")                      # not ours: never touched, never listed as orphan
    ended = mkdep("terminated", iid="i-old", terminated_at=now() - timedelta(minutes=30))
    inst("i-old", instance_name(ended))                   # OpenGrid says terminated, instance alive
    other = mkdep("terminated", iid="i-other", ref="byo:9", terminated_at=now())
    inst("i-other", instance_name(other), acct=REF)       # og-named, but listed under a credential it was NOT pinned to
    r = reconcile.run_once()
    rows = {o.instance_id: o for o in orphan_rows()}
    assert set(rows) == {"x-1", "i-old", "i-other"}, set(rows)
    assert rows["x-1"].kind == "og_no_deployment" and not rows["x-1"].provably_ours
    assert rows["i-old"].kind == "deployment_ended_alive" and rows["i-old"].provably_ours
    assert rows["i-old"].auto_terminated and rows["i-old"].status == "terminating"
    assert not rows["i-other"].provably_ours and not rows["i-other"].auto_terminated
    assert calls("terminate") == [("terminate", "i-old")], "only the provable og-* instance is auto-terminated"
    assert World.INST["x-1"]["state"] == "running" and World.INST["x-2"]["state"] == "running"
    assert r["counts"].get("orphan", 0) >= 3
    reconcile.run_once()
    rows = {o.instance_id: o for o in orphan_rows()}
    assert rows["i-old"].status == "terminated", "confirmed gone by the next list"
    assert rows["x-1"].status == "open" and rows["x-1"].seen_count == 2


def test_rejected_launch_that_created_an_instance():
    setup()
    d = mkdep("provision_failed")
    inst("i-r", instance_name(d))
    reconcile.run_once()
    row = dep(d)
    assert row.provider_instance_id == "i-r" and row.status == "terminating", row.status
    assert ("terminate", "i-r") in World.CALLS
    reconcile.run_once()
    assert dep(d).status == "terminated"


def test_resolve_orphan_actions():
    setup()
    inst("x-1", "og-dep-aaaaaaaaaaaa")
    inst("x-2", "og-dep-bbbbbbbbbbbb")
    reconcile.run_once()
    rows = {o.instance_id: o for o in orphan_rows()}
    out = reconcile.resolve_orphan(rows["x-1"].id, "ignore", "ops@test", note="known test box")
    assert out["status"] == "ignored" and out["resolved_by"] == "ops@test"
    out = reconcile.resolve_orphan(rows["x-2"].id, "terminate", "ops@test")
    assert out["status"] == "terminating" and World.INST["x-2"]["state"] == "gone"
    try:
        reconcile.resolve_orphan(rows["x-1"].id, "adopt", "ops@test")
        raise AssertionError("adopt needs an unresolved deployment")
    except ValueError:
        pass
    try:
        reconcile.resolve_orphan(rows["x-1"].id, "delete", "ops@test")
        raise AssertionError("unknown action")
    except ValueError:
        pass
    reconcile.run_once()
    assert {o["instance_id"]: o["status"] for o in reconcile.orphans()}["x-2"] == "terminated"
    assert reconcile.orphans(status="ignored")[0]["instance_id"] == "x-1"


def test_runs_are_recorded():
    setup()
    World.LIST_FAIL = True
    mkdep("running", iid="i-6")
    r = reconcile.run_once()
    assert r["status"] == "partial" and r["counts"].get("list_failed", 0) >= 1
    with S() as s:
        run = s.get(ReconciliationRun, r["run_id"])
    assert run.finished_at and run.findings and run.providers["syn_w"]["list_errors"] == 1
    assert reconcile.last_run()["id"] == r["run_id"]


def test_ops_alerts_sent_and_deduplicated():
    from alerts import ops

    setup()
    sent = []
    orig = ops.alert
    ops.alert = lambda kind, title, **kw: sent.append((kind, kw.get("dedupe"), kw.get("detail") or {})) or {}
    try:
        inst("x-9", "og-dep-cccccccccccc", price=31.5)
        d = mkdep("running", iid="i-d", max_runtime_minutes=5, terminate_deadline_at=now() - timedelta(minutes=1))
        inst("i-d", instance_name(d))
        u = mkdep("launch_unknown", age=timedelta(hours=1))
        with S.begin() as s:
            s.execute(text("UPDATE provision_attempts SET started_at = now() - interval '1 hour' WHERE deployment_id = :d"),
                      {"d": u})
        World.LIST_FAIL = True
        reconcile.run_once()
        World.LIST_FAIL = False
        reconcile.run_once()
        reconcile.run_once()
    finally:
        ops.alert = orig
    kinds = [k for k, _, _ in sent]
    assert kinds.count("orphan_detected") == 1, kinds
    orphan = next(dt for k, _, dt in sent if k == "orphan_detected")
    assert orphan["instance_id"] == "x-9" and orphan["estimated_hourly_cost_usd"] == 31.5
    assert kinds.count("deadline_terminate") == 1, kinds
    assert kinds.count("launch_unknown") == 1, "unresolved after the timeout, sent once despite repeated runs"
    assert "reconciliation_failed" not in kinds


# --------------------------------------------------------------------------
# Metering
# --------------------------------------------------------------------------

def _metered(d):
    with S() as s:
        return list(s.scalars(select(UsageSlice).where(UsageSlice.deployment_id == d).order_by(UsageSlice.period_start)))


def _usage(d):
    with S() as s:
        return list(s.scalars(select(UsageRecord).where(UsageRecord.deployment_id == d).order_by(UsageRecord.period_start)))


def test_hourly_slices_idempotent_month_split_storage_only():
    setup()
    t0 = datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc)
    ev = [(t0, "created"), (t0 + timedelta(minutes=5), "provisioning"), (t0 + timedelta(minutes=10), "running"),
          (t0 + timedelta(hours=1, minutes=10), "stopped"), (t0 + timedelta(hours=2, minutes=10), "running"),
          (t0 + timedelta(hours=3), "terminating"), (t0 + timedelta(hours=3, minutes=10), "terminated")]
    d = mkdep("terminated", iid="i-m", events=ev, gpu_count=2, actual_price_per_gpu_hour=Decimal("1.5"),
              terminated_at=t0 + timedelta(hours=3, minutes=5))   # provider-confirmed end (before observation)
    with S.begin() as s:
        w = tracker.watch_row(s, d)
        w.ended_at, w.ended_basis, w.updated_at = t0 + timedelta(hours=3, minutes=5), "provider_reported", now()
    tracker.meter(d)
    tracker.meter(d)
    sl = _metered(d)
    assert [x.period_start.hour for x in sl] == [22, 23, 0, 1], [x.period_start for x in sl]
    for x in sl:
        assert x.period_start.month == (x.period_end - timedelta(microseconds=1)).month, "never crosses a month"
    run = sum(x.running_seconds for x in sl)
    stopped = sum(x.stopped_seconds for x in sl)
    # running 22:40-23:40 (60m) + 00:40-01:30 (50m) + terminating 01:30-01:35 (5m, billed until the provider's end)
    assert run == 115 * 60 and stopped == 60 * 60, (run, stopped)
    assert all(x.stopped_billing == "storage_only" for x in sl)
    cost = sum(Decimal(x.cost_usd) for x in sl)
    assert abs(cost - Decimal("1.5") * 2 * Decimal(115) / 60) < Decimal("0.00001"), cost
    assert sl[-1].final and not sl[-1].end_estimated and sl[-1].period_end == t0 + timedelta(hours=3, minutes=5)
    u = _usage(d)
    assert len(u) == len([x for x in sl if x.billable_seconds > 0]), "one usage record per billable slice"
    assert sum(x.provider_cost_usd for x in u) == cost
    assert {x.period_start.month for x in u} == {10, 11}, "October hours on October, November on November"
    assert abs(u[0].gpu_hours - Decimal(2) * Decimal(sl[0].billable_seconds) / 3600) < Decimal("0.00001"), \
        "GPU-hours = billable time only"
    oct_cost = sum(x.provider_cost_usd for x in u if x.period_start.month == 10)
    assert oct_cost == Decimal("1.5") * 2 * Decimal(60) / 60, oct_cost


def test_stopped_billing_full_and_error_state_not_billed():
    setup()
    t0 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    ev = [(t0, "running"), (t0 + timedelta(minutes=30), "stopped"), (t0 + timedelta(minutes=60), "running"),
          (t0 + timedelta(minutes=90), "degraded"), (t0 + timedelta(minutes=100), "terminating"),
          (t0 + timedelta(minutes=120), "terminated")]
    d = mkdep("terminated", provider="syn_full", iid="i-f", ref="platform:syn_full", events=ev,
              terminated_at=t0 + timedelta(minutes=120))
    tracker.meter(d)
    sl = _metered(d)
    assert len(sl) == 2 and sl[-1].end_estimated, "no provider end time: flagged estimate"
    run = sum(x.running_seconds for x in sl)
    stopped = sum(x.stopped_seconds for x in sl)
    assert run == (30 + 30 + 20) * 60 and stopped == 30 * 60, (run, stopped)   # degraded 10 min not billed
    assert sum(x.billable_seconds for x in sl) == (80 + 30) * 60, "stopped time bills in full on this provider"
    assert abs(sum(Decimal(x.cost_usd) for x in sl) - Decimal("2.0") * Decimal(110) / 60) < Decimal("0.00001")


def test_live_metering_closed_hours_only_until_last_observation():
    setup()
    start = (now() - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0) + timedelta(minutes=20)
    d = mkdep("running", iid="i-l", events=[(start, "running")])
    tracker.meter(d)
    assert not _metered(d), "never observed by the tracker: nothing metered"
    with S.begin() as s:
        w = tracker.watch_row(s, d)
        w.last_observed_at, w.updated_at = start + timedelta(hours=1, minutes=50), now()
    tracker.meter(d)
    sl = _metered(d)
    assert [x.running_seconds for x in sl] == [40 * 60, 3600] and not sl[-1].final, \
        "only closed hours that end before the last successful observation"
    with S.begin() as s:
        w = tracker.watch_row(s, d)
        w.last_observed_at = now()
    tracker.meter(d)
    tracker.meter(d)
    sl = _metered(d)
    assert len(sl) == 3 and [x.running_seconds for x in sl] == [40 * 60, 3600, 3600]
    assert len({x.period_start for x in sl}) == 3


def test_poll_records_provider_end_time():
    setup()
    t0 = now() - timedelta(hours=2)
    d = mkdep("running", iid="i-e", events=[(t0, "running")])
    end = now() - timedelta(minutes=10)
    inst("i-e", instance_name(d), "terminated", ended_at=end)
    tracker.poll(d)
    row = dep(d)
    assert row.status == "terminated" and abs((row.terminated_at - end).total_seconds()) < 1, row.terminated_at
    assert watch(d).ended_basis == "provider_reported"
    sl = _metered(d)
    assert sl and sl[-1].final and not sl[-1].end_estimated and sl[-1].period_end == row.terminated_at


# --------------------------------------------------------------------------
# Cost reconciliation
# --------------------------------------------------------------------------

def test_cost_reconciliation_numbers():
    setup()
    t0 = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    ev = [(t0, "running"), (t0 + timedelta(hours=3), "terminated")]
    d = mkdep("terminated", iid="i-c", events=ev, gpu_count=2, actual_price_per_gpu_hour=Decimal("2.1"),
              terminated_at=t0 + timedelta(hours=3))
    with S.begin() as s:
        s.add(ExecutionRecord(deployment_id=d, route_request_id="rr_t", provider="syn_w", gpu=G, gpu_count=2,
                              provision_ok=True, attempts=1, uptime_seconds=0, interruptions=0,
                              created_at=now(), updated_at=now()))
    World.COST = 13.0
    inst("i-c", instance_name(d), "gone")
    transactions.bill(d)
    row = dep(d)
    rec = row.reconciliation
    assert rec["opengrid_transaction_cost"]["amount_usd"] == 12.6
    assert rec["expected_cost"]["amount_usd"] == 12.0
    assert rec["quote_error"]["amount_usd"] == 0.6 and rec["quote_error"]["pct"] == 5.0
    assert rec["effective_hourly_rate"]["per_gpu_hour_transaction"] == 2.1
    assert abs(rec["effective_hourly_rate"]["per_gpu_hour_provider"] - 13.0 / 6) < 1e-6
    assert rec["provider_reported_cost"]["amount_usd"] == 13.0 and float(row.provider_reported_cost) == 13.0
    assert rec["billing_rounding"]["amount_usd"] == 0.0 and rec["billing_rounding"]["billing_unit"] == "per second"
    assert rec["unexpected_fees"]["amount_usd"] == 0.4
    assert row.reconciled_at is not None
    with S() as s:
        er = s.get(ExecutionRecord, d)
    assert float(er.provider_cost_usd) == 12.6 and er.cost_basis == "metered" and er.usage_record_id
    assert transactions.unbilled() == [], "metered + reconciled: nothing left to bill"
    n = len(_usage(d))
    transactions.bill(d)
    assert len(_usage(d)) == n, "billing again never double-charges"
    # per-minute billing unit: 3h + 30s rounds up to the next minute; no provider cost -> null + reason
    t1 = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    d2 = mkdep("terminated", provider="syn_full", iid="i-c2", ref="platform:syn_full", gpu_count=1,
               events=[(t1, "running"), (t1 + timedelta(hours=3, seconds=30), "terminated")],
               actual_price_per_gpu_hour=Decimal("1.2"), terminated_at=t1 + timedelta(hours=3, seconds=30))
    transactions.bill(d2)
    rec = dep(d2).reconciliation
    assert rec["runtime"]["billable_seconds"] == 3 * 3600 + 30, rec["runtime"]
    assert rec["billing_rounding"]["amount_usd"] == round(1.2 * 30 / 3600, 6) and rec["billing_rounding"]["unit_seconds"] == 60
    assert rec["provider_reported_cost"]["amount_usd"] is None and rec["provider_reported_cost"]["reason"]
    assert rec["unexpected_fees"]["amount_usd"] is None


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
        if S is not None:
            S.kw["bind"].dispose()
        scratchdb.drop(DB)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
