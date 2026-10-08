"""Execution core: control plane, quotes, idempotency, the state machine, cost guards, pinned credentials,
SUPERVISED / LIVE / validation flows, terminate safety, ssh key policy, provider-call logging.

Scratch database, synthetic `syn_*` providers and a fake adapter returning the explicit result types
(ProvisionResult / InstanceState / TerminateResult). No real provider is ever called.

Run:  .venv/Scripts/python tests/test_execution_core.py
"""

import logging
import os
import sys
import threading
import time
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
from accounts.auth import OPERATOR, Principal  # noqa: E402
from analytics import rollups  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import (adapters, control, credentials, deployments, engine, guards, idempotency,  # noqa: E402
                     quotes, scoring)
from routing.adapters.base import Adapter, Availability  # noqa: E402
from routing.adapters.results import Capabilities, InstanceState, ProvisionResult, TerminateResult  # noqa: E402
from sqlalchemy import text  # noqa: E402
from store.routing import Deployment, ProvisionAttempt, QuoteRow  # noqa: E402
from tables import ComputeListingRow, ListingObservation  # noqa: E402

DB = "og_test_execcore"
G = "NVIDIA RTX 4090 24GB"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = NOW - timedelta(days=10)
SCOPES = frozenset({"data:read", "route:preview", "route:execute", "deployments:read", "deployments:write"})
PRICES = {"syn_a": 1.00, "syn_b": 1.10, "syn_c": 1.20, "syn_d": 1.30, "syn_pricey": 5.00, "vast": 0.80}
SYN = ("syn_a", "syn_b", "syn_c", "syn_d", "syn_pricey")


def pubkey(n: int = 1, comment: str = "me@laptop") -> str:
    """A structurally valid ssh-ed25519 public key (deterministic per n)."""
    import base64
    import hashlib
    import struct
    raw = hashlib.sha256(f"test-key-{n}".encode()).digest()
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + raw
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + (f" {comment}" if comment else "")


def fake_caps(forces="NO") -> Capabilities:
    c = Capabilities()
    c.forces_account_ssh_key = (forces, "test fake")   # instance attribute: works before and after the field exists
    return c


def key(account_id: int, key_id: int | None = None, extra=()) -> Principal:
    return Principal(kind="api_key", account_id=account_id, key_id=key_id or account_id * 10,
                     scopes=SCOPES | frozenset(extra))


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def seed(Session):
    rows, obs = [], []
    for prov, price in PRICES.items():
        rows.append(ComputeListingRow(
            provider=prov, listing_id=f"{prov}:4090", sku=f"{prov}-4090", raw_gpu_name="RTX 4090",
            canonical_gpu_name=G, gpu_count=1, region="us-east", country="US",
            price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(price)), currency="USD",
            market_type="on_demand", provider_tier=None, interruptible=False, available=True, capacity=None,
            capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=NOW, first_seen_at=START))
        obs.append(ListingObservation(provider=prov, listing_id=f"{prov}:4090", observed_at=START,
                                      price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(price)),
                                      available=True, capacity=None, capacity_unit=None))
    from store.accounts import Account
    with Session.begin() as s:
        s.add_all(rows)
        s.flush()
        s.add_all(obs)
        for aid in range(7, 30):
            s.add(Account(id=aid, name=f"acct{aid}", status="active", plan="free", settings={}, is_operator=False))


class Fake(Adapter):
    """New result types. MODE[provider]: ok | capacity | auth | timeout | server | reset | unavailable."""
    LEVEL = 3
    SUPPORTS_STOP = True
    CREDENTIALS = ()
    CHECK_NEEDS_CREDENTIALS = False
    SSH_KEY_REGISTRATION = True
    CAPABILITIES = fake_caps("NO")
    MODE: dict = {}
    PRICE: dict = {}
    CALLS: list = []
    LAUNCHES: list = []          # (provider, name, ssh_public_key, ssh_key) of every provision call
    INST: dict = {}
    DELAY = 0.0
    LOCK = threading.Lock()

    def _rec(self, *a):
        with Fake.LOCK:
            Fake.CALLS.append((a[0], self.provider, *a[1:]))

    def check_availability(self, offer):
        self._rec("check")
        if Fake.MODE.get(self.provider) == "unavailable":
            return Availability(available=False, live=True, note="sold out")
        return Availability(available=True, live=True, region=offer.region,
                            list_price_per_gpu_hour=Fake.PRICE.get(self.provider, offer.price_per_gpu_hour))

    def provision(self, offer, availability, launch, name):
        self._rec("provision", name, (self.credentials or {}).get("api_key"))
        with Fake.LOCK:
            Fake.LAUNCHES.append((self.provider, name, getattr(launch, "ssh_public_key", None),
                                  getattr(launch, "ssh_key", None)))
        if Fake.DELAY:
            time.sleep(Fake.DELAY)
        m = Fake.MODE.get(self.provider, "ok")
        if m == "capacity":
            return ProvisionResult("rejected", error_kind="capacity", message="no capacity", status_code=409)
        if m == "auth":
            return ProvisionResult("rejected", error_kind="auth", message="bad key", status_code=401)
        if m == "timeout":
            return ProvisionResult("unknown", error_kind="timeout", message="read timeout")
        if m == "server":
            return ProvisionResult("unknown", error_kind="server", message="502", status_code=502)
        if m == "reset":
            return ProvisionResult("unknown", error_kind="network", message="connection reset after send")
        with Fake.LOCK:
            iid = f"i-{len(Fake.INST) + 1}"
            Fake.INST[iid] = {"state": "pending", "name": name}
        return ProvisionResult("accepted", instance_id=iid, status_code=200)

    def status(self, iid):
        self._rec("status", iid, (self.credentials or {}).get("api_key"))
        st = Fake.INST.get(iid)
        if st is None or st["state"] == "gone":
            return InstanceState("not_found", instance_id=iid)
        return InstanceState(st["state"], instance_id=iid, name=st["name"], price_per_hour=1.25)

    def terminate(self, iid):
        self._rec("terminate", iid, (self.credentials or {}).get("api_key"))
        if Fake.MODE.get(self.provider) == "term_fail":
            return TerminateResult("failed", "refused", 400)
        if iid in Fake.INST:
            Fake.INST[iid]["state"] = "terminated"
        return TerminateResult("accepted", "deleting")

    def stop(self, iid):
        self._rec("stop", iid)
        Fake.INST[iid]["state"] = "stopped"
        return TerminateResult("accepted", "stopping")

    def list_instances(self):
        return [InstanceState(v["state"], instance_id=k, name=v["name"]) for k, v in Fake.INST.items()]


