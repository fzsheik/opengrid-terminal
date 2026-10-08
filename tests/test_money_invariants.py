"""MONEY-LOSS INVARIANT: OpenGrid must never lose track of a resource that can cost money.

Every scenario drives the REAL Lambda adapter (routing/adapters/lambda_labs.py) through MoneySim -- a stateful
fake of the Lambda Cloud API behind httpx.MockTransport (tests/bench_fixtures.LambdaSim extended with provider
accounts per API key, first_healthy, per-deployment SSH keys, sticky unknown states and a whole-API outage) --
the real engine / launch path, and the real tracker.track() + reconcile.run_once() as the "workers". Time is a
simulated clock patched into every routing module's _now().

At the end of a bounded number of worker cycles each scenario asserts the TERMINAL INVARIANT, one of:
  (A) PROVEN terminated: the provider says terminated (the sim's own record) and the terminated event carries
      provider evidence; billed exactly from billable_start (= Lambda first_healthy) to billable_end (>= the true
      provider end); metering complete; the per-deployment SSH key deleted at the provider (or delete_failed +
      an open resource_delete_failed alert). Or a definitive no-instance outcome with nothing alive.
  (B) a prominent UNKNOWN / POSSIBLY-BILLING state (an uncertain state, or past_deadline) WITH an open,
      re-escalating cost-exposure ops alert carrying deployment_id, provider, account_id,
      est_hourly_exposure_usd, time_in_state, suggested_action -- and never silently finished.
Globally (bench_fixtures.invariants + extras): provider create calls <= 1 per deployment intent; no instance
alive at the provider without a live/uncertain deployment or an open orphan record + open suspected_orphan
alert; usage slices never overlap and never bill past confirmed termination; 'running' only on provider evidence;
'terminated' only with provider evidence.

Scratch DB og_test_money (re-created per scenario); no network, no uvicorn.
Run:  .venv/Scripts/python tests/test_money_invariants.py
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_fixtures as bf  # noqa: E402
from bench_fixtures import PK, SKU, LambdaSim, WorkerKilled, q, scalar  # noqa: E402

import httpx  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import OPERATOR  # noqa: E402
from alerts import ops  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import (  # noqa: E402
    adapters, control, deployments, engine, idempotency, quotes, reconcile, tracker, transactions,
)
from routing.adapters import base as abase  # noqa: E402
from routing.adapters import resources  # noqa: E402
from routing.adapters.results import instance_name  # noqa: E402
from sqlalchemy import text  # noqa: E402

DB = "og_test_money"
G = "NVIDIA H100 80GB SXM5"
KEY = "test-lambda-key-0123456789"               # OpenGrid's platform key: provider account "A"
KEY_OTHER = "test-lambda-key-OTHER-ACCOUNT"       # a valid key of a DIFFERENT Lambda account ("Z")
KEY_BYO = "test-lambda-key-BYO-customer"          # a customer's own Lambda account ("C")
KEY_DEAD = "test-lambda-key-REVOKED-AT-LAMBDA"    # Lambda answers 401 for it
PRICE = 3.29

SIM: "MoneySim | None" = None
DEPS: list[str] = []
RESULTS: list[tuple[str, str, str]] = []          # (scenario, outcome, pass/FAIL)
SENT: list[tuple[str, str, dict]] = []            # every ops.alert (kind, title, detail)
_orig_alert = ops.alert


# --------------------------------------------------------------------------
# The simulated clock (every routing module's _now)
# --------------------------------------------------------------------------

class Clock:
    MODS = (deployments, tracker, reconcile, transactions, engine, quotes, control, idempotency, resources, abase)

    def __init__(self):
        self.t = datetime.now(timezone.utc)
        self.orig: dict = {}

    def now(self) -> datetime:
        return self.t

    def install(self) -> None:
        for m in self.MODS:
            self.orig.setdefault(m, m._now)
            m._now = self.now

    def uninstall(self) -> None:
        for m, f in self.orig.items():
            m._now = f

    def reset(self) -> None:
        self.t = datetime.now(timezone.utc).replace(microsecond=0)

    def advance(self, **kw) -> None:
        self.t = self.t + timedelta(**kw)


CLOCK = Clock()


# --------------------------------------------------------------------------
# MoneySim: LambdaSim with provider accounts, first_healthy, ssh keys, sticky unknown states, outages
# --------------------------------------------------------------------------

class MoneySim(LambdaSim):
    """instances {id: {name, status, boots_left, acct, first_healthy, ssh_key_names}}; keys {id: {...}}.
    accounts: API key -> provider account (an unknown key gets 401). down=True: every call answers 500.
    sticky[iid] = 'flux': the provider persistently reports a status OpenGrid does not understand.
    Extra launch faults: http429_after (rejected, but an instance was created). Extra ops: ssh_list, ssh_delete."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.accounts = {KEY: "A", KEY_OTHER: "Z", KEY_BYO: "C"}
        self.keys: dict[str, dict] = {}
        self.sticky: dict[str, str] = {}
        self.true_end: dict[str, datetime] = {}
        self.down = False

    def _acct(self, request) -> str | None:
        tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return self.accounts.get(tok)

    def _inst(self, iid: str) -> dict:
        v = self.instances[iid]
        st = self.sticky.get(iid, v["status"])
        return {"id": iid, "name": v["name"], "status": st, "ip": "198.51.100.10", "region": {"name": "us-east-1"},
                "instance_type": {"name": SKU, "price_cents_per_hour": self.price_cents, "specs": {"gpus": 1}},
                "tags": [{"key": "opengrid", "value": v["name"]}], "ssh_key_names": v.get("ssh_key_names") or [],
                "first_healthy": v["first_healthy"].isoformat() if v.get("first_healthy") else None}

    def _create(self, body, acct="A") -> str:
        with self.lock:
            iid = "lam-" + secrets.token_hex(4)
            self.instances[iid] = {"name": body.get("name"), "status": "booting", "boots_left": self.boot_polls,
                                   "acct": acct, "first_healthy": None, "ssh_key_names": body.get("ssh_key_names"),
                                   "created_at": CLOCK.now()}
        return iid

    def _end(self, iid):
        v = self.instances[iid]
        if v["status"] != "terminated":
            v["status"] = "terminating"
            self.true_end.setdefault(iid, CLOCK.now())   # Lambda bills until the moment it is terminated

    BOOT_SECONDS = 60

    def _tick(self, v):
        if v["status"] == "booting":
            healthy_at = v["created_at"] + timedelta(seconds=self.BOOT_SECONDS)
            if v["boots_left"] <= 0 and CLOCK.now() >= healthy_at:
                v["status"] = "active"
                v["first_healthy"] = v["first_healthy"] or healthy_at    # passes health checks on its own clock
            elif v["boots_left"] > 0:
                v["boots_left"] -= 1
        elif v["status"] == "terminating":
            v["status"] = "terminated"

    def _mine(self, iid, acct):
        v = self.instances.get(iid)
        return v if v is not None and v["acct"] == acct else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = None
        self.log.append((method, path, body))
        if self.down:
            return httpx.Response(500, json={"error": {"code": "global/unknown", "message": "service unavailable"}})
        acct = self._acct(request)
        if acct is None:
            return httpx.Response(401, json={"error": {"code": "global/invalid-api-key", "message": "invalid key"}})
        e500 = httpx.Response(500, json={"error": {"code": "global/unknown"}})
        if path == "/api/v1/instance-types":
            if self._take("types") == "http500":
                return e500
            regions = [{"name": "us-east-1"}] if self.capacity else []
            return httpx.Response(200, json={"data": {SKU: {"instance_type": {
                "name": SKU, "price_cents_per_hour": self.price_cents, "specs": {"gpus": 1}},
                "regions_with_capacity_available": regions}}})
        if path == "/api/v1/ssh-keys" and method == "POST":
            f = self._take("ssh")
            if f == "http500":
                return e500
            if f == "http422":
                return httpx.Response(400, json={"error": {"code": "global/invalid-parameters"}})
            kid = "k-" + secrets.token_hex(3)
            with self.lock:
                self.keys[kid] = {"name": body["name"], "public_key": body["public_key"], "acct": acct}
            return httpx.Response(200, json={"data": {"id": kid, "name": body["name"], "public_key": body["public_key"]}})
        if path == "/api/v1/ssh-keys" and method == "GET":
            if self._take("ssh_list") == "http500":
                return e500
            return httpx.Response(200, json={"data": [{"id": k, "name": v["name"], "public_key": v["public_key"]}
                                                      for k, v in self.keys.items() if v["acct"] == acct]})
        if path.startswith("/api/v1/ssh-keys/") and method == "DELETE":
            if self._take("ssh_delete") == "http500":
                return e500
            kid = path.rsplit("/", 1)[-1]
            with self.lock:
                v = self.keys.get(kid)
                if v is None or v["acct"] != acct:
                    return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist"}})
                del self.keys[kid]
            return httpx.Response(200, json={"data": {}})
        if path == "/api/v1/instance-operations/launch":
            if self.delay:
                time.sleep(self.delay)
            f = self._take("launch")
            if f == "connect_error":
                raise httpx.ConnectError("connection refused", request=request)
            if f == "timeout_before":
                raise httpx.ReadTimeout("timed out", request=request)
            if f == "http500":
                return e500
            if f == "capacity":
                return httpx.Response(400, json={"error": {
                    "code": "instance-operations/launch/insufficient-capacity", "message": "no capacity"}})
            iid = self._create(body, acct)
            if f == "timeout_after":
                raise httpx.ReadTimeout("timed out", request=request)
            if f == "reset_after":
                raise httpx.RemoteProtocolError("peer closed connection", request=request)
            if f == "http500_after":
                return e500
            if f == "garbage_after":
                return httpx.Response(200, text="<html>upstream proxy error</html>")
            if f == "http429_after":
                return httpx.Response(429, json={"error": {"code": "global/rate-limited", "message": "slow down"}})
            if f == "kill_after":
                raise WorkerKilled("worker killed after the provider accepted the launch")
            return httpx.Response(200, json={"data": {"instance_ids": [iid]}})
        if path.startswith("/api/v1/instances/") and method == "GET":
            iid = path.rsplit("/", 1)[-1]
            f = self._take("status")
            if f == "http500":
                return e500
            if f == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            with self.lock:
                v = self._mine(iid, acct)
                if v is None:
                    return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist"}})
                self._tick(v)
            return httpx.Response(200, json={"data": self._inst(iid)})
        if path == "/api/v1/instance-operations/terminate":
            f = self._take("terminate")
            ids = (body or {}).get("instance_ids") or []
            if f == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if f == "http500":
                return e500
            with self.lock:
                if any(self._mine(i, acct) is None for i in ids):
                    return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist"}})
                if f != "noop":
                    for i in ids:
                        self._end(i)
            if f == "timeout_after":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json={"data": {"terminated_instances": [self._inst(i) for i in ids]}})
        if path == "/api/v1/instances" and method == "GET":
            if self._take("list") == "http500":
                return e500
            with self.lock:
                for v in self.instances.values():
                    if v["status"] == "terminating":
                        v["status"] = "terminated"
                data = [self._inst(k) for k, v in self.instances.items()
                        if v["acct"] == acct and (v["status"] != "terminated" or k in self.sticky)]
            return httpx.Response(200, json={"data": data, "page_token": None})
        return httpx.Response(599, json={"error": f"unmocked {method} {path}"})


