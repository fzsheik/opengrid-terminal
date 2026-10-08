"""Lifecycle hardening: per-deployment SSH keys, the billable window, deadlines through provider outages, and
the cost-exposure alert catalogue.

SSH keys (Lambda adapter over httpx.MockTransport): the provider_resources row exists BEFORE POST /ssh-keys;
the launch names only that key; the key is deleted only after CONFIRMED termination; delete failures retry with
backoff -> delete_failed + ops alert; abandoned og-* keys are detected; keys OpenGrid did not create are never
deleted; a record that cannot be written refuses the registration.
Billing: terminate requested mid-launch -> the provider's real runtime (first_healthy) is metered although
OpenGrid never recorded 'running'. Deadlines: a deadline during a provider outage is retried every run, alerted,
and terminated when the API returns. Alerts: every cost-exposure kind fires once with the required fields,
is deduplicated, re-escalates, and resolves only on evidence.

Scratch DB og_test_lifecycle_*; no network.
Run:  .venv/Scripts/python tests/test_lifecycle.py
"""

import json
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

import httpx  # noqa: E402

import fixtures  # noqa: E402
import scratchdb  # noqa: E402
from bench_fixtures import PK  # noqa: E402

import normalize  # noqa: E402
from alerts import ops  # noqa: E402
from config import settings  # noqa: E402
from routing import adapters, credentials, deployments, reconcile, tracker  # noqa: E402
from routing.adapters import resources  # noqa: E402
from routing.adapters.base import Adapter, AdapterError, Availability, LaunchSpec, Offer  # noqa: E402
from routing.adapters.lambda_labs import LambdaAdapter  # noqa: E402
from routing.adapters.results import Capabilities, InstanceState, TerminateResult, instance_name  # noqa: E402
from sqlalchemy import select  # noqa: E402
from store.accounts import Account  # noqa: E402
from store.reconcile import DeploymentWatch, OpsAlertState, ProviderResource, UsageSlice  # noqa: E402
from store.routing import Deployment, DeploymentEvent, ProvisionAttempt  # noqa: E402

DB = "og_test_lifecycle_main"
G = "NVIDIA A10 24GB"
S = None
SENT: list = []           # (kind, title, detail) of every ops.alert
_orig_alert = ops.alert


def _capture(kind, title, **kw):
    SENT.append((kind, title, kw.get("detail") or {}))
    return {"recorded": True, "delivered": False}


def now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# A fake provider account (like tests/test_reconcile.World) with provider timestamps
# --------------------------------------------------------------------------

class Fake(Adapter):
    LEVEL = 3
    CHECK_NEEDS_CREDENTIALS = False
    CAPABILITIES = Capabilities(billing_unit=("per second", "test"), stopped_billing=("full", "test"),
                                billing_starts=("running", "test: billing starts at first healthy"),
                                forces_account_ssh_key=("NO", "test"))
    INST: dict = {}
    TERMINATE = "ok"           # ok | noop | down (never sent) | unknown (5xx after send)
    LIST_DOWN: set = set()     # provider names whose list/status are down
    CALLS: list = []

    def _down(self):
        return self.provider in Fake.LIST_DOWN

    def _st(self, iid, v):
        return InstanceState(v["state"], instance_id=iid, name=v["name"], price_per_hour=v.get("price"),
                             running_at=v.get("running_at"), created_at=v.get("created_at"), ended_at=v.get("ended_at"),
                             time_fields={"running_at": "first_healthy"} if v.get("running_at") else {})

    def _status(self, iid):
        Fake.CALLS.append(("status", self.provider, iid))
        if self._down():
            raise AdapterError("server", "provider down", 503, sent=True)
        v = Fake.INST.get(iid)
        if v is None or v["state"] == "gone" or v["prov"] != self.provider:
            raise AdapterError("not_found", "no such instance", 404, sent=True)
        return self._st(iid, v)

    def _terminate(self, iid):
        Fake.CALLS.append(("terminate", self.provider, iid))
        if Fake.TERMINATE == "down" or self._down():
            raise AdapterError("network", "connect refused", sent=False)
        if Fake.TERMINATE == "unknown":
            raise AdapterError("server", "503", 503, sent=True)
        v = Fake.INST.get(iid)
        if v is None or v["state"] == "gone":
            raise AdapterError("not_found", "gone", 404, sent=True)
        if Fake.TERMINATE == "ok":
            v["state"] = "gone"
        return TerminateResult("accepted", "deleting", 200)

    def _list(self):
        Fake.CALLS.append(("list", self.provider))
        if self._down():
            raise AdapterError("server", "list down", 503, sent=True)
        return [self._st(k, v) for k, v in Fake.INST.items() if v["prov"] == self.provider and v["state"] != "gone"]

    def provision(self, *a, **k):
        raise AssertionError("never provisions")