class FakeKeyed(Fake):
    CREDENTIALS = ("api_key",)


def reset():
    Fake.MODE.clear()
    Fake.PRICE.clear()
    Fake.CALLS.clear()
    Fake.LAUNCHES.clear()
    Fake.DELAY = 0.0
    settings.routing_max_attempts = 1
    settings.route_live_check_candidates = 3
    with normalize.SessionLocal.begin() as s:   # each test starts from no active deployments / limits / flags
        s.execute(text("UPDATE deployments SET status = 'terminated' WHERE status <> 'terminated' AND "
                       "status NOT IN ('rejected','provision_failed','provider_rejected','quote_failed')"))
        s.execute(text("DELETE FROM account_limits"))
        s.execute(text("DELETE FROM provider_execution_flags"))


def calls(op, provider=None):
    return [c for c in Fake.CALLS if c[0] == op and (provider is None or c[1] == provider)]


def mode(m, env=True):
    settings.routing_live_provisioning = env
    control.set_mode(m, reason="test", by="test")


def enable(*providers, live=False, supervised=True, validated=True):
    for p in providers:
        if validated:
            control.mark_validated(p, "dep-test", {"test": "synthetic"}, "test")
        control.set_provider_flags(p, reason="test", by="test", supervised_enabled=supervised, live_enabled=live)


def spec(*providers, **kw):
    base = {"gpu": G, "count": 1, "region_group": None, "max_price_per_gpu_hour": None, "duration_hours": 10,
            "deadline_hours": None, "mode": "CHEAPEST", "weights": None,
            "preferences": {"include_providers": list(providers)} if providers else {}, "launch": None}
    base.update(kw)
    return base


def validation_ready(*providers):
    """Make every validation precondition true for `providers` (drills, worker heartbeats, a reconciliation
    pass, an ops channel and a delivered test alert)."""
    from store.reconcile import ReconciliationRun
    settings.validation_allowed_providers = list(providers)
    mode("SUPERVISED")
    control.kill_all("drill", "test")
    control.set_mode("SUPERVISED", reason="drill done", by="test")
    for p in providers:
        control.kill_provider(p, "drill", "test")
        control.unkill_provider(p, "drill done", "test")
    control.record_job_health("reconcile", ok=True)
    control.record_job_health("routing_tracker", ok=True)
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        s.add(ReconciliationRun(started_at=now, finished_at=now, trigger="test", provider=None, status="ok",
                                providers={p: {"credentials": 1, "deployments": 0, "listed": 0, "list_errors": 0}
                                           for p in providers}, findings=[], counts={}))
    control.ops_channel_configured = lambda: True
    control.record("ops_test_alert", "alerts:ops", after={"delivered": True, "channel_configured": True},
                   reason="test", actor="test")


def count(table, where="true", **params):
    with normalize.SessionLocal() as s:
        return s.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params).scalar()


def dep(dep_id):
    with normalize.SessionLocal() as s:
        return s.get(Deployment, dep_id)


def refused(fn, code=None, status=None):
    try:
        fn()
    except HTTPException as e:
        if code is not None:
            assert isinstance(e.detail, dict) and e.detail.get("code") == code, (code, e.detail)
        if status is not None:
            assert e.status_code == status, (status, e.status_code, e.detail)
        return e
    raise AssertionError(f"expected refusal {code}")


def confirm_terminated(dep_id):
    deployments.refresh(dep_id)
    assert dep(dep_id).status == "terminated", dep(dep_id).status


# --------------------------------------------------------------------------
# control plane
# --------------------------------------------------------------------------

def test_mode_flag_kill_matrix():
    """Every combination of mode x adapter_status x flags x killed x purpose."""
    n = 0
    for m in control.MODES:
        for status in control.ADAPTER_STATUSES:
            for sup in (False, True):
                for live in (False, True):
                    for killed in (False, True):
                        for purpose in control.PURPOSES:
                            flags = {"adapter_status": status, "supervised_enabled": sup, "live_enabled": live,
                                     "killed": killed}
                            ok, used, why = control.launch_permission("syn_x", purpose=purpose, mode=m, flags=flags)
                            n += 1
                            if m in ("DISABLED", "PREVIEW_ONLY") or killed:
                                assert not ok and used is None, (m, flags, purpose)
                                continue
                            if purpose == "validation":
                                assert ok and used == "SUPERVISED", (m, flags)
                                continue
                            if status != "validated":
                                assert not ok, ("unvalidated adapter launched customer compute", m, flags)
                                continue
                            if m == "LIVE" and live:
                                assert ok and used == "LIVE"
                            elif sup:
                                assert ok and used == "SUPERVISED", (m, flags)
                            else:
                                assert not ok, (m, flags)
    assert n == 4 * 2 * 2 * 2 * 2 * 2
    # env ceiling: whatever the DB says, env false -> at most PREVIEW_ONLY
    mode("LIVE", env=False)
    assert control.effective_mode() == "PREVIEW_ONLY" and control.stored_mode()["mode"] == "LIVE"
    mode("DISABLED", env=False)
    assert control.effective_mode() == "DISABLED"
    mode("LIVE", env=True)
    assert control.effective_mode() == "LIVE"
    # DB flags + kill switches
    enable("syn_a", live=True)
    assert control.launch_permission("syn_a", purpose="customer") == (True, "LIVE", control.launch_permission(
        "syn_a", purpose="customer")[2])
    control.kill_provider("syn_a", "incident", "test")
    assert not control.launch_permission("syn_a", purpose="customer")[0]
    assert not control.launch_permission("syn_a", purpose="validation")[0]
    control.unkill_provider("syn_a", "resolved", "test")
    assert control.launch_permission("syn_a", purpose="customer")[0]
    control.kill_all("global incident", "test")
    assert control.effective_mode() == "DISABLED" and not control.launch_permission("syn_a", purpose="customer")[0]
    try:
        control.set_mode("LIVE", reason="  ", by="t")
        raise AssertionError("a reason is required")
    except control.ControlError:
        pass
    try:
        control.set_provider_flags("syn_a", reason="x", by="t", adapter_status="validated")
        raise AssertionError("validated only through mark_validated")
    except control.ControlError:
        pass
    acts = [r["action"] for r in control.recent_log(50)]
    assert {"set_mode", "kill_provider", "unkill_provider", "kill_all", "mark_validated"} <= set(acts)