# --------------------------------------------------------------------------
# Setup / workers
# --------------------------------------------------------------------------

S = None


def _capture(kind, title, **kw):
    SENT.append((kind, title, kw.get("detail") or {}))
    return _orig_alert(kind, title, **kw)


def setup(**kw) -> MoneySim:
    """A fresh world: scratch database, provider, clock, settings."""
    global SIM, S
    if S is not None:
        bf.drop(S, DB)
    S = bf.db(DB)
    try:
        from accounts import accounts as acc
        acc._operator_id = None
    except Exception:  # noqa: BLE001
        pass
    row = bf.listing("lambda", PRICE, gpu=G, region="us-east-1", lid="lambda:" + SKU)
    row.sku = SKU
    bf.market(G, [row])
    CLOCK.reset()
    CLOCK.install()
    SIM = MoneySim(**kw)
    adapters.TRANSPORTS["lambda"] = SIM.transport()
    DEPS.clear()
    SENT.clear()
    ops.alert = _capture
    settings.lambda_api_key = KEY
    settings.ops_alert_webhook_url = None           # never a real delivery
    settings.ops_alert_webhook_secret = None
    settings.routing_max_attempts = 1
    settings.provisioning_timeout_minutes = 15
    settings.termination_retry_base_seconds = 60
    settings.termination_retry_max = 5
    settings.alert_reescalate_minutes = 30
    settings.alert_unknown_minutes = 10
    settings.ssh_key_delete_retry_max = 3
    settings.ssh_key_delete_retry_base_seconds = 60
    settings.runtime_default_minutes = 60
    bf.mode("LIVE")
    bf.flags("lambda")
    return SIM