class FakeDown(Fake):
    pass


def _for_ref(ref, provider):
    if not ref:
        from routing.credentials import CredentialsUnavailable
        raise CredentialsUnavailable("no ref", ref=ref)
    return {"api_key": f"key-for-{ref}"}


def _platform(p):
    return {"api_key": f"key-for-platform:{p}"} if p in ("syn_l", "syn_down", "lambda") else None


def setup():
    global S
    if S is not None:
        S.kw["bind"].dispose()
    S = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = S
    with S.begin() as s:
        s.add(Account(id=7, name="acct7", status="active", plan="free", settings={}, is_operator=False))
    adapters.register("syn_l", Fake)
    adapters.register("syn_down", FakeDown)
    Fake.INST.clear()
    Fake.CALLS.clear()
    Fake.LIST_DOWN.clear()
    Fake.TERMINATE = "ok"
    LAMBDA.reset()
    adapters.TRANSPORTS["lambda"] = httpx.MockTransport(LAMBDA)
    credentials.for_ref = _for_ref
    credentials.platform = _platform
    settings.provisioning_timeout_minutes = 15
    settings.termination_retry_base_seconds = 60
    settings.termination_retry_max = 5
    settings.alert_reescalate_minutes = 30
    settings.alert_unknown_minutes = 10
    settings.alert_overspend_pct = 20.0
    settings.ssh_key_delete_retry_max = 3
    settings.ssh_key_delete_retry_base_seconds = 60
    SENT.clear()
    ops.alert = _capture
    return S


def mkdep(status, *, provider="syn_l", iid=None, ref=None, age=timedelta(hours=1), events=None, **kw):
    dep_id = "dep-" + secrets.token_hex(6)
    t = now() - age
    ref = ref or f"platform:{provider}"
    with S.begin() as s:
        s.add(Deployment(deployment_id=dep_id, account_id=7, route_request_id="rr_t", provider=provider, listing_id="L",
                         provider_instance_id=iid, gpu=G, gpu_count=kw.pop("gpu_count", 1), status=status, created_at=t,
                         uptime_seconds=0, interruptions=0, purpose="customer", client_name=instance_name(dep_id),
                         credential_source="opengrid", credential_ref=ref, launch_token=secrets.token_hex(8),
                         state_changed_at=kw.pop("state_changed_at", t), quoted_price_per_gpu_hour=Decimal("2.0"),
                         provider_metadata={}, override_limits=False, provisioned_at=t if iid else None,
                         effective_max_runtime_minutes=kw.pop("effective_max_runtime_minutes", 1440), **kw))
        for at, to in (events or [(t, status)]):
            s.add(DeploymentEvent(deployment_id=dep_id, at=at, from_status=None, to_status=to, actor="system"))
        s.add(ProvisionAttempt(deployment_id=dep_id, route_request_id="rr_t", provider=provider, listing_id="L",
                               started_at=t, ok=None, outcome="accepted" if iid else "provisioning", instance_id=iid,
                               client_name=instance_name(dep_id), credential_ref=ref, launch_token=secrets.token_hex(8)))
    return dep_id


def dep(d):
    with S() as s:
        return s.get(Deployment, d)


def inst(iid, name, state="running", prov="syn_l", **kw):
    Fake.INST[iid] = {"state": state, "name": name, "prov": prov, **kw}


def res_rows():
    with S() as s:
        return list(s.scalars(select(ProviderResource).order_by(ProviderResource.id)))