def test_preview_only_and_unvalidated_never_launch():
    reset()
    mode("LIVE", env=False)  # env ceiling
    code, out = engine.route(spec("syn_a"), key(7))
    assert out["status"] == "not_provisioned" and out["reason"] == engine.NOT_LIVE and not calls("provision")
    assert out["deployment"] is None
    mode("LIVE")
    enable("syn_a", live=True, validated=False)   # enabled but adapter still simulated
    code, out = engine.route(spec("syn_a"), key(7))
    assert out["status"] == "not_provisioned" and not calls("provision") and out["deployment"] is None
    assert any("not validated" in c["reason"] for c in out["considered"]), out["considered"]
    mode("DISABLED")
    code, out = engine.route(spec("syn_a"), key(7))
    assert out["status"] == "not_provisioned" and not calls("provision")


# --------------------------------------------------------------------------
# SUPERVISED flow over HTTP, idempotency
# --------------------------------------------------------------------------

def _client():
    import main
    from fastapi.testclient import TestClient
    return main, TestClient(main.app, headers={"X-OpenGrid-Request": "1"})


def _as(main, who):
    from accounts.auth import principal
    main.app.dependency_overrides[principal] = lambda: who


BODY = {"gpu": "rtx-4090-24gb", "count": 1, "mode": "cheapest", "duration_hours": 4,
        "preferences": {"include_providers": ["syn_a"]}}


def test_supervised_flow_http_and_double_approve():
    reset()
    mode("SUPERVISED")
    enable("syn_a")
    main, c = _client()
    try:
        _as(main, key(8))
        r = c.post("/v1/route", json=BODY, headers={"Idempotency-Key": "sup-1"})
        assert r.status_code == 202, r.text
        d = r.json()["data"]
        assert d["status"] == "pending_approval" and not calls("provision") and calls("check", "syn_a")
        ap = d["approval"]
        assert ap["provider"] == "syn_a" and ap["gpu"] == G and ap["gpu_count"] == 1 and ap["region"] == "us-east"
        assert ap["quote_price_per_gpu_hour"] == 1.0 and ap["est_hourly_cost"] == 1.0 and ap["est_total_cost"] == 4.0
        assert ap["quote_expires_at"] and ap["body"]["quote_id"].startswith("q_")
        assert d["deployment"]["status"] == "pending_approval" and d["deployment"]["deployment_id"].startswith("dep-")
        rr, qid = d["route_request_id"], ap["body"]["quote_id"]
        # the customer cannot approve
        assert c.post(f"/v1/route/{rr}/approve", json={"quote_id": qid}, headers={"Idempotency-Key": "a"}).status_code == 403
        _as(main, OPERATOR)
        assert c.post(f"/v1/route/{rr}/approve", json={"quote_id": qid}).status_code == 428
        r1 = c.post(f"/v1/route/{rr}/approve", json={"quote_id": qid}, headers={"Idempotency-Key": "ap-1"})
        assert r1.status_code == 200, r1.text
        assert r1.json()["data"]["status"] == "provisioned" and len(calls("provision")) == 1
        # same key replay: same response, no new call
        r2 = c.post(f"/v1/route/{rr}/approve", json={"quote_id": qid}, headers={"Idempotency-Key": "ap-1"})
        assert r2.status_code == 200 and r2.headers.get("Idempotent-Replayed") == "true"
        assert r2.json() == r1.json() and len(calls("provision")) == 1
        # browser refresh: a NEW key, same approval -> already approved, still one launch
        r3 = c.post(f"/v1/route/{rr}/approve", json={"quote_id": qid}, headers={"Idempotency-Key": "ap-2"})
        assert r3.status_code == 200 and r3.json()["data"]["already_approved"] and len(calls("provision")) == 1
        dep_id = r1.json()["data"]["deployment"]["deployment_id"]
        row = dep(dep_id)
        assert row.approved_by == "operator" and row.launch_token and row.client_name == "og-" + dep_id
        assert row.credential_ref == "platform:syn_a" and row.credential_source == "opengrid"
        with normalize.SessionLocal() as s:
            q = s.get(QuoteRow, qid)
            assert q.status == "consumed" and q.consumed_by_deployment_id == dep_id
        # reject is refused once launched; terminate goes through the provider
        _as(main, key(8))
        assert c.post(f"/v1/deployments/{dep_id}/terminate").status_code == 428
        t = c.post(f"/v1/deployments/{dep_id}/terminate", headers={"Idempotency-Key": "t-1"})
        assert t.status_code == 202 and t.json()["data"]["status"] == "terminating", t.text
        t2 = c.post(f"/v1/deployments/{dep_id}/terminate", headers={"Idempotency-Key": "t-2"})
        assert t2.json()["data"]["terminate"]["provider_called"] is False and len(calls("terminate")) == 1
        confirm_terminated(dep_id)
    finally:
        main.app.dependency_overrides.clear()


def test_idempotency_rules_http():
    reset()
    mode("SUPERVISED")
    enable("syn_a")
    main, c = _client()
    try:
        _as(main, key(9))
        before = count("route_requests")
        r1 = c.post("/v1/route", json=BODY, headers={"Idempotency-Key": "k-1"})
        r2 = c.post("/v1/route", json=BODY, headers={"Idempotency-Key": "k-1"})
        assert r1.status_code == r2.status_code == 202 and r1.json() == r2.json()
        assert count("route_requests") == before + 1, "replay never re-runs the route"
        r3 = c.post("/v1/route", json={**BODY, "count": 2}, headers={"Idempotency-Key": "k-1"})
        assert r3.status_code == 422 and r3.json()["detail"]["code"] == "idempotency_key_reused"
        # in progress: a claimed, unfinished key -> 409 Retry-After
        from routing.adapters.base import Offer  # noqa: F401
        body = {**BODY, "duration_hours": 5}
        from api.routing import RouteBody
        h = idempotency.request_hash(RouteBody(**body).model_dump(mode="json"))
        assert idempotency.claim("acct:9", "route", "k-busy", h)[0] == "new"
        r4 = c.post("/v1/route", json=body, headers={"Idempotency-Key": "k-busy"})
        assert r4.status_code == 409 and r4.headers.get("retry-after") and \
            r4.json()["detail"]["code"] == "idempotency_in_progress"
        # keys are per principal: another account may use the same key string
        _as(main, key(10))
        assert c.post("/v1/route", json=BODY, headers={"Idempotency-Key": "k-1"}).status_code == 202
        # a stale in_progress key may be reclaimed
        with normalize.SessionLocal.begin() as s:
            s.execute(text("UPDATE idempotency_keys SET updated_at = now() - interval '1 hour' WHERE key = 'k-busy'"))
        _as(main, key(9))
        assert c.post("/v1/route", json=body, headers={"Idempotency-Key": "k-busy"}).status_code == 202
    finally:
        main.app.dependency_overrides.clear()