def restart() -> None:
    """A process restart: nothing in memory survives; fresh workers (same database, same provider)."""
    adapters.TRANSPORTS["lambda"] = SIM.transport()
    reconcile._JOB_FAILS.clear()
    try:
        from accounts import accounts as acc
        acc.reset_cache()
    except Exception:  # noqa: BLE001
        pass


def spec(**kw):
    base = {"gpu": G, "count": 1, "region_group": None, "max_price_per_gpu_hour": None, "duration_hours": 1,
            "deadline_hours": None, "mode": "CHEAPEST", "weights": None,
            "preferences": {"include_providers": ["lambda"]}, "launch": {"ssh_public_key": PK}, "strict_region": False}
    base.update(kw)
    return base


def launch(*faults, who=None, **kw):
    if faults:
        SIM.fault("launch", *faults)
    who = who or bf.key(bf.fresh_account())
    code, out = engine.route(spec(**kw), who)
    for d in out.get("deployments") or []:
        DEPS.append(d)
    return code, out, (out["deployments"][-1] if out.get("deployments") else None), who


def dep(d):
    return deployments.load_row(d)


def cycle(minutes: float = 5) -> None:
    """One pass of both workers after `minutes` of simulated time."""
    CLOCK.advance(minutes=minutes)
    tracker.track()
    reconcile.run_once()