def sent(kind):
    return [x for x in SENT if x[0] == kind]


# --------------------------------------------------------------------------
# A Lambda Cloud account over MockTransport (shapes from the OpenAPI spec)
# --------------------------------------------------------------------------

class LambdaAPI:
    def reset(self):
        self.keys = {}            # id -> {id, name, public_key}
        self.instances = {}
        self.calls = []
        self.delete_status = 200
        self.on_register = None   # callback before the key is created (asserts write-ahead)

    def __call__(self, req: httpx.Request):
        path = req.url.path.replace("/api/v1", "")
        if "byo" in req.headers.get("authorization", "") and path.startswith("/ssh-keys"):
            # a different Lambda account (a BYO credential): it has no keys of OpenGrid's account
            return httpx.Response(200, json={"data": []}) if req.method == "GET" else httpx.Response(
                404, json={"error": {"code": "global/object-does-not-exist", "message": "no"}})
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path, body))
        if req.method == "POST" and path == "/ssh-keys":
            if self.on_register:
                self.on_register(body)
            kid = "k" + secrets.token_hex(3)
            self.keys[kid] = {"id": kid, "name": body["name"], "public_key": body["public_key"]}
            return httpx.Response(200, json={"data": self.keys[kid]})
        if req.method == "GET" and path == "/ssh-keys":
            return httpx.Response(200, json={"data": list(self.keys.values())})
        if req.method == "DELETE" and path.startswith("/ssh-keys/"):
            kid = path.rsplit("/", 1)[1]
            if self.delete_status != 200:
                return httpx.Response(self.delete_status, json={"error": {"code": "global/unknown", "message": "x"}})
            if kid not in self.keys:
                return httpx.Response(404, json={"error": {"code": "global/object-does-not-exist", "message": "no"}})
            del self.keys[kid]
            return httpx.Response(200, json={"data": {}})
        if req.method == "POST" and path == "/instance-operations/launch":
            iid = "i" + secrets.token_hex(3)
            self.instances[iid] = {"id": iid, "name": body["name"], "status": "booting", "ssh_key_names": body["ssh_key_names"],
                                   "first_healthy": None, "region": {"name": body["region_name"]},
                                   "instance_type": {"name": body["instance_type_name"], "price_cents_per_hour": 75,
                                                     "specs": {"gpus": 1}}, "tags": body.get("tags")}
            return httpx.Response(200, json={"data": {"instance_ids": [iid]}})
        if req.method == "GET" and path == "/instances":
            return httpx.Response(200, json={"data": list(self.instances.values())})
        if req.method == "GET" and path.startswith("/instances/"):
            i = self.instances.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"data": i}) if i else httpx.Response(
                404, json={"error": {"code": "global/object-does-not-exist", "message": "no"}})
        return httpx.Response(404, json={"error": {"code": "global/not-found", "message": path}})


LAMBDA = LambdaAPI()
OFFER = Offer(provider="lambda", listing_id="lambda:gpu_1x_a10", sku="gpu_1x_a10", raw_gpu_name="A10", gpu=G,
              gpu_count=1, region="us-east-1", price_per_gpu_hour=0.75)
AV = Availability(available=True, live=True, region="us-east-1")


def lambda_launch(d):
    a = adapters.build("lambda", {"api_key": "lambda-secret-key"})
    a.log_context = {"deployment_id": d}
    try:
        return a.provision(OFFER, AV, LaunchSpec(ssh_public_key=PK), instance_name(d))
    finally:
        a.close()


# --------------------------------------------------------------------------
# SSH keys
# --------------------------------------------------------------------------

def test_capabilities_declared_for_every_adapter():
    for p, cls in adapters.ADAPTERS.items():
        if p.startswith("syn"):
            continue
        v, ev = cls.CAPABILITIES.forces_account_ssh_key
        assert v in ("YES", "NO", "UNKNOWN") and ev, (p, v)
        assert str(cls.SSH_KEY_REGISTRATION) in ("per_deployment", "account_only", "none"), p
        if cls.SSH_KEY_RESOURCE:
            for m in ("_register_ssh_key", "_delete_ssh_key", "_list_ssh_keys"):
                assert getattr(cls, m) is not getattr(Adapter, m), (p, m)
    assert LambdaAdapter.CAPABILITIES.forces_account_ssh_key[0] == "NO"
    assert LambdaAdapter.CAPABILITIES.billing_starts[0] == "running"