def test_concurrent_identical_requests_one_deployment():
    reset()
    mode("LIVE")
    enable("syn_a", live=True)
    Fake.DELAY = 0.4
    who = key(11)
    s = spec("syn_a")
    results, errors = [], []

    def worker():
        try:
            results.append(idempotency.run(who=who, scope="route", key="same-key", body={"spec": "x"},
                                           fn=lambda: engine.route(s, who)))
        except HTTPException as e:
            errors.append(e.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert count("deployments", "account_id = 11") == 1, count("deployments", "account_id = 11")
    assert len(calls("provision")) == 1
    assert len(results) + len(errors) == 8 and set(errors) <= {409}, errors
    assert sum(1 for r in results if not r[2]) == 1, "exactly one request executed; the rest replayed or got 409"


def test_concurrent_double_approve_one_launch():
    reset()
    mode("SUPERVISED")
    enable("syn_a")
    code, out = engine.route(spec("syn_a"), key(12))
    rr, qid = out["route_request_id"], out["quote"]["quote_id"]
    Fake.DELAY = 0.3
    outs = []

    def worker():
        try:
            outs.append(engine.approve(rr, OPERATOR, quote_id=qid))
        except HTTPException as e:
            outs.append((e.status_code, e.detail))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls("provision")) == 1, calls("provision")
    with normalize.SessionLocal() as s:
        n = s.execute(text("SELECT count(*) FROM provision_attempts WHERE route_request_id = :r"), {"r": rr}).scalar()
    assert n == 1


# --------------------------------------------------------------------------
# outcomes: ambiguous never fails over; rejection fails over as a NEW deployment
# --------------------------------------------------------------------------

def test_ambiguous_outcomes_never_fail_over():
    for behaviour, expect in (("timeout", "provider_timeout"), ("server", "launch_unknown"), ("reset", "launch_unknown")):
        reset()
        mode("LIVE")
        enable("syn_a", "syn_b", live=True)
        settings.routing_max_attempts = 3
        Fake.MODE["syn_a"] = behaviour
        code, out = engine.route(spec("syn_a", "syn_b"), key(13))
        assert out["status"] == expect, (behaviour, out["status"], out["considered"])
        assert not calls("provision", "syn_b"), f"{behaviour}: failed over after an ambiguous outcome"
        assert code == 202 and "NOT failing over" in out["reason"]
        d = out["deployment"]
        assert d["status"] == expect and d["uncertain"]
        assert [a["outcome"] for a in d["provision_attempts"]] == ["unknown"]
        assert len(out["deployments"]) == 1
        # an uncertain deployment is live (tracked) and counts against guards
        assert d["deployment_id"] in deployments.live_ids()


def test_definitive_rejection_fails_over_as_new_deployment():
    reset()
    mode("LIVE")
    enable("syn_a", "syn_b", live=True)
    Fake.MODE["syn_a"] = "capacity"
    code, out = engine.route(spec("syn_a", "syn_b"), key(14))
    assert out["status"] == "provision_failed" and not calls("provision", "syn_b"), "max_attempts 1: no failover"
    reset()
    mode("LIVE")
    enable("syn_a", "syn_b", live=True)
    settings.routing_max_attempts = 2
    Fake.MODE["syn_a"] = "auth"
    code, out = engine.route(spec("syn_a", "syn_b"), key(14))
    assert out["status"] == "provisioned" and len(out["deployments"]) == 2, out["considered"]
    first, second = (dep(x) for x in out["deployments"])
    assert first.status == "provider_rejected" and first.provider == "syn_a" and second.provider == "syn_b"
    with normalize.SessionLocal() as s:
        a1 = s.scalars(text("SELECT finished_at FROM provision_attempts WHERE deployment_id = :d").bindparams(
            d=first.deployment_id)).first()
    assert second.created_at >= a1, "the failover deployment exists only after the rejection was recorded"
    assert [c[1] for c in calls("provision")] == ["syn_a", "syn_b"]


def test_crash_between_provider_accept_and_db_write():
    reset()
    mode("LIVE")
    enable("syn_a", live=True)
    real = deployments._record_launch

    def boom(*a, **kw):
        raise RuntimeError("db connection lost")

    deployments._record_launch = boom
    try:
        code, out = engine.route(spec("syn_a"), key(15))
    finally:
        deployments._record_launch = real
    dep_id = out["deployments"][0]
    row = dep(dep_id)
    assert row.status == "provisioning" and row.provider_instance_id is None and row.launch_token
    with normalize.SessionLocal() as s:
        att = s.scalars(text("SELECT outcome, client_name, launch_token FROM provision_attempts WHERE deployment_id = :d")
                        .bindparams(d=dep_id)).all()
        att = s.execute(text("SELECT outcome, client_name, launch_token FROM provision_attempts WHERE deployment_id = :d"),
                        {"d": dep_id}).all()
    assert len(att) == 1 and att[0][0] == "provisioning" and att[0][1] == "og-" + dep_id and att[0][2] == row.launch_token
    # the instance really exists at the provider under the name reconciliation searches for
    assert any(v["name"] == "og-" + dep_id for v in Fake.INST.values())
    # no second provision call for this deployment, ever
    r = deployments.launch(dep_id, adapter=Fake(None, provider="syn_a"), offer=None, availability=None,
                           launch_spec=None, resolved=None)
    assert r["launched"] is False and len(calls("provision")) == 1
    assert dep_id in deployments.live_ids()


# --------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------