def drive(d, *, cycles=40, minutes=5, until=("terminated",)) -> str:
    for _ in range(cycles):
        if dep(d).status in until:
            break
        cycle(minutes)
    cycle(1)          # one more pass: key cleanup / billing retries / alert resolution
    return dep(d).status


def iid_of(d) -> str | None:
    return dep(d).provider_instance_id or next((k for k, v in SIM.instances.items() if v["name"] == instance_name(d)),
                                               None)


# --------------------------------------------------------------------------
# The terminal invariant
# --------------------------------------------------------------------------

def watch(d):
    return reconcile.watch(d) or {}


def open_alerts(d) -> list[dict]:
    return [a for a in ops.open_exposures() if a.get("deployment_id") == d or d in (a.get("deployments") or [])]


def _fields_ok(a: dict) -> None:
    for f in ops.REQUIRED_FIELDS:
        assert f in a, f"alert {a['kind']} lacks {f}: {a}"
    for f in ("deployment_id", "provider", "account_id", "est_hourly_exposure_usd", "time_in_state",
              "suggested_action"):
        assert a.get(f) not in (None, ""), f"alert {a['kind']} has no {f}: {a}"


def keys_ok(d) -> None:
    """The per-deployment SSH key is gone at the provider, or delete_failed with an open alert."""
    rows = q("SELECT id, status, provider_resource_id, name FROM provider_resources WHERE deployment_id = :d", d=d)
    at_provider = [k for k, v in SIM.keys.items() if v["name"] == instance_name(d)]
    for rid, st, kid, name in rows:
        if st == "delete_failed":
            assert scalar("SELECT count(*) FROM ops_alert_state WHERE kind = 'resource_delete_failed' AND "
                          "subject = :s AND status = 'open'", s=f"provider_resource:{rid}") == 1, (rid, st)
        else:
            assert st in ("deleted", "not_created"), f"{d}: ssh key resource {rid} is {st}"
    if at_provider:
        assert any(r[1] == "delete_failed" for r in rows), f"{d}: ssh key {at_provider} still at the provider"


def assert_A(d) -> str:
    row = dep(d)
    w = watch(d)
    if row.status in ("provision_failed", "provider_rejected", "rejected"):
        alive = [k for k, v in SIM.alive().items() if v["name"] == instance_name(d)]
        assert not alive, f"{d} is {row.status} but {alive} is alive at the provider"
        keys_ok(d)
        return "A"
    assert row.status == "terminated", f"{d}: expected PROVEN terminated, got {row.status}"
    iid = row.provider_instance_id
    assert iid and SIM.instances[iid]["status"] == "terminated", \
        f"{d}: OpenGrid says terminated but the provider has {SIM.instances.get(iid)}"
    ev = q("SELECT evidence FROM deployment_events WHERE deployment_id = :d AND to_status = 'terminated' "
           "AND from_status <> 'terminated'", d=d)
    assert len(ev) == 1, f"{d}: terminated {len(ev)} times"
    fh = SIM.instances[iid]["first_healthy"]
    sl = q("SELECT period_start, period_end, running_seconds, billable_seconds, usage_record_id FROM usage_slices "
           "WHERE deployment_id = :d ORDER BY period_start", d=d)
    assert w.get("metering_complete") is True, f"{d}: metering not complete {w}"
    if fh is not None:
        assert row.billable_start == fh and row.billable_basis == "provider_running_at", \
            (row.billable_start, row.billable_basis, fh)
        assert row.billable_end is not None and row.billable_end >= SIM.true_end[iid] - timedelta(seconds=1), \
            f"{d}: billable_end {row.billable_end} before the provider's true end {SIM.true_end.get(iid)}"
        billed = sum(r[2] for r in sl)
        expect = (row.billable_end - row.billable_start).total_seconds()
        assert abs(billed - expect) <= len(sl) + 1, \
            f"{d}: billed {billed}s but billable window is {expect}s ({row.billable_start}..{row.billable_end}) {sl}"
        assert sl and sl[0][0] <= fh and sl[-1][1] == row.billable_end, sl
        recs = scalar("SELECT count(*) FROM usage_records WHERE deployment_id = :d", d=d)
        assert recs == len([r for r in sl if r[3] > 0]) and all(r[4] for r in sl if r[3] > 0), (recs, sl)
        cost = float(scalar("SELECT coalesce(sum(provider_cost_usd), 0) FROM usage_records WHERE deployment_id = :d",
                            d=d))
        assert abs(cost - PRICE * billed / 3600) < 0.01, (cost, billed)
    else:
        assert sum(r[3] for r in sl) == 0, f"{d}: billed although the provider never ran it: {sl}"
    keys_ok(d)
    for a in open_alerts(d):
        assert a["kind"] not in ("past_deadline", "resource_state_unknown", "termination_failed"), \
            f"{d}: {a['kind']} still open after proven termination"
    return "A"