def test_key_recorded_before_registration_and_only_that_key_launched():
    setup()
    d = mkdep("provisioning", provider="lambda", ref="platform:lambda")
    seen = {}

    def on_register(body):
        with S() as s:
            rows = list(s.scalars(select(ProviderResource).where(ProviderResource.name == body["name"])))
        seen["rows"] = [(r.status, r.deployment_id, r.credential_ref, r.provider_resource_id) for r in rows]

    LAMBDA.on_register = on_register
    r = lambda_launch(d)
    assert r.outcome == "accepted", r
    assert seen["rows"] == [("creating", d, "platform:lambda", None)], seen
    launch = [b for m, p, b in LAMBDA.calls if p == "/instance-operations/launch"][0]
    assert launch["ssh_key_names"] == [instance_name(d)], "only the per-deployment key: never an account key"
    (row,) = res_rows()
    assert row.status == "active" and row.provider_resource_id in LAMBDA.keys and row.fingerprint.startswith("SHA256:")
    assert row.recorded_by == "register"
    # the instance reports its keys; the tracker would alert on any other key
    st = adapters.build("lambda", {"api_key": "x"}).status(r.instance_id)
    assert st.ssh_key_names == [instance_name(d)]


def test_record_failure_refuses_registration():
    setup()
    d = mkdep("provisioning", provider="lambda", ref="platform:lambda")
    a = adapters.build("lambda", {"api_key": "lambda-secret-key"})
    a.log_context = {"deployment_id": d + "-" + "x" * 40}     # cannot be stored (String(32)): the write fails
    r = a.provision(OFFER, AV, LaunchSpec(ssh_public_key=PK), instance_name(d))
    assert r.outcome == "rejected", r
    assert not [c for c in LAMBDA.calls if c[1] == "/ssh-keys"], "no key is ever created without a record"
    assert not [c for c in LAMBDA.calls if c[1] == "/instance-operations/launch"]


def test_key_deleted_only_after_confirmed_termination():
    setup()
    d = mkdep("provisioning", provider="lambda", ref="platform:lambda")
    lambda_launch(d)
    (row,) = res_rows()
    for status in ("provisioning", "running", "terminating", "termination_failed"):
        with S.begin() as s:
            s.get(Deployment, d).status = status
        resources.cleanup_due()
        assert res_rows()[0].status == "active" and row.provider_resource_id in LAMBDA.keys, status
    with S.begin() as s:
        x = s.get(Deployment, d)
        x.status, x.terminated_at = "terminated", now()
    out = resources.cleanup_due()
    assert out and out[0]["outcome"] == "accepted", out
    r = res_rows()[0]
    assert r.status == "deleted" and r.deleted_at is not None and not LAMBDA.keys
    assert ("DELETE", f"/ssh-keys/{row.provider_resource_id}", None) in LAMBDA.calls
    assert resources.cleanup_due() == [], "idempotent"


def test_delete_failure_retries_with_backoff_then_alerts():
    setup()
    d = mkdep("provisioning", provider="lambda", ref="platform:lambda")
    lambda_launch(d)
    with S.begin() as s:
        x = s.get(Deployment, d)
        x.status, x.terminated_at = "terminated", now()
    LAMBDA.delete_status = 500
    resources.cleanup_due()
    r = res_rows()[0]
    assert r.status == "deleting" and r.delete_attempts == 1 and r.next_delete_at > now()
    resources.cleanup_due()
    assert res_rows()[0].delete_attempts == 1, "inside the backoff window: not retried"
    settings.ssh_key_delete_retry_base_seconds = 0
    with S.begin() as s:                                   # the backoff window passes
        s.get(ProviderResource, r.id).next_delete_at = now() - timedelta(seconds=1)
    resources.cleanup_due()
    resources.cleanup_due()
    r = res_rows()[0]
    assert r.status == "delete_failed" and r.delete_attempts == 3, (r.status, r.delete_attempts)
    al = sent("resource_delete_failed")
    assert len(al) == 1 and all(k in al[0][2] for k in ops.REQUIRED_FIELDS), al
    resources.cleanup_due()                                # keeps retrying after delete_failed
    assert res_rows()[0].delete_attempts == 4 and len(sent("resource_delete_failed")) == 1, "deduplicated"
    LAMBDA.delete_status = 200
    resources.cleanup_due()
    assert res_rows()[0].status == "deleted" and not LAMBDA.keys
    with S() as s:
        a = s.scalars(select(OpsAlertState).where(OpsAlertState.kind == "resource_delete_failed")).one()
    assert a.status == "resolved" and a.resolution["basis"].startswith("provider delete")