def test_quote_expiry_and_price_move():
    reset()
    mode("SUPERVISED")
    enable("syn_a")
    code, out = engine.route(spec("syn_a"), key(16))
    rr, q1 = out["route_request_id"], out["quote"]["quote_id"]
    assert quotes.get(q1)["status"] == "active" and quotes.get(q1)["taxes"]["amount_usd"] is None
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE quotes SET expires_at = now() - interval '1 second' WHERE id = :q"), {"q": q1})
    assert quotes.get(q1)["status"] == "expired"
    e = refused(lambda: engine.approve(rr, OPERATOR, quote_id=q1), "quote_expired", 409)
    q2 = e.detail["new_quote"]["quote_id"]
    assert q2 != q1 and not calls("provision"), "never launch on an expired quote"
    d = deployments.for_request(rr)
    assert d.quote_id == q2 and d.status == "pending_approval"
    assert quotes.get(q1)["status"] in ("superseded", "expired")
    refused(lambda: engine.approve(rr, OPERATOR, quote_id=q1), "quote_mismatch", 409)
    # price moved 10% (> 2% tolerance): refuse, new quote at the new price
    Fake.PRICE["syn_a"] = 1.10
    e = refused(lambda: engine.approve(rr, OPERATOR, quote_id=q2), "quote_invalid", 409)
    q3 = e.detail["new_quote"]
    assert q3["quote_price_per_gpu_hour"] == 1.10 and not calls("provision")
    assert quotes.get(q2)["status"] == "superseded"
    # within tolerance (1%): launches at the quoted price
    Fake.PRICE["syn_a"] = 1.11
    code, res = engine.approve(rr, OPERATOR, quote_id=q3["quote_id"])
    assert res["status"] == "provisioned" and len(calls("provision")) == 1
    # listing gone on re-check -> refused, no new quote
    code, out = engine.route(spec("syn_a"), key(17))
    Fake.MODE["syn_a"] = "unavailable"
    e = refused(lambda: engine.approve(out["route_request_id"], OPERATOR, quote_id=out["quote"]["quote_id"]),
                "quote_invalid", 409)
    assert e.detail["new_quote"] is None and len(calls("provision")) == 1
    # preview issues a persisted observed-price quote with an expiry; route with quote_id re-validates it
    Fake.MODE.clear()
    Fake.PRICE.clear()
    mode("LIVE")
    enable("syn_a", live=True)
    pv = engine.preview(spec("syn_a"), key(18))
    qp = pv["quote_record"]
    assert qp["price_source"] == "observed" and qp["provider"] == "syn_a" and qp["expires_at"]
    code, out = engine.route(spec("syn_a", quote_id=qp["quote_id"]), key(18))
    assert out["status"] == "provisioned" and quotes.get(qp["quote_id"])["status"] == "consumed"
    refused(lambda: engine.route(spec("syn_a", quote_id=qp["quote_id"]), key(18)), "quote_invalid", 409)
    refused(lambda: engine.route(spec("syn_a", quote_id=qp["quote_id"]), key(19)), "quote_not_found", 404)


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------

def test_requote_never_exceeds_customer_max_price():
    """A price move issues a re-quote; approving it must not launch above the request's own ceiling."""
    reset()
    mode("SUPERVISED")
    enable("syn_a")
    code, out = engine.route(spec("syn_a", max_price_per_gpu_hour=1.05), key(26))
    rr, q1 = out["route_request_id"], out["quote"]["quote_id"]
    Fake.PRICE["syn_a"] = 1.10   # moved beyond tolerance and above the customer's $1.05 cap
    e = refused(lambda: engine.approve(rr, OPERATOR, quote_id=q1), "quote_invalid", 409)
    q2 = e.detail["new_quote"]["quote_id"]
    refused(lambda: engine.approve(rr, OPERATOR, quote_id=q2), "over_max_price", 422)
    assert not calls("provision"), "nothing launched above the customer's max price"


def test_guards_each_limit_blocks_and_admin_override():
    # monthly_spend_limit 0.5 (was 1): a launch's projection is now its maximum exposure, quote x GPUs x its
    # runtime ceiling ($1/h x 60 min default = $1.00), no longer the (impossible) 10 h duration estimate.
    cases = [("max_price_per_gpu_hour", Decimal("0.5")), ("max_hourly_cost", Decimal("0.5")),
             ("max_total_cost", Decimal("5")), ("max_gpus", 0), ("max_active_deployments", 0),
             ("provider_allowlist", ["syn_b"]), ("region_allowlist", ["Europe"]), ("monthly_spend_limit", Decimal("0.5"))]
    for field, value in cases:
        reset()
        mode("LIVE")
        enable("syn_a", live=True)
        guards.set_limits(20, {field: value}, by="test", reason="test")
        code, out = engine.route(spec("syn_a"), key(20))
        assert out["status"] == "pending_approval", (field, out["status"])
        codes = [v["code"] for v in out["deployment"]["limit_violations"]]
        assert field in codes, (field, codes)
        assert not calls("provision"), f"{field}: launched over a limit"
        rr, qid = out["route_request_id"], out["quote"]["quote_id"]
        refused(lambda: engine.approve(rr, OPERATOR, quote_id=qid), "limits_exceeded", 409)
        refused(lambda: engine.approve(rr, OPERATOR, quote_id=qid, override_limits=True), "reason_required", 422)
        assert not calls("provision")
        if field == "max_price_per_gpu_hour":
            code, res = engine.approve(rr, OPERATOR, quote_id=qid, override_limits=True, reason="partner pilot, approved")
            assert res["status"] == "provisioned" and len(calls("provision")) == 1
            row = dep(res["deployment"]["deployment_id"])
            assert row.override_limits and row.override_reason == "partner pilot, approved"
            assert any(r["action"] == "approve_launch" for r in control.recent_log(20))
        else:
            engine.reject(rr, OPERATOR, reason="test cleanup")
    reset()
    lim = guards.limits_for(21)
    assert lim["max_hourly_cost"] == 50 and lim["max_gpus"] == 8 and lim["max_active_deployments"] == 2
    assert lim["monthly_spend_limit"] == 2000


# --------------------------------------------------------------------------
# kill switch, terminate safety, suspension
# --------------------------------------------------------------------------

def test_kill_switch_blocks_launch_but_terminate_works():
    reset()
    mode("LIVE")
    enable("syn_a", live=True)
    code, out = engine.route(spec("syn_a"), key(22))
    dep_id = out["deployment"]["deployment_id"]
    iid = out["deployment"]["provider_instance_id"]
    Fake.INST[iid]["state"] = "running"
    deployments.refresh(dep_id)
    assert dep(dep_id).status == "running"
    control.kill_all("incident", "test")
    code, out2 = engine.route(spec("syn_a"), key(22))
    assert out2["status"] == "not_provisioned" and len(calls("provision")) == 1
    r = deployments.terminate(dep_id, key(22))
    assert r["status"] == "terminating" and len(calls("terminate")) == 1
    confirm_terminated(dep_id)
    # provider kill: pending approvals for that provider cannot launch
    mode("SUPERVISED")
    enable("syn_a")
    code, out = engine.route(spec("syn_a"), key(22))
    control.kill_provider("syn_a", "provider incident", "test")
    refused(lambda: engine.approve(out["route_request_id"], OPERATOR, quote_id=out["quote"]["quote_id"]),
            "launch_not_permitted", 409)
    control.unkill_provider("syn_a", "ok", "test")