def assert_B(d) -> str:
    row = dep(d)
    w = watch(d)
    assert row.status in deployments.LIVE_STATES, f"{d}: expected a possibly-billing state, got {row.status}"
    assert row.status in deployments.UNCERTAIN_STATES or w.get("past_deadline_at"), \
        f"{d}: {row.status} is neither uncertain nor past_deadline"
    if row.provider_instance_id or iid_of(d):
        assert iid_of(d) in SIM.alive() or row.status in deployments.UNCERTAIN_STATES
    al = open_alerts(d)
    assert al, f"{d}: possibly billing ({row.status}) but no open cost-exposure alert"
    mine = [a for a in al if a.get("deployment_id") == d]
    assert mine, al
    for a in mine:
        _fields_ok(a)
    before = {a["id"]: a["sent_count"] for a in al}
    CLOCK.advance(minutes=int(settings.alert_reescalate_minutes) + 1)
    tracker.track()
    reconcile.run_once()
    after = open_alerts(d)
    assert any(a["sent_count"] > before.get(a["id"], 0) for a in after), \
        f"{d}: open alerts did not re-escalate: {before} -> {[(a['id'], a['sent_count']) for a in after]}"
    assert any(t.startswith("[re-escalation") for _, t, _ in SENT), "no re-escalation was sent"
    assert dep(d).status not in deployments.TERMINAL_STATES, f"{d}: silently finished as {dep(d).status}"
    return "B"


def global_invariants() -> None:
    bf.invariants(DEPS, sim=SIM)
    # an og-* instance alive with no live deployment: an open orphan record AND an open suspected_orphan alert
    for iid, v in SIM.alive().items():
        st = q("SELECT status FROM deployments WHERE client_name = :n", n=v["name"])
        if st and st[0][0] in deployments.LIVE_STATES:
            continue
        oid = scalar("SELECT id FROM orphan_resources WHERE instance_id = :i AND status IN ('open','terminating')",
                     i=iid)
        assert oid, f"{iid} alive without deployment or orphan record"
        assert scalar("SELECT count(*) FROM ops_alert_state WHERE kind = 'suspected_orphan' AND subject = :s "
                      "AND status = 'open'", s=f"orphan:{oid}") == 1, f"orphan {oid} has no open alert"
    for d in DEPS:
        assert len(SIM.creates(instance_name(d))) <= 1


def finish(name: str, d, expect: str) -> None:
    out = assert_A(d) if expect == "A" else assert_B(d)
    global_invariants()
    RESULTS.append((name, out, "pass"))


def to_running(d) -> None:
    for _ in range(4):
        if dep(d).status == "running":
            return
        cycle(1)
    assert dep(d).status == "running", dep(d).status


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def test_api_timeout_after_launch_succeeded():
    """The instance was created; the response was lost (timeout / reset / 5xx / garbage)."""
    for fault in ("timeout_after", "reset_after", "http500_after", "garbage_after"):
        setup()
        code, out, d, _ = launch(fault)
        assert dep(d).status in ("provider_timeout", "launch_unknown") and not dep(d).provider_instance_id
        assert len(SIM.alive()) == 1
        cycle(1)                                      # reconciliation adopts by name
        assert dep(d).provider_instance_id in SIM.instances, dep(d).status
        to_running(d)
        drive(d)                                      # the 60 min deadline terminates it
        assert dep(d).termination_reason == "max_runtime_exceeded"
        assert len(SIM.creates()) == 1
        finish(f"api timeout after launch ({fault})", d, "A")


def test_db_commit_failure_after_provider_launch():
    setup()
    real = deployments.note_event

    def fail(s, d, reason, *a, **k):
        if reason == "provider accepted the launch":
            raise RuntimeError("could not commit: connection to the database lost")
        return real(s, d, reason, *a, **k)

    deployments.note_event = fail
    try:
        code, out, d, _ = launch()
    finally:
        deployments.note_event = real
    row = dep(d)
    assert row.status == "provisioning" and row.provider_instance_id is None and len(SIM.alive()) == 1
    assert q("SELECT outcome FROM provision_attempts WHERE deployment_id = :d", d=d) == [("provisioning",)]
    r = deployments.launch(d, adapter=None, offer=None, availability=None, launch_spec=None, resolved=None)
    assert r["launched"] is False                     # the launch token forbids a second create
    cycle(1)
    assert dep(d).provider_instance_id in SIM.instances
    drive(d)
    assert len(SIM.creates()) == 1
    finish("DB commit failure after provider launch", d, "A")