def test_abandoned_keys_detected_foreign_keys_never_deleted():
    setup()
    term = mkdep("terminated", provider="lambda", ref="platform:lambda", terminated_at=now())
    other = mkdep("terminated", provider="lambda", ref="byo:99", terminated_at=now())
    live = mkdep("running", provider="lambda", ref="platform:lambda", iid="i-live")
    LAMBDA.instances["i-live"] = {"id": "i-live", "name": instance_name(live), "status": "active", "ssh_key_names": [],
                                  "first_healthy": None, "region": {"name": "us-east-1"},
                                  "instance_type": {"name": "gpu_1x_a10", "price_cents_per_hour": 75,
                                                    "specs": {"gpus": 1}}}
    LAMBDA.keys = {
        "k-term": {"id": "k-term", "name": instance_name(term), "public_key": PK},   # provably ours, no record
        "k-other": {"id": "k-other", "name": instance_name(other), "public_key": PK},  # other credential: not provable
        "k-none": {"id": "k-none", "name": "og-dep-ffffffffffff", "public_key": PK},  # og-* with no deployment
        "k-live": {"id": "k-live", "name": instance_name(live), "public_key": PK},    # live deployment: keep
        "k-mine": {"id": "k-mine", "name": "alice-laptop", "public_key": PK},        # not og-*: ignored entirely
    }
    r = reconcile.run_once("lambda")
    kinds = [f["kind"] for f in r["findings"]]
    assert "ssh_key_abandoned" in kinds, r["findings"]
    assert set(LAMBDA.keys) == {"k-other", "k-none", "k-live", "k-mine"}, LAMBDA.keys
    rows = {x.provider_resource_id: x for x in res_rows()}
    assert rows["k-term"].status == "deleted" and rows["k-term"].recorded_by == "reconcile_found"
    assert rows["k-none"].status == "abandoned" and rows["k-none"].recorded_by == "reconcile_unowned"
    assert rows["k-none"].deployment_id is None
    assert rows["k-live"].status == "abandoned" and rows["k-live"].deployment_id == live
    assert "k-mine" not in rows, "keys not named og-* are never even recorded"
    assert not [c for c in LAMBDA.calls if c[0] == "DELETE" and c[1] in ("/ssh-keys/k-none", "/ssh-keys/k-mine",
                                                                         "/ssh-keys/k-live")]
    unowned = sent("unowned_provider_resource")
    assert sorted(u[2]["key_id"] for u in unowned) == ["k-none", "k-other"], unowned
    assert rows["k-other"].recorded_by == "reconcile_unowned", "pinned to another credential: not provably ours"
    # operator cleanup refuses anything not provably ours
    for kid in ("k-none", "k-other"):
        try:
            resources.cleanup(rows[kid].id, "admin:test")
            raise AssertionError("must refuse")
        except ValueError as e:
            assert "never deletes" in str(e)
    try:
        resources.cleanup(rows["k-live"].id, "admin:test")
        raise AssertionError("must refuse: deployment still running")
    except ValueError as e:
        assert "confirmed termination" in str(e)
    # the live deployment ends -> its key is deleted on the next pass
    with S.begin() as s:
        x = s.get(Deployment, live)
        x.status, x.terminated_at = "terminated", now()
    reconcile.run_once("lambda")
    assert "k-live" not in LAMBDA.keys and "k-none" in LAMBDA.keys and "k-mine" in LAMBDA.keys