def test_terminate_never_marks_terminated_without_confirmation():
    reset()
    mode("LIVE")
    enable("syn_a", live=True)
    code, out = engine.route(spec("syn_a"), key(23))
    dep_id, iid = out["deployment"]["deployment_id"], out["deployment"]["provider_instance_id"]
    Fake.INST[iid]["state"] = "running"
    deployments.refresh(dep_id)
    Fake.MODE["syn_a"] = "term_fail"
    deployments.terminate(dep_id, key(23))
    assert dep(dep_id).status == "termination_failed"
    Fake.MODE.clear()
    deployments.terminate(dep_id, key(23))       # re-issue from termination_failed
    assert dep(dep_id).status == "terminating"
    # one not_found read is not proof; a second >= 60 s later is
    Fake.INST[iid]["state"] = "gone"
    deployments.refresh(dep_id)
    assert dep(dep_id).status == "terminating"
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, dep_id)
        md = dict(row.provider_metadata)
        md["not_found_reads"] = [(datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()]
        row.provider_metadata = md
        row.last_checked_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    deployments.refresh(dep_id)
    row = dep(dep_id)
    assert row.status == "terminated" and row.terminated_at is not None
    with normalize.SessionLocal() as s:
        ev = s.execute(text("SELECT evidence FROM deployment_events WHERE deployment_id = :d AND to_status = 'terminated'"),
                       {"d": dep_id}).scalar()
    assert "consecutive not_found" in ev["basis"]
    # terminated is absorbing: a late 'running' read cannot resurrect it
    Fake.INST[iid]["state"] = "running"
    r = deployments.observe(dep_id, InstanceState("running", instance_id=iid))
    assert r["applied"] is False and dep(dep_id).status == "terminated"
    # terminate before launch cancels (no provider call)
    mode("SUPERVISED")
    enable("syn_a")
    code, out = engine.route(spec("syn_a"), key(23))
    n = len(calls("terminate"))
    r = deployments.terminate(out["deployment"]["deployment_id"], key(23))
    assert r["status"] == "rejected" and len(calls("terminate")) == n


def test_suspended_account_can_terminate_not_route():
    reset()
    mode("LIVE")
    enable("syn_a", live=True)
    code, out = engine.route(spec("syn_a"), key(24))
    dep_id = out["deployment"]["deployment_id"]
    from accounts.accounts import set_status
    set_status(24, "suspended")
    try:
        refused(lambda: engine.route(spec("syn_a"), key(24)), "account_suspended", 403)
        r = deployments.terminate(dep_id, key(24))
        assert r["status"] == "terminating" and calls("terminate")
        confirm_terminated(dep_id)
    finally:
        set_status(24, "active")


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def test_pinned_credentials():
    reset()
    original = adapters.get("vast")
    adapters.register("vast", FakeKeyed)
    old_key = settings.vast_api_key
    settings.vast_api_key = "PLATFORM-KEY-123456"
    from accounts import credentials as ac
    try:
        mode("LIVE")
        enable("vast", live=True)
        code, out = engine.route(spec("vast"), key(25))
        d1 = out["deployment"]["deployment_id"]
        assert dep(d1).credential_ref == "platform:vast" and calls("provision")[-1][3] == "PLATFORM-KEY-123456"
        # the customer adds a BYO key AFTER launch: management keeps using the launch credential
        byo = ac.add(25, "vast", "BYO-KEY-AAAAAAAA")
        deployments.refresh(d1)
        assert calls("status")[-1][3] == "PLATFORM-KEY-123456", calls("status")
        # a new launch now uses (and pins) the BYO key
        code, out = engine.route(spec("vast"), key(25))
        d2 = out["deployment"]["deployment_id"]
        assert dep(d2).credential_ref == f"byo:{byo['id']}" and calls("provision")[-1][3] == "BYO-KEY-AAAAAAAA"
        # replacing the BYO key revokes the pinned one: d2 -> credentials_unavailable, never terminated,
        # and no call is ever made with another key
        ac.add(25, "vast", "BYO-KEY-BBBBBBBB")
        n = len(Fake.CALLS)
        r = deployments.refresh(d2)
        assert dep(d2).status == "credentials_unavailable" and len(Fake.CALLS) == n, r
        refused(lambda: deployments.terminate(d2, key(25)), "credentials_unavailable", 409)
        assert dep(d2).status == "credentials_unavailable" and len(Fake.CALLS) == n
        assert dep(d2).terminate_requested_at is not None
        # d1 is unaffected
        deployments.refresh(d1)
        assert calls("status")[-1][3] == "PLATFORM-KEY-123456"
        # a BYO credential that does not decrypt FAILS CLOSED (no fallback to the platform key)
        real = ac.decrypt

        def bad(blob):
            raise ValueError("InvalidToken")

        ac.decrypt = bad
        try:
            code, out = engine.route(spec("vast"), key(25))
        finally:
            ac.decrypt = real
        assert out["status"] == "not_provisioned" and any("credential_unusable" in c["reason"] for c in out["considered"])
        assert calls("provision")[-1][3] == "BYO-KEY-AAAAAAAA", "no launch with the platform key"
    finally:
        settings.vast_api_key = old_key
        adapters.register("vast", original)


def test_credential_provider_for_aggregated_clouds():
    """A native Crusoe key stored under 'crusoe' is never used for (sent to) Shadeform."""
    from accounts import credentials as ac
    old = settings.shadeform_api_key
    settings.shadeform_api_key = "SHADEFORM-PLATFORM"
    try:
        assert credentials.credential_provider("crusoe") == "shadeform"
        ac.add(26, "crusoe", "NATIVE-CRUSOE-KEY-XX")
        r = credentials.resolve_for_launch(26, "crusoe")
        assert r.ref == "platform:shadeform" and r.credentials["api_key"] == "SHADEFORM-PLATFORM"
        row = ac.add(26, "shadeform", "BYO-SHADEFORM-KEY-X")
        r = credentials.resolve_for_launch(26, "denvr")
        assert r.ref == f"byo:{row['id']}" and r.credentials["api_key"] == "BYO-SHADEFORM-KEY-X"
    finally:
        settings.shadeform_api_key = old


# --------------------------------------------------------------------------
# state machine
# --------------------------------------------------------------------------

def test_transitions():
    for s_ in deployments.STATUSES:
        for t in deployments.ALLOWED_TRANSITIONS[s_]:
            assert t in deployments.STATUSES
    assert deployments.ALLOWED_TRANSITIONS["terminated"] == () and deployments.ALLOWED_TRANSITIONS["rejected"] == ()
    for s_ in deployments.LIVE_STATES:
        assert s_ not in deployments.TERMINAL_STATES
    for s_ in deployments.UNCERTAIN_STATES:
        assert "provision_failed" not in deployments.ALLOWED_TRANSITIONS[s_] or s_ in ("provider_timeout", "launch_unknown")
    reset()
    mode("SUPERVISED")
    enable("syn_a")
    code, out = engine.route(spec("syn_a"), key(27))
    dep_id = out["deployment"]["deployment_id"]
    for bad in ("running", "terminated", "provisioning"):
        try:
            deployments.transition(dep_id, bad, "test")
            raise AssertionError(f"pending_approval -> {bad} must be illegal")
        except deployments.IllegalTransition:
            pass
    deployments.transition(dep_id, "rejected", "test", {"why": "test"}, actor="admin", actor_id="tester")
    for bad in ("approved", "pending_approval", "running"):
        try:
            deployments.transition(dep_id, bad, "test")
            raise AssertionError(f"rejected -> {bad} must be illegal")
        except deployments.IllegalTransition:
            pass
    ev = deployments.public(dep_id)["events"][-1]
    assert ev["to"] == "rejected" and ev["actor"] == "admin" and ev["actor_id"] == "tester" and ev["evidence"]
    try:
        deployments.transition(dep_id, "rejected", "x", actor="nobody")
    except ValueError:
        pass
    # running only after the provider reports it: accepted -> provisioning, never running
    mode("LIVE")
    enable("syn_a", live=True)
    code, out = engine.route(spec("syn_a"), key(27))
    d = out["deployment"]
    assert d["status"] == "provisioning" and d["provisioned_at"] and d["uptime_seconds"] == 0
    Fake.INST[d["provider_instance_id"]]["state"] = "running"
    deployments.refresh(d["deployment_id"])
    assert dep(d["deployment_id"]).status == "running"
    deployments.terminate(d["deployment_id"], key(27))
    confirm_terminated(d["deployment_id"])
    try:
        deployments.transition(d["deployment_id"], "running", "late read")
        raise AssertionError("terminated -> running must be illegal")
    except deployments.IllegalTransition:
        pass


# --------------------------------------------------------------------------
# ssh key policy, validation launches, latency, logging, admin API
# --------------------------------------------------------------------------

def test_ssh_key_policy():
    old = settings.routing_launch_defaults
    settings.routing_launch_defaults = {"syn_a": {"ssh_key": "operator-key", "image": "img"}}
    try:
        cls = adapters.get("syn_a")
        spec_, why = engine.launch_spec_for("syn_a", {"ssh_key": "someone-elses"}, purpose="customer",
                                            credential_source="opengrid", adapter_cls=cls)
        assert spec_ is None and why.startswith("ssh_key_reference_forbidden")
        spec_, why = engine.launch_spec_for("syn_a", {"ssh_key": "mine"}, purpose="customer", credential_source="byo",
                                            adapter_cls=cls)
        assert why is None and spec_.ssh_key == "mine"
        spec_, why = engine.launch_spec_for("syn_a", None, purpose="customer", credential_source="opengrid",
                                            adapter_cls=cls)
        assert why is None and spec_.ssh_key is None and spec_.image == "img", "operator key never on customer machines"
        pk = pubkey(1)
        spec_, why = engine.launch_spec_for("syn_a", {"ssh_public_key": pk}, purpose="customer",
                                            credential_source="opengrid", adapter_cls=cls)
        assert why is None and spec_.ssh_public_key == pk

        class NoReg(Fake):
            SSH_KEY_REGISTRATION = False

        spec_, why = engine.launch_spec_for("syn_a", {"ssh_public_key": pk}, purpose="customer",
                                            credential_source="opengrid", adapter_cls=NoReg)
        assert spec_ is None and why.startswith("ssh_key_registration_unsupported")
        spec_, why = engine.launch_spec_for("syn_a", None, purpose="validation", credential_source="opengrid",
                                            adapter_cls=cls)
        assert spec_.ssh_key == "operator-key", "the operator default key is for validation launches"
        # through a route: a key reference on OpenGrid's account skips the candidate (no launch)
        reset()
        mode("LIVE")
        enable("syn_a", live=True)
        code, out = engine.route(spec("syn_a", launch={"ssh_key": "victim"}), key(28))
        assert out["status"] == "not_provisioned" and not calls("provision")
        assert "ssh_key_reference_forbidden" in out["considered"][0]["reason"]
        # the public key reaches the adapter
        code, out = engine.route(spec("syn_a", launch={"ssh_public_key": pk}), key(28))
        assert out["status"] == "provisioned"
        assert dep(out["deployment"]["deployment_id"]).launch["ssh_public_key"] == pk
    finally:
        settings.routing_launch_defaults = old


def test_validation_launch():
    reset()
    mode("SUPERVISED")
    # syn_c is simulated: customers cannot launch on it, validation can (admin approval, caps, the gate)
    code, out = engine.route(spec("syn_c"), key(29))
    assert out["status"] == "not_provisioned" and not calls("provision")
    # the validation gate refuses (explicitly, every failed condition with its reason) until all hold
    settings.validation_allowed_providers = ["lambda"]
    e = refused(lambda: engine.create_validation_route("syn_c", None, by="operator"),
                "validation_preconditions_failed", 409)
    codes = {f["code"] for f in e.detail["failed"]}
    assert "provider_allowed" in codes and all(f["reason"] for f in e.detail["failed"]), e.detail
    validation_ready("syn_c", "syn_pricey")
    rr = engine.create_validation_route("syn_c", None, by="operator")
    d = deployments.for_request(rr)
    assert d.purpose == "validation" and d.status == "pending_approval" and d.max_runtime_minutes == 30
    assert d.effective_max_runtime_minutes == 30 and d.operator_access == "validation_operator_key"
    assert d.account_id is None and not calls("provision")
    code, res = engine.approve(rr, OPERATOR, quote_id=d.quote_id)
    assert res["status"] == "provisioned" and len(calls("provision")) == 1
    row = dep(d.deployment_id)
    assert row.terminate_deadline_at is not None and 29 <= (row.terminate_deadline_at - row.approved_at).total_seconds() / 60 <= 31
    # one validation instance at a time, and the over-cap listing: refused AT START (not overridable)
    e = refused(lambda: engine.create_validation_route("syn_pricey", None, by="operator"),
                "validation_preconditions_failed", 409)
    codes = {f["code"] for f in e.detail["failed"]}
    assert {"price_cap", "one_active_validation"} <= codes, codes
    assert len(calls("provision")) == 1
    refused(lambda: engine.create_validation_route("syn_c", None, by="operator", max_runtime_minutes=90),
            "validation_max_runtime_minutes", 422)
    control.mark_validated("syn_c", d.deployment_id, {"launched": True, "running": True, "terminated": True}, "test")
    assert control.provider_flags("syn_c")["adapter_status"] == "validated"
    assert not control.launch_permission("syn_c", purpose="customer")[0], "validated alone does not enable launches"
    mode("PREVIEW_ONLY")
    refused(lambda: engine.create_validation_route("syn_c", None, by="operator"), "launch_not_permitted", 409)


def test_route_latency_limit():
    reset()
    mode("LIVE")
    enable("syn_a", "syn_b", "syn_c", "syn_d", live=True)
    for p in ("syn_a", "syn_b", "syn_c", "syn_d"):
        Fake.MODE[p] = "unavailable"
    settings.route_live_check_candidates = 2
    code, out = engine.route(spec("syn_a", "syn_b", "syn_c", "syn_d"), key(7))
    assert len(calls("check")) == 2, calls("check")
    assert any(c["outcome"] == "not_checked" for c in out["considered"])


def test_provider_call_logging_redacts():
    reset()
    mode("LIVE")
    enable("vast", live=True)
    original = adapters.get("vast")
    adapters.register("vast", FakeKeyed)
    old_key = settings.vast_api_key
    settings.vast_api_key = "SECRET-PLATFORM-KEY-999"
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = H()
    lg = logging.getLogger("opengrid.provider")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        code, out = engine.route(spec("vast"), key(7))
    finally:
        lg.removeHandler(h)
        settings.vast_api_key = old_key
        adapters.register("vast", original)
    prov = [r for r in records if getattr(r, "op", None) == "provision"]
    assert prov and prov[0].provider == "vast" and prov[0].deployment_id == out["deployment"]["deployment_id"]
    assert prov[0].route_request_id == out["route_request_id"] and isinstance(prov[0].latency_ms, int)
    assert prov[0].status == "accepted"
    assert {"check_availability"} <= {getattr(r, "op", None) for r in records}
    for r in records:
        assert "SECRET-PLATFORM-KEY-999" not in r.getMessage() and "SECRET-PLATFORM-KEY-999" not in str(r.__dict__)


def test_admin_api():
    reset()
    main, c = _client()
    try:
        _as(main, key(7))
        assert c.get("/v1/admin/execution/mode").status_code == 403
        assert c.get("/v1/admin/deployments").status_code == 403
        _as(main, OPERATOR)
        assert c.post("/v1/admin/execution/mode", json={"mode": "LIVE"}).status_code == 422, "reason required"
        r = c.post("/v1/admin/execution/mode", json={"mode": "supervised", "reason": "first partner pilot"})
        assert r.status_code == 200 and r.json()["data"]["effective_mode"] == "SUPERVISED"
        settings.routing_live_provisioning = False
        assert c.get("/v1/admin/execution/mode").json()["data"]["effective_mode"] == "PREVIEW_ONLY"
        settings.routing_live_provisioning = True
        r = c.post("/v1/admin/execution/providers/syn_a", json={"adapter_status": "validated", "reason": "x" * 5})
        assert r.status_code == 422
        r = c.post("/v1/admin/execution/providers/syn_a", json={"supervised_enabled": True, "reason": "pilot"})
        assert r.status_code == 200 and r.json()["data"]["supervised_enabled"] is True
        g = c.get("/v1/admin/execution/providers/syn_a").json()["data"]
        assert g["launch_permission"]["customer"]["allowed"] is False, "simulated adapter"
        assert g["launch_permission"]["validation"]["allowed"] is True
        assert c.post("/v1/admin/execution/providers/syn_a/kill", json={"reason": "incident"}).json()["data"]["killed"]
        assert not c.post("/v1/admin/execution/providers/syn_a/unkill", json={"reason": "ok now"}).json()["data"]["killed"]
        r = c.post("/v1/admin/execution/limits/7", json={"max_gpus": 4, "reason": "pilot cap"})
        assert r.status_code == 200 and r.json()["data"]["max_gpus"] == 4
        assert c.post("/v1/admin/execution/limits/7", json={"max_gpus": 4}).status_code == 422
        log = c.get("/v1/admin/execution/log").json()["data"]
        assert {"set_mode", "set_provider_flags", "kill_provider", "unkill_provider", "set_account_limits"} <= {
            x["action"] for x in log}
        assert all(x["reason"] for x in log)
        # validation route (Idempotency-Key required) + admin views + force terminate
        validation_ready("syn_a")
        vb = {"provider": "syn_a", "reason": "validate syn_a"}
        assert c.post("/v1/admin/execution/validation", json=vb).status_code == 428
        r = c.post("/v1/admin/execution/validation", json=vb, headers={"Idempotency-Key": "val-1"})
        assert r.status_code == 202, r.text
        r2 = c.post("/v1/admin/execution/validation", json=vb, headers={"Idempotency-Key": "val-1"})
        assert r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true"
        assert r2.json() == r.json() and count("deployments", "purpose = 'validation' AND status = "
                                                             "'pending_approval'") == 1
        rr = r.json()["data"]["route_request_id"]
        qid = r.json()["data"]["quote"]["quote_id"]
        r = c.post(f"/v1/route/{rr}/approve", json={"quote_id": qid}, headers={"Idempotency-Key": "v-1"})
        assert r.status_code == 200, r.text
        dep_id = r.json()["data"]["deployment"]["deployment_id"]
        live = c.get("/v1/admin/deployments?state=live").json()["data"]
        assert any(x["deployment_id"] == dep_id for x in live)
        r = c.post(f"/v1/admin/deployments/{dep_id}/terminate", json={"reason": "validation done"})
        assert r.status_code == 202 and r.json()["data"]["status"] == "terminating"
        r = c.post(f"/v1/admin/deployments/{dep_id}/terminate", json={"reason": "validation done"},
                   headers={"Idempotency-Key": "ft-1"})
        assert r.status_code == 202
        confirm_terminated(dep_id)
        r = c.post("/v1/admin/execution/kill", json={"reason": "drill"})
        assert r.status_code == 200 and r.json()["data"]["effective_mode"] == "DISABLED"
        # bad ssh public key is a 422 at the API
        _as(main, key(7))
        bad = {**BODY, "launch": {"ssh_public_key": "not a key"}}
        assert c.post("/v1/route", json=bad, headers={"Idempotency-Key": "bad-pk"}).status_code == 422
    finally:
        main.app.dependency_overrides.clear()


# --------------------------------------------------------------------------

def main():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    seed(Session)
    rollups.refresh()
    scoring.history.cache_clear()
    try:
        from accounts import accounts as acc
        acc.reset_cache()
    except Exception:  # noqa: BLE001
        pass
    for p in SYN:
        adapters.register(p, Fake)
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
        for p in SYN:
            adapters.unregister(p)
        settings.routing_live_provisioning = False
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)


if __name__ == "__main__":
    main()