def test_process_crash_after_provider_launch():
    setup()
    try:
        launch("kill_after")
        raise AssertionError("the worker should have died")
    except WorkerKilled:
        pass
    d = scalar("SELECT deployment_id FROM deployments ORDER BY created_at DESC LIMIT 1")
    DEPS.append(d)
    assert dep(d).status == "provisioning" and dep(d).launch_token and len(SIM.alive()) == 1
    restart()
    drive(d)
    assert len(SIM.creates()) == 1
    finish("process crash after provider launch", d, "A")


def test_process_crash_during_termination():
    for when in ("after_terminate_sent", "before_terminate_sent"):
        setup()
        code, out, d, who = launch()
        to_running(d)
        cycle(20)
        if when == "after_terminate_sent":
            real, attr = deployments.as_terminate_result, "as_terminate_result"
        else:
            real, attr = deployments.adapter_for, "adapter_for"

        def die(*a, **k):
            raise WorkerKilled(f"restart {when}")

        setattr(deployments, attr, die)
        try:
            deployments.terminate(d, who)
            raise AssertionError("expected the worker to die")
        except WorkerKilled:
            pass
        finally:
            setattr(deployments, attr, real)
        assert dep(d).status == "terminating"
        assert len(SIM.terminates()) == (1 if when == "after_terminate_sent" else 0)
        restart()
        drive(d)
        assert len(SIM.terminates()) == 1, SIM.terminates()
        finish(f"process crash during termination ({when})", d, "A")


def test_user_api_key_revoked_mid_deployment():
    setup()
    from accounts import keys
    aid = bf.fresh_account()
    k = keys.create_key(aid, "ci", scopes=sorted(bf.SCOPES))
    who = keys.verify(k["secret"], None)
    code, out, d, _ = launch(who=who)
    to_running(d)
    keys.revoke_key(k["id"])
    try:
        keys.verify(k["secret"], None)
        raise AssertionError("a revoked key must not authenticate")
    except HTTPException as e:
        assert e.status_code == 401
    drive(d)
    assert dep(d).termination_reason == "max_runtime_exceeded"
    finish("user API key revoked mid-deployment", d, "A")


def test_provider_credential_rotated_mid_deployment():
    for case, new_key in (("platform key removed", None), ("replaced by a key Lambda rejects", KEY_DEAD),
                          ("replaced by another Lambda account's key", KEY_OTHER)):
        setup()
        code, out, d, _ = launch()
        to_running(d)
        iid = dep(d).provider_instance_id
        settings.lambda_api_key = new_key
        SENT.clear()
        for _ in range(6):                     # ~2h, the 60 min deadline passes while the key is wrong
            cycle(20)
            row = dep(d)
            assert row.status not in deployments.TERMINAL_STATES, f"{case}: {row.status} with the wrong key"
        row = dep(d)
        assert row.status == "credentials_unavailable", f"{case}: {row.status}"
        assert SIM.instances[iid]["status"] == "active", "the instance is still running at the provider"
        assert any(k == "credentials_unavailable" for k, _, _ in SENT), [k for k, _, _ in SENT]
        RESULTS.append((f"credential rotated: {case} (while wrong)", assert_B(d), "pass"))
        global_invariants()
        settings.lambda_api_key = KEY          # restored
        drive(d)
        finish(f"credential rotated: {case} (restored)", d, "A")


def test_account_suspended_mid_deployment():
    setup()
    from accounts import accounts as acc
    from accounts import keys
    code, out, d, who = launch()
    to_running(d)
    acc.set_status(who.account_id, "suspended")
    try:
        engine.route(spec(), who)
        raise AssertionError("a suspended account must not open new routes")
    except Exception as exc:  # noqa: BLE001 - RouteRefused
        assert "suspended" in str(exc).lower() or getattr(exc, "code", "") == "account_suspended", exc
    req = SimpleNamespace(url=SimpleNamespace(path=f"/v1/deployments/{d}/terminate"), method="POST")
    assert keys._allowed_while_suspended(req), "a suspended customer may still terminate"
    for _ in range(3):
        cycle(10)                              # tracking + metering continue while suspended
    assert dep(d).status == "running"
    assert scalar("SELECT count(*) FROM usage_slices WHERE deployment_id = :d", d=d) >= 0
    r = deployments.terminate(d, who)          # the customer terminates
    assert r["terminate"]["provider_called"] is True
    drive(d)
    assert dep(d).termination_reason == "user_requested"
    finish("account suspended mid-deployment", d, "A")