def test_admin_resource_endpoints():
    setup()
    import main
    from fastapi.testclient import TestClient
    from accounts.auth import OPERATOR, principal

    d = mkdep("provisioning", provider="lambda", ref="platform:lambda")
    lambda_launch(d)
    with S.begin() as s:
        x = s.get(Deployment, d)
        x.status, x.terminated_at = "terminated", now()
    app = main.app
    app.dependency_overrides[principal] = lambda: OPERATOR
    try:
        c = TestClient(app, headers={"X-OpenGrid-Request": "1"})
        r = c.get("/v1/admin/resources?type=ssh_key")
        assert r.status_code == 200, r.text
        items = r.json()["data"]
        assert len(items) == 1 and items[0]["status"] == "active" and items[0]["provably_ours"]
        rid = items[0]["id"]
        h = {"Idempotency-Key": "k-1", "X-OpenGrid-Request": "1"}
        r1 = c.post(f"/v1/admin/resources/{rid}/cleanup", headers=h)
        assert r1.status_code == 200, r1.text
        assert r1.json()["data"]["status"] == "deleted"
        r2 = c.post(f"/v1/admin/resources/{rid}/cleanup", headers=h)
        assert r2.status_code == 200 and r2.headers.get("Idempotent-Replayed") == "true"
        assert len([x for x in LAMBDA.calls if x[0] == "DELETE"]) == 1, "a replay never deletes twice"
        assert c.post(f"/v1/admin/resources/{rid}/cleanup", headers={"X-OpenGrid-Request": "1"}).status_code in (400, 428)
        assert c.get("/v1/admin/resources?type=ssh_key&status=deleted").json()["data"][0]["id"] == rid
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Billable window
# --------------------------------------------------------------------------

def test_lambda_first_healthy_is_provider_running_time():
    a = LambdaAdapter({"api_key": "x"})
    st = a._state({"id": "i1", "status": "active", "first_healthy": "2026-10-07T10:00:00Z", "ssh_key_names": ["og-a"]})
    assert st.running_at == datetime(2026, 10, 7, 10, tzinfo=timezone.utc) and st.created_at is None
    assert st.time_fields == {"running_at": "first_healthy"} and st.ended_at is None


def test_mid_launch_terminate_meters_real_provider_runtime():
    setup()
    t = now()
    # the provision call was in flight when the customer asked to terminate; it then returned an instance id
    d = mkdep("provisioning", iid="i-m", age=timedelta(minutes=40), terminate_requested_at=t - timedelta(minutes=38),
              requested_termination_at=t - timedelta(minutes=38))
    ran = t - timedelta(minutes=36)
    inst("i-m", instance_name(d), "running", running_at=ran, price=2.0)
    tracker.poll(d)                     # existence confirmed -> terminate immediately
    row = dep(d)
    assert row.status == "terminating" and ("terminate", "syn_l", "i-m") in Fake.CALLS, row.status
    assert row.provider_running_at == ran and row.billable_start == ran and row.billable_basis == "provider_running_at"
    reconcile.run_once("syn_l")         # list omits it AND status not_found -> terminated -> billed
    row = dep(d)
    assert row.status == "terminated", row.status
    with S() as s:
        evs = [e.to_status for e in s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == d))]
        sl = list(s.scalars(select(UsageSlice).where(UsageSlice.deployment_id == d).order_by(UsageSlice.period_start)))
    assert "running" not in evs, "OpenGrid never recorded running ..."
    billed = sum(x.billable_seconds for x in sl)
    expect = (row.terminated_at - ran).total_seconds()
    assert billed > 0 and abs(billed - expect) <= 2, (billed, expect)   # ... but the provider's runtime is billed
    assert sl[-1].final and sl[-1].end_estimated, "Lambda exposes no terminated time: the end is an estimate"
    assert row.billable_end == row.terminated_at
    rec = row.reconciliation
    assert rec["runtime"]["billable_seconds"] == billed and rec["billable_window"]["basis"] == "provider_running_at"
    assert rec["billable_window"]["end_estimated"] is True