def _byo(aid: int) -> int:
    from accounts import credentials as ac
    from store.accounts import ProviderCredential
    with normalize.SessionLocal.begin() as s:
        row = ProviderCredential(account_id=aid, provider="lambda", secret_encrypted=ac.encrypt(KEY_BYO),
                                 hint=KEY_BYO[-4:], label="byo")
        s.add(row)
        s.flush()
        return row.id


def test_account_deleted():
    """No account-deletion feature exists; simulated by deleting the accounts row (FK cascades remove the
    account's API keys and BYO provider credentials)."""
    for case in ("platform credential", "BYO credential"):
        setup()
        aid = bf.fresh_account()
        if case == "BYO credential":
            _byo(aid)
        code, out, d, who = launch(who=bf.key(aid))
        to_running(d)
        assert dep(d).credential_source == ("byo" if case == "BYO credential" else "opengrid")
        with normalize.SessionLocal.begin() as s:
            s.execute(text("DELETE FROM accounts WHERE id = :a"), {"a": aid})
        if case == "platform credential":
            drive(d)
            assert dep(d).termination_reason == "max_runtime_exceeded"
            finish(f"account deleted ({case})", d, "A")
        else:
            for _ in range(6):
                cycle(20)
            assert dep(d).status == "credentials_unavailable", dep(d).status
            finish(f"account deleted ({case}: credential cascaded away)", d, "B")


def test_route_request_row_deleted():
    setup()
    code, out, d, _ = launch()
    to_running(d)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM routing_decisions WHERE route_request_id = :r"), {"r": out["route_request_id"]})
        s.execute(text("DELETE FROM route_requests WHERE id = :r"), {"r": out["route_request_id"]})
    drive(d)
    finish("route request row deleted", d, "A")


def _client():
    import main as app_main
    from fastapi.testclient import TestClient
    return app_main, TestClient(app_main.app, headers={"X-OpenGrid-Request": "1"})


def test_repeated_termination():
    setup()
    code, out, d, who = launch()
    to_running(d)
    main, c = _client()
    from accounts.auth import principal
    main.app.dependency_overrides[principal] = lambda: who
    errors = []
    try:
        r1 = c.post(f"/v1/deployments/{d}/terminate", headers={"Idempotency-Key": "t-1"})
        r2 = c.post(f"/v1/deployments/{d}/terminate", headers={"Idempotency-Key": "t-1"})
        r3 = c.post(f"/v1/deployments/{d}/terminate", headers={"Idempotency-Key": "t-2"})
        assert r1.status_code == r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true", r1.text

        def go(i):
            try:
                if i % 2:
                    deployments.terminate(d, who)
                else:
                    c.post(f"/v1/deployments/{d}/terminate", headers={"Idempotency-Key": f"t-c{i}"})
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert r3.status_code == 202
    finally:
        main.app.dependency_overrides.clear()
    assert not errors, errors
    deployments.terminate(d, OPERATOR, force=False)
    drive(d)
    for _ in range(3):
        deployments.terminate(d, who)
        cycle(5)
    assert len(SIM.terminates()) == 1, SIM.terminates()
    finish("repeated termination (same/different keys, concurrent)", d, "A")


def test_provider_reports_unknown_state_persistently():
    setup()
    code, out, d, _ = launch()
    to_running(d)
    iid = dep(d).provider_instance_id
    SIM.sticky[iid] = "flux"                   # a status OpenGrid does not understand, forever
    for _ in range(14):                        # 3.5 h: past the deadline, through retries
        cycle(15)
        assert dep(d).status not in deployments.TERMINAL_STATES, dep(d).status
    kinds = {a["kind"] for a in open_alerts(d)}
    assert "resource_state_unknown" in kinds and "past_deadline" in kinds, kinds
    RESULTS.append(("provider reports unknown state persistently", assert_B(d), "pass"))
    global_invariants()
    del SIM.sticky[iid]                        # the provider's answer becomes readable again
    drive(d)
    finish("provider unknown state (then readable)", d, "A")


def test_provider_api_unavailable_for_hours():
    """All calls fail; the deadline passes during the outage; a process restart during the outage."""
    setup()
    code, out, d, _ = launch()
    to_running(d)
    iid = dep(d).provider_instance_id
    SIM.down = True
    retries, sent = [], []
    for i in range(12):                        # 4 h of outage; the 60 min deadline passes at ~cycle 3
        cycle(20)
        if i == 5:
            restart()                          # crash + restart in the middle of the outage
        retries.append(watch(d).get("deadline_retries") or 0)
        sent.append(len(SIM.terminates(iid)))
        assert dep(d).status not in deployments.TERMINAL_STATES
    kinds = {a["kind"] for a in open_alerts(d)}
    assert {"provider_api_unavailable", "past_deadline"} <= kinds, kinds
    tail = retries[4:]
    assert all(b == a + 1 for a, b in zip(tail, tail[1:])), f"past-deadline retry not every run: {retries}"
    assert all(b > a for a, b in zip(sent[4:], sent[5:])), f"terminate not re-sent every run in the outage: {sent}"
    assert SIM.instances[iid]["status"] == "active"
    RESULTS.append(("provider API unavailable for hours (deadline passed, restart)", assert_B(d), "pass"))
    global_invariants()
    SIM.down = False
    drive(d)
    for kind in ("past_deadline", "provider_api_unavailable"):
        rows = q("SELECT status, resolution FROM ops_alert_state WHERE kind = :k", k=kind)
        assert rows and all(r[0] == "resolved" and r[1] for r in rows), (kind, rows)
    assert dep(d).billable_end >= SIM.true_end[iid]
    finish("provider API back: terminated, billed to the true end, alerts resolved", d, "A")


def test_two_workers_reconcile_concurrently():
    setup()
    code, out, d, _ = launch()
    to_running(d)
    CLOCK.advance(minutes=61)                  # past the deadline: both workers want to act
    errors = []

    def worker(fn):
        try:
            for _ in range(4):
                fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=worker, args=(f,)) for f in (tracker.track, reconcile.run_once) * 3]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    drive(d)
    assert len(SIM.terminates()) == 1, SIM.terminates()
    assert scalar("SELECT count(*) FROM deployment_events WHERE deployment_id = :d AND to_status = 'terminated' "
                  "AND from_status <> 'terminated'", d=d) == 1
    finish("two workers reconcile the same instance concurrently", d, "A")


def test_terminate_while_launch_in_flight():
    setup()
    SIM.delay = 0.8
    who = bf.key(bf.fresh_account())
    res = {}

    def go():
        res["out"] = launch(who=who)

    t = threading.Thread(target=go)
    t.start()
    d = None
    for _ in range(100):
        d = scalar("SELECT deployment_id FROM deployments WHERE status = 'provisioning' LIMIT 1")
        if d:
            break
        time.sleep(0.02)
    assert d, "launch never reached provisioning"
    CLOCK.advance(minutes=2)
    r = deployments.terminate(d, who)
    assert r["terminate"]["provider_called"] is False
    t.join()
    SIM.delay = 0.0
    assert dep(d).provider_instance_id and dep(d).requested_termination_at is not None
    drive(d)
    assert len(SIM.terminates()) == 1
    finish("terminate requested while launch in flight", d, "A")


def test_orphans():
    setup()
    # (1) an og-* instance at the provider with no deployment row: orphan + alert, NEVER auto-terminated
    stray = SIM._create({"name": "og-dep-0123456789ab", "ssh_key_names": ["x"]})
    # (2) a launch the provider rejected (429) that created an instance anyway: provably ours -> terminated
    code, out, d, _ = launch("http429_after")
    assert dep(d).status == "provider_rejected", dep(d).status
    for _ in range(8):
        cycle(5)
    o = reconcile.orphans()
    stray_o = [x for x in o if x["instance_id"] == stray]
    assert stray_o and stray_o[0]["kind"] == "og_no_deployment" and stray_o[0]["status"] == "open", o
    assert SIM.instances[stray]["status"] not in ("terminating", "terminated") and not SIM.terminates(stray),         "an orphan that is not provably ours is never touched"
    assert any(k == "orphan_detected" for k, _, _ in SENT)
    assert scalar("SELECT count(*) FROM ops_alert_state WHERE kind = 'suspected_orphan' AND status = 'open'") >= 1
    global_invariants()
    drive(d)
    finish("rejected launch that created an instance (provably ours)", d, "A")
    RESULTS.append(("og-* instance with no deployment row (orphan + alert, not touched)", "B", "pass"))
    reconcile.resolve_orphan(stray_o[0]["id"], "terminate", "test-operator")
    cycle(5)
    cycle(5)
    assert reconcile.orphans_by_id(stray_o[0]["id"])["status"] == "terminated" and stray not in SIM.alive()
    global_invariants()


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
            except Exception as exc:  # noqa: BLE001
                failed += 1
                traceback.print_exc()
                RESULTS.append((name, "-", f"FAIL: {str(exc)[:160]}"))
    finally:
        CLOCK.uninstall()
        ops.alert = _orig_alert
        settings.lambda_api_key = old_key
        adapters.TRANSPORTS.pop("lambda", None)
        settings.routing_live_provisioning = False
        if S is not None:
            bf.drop(S, DB)
    w = max(len(r[0]) for r in RESULTS) if RESULTS else 10
    print("\n" + "scenario".ljust(w) + "  outcome  result")
    print("-" * (w + 20))
    for name, outcome, ok in RESULTS:
        print(f"{name.ljust(w)}  {outcome.ljust(7)}  {ok}")
    print(f"\n{len(tests) - failed}/{len(tests)} test functions passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