def test_provider_end_time_used_and_never_running_not_billed():
    setup()
    t = now()
    ran, end = t - timedelta(minutes=50), t - timedelta(minutes=20)
    d = mkdep("provisioning", iid="i-e", age=timedelta(minutes=55), terminate_requested_at=t - timedelta(minutes=54))
    inst("i-e", instance_name(d), "running", running_at=ran)
    tracker.poll(d)
    Fake.INST["i-e"].update(state="terminated", ended_at=end)
    tracker.poll(d)
    row = dep(d)
    assert row.status == "terminated" and row.provider_terminated_at == end and row.terminated_at == end
    with S() as s:
        sl = list(s.scalars(select(UsageSlice).where(UsageSlice.deployment_id == d)))
    assert sum(x.billable_seconds for x in sl) == 30 * 60 and not sl[-1].end_estimated
    # booting at the provider (no first_healthy) and terminated: Lambda-style billing has nothing to charge
    d2 = mkdep("provisioning", iid="i-b", age=timedelta(minutes=10), terminate_requested_at=t - timedelta(minutes=9))
    inst("i-b", instance_name(d2), "pending")
    tracker.poll(d2)
    reconcile.run_once("syn_l")
    assert dep(d2).status == "terminated"
    with S() as s:
        assert sum(x.billable_seconds for x in s.scalars(select(UsageSlice).where(UsageSlice.deployment_id == d2))) == 0


# --------------------------------------------------------------------------
# Deadlines through provider outages
# --------------------------------------------------------------------------

def test_deadline_during_outage_retried_alerted_then_terminated():
    setup()
    settings.termination_retry_base_seconds = 3600          # backoff alone would wait an hour
    d = mkdep("running", iid="i-d", max_runtime_minutes=30, terminate_deadline_at=now() - timedelta(minutes=2))
    inst("i-d", instance_name(d), "running", price=2.0)
    Fake.LIST_DOWN.add("syn_l")                              # the provider API is unavailable
    r1 = reconcile.run_once()
    assert r1["findings"][0]["deployment_id"] == d and r1["findings"][0]["kind"] in (
        "terminate_issued", "deadline_terminated"), "overdue deadlines are enforced first"
    assert any(f["kind"] == "deadline_terminated" for f in r1["findings"])
    row = dep(d)
    assert row.status == "terminating" and row.termination_reason == "max_runtime_exceeded"
    with S() as s:
        w = s.get(DeploymentWatch, d)
    assert w.past_deadline_at is not None
    n1 = len([c for c in Fake.CALLS if c[0] == "terminate"])
    pd = sent("past_deadline")
    assert len(pd) == 1 and all(k in pd[0][2] for k in ops.REQUIRED_FIELDS), pd
    assert pd[0][2]["deployment_id"] == d and pd[0][2]["est_hourly_exposure_usd"] == 2.0
    assert len(sent("provider_api_unavailable")) == 1
    reconcile.run_once()
    reconcile.run_once()
    n3 = len([c for c in Fake.CALLS if c[0] == "terminate"])
    assert n3 == n1 + 2, ("re-issued every run despite the backoff", n1, n3)
    assert dep(d).status in ("terminating", "termination_failed") and dep(d).terminated_at is None
    assert len(sent("past_deadline")) == 1, "deduplicated within alert_reescalate_minutes"
    settings.alert_reescalate_minutes = 0
    reconcile.run_once()
    pd = sent("past_deadline")
    assert len(pd) == 2 and pd[1][2]["escalation"] == 2, "re-escalated while unresolved"
    settings.alert_reescalate_minutes = 30
    Fake.LIST_DOWN.clear()                                   # the API comes back
    reconcile.run_once()
    row = dep(d)
    assert row.status == "terminated", row.status
    with S() as s:
        st = {a.kind: a for a in s.scalars(select(OpsAlertState).where(OpsAlertState.subject.in_(
            [f"dep:{d}", f"provider:syn_l:platform:syn_l"])))}
    assert st["past_deadline"].status == "resolved" and "terminated" in st["past_deadline"].resolution["basis"]
    assert st["provider_api_unavailable"].status == "resolved"


# --------------------------------------------------------------------------
# The alert catalogue
# --------------------------------------------------------------------------

def test_every_alert_type_fires_once_with_fields_and_reescalates():
    setup()
    settings.provisioning_timeout_minutes = 600              # keep the unknown launch unresolved
    settings.termination_retry_base_seconds = 0
    Fake.TERMINATE = "noop"                                  # deletes are accepted but nothing goes away
    t = now()
    unk = mkdep("launch_unknown", age=timedelta(minutes=30))
    tf = mkdep("termination_failed", iid="i-tf", terminate_requested_at=t - timedelta(hours=1))
    inst("i-tf", instance_name(tf), "running", price=2.0)
    pdl = mkdep("running", iid="i-pd", terminate_deadline_at=t - timedelta(minutes=5))
    inst("i-pd", instance_name(pdl), "running", price=2.0)
    ovr = mkdep("running", iid="i-ov", actual_price_per_gpu_hour=Decimal("3.0"), age=timedelta(minutes=20))
    inst("i-ov", instance_name(ovr), "running", price=3.0)
    nob = mkdep("running", iid="i-nb", age=timedelta(hours=5))
    inst("i-nb", instance_name(nob), "running", price=2.0)
    inst("i-orph", "og-dep-ffffffffffff", "running", price=4.0)
    down = mkdep("running", provider="syn_down", iid="i-dn")
    inst("i-dn", instance_name(down), "running", prov="syn_down")
    Fake.LIST_DOWN.add("syn_down")
    with S.begin() as s:
        s.add(ProviderResource(provider="syn_l", resource_type="ssh_key", provider_resource_id="kx",
                               name="og-dep-eeeeeeeeeeee", credential_ref="platform:syn_l",
                               recorded_by="reconcile_unowned", created_at=t, status="abandoned", delete_attempts=0,
                               updated_at=t))
    expected = {"resource_state_unknown": f"dep:{unk}", "termination_failed": f"dep:{tf}",
                "past_deadline": f"dep:{pdl}", "suspected_orphan": None, "provider_api_unavailable": None,
                "overspend": f"dep:{ovr}", "active_without_billing": f"dep:{nob}", "unowned_provider_resource": None}
    reconcile.run_once()
    for kind in expected:
        got = sent(kind)
        assert len(got) == 1, (kind, len(got), [x[0] for x in SENT])
        det = got[0][2]
        missing = [k for k in ops.REQUIRED_FIELDS if k not in det]
        assert not missing, (kind, missing)
        assert det["suggested_action"] and det["time_in_state"] is not None, (kind, det)
    assert sent("overspend")[0][2]["deployment_id"] == ovr and sent("overspend")[0][2]["account_id"] == 7
    assert sent("suspected_orphan")[0][2]["est_hourly_exposure_usd"] == 4.0
    reconcile.run_once()
    assert all(len(sent(k)) == 1 for k in expected), "deduplicated"
    settings.alert_reescalate_minutes = 0
    reconcile.run_once()
    for kind in expected:
        got = sent(kind)
        assert len(got) == 2 and got[1][2]["escalation"] == 2 and got[1][1].startswith("[re-escalation 2]"), kind
    settings.alert_reescalate_minutes = 30
    # never silently resolved: still-true conditions stay open; ending one resolves it WITH evidence
    with S() as s:
        open_kinds = {a.kind for a in s.scalars(select(OpsAlertState).where(OpsAlertState.status == "open"))}
    assert set(expected) <= open_kinds, open_kinds
    Fake.TERMINATE = "ok"
    reconcile.run_once()                                     # re-issued delete succeeds ...
    reconcile.run_once()                                     # ... confirmed gone by two signals
    with S() as s:
        a = s.scalars(select(OpsAlertState).where(OpsAlertState.kind == "termination_failed")).one()
    assert dep(tf).status == "terminated" and a.status == "resolved" and a.resolution["basis"]
    try:
        ops.resolve("overspend", f"dep:{ovr}", {})
        raise AssertionError("resolving without evidence must be refused")
    except ValueError:
        pass


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
        ops.alert = _orig_alert
        if S is not None:
            S.kw["bind"].dispose()
        scratchdb.drop(DB)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
