"""Core limits (HARDEN2 B-core, C, D, F-core, I): runtime ceilings, atomic concurrency limits, customer-only SSH
access, lifecycle timestamps, the validation launch gate.

Scratch database, synthetic syn_* providers on the execution-core Fake adapter. No real provider is ever called.
Concurrency is real: threads, and two separate OS processes (subprocess), against one Postgres.

Run:  .venv/Scripts/python tests/test_limits.py
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import scratchdb  # noqa: E402
import test_execution_core as ec  # noqa: E402  (harness: Fake adapter, seed, helpers)

import normalize  # noqa: E402
from accounts.auth import OPERATOR  # noqa: E402
from analytics import rollups  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import adapters, checklist, control, credentials, deployments, engine, guards, scoring, validation  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

DB = "og_test_limits"
MIG_DB = "og_test_limits_mig"
OPKEY = ec.pubkey(99, "operator@opengrid")
RESULTS: dict = {}


def approve_quiet(rr, qid, **kw):
    try:
        code, res = engine.approve(rr, OPERATOR, quote_id=qid, **kw)
        return ("ok", res.get("status"))
    except HTTPException as e:
        return ("refused", (e.detail or {}).get("code") if isinstance(e.detail, dict) else str(e.detail))


def pending_routes(acct, n, provider="syn_a", **kw):
    out = []
    for _ in range(n):
        code, o = engine.route(ec.spec(provider, **kw), ec.key(acct))
        assert o["status"] == "pending_approval", (o["status"], o.get("reason"), o.get("considered"))
        out.append((o["route_request_id"], o["quote"]["quote_id"]))
    return out


def run_threads(fns):
    outs = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def w(i):
        barrier.wait()
        outs[i] = fns[i]()

    ts = [threading.Thread(target=w, args=(i,)) for i in range(len(fns))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return outs


def provisioning_count(acct):
    return ec.count("deployments", "account_id = :a AND launch_token IS NOT NULL", a=acct)


# --------------------------------------------------------------------------
# C. runtime ceilings
# --------------------------------------------------------------------------

def test_runtime_ceiling_combinations():
    saved = (settings.runtime_hard_max_minutes, settings.runtime_default_minutes)
    settings.runtime_hard_max_minutes, settings.runtime_default_minutes = 1440, 60
    try:
        L = lambda mx=None, df=None: {"max_runtime_minutes": mx, "default_runtime_minutes": df}  # noqa: E731
        rc = guards.runtime_ceiling
        cases = [
            # (requested, account max, account default, purpose) -> (effective, source, clamped)
            ((None, None, None, "customer"), (60, "system_default", False)),
            ((90, None, None, "customer"), (90, "request", False)),
            ((None, None, 120, "customer"), (120, "account", False)),
            ((45, None, 120, "customer"), (45, "request", False)),
            ((300, 240, None, "customer"), (240, "account", True)),
            ((None, 30, None, "customer"), (30, "account", False)),         # system default clamped by account max
            ((None, 100, 200, "customer"), (100, "account", False)),        # account default above account max
            ((5000, None, None, "customer"), (1440, "system_hard_max", True)),
            ((5000, 3000, None, "customer"), (1440, "system_hard_max", True)),
            ((None, None, 5000, "customer"), (1440, "system_hard_max", False)),
            ((None, None, None, "validation"), (30, "validation_cap", False)),
            ((20, None, None, "validation"), (20, "request", False)),
            ((90, None, None, "validation"), (30, "validation_cap", True)),
        ]
        for (req, mx, df, purpose), (eff, src, clamped) in cases:
            r = rc(7, req, purpose=purpose, limits=L(mx, df))
            assert (r["effective_max_runtime_minutes"], r["runtime_ceiling_source"], r["clamped"]) == (eff, src, clamped), \
                ((req, mx, df, purpose), r)
            assert r["effective_max_runtime_minutes"] and r["effective_max_runtime_minutes"] > 0
            if clamped:
                assert r["note"], r
        settings.runtime_default_minutes = 99999          # a misconfigured default is still capped
        assert rc(7, None, limits=L())["effective_max_runtime_minutes"] == 1440
    finally:
        settings.runtime_hard_max_minutes, settings.runtime_default_minutes = saved
    RESULTS["runtime_cases"] = len(cases)


def test_runtime_through_route_approve_launch():
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    guards.set_limits(8, {"max_runtime_minutes": 240, "default_runtime_minutes": 90}, by="t", reason="pilot")
    # no request: account default 90; the ticket shows the ceiling and warns that the 10 h duration will be cut
    code, out = engine.route(ec.spec("syn_a"), ec.key(8))
    rt = out["approval"]["runtime"]
    assert rt["effective_max_runtime_minutes"] == 90 and rt["runtime_ceiling_source"] == "account", rt
    assert rt["auto_terminate_at_if_launched_now"] and "duration_warning" in rt and rt["terminate_deadline_at"] is None
    d = ec.dep(out["deployment"]["deployment_id"])
    assert d.effective_max_runtime_minutes == 90 and d.runtime_ceiling_source == "account"
    # approval: exact deadline = approval time + ceiling, returned; launch re-states it from launch time
    code, res = engine.approve(out["route_request_id"], OPERATOR, quote_id=out["quote"]["quote_id"])
    assert res["status"] == "provisioned"
    d = ec.dep(d.deployment_id)
    at = res["auto_termination"]
    assert at["effective_max_runtime_minutes"] == 90 and at["terminate_deadline_at"] == d.terminate_deadline_at.isoformat()
    assert 89.9 <= (d.terminate_deadline_at - d.approved_at).total_seconds() / 60 <= 90.5
    assert res["deployment"]["auto_termination"]["terminate_deadline_at"] == at["terminate_deadline_at"]
    # request above the account max: clamped (never exceeded), with a note
    code, out = engine.route(ec.spec("syn_a", max_runtime_minutes=1000), ec.key(8))
    rt = out["approval"]["runtime"]
    assert rt["effective_max_runtime_minutes"] == 240 and rt["clamped"] and "clamped" in rt["note"], rt
    engine.reject(out["route_request_id"], OPERATOR, reason="cleanup")
    # limits changed between quote and approval: the approval recomputes (stricter wins)
    code, out = engine.route(ec.spec("syn_a", max_runtime_minutes=200), ec.key(8))
    guards.set_limits(8, {"max_runtime_minutes": 50}, by="t", reason="tighter")
    code, res = engine.approve(out["route_request_id"], OPERATOR, quote_id=out["quote"]["quote_id"])
    d = ec.dep(res["deployment"]["deployment_id"])
    assert d.effective_max_runtime_minutes == 50 and (d.terminate_deadline_at - d.provisioned_at).total_seconds() <= 50 * 60 + 5
    # LIVE route: approved and launched with the system default (60) when nothing is set
    ec.reset()
    ec.mode("LIVE")
    ec.enable("syn_a", live=True)
    code, out = engine.route(ec.spec("syn_a"), ec.key(9))
    d = ec.dep(out["deployment"]["deployment_id"])
    assert out["status"] == "provisioned" and d.effective_max_runtime_minutes == 60
    assert d.runtime_ceiling_source == "system_default" and d.terminate_deadline_at is not None
    assert out["runtime"]["terminate_deadline_at"] == d.terminate_deadline_at.isoformat()
    # the DB refuses a NULL / non-positive ceiling
    for bad in ("NULL", "0"):
        try:
            with normalize.SessionLocal.begin() as s:
                s.execute(text(f"UPDATE deployments SET effective_max_runtime_minutes = {bad} "
                               "WHERE deployment_id = :d"), {"d": d.deployment_id})
            raise AssertionError(f"effective_max_runtime_minutes = {bad} accepted")
        except AssertionError:
            raise
        except Exception:  # noqa: BLE001 - IntegrityError
            pass


def test_migration_backfills_legacy_rows():
    url = scratchdb.create(MIG_DB)
    e = create_engine(url)
    env = dict(os.environ, DATABASE_URL=url)

    def up(rev):
        r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", rev], cwd=str(ROOT), env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-3000:]
    try:
        up("0013_security")
        with e.begin() as c:
            for d, mr, dl in (("dep-legacy1", None, None), ("dep-legacy2", 30, None),
                              ("dep-legacy3", None, "2026-09-01T09:00:00+00")):
                c.execute(text("INSERT INTO route_requests (id,account_id,principal_kind,preview,mode,gpu,request,status,"
                               "created_at) VALUES (:r,5,'api_key',false,'BALANCED','G','{}','provisioned',now())"),
                          {"r": "rr_" + d})
                c.execute(text(
                    "INSERT INTO deployments (deployment_id,account_id,route_request_id,provider,gpu,gpu_count,status,"
                    "created_at,uptime_seconds,interruptions,purpose,override_limits,max_runtime_minutes,"
                    "terminate_deadline_at,terminate_requested_at) VALUES (CAST(:d AS text),5,:r,'lambda','G',1,'running',"
                    "'2026-09-01T08:00:00+00',0,0,'customer',false,:mr,CAST(:dl AS timestamptz),"
                    "CASE WHEN CAST(:d AS text) = 'dep-legacy1' THEN TIMESTAMPTZ '2026-09-01T08:30:00+00' END)"),
                    {"d": d, "r": "rr_" + d, "mr": mr, "dl": dl})
        up("0014_limits")
        with e.connect() as c:
            rows = {r[0]: r[1:] for r in c.execute(text(
                "SELECT deployment_id, effective_max_runtime_minutes, runtime_ceiling_source, terminate_deadline_at, "
                "requested_termination_at FROM deployments"))}
            nullable = c.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_name = "
                                      "'deployments' AND column_name = 'effective_max_runtime_minutes'")).scalar()
            cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE "
                                                 "table_name = 'account_limits'"))}
        t0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
        assert rows["dep-legacy1"][:3] == (60, "legacy_backfill", t0 + timedelta(minutes=60)), rows
        assert rows["dep-legacy2"][:3] == (30, "legacy_backfill", t0 + timedelta(minutes=30)), rows
        assert rows["dep-legacy3"][2] == datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc), "existing deadline kept"
        assert rows["dep-legacy1"][3] == datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc)
        assert nullable == "NO" and {"max_runtime_minutes", "default_runtime_minutes"} <= cols
    finally:
        e.dispose()
        scratchdb.drop(MIG_DB)


# --------------------------------------------------------------------------
# D. atomic concurrency limits
# --------------------------------------------------------------------------

def _concurrent_approvals(acct, limits, n=8, label="max_active_deployments=1", **route_kw):
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    ec.Fake.DELAY = 0.3
    guards.set_limits(acct, limits, by="t", reason="concurrency test")
    rrs = pending_routes(acct, n, **route_kw)
    outs = run_threads([lambda rr=rr, q=q: approve_quiet(rr, q) for rr, q in rrs])
    ok = [o for o in outs if o[0] == "ok" and o[1] == "provisioned"]
    refused = [o for o in outs if o == ("refused", "limits_exceeded")]
    assert len(ok) == 1 and len(refused) == n - 1, (label, outs)
    assert len(ec.calls("provision")) == 1 and provisioning_count(acct) == 1
    RESULTS[label] = f"{n} threads x {label} -> {len(ok)} provisioning, {len(refused)} limits_exceeded"
    ec.Fake.DELAY = 0.0


def test_concurrent_max_active():
    _concurrent_approvals(10, {"max_active_deployments": 1}, label="max_active_deployments=1")


def test_concurrent_max_gpus():
    _concurrent_approvals(11, {"max_gpus": 1}, label="max_gpus=1")


def test_concurrent_hourly_spend():
    _concurrent_approvals(12, {"max_hourly_cost": 1.5}, label="max_hourly_cost=$1.50/h ($1/h each)")


def test_concurrent_monthly_spend():
    _concurrent_approvals(13, {"monthly_spend_limit": 1.5}, label="monthly_spend_limit=$1.50 ($1 max exposure each)")


def test_concurrent_live_routes():
    ec.reset()
    ec.mode("LIVE")
    ec.enable("syn_a", live=True)
    ec.Fake.DELAY = 0.3
    guards.set_limits(14, {"max_active_deployments": 1}, by="t", reason="concurrency test")
    outs = run_threads([lambda: engine.route(ec.spec("syn_a"), ec.key(14))[1]["status"] for _ in range(6)])
    assert outs.count("provisioned") == 1 and outs.count("pending_approval") == 5, outs
    assert len(ec.calls("provision")) == 1 and provisioning_count(14) == 1
    RESULTS["live"] = f"6 LIVE route threads x max_active_deployments=1 -> 1 provisioning, 5 held pending_approval"
    ec.Fake.DELAY = 0.0


def test_lock_not_held_across_provider_call():
    """While the provider call is in flight, the account lock and the row lock are free (committed before)."""
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    (rr, q), = pending_routes(15, 1)
    seen = {}
    real = ec.Fake.provision

    def probing(self, offer, availability, launch, name):
        with normalize.SessionLocal() as s:
            seen["advisory_free"] = s.execute(text("SELECT pg_try_advisory_xact_lock(:ns, :k)"),
                                              {"ns": guards.LOCK_ACCOUNT, "k": 15}).scalar()
            dep_id = name[3:]
            seen["row_free"] = s.execute(text("SELECT status FROM deployments WHERE deployment_id = :d "
                                              "FOR UPDATE NOWAIT"), {"d": dep_id}).scalar()
            s.rollback()
        return real(self, offer, availability, launch, name)

    ec.Fake.provision = probing
    try:
        assert approve_quiet(rr, q) == ("ok", "provisioned")
    finally:
        ec.Fake.provision = real
    assert seen == {"advisory_free": True, "row_free": "provisioning"}, seen


def test_one_active_validation_concurrent():
    ec.reset()
    ec.validation_ready("syn_c")
    rrs = []
    for _ in range(5):   # pending validation routes do not count as active: all can be created
        rr = engine.create_validation_route("syn_c", None, by="operator")
        rrs.append((rr, deployments.for_request(rr).quote_id))
    ec.Fake.DELAY = 0.3
    outs = run_threads([lambda rr=rr, q=q: approve_quiet(rr, q) for rr, q in rrs])
    ok = [o for o in outs if o == ("ok", "provisioned")]
    refused = [o for o in outs if o[0] == "refused" and o[1] in ("limits_exceeded", "validation_preconditions_failed")]
    assert len(ok) == 1 and len(refused) == 4, outs
    assert ec.count("deployments", "purpose = 'validation' AND launch_token IS NOT NULL") == 1
    RESULTS["validation"] = (f"5 threads x one-active-validation -> 1 provisioning, 4 refused "
                             f"({sorted(set(o[1] for o in refused))})")
    ec.Fake.DELAY = 0.0


CHILD = r"""
import json, os, sys, time
os.environ["OPENGRID_NO_JOBS"] = "1"; os.environ["POLLER_ENABLED"] = "false"
sys.path.insert(0, {root!r}); sys.path.insert(0, {tests!r})
import fixtures, normalize, test_execution_core as ec
from config import settings
from routing import adapters, engine
from accounts.auth import OPERATOR
from fastapi import HTTPException
normalize.SessionLocal = fixtures.session({url!r})
settings.routing_live_provisioning = True
adapters.register("syn_a", ec.Fake)
ec.Fake.DELAY = 0.5
while time.time() < {start}:
    time.sleep(0.005)
try:
    code, res = engine.approve({rr!r}, OPERATOR, quote_id={q!r})
    print(json.dumps(["ok", res.get("status")]))
except HTTPException as e:
    print(json.dumps(["refused", (e.detail or {{}}).get("code")]))
"""


def test_two_processes_max_active():
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    guards.set_limits(16, {"max_active_deployments": 1}, by="t", reason="two-process test")
    rrs = pending_routes(16, 2)
    start = time.time() + 6
    procs = [subprocess.Popen([sys.executable, "-c", CHILD.format(
        root=str(ROOT), tests=str(ROOT / "tests"), url=scratchdb.url(DB), start=start, rr=rr, q=q)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for rr, q in rrs]
    outs = []
    for p in procs:
        o, err = p.communicate(timeout=120)
        assert p.returncode == 0, err[-2000:]
        outs.append(tuple(json.loads(o.strip().splitlines()[-1])))
    assert sorted(outs) == [("ok", "provisioned"), ("refused", "limits_exceeded")], outs
    assert provisioning_count(16) == 1
    RESULTS["processes"] = "2 OS processes x max_active_deployments=1 -> 1 provisioning, 1 limits_exceeded"


def test_override_covers_only_recorded_violations():
    """An admin override recorded at approval covers those violation codes, not a new one at the launch gate."""
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    guards.set_limits(17, {"max_price_per_gpu_hour": 0.5}, by="t", reason="t")
    (rr, q), = pending_routes(17, 1)
    code, res = engine.approve(rr, OPERATOR, quote_id=q, override_limits=True, reason="pilot price ok")
    assert res["status"] == "provisioned"
    d = ec.dep(res["deployment"]["deployment_id"])
    assert d.override_limits and {v["code"] for v in d.limit_violations} == {"max_price_per_gpu_hour"}


# --------------------------------------------------------------------------
# B. SSH access
# --------------------------------------------------------------------------

def _rsa_pub(bits):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    pub = k.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                           serialization.NoEncryption()).decode()
    return pub, priv


def test_public_key_validation():
    import base64
    import hashlib
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec as ecc

    pk = ec.pubkey(1)
    r = credentials.parse_public_key(pk)
    blob = base64.b64decode(pk.split()[1])
    assert r["fingerprint"] == "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    assert r["type"] == "ssh-ed25519" and r["comment"] == "me@laptop"
    good_rsa, priv = _rsa_pub(2048)
    assert credentials.parse_public_key(good_rsa)["bits"] == 2048
    e = ecc.generate_private_key(ecc.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    assert credentials.parse_public_key(e)["type"] == "ecdsa-sha2-nistp256"
    weak, _ = _rsa_pub(1024)
    bad = {"rsa 1024": weak, "private": priv, "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",
           "options": 'command="/bin/evil" ' + pk, "from=": 'from="1.2.3.4" ' + pk, "two lines": pk + "\n" + ec.pubkey(2),
           "dsa": "ssh-dss AAAAB3NzaC1kc3MAAACBAP" + "A" * 60, "zeros": "ssh-ed25519 " + "A" * 68,
           "type mismatch": "ssh-rsa " + pk.split()[1], "empty": "  ", "junk": "not a key"}
    for name, v in bad.items():
        try:
            credentials.parse_public_key(v)
            raise AssertionError(f"accepted {name}")
        except credentials.SSHKeyError as exc:
            msg = str(exc)
            assert v.strip()[:40] not in msg or len(v.strip()) < 12, (name, msg)
            if name in ("private", "pem"):
                assert "PRIVATE key" in msg, msg
            if name == "rsa 1024":
                assert "2048" in msg


def test_ssh_customer_keys_isolated_and_operator_key_never_on_customers():
    ec.reset()
    saved = settings.routing_launch_defaults
    settings.routing_launch_defaults = {"syn_a": {"ssh_public_key": OPKEY, "ssh_key": "operator-key", "image": "img"}}
    try:
        ec.mode("LIVE")
        ec.enable("syn_a", live=True)
        ka, kb = ec.pubkey(101, "alice@a"), ec.pubkey(202, "bob@b")
        ec.Fake.DELAY = 0.2
        outs = run_threads([lambda: engine.route(ec.spec("syn_a", launch={"ssh_public_key": ka}), ec.key(18))[1],
                            lambda: engine.route(ec.spec("syn_a", launch={"ssh_public_key": kb}), ec.key(19))[1]])
        ec.Fake.DELAY = 0.0
        assert all(o["status"] == "provisioned" for o in outs), [o["status"] for o in outs]
        by_dep = {o["deployment"]["deployment_id"]: o for o in outs}
        launches = {name: (pub, ref) for _, name, pub, ref in ec.Fake.LAUNCHES}
        assert len(launches) == 2
        for o, key_ in zip(outs, (ka, kb)):
            dep_id = o["deployment"]["deployment_id"]
            pub, ref = launches["og-" + dep_id]
            assert pub == key_ and ref is None, "each launch carries exactly its own customer's key"
            other = kb if key_ == ka else ka
            assert other.split()[1] not in str(launches["og-" + dep_id])
            assert OPKEY.split()[1] not in str(launches["og-" + dep_id]) and "operator-key" not in str(launches["og-" + dep_id])
            sa = o["ssh_access"]
            assert sa["customer_key_fingerprint"] == credentials.parse_public_key(key_)["fingerprint"]
            assert sa["operator_access"] == "NONE", sa
            row = ec.dep(dep_id)
            assert row.ssh_key_fingerprint == sa["customer_key_fingerprint"] and row.operator_access == "none"
            view = deployments.public(dep_id)
            assert view["ssh_access"] == {"customer_key_fingerprint": row.ssh_key_fingerprint, "operator_access": "NONE"}
        assert len(by_dep) == 2
        # a customer with no key gets no key (never the operator's)
        code, o = engine.route(ec.spec("syn_a"), ec.key(20))
        _, name, pub, ref = ec.Fake.LAUNCHES[-1]
        assert o["status"] == "provisioned" and pub is None and ref is None
        assert o["ssh_access"]["customer_key_fingerprint"] is None and o["ssh_access"]["operator_access"] == "NONE"
        # a customer submitting the operator's own key material is refused at the last moment
        code, o = engine.route(ec.spec("syn_a", launch={"ssh_public_key": OPKEY}), ec.key(20))
        assert o["status"] == "pending_approval" and "operator" in (o["reason"] or ""), o["reason"]
        # validation launches use the operator key
        ec.validation_ready("syn_a")
        rr = engine.create_validation_route("syn_a", None, by="operator")
        d = deployments.for_request(rr)
        code, res = engine.approve(rr, OPERATOR, quote_id=d.quote_id)
        _, name, pub, ref = ec.Fake.LAUNCHES[-1]
        assert name == "og-" + d.deployment_id and pub == OPKEY and ref == "operator-key"
        assert res["ssh_access"]["operator_access"] == "validation_operator_key"
    finally:
        settings.routing_launch_defaults = saved
        ec.Fake.DELAY = 0.0


def test_operator_defaults_allowlisted_on_customer_launches_every_adapter():
    """An operator default startup_script that writes an authorized_keys line, a default env, user_data,
    cloud-init or keys never reach a customer launch spec, on any adapter; validation launches keep them."""
    evil = "#!/bin/sh\necho '" + OPKEY + "' >> /root/.ssh/authorized_keys"
    poison = {"startup_script": evil, "user_data": evil, "cloud_init": evil, "env": {"OPS_BACKDOOR": "1"},
              "ssh_key": "operator-key", "ssh_public_key": OPKEY, "authorized_keys": OPKEY,
              "image": "img-1", "disk_gb": 100, "environments": {"CANADA-1": "env-ca"}}
    saved = settings.routing_launch_defaults
    try:
        names = sorted(adapters.ADAPTERS)
        assert {"lambda", "runpod", "vast", "hyperstack", "verda", "digitalocean", "crusoe"} <= set(names), names
        for p in names:
            settings.routing_launch_defaults = {p: dict(poison)}
            cls = adapters.get(p)
            for req in (None, {"ssh_public_key": ec.pubkey(4)}):
                spec, problem = engine.launch_spec_for(p, req, purpose="customer", credential_source="opengrid",
                                                       adapter_cls=cls)
                if spec is None:
                    assert problem.startswith("ssh_key_registration_unsupported"), (p, problem)
                    continue
                dump = repr({k: v for k, v in spec.__dict__.items() if k != "defaults_withheld"})
                assert spec.startup_script is None and spec.env == {} and spec.ssh_key is None, (p, spec)
                assert OPKEY.split()[1] not in dump and "authorized_keys" not in dump and "OPS_BACKDOOR" not in dump, p
                assert not set(spec.extra) & {"user_data", "cloud_init", "authorized_keys", "env"}, (p, spec.extra)
                assert spec.image == "img-1" and spec.disk_gb == 100 and spec.extra.get("environments"), (p, spec)
                assert set(spec.defaults_applied) == {"image", "disk_gb", "environments"}, spec.defaults_applied
                assert {"startup_script", "env", "ssh_key", "ssh_public_key"} <= set(spec.defaults_withheld)
            spec, _ = engine.launch_spec_for(p, None, purpose="validation", credential_source="opengrid", adapter_cls=cls)
            assert spec.startup_script == evil and spec.ssh_key == "operator-key", "validation keeps operator defaults"
        # a customer's own startup_script is theirs: allowed
        spec, _ = engine.launch_spec_for("lambda", {"startup_script": "echo hi"}, purpose="customer",
                                         credential_source="opengrid", adapter_cls=adapters.get("lambda"))
        assert spec.startup_script == "echo hi"
        # end to end: what was applied is recorded in the launch summary
        ec.reset()
        ec.mode("LIVE")
        ec.enable("syn_a", live=True)
        settings.routing_launch_defaults = {"syn_a": dict(poison)}
        code, o = engine.route(ec.spec("syn_a", launch={"ssh_public_key": ec.pubkey(8)}), ec.key(24))
        assert o["status"] == "provisioned"
        with normalize.SessionLocal() as s:
            summ = s.execute(text("SELECT request_summary FROM provision_attempts WHERE deployment_id = :d"),
                             {"d": o["deployment"]["deployment_id"]}).scalar()
        assert summ["operator_defaults_applied"] == ["disk_gb", "environments", "image"], summ
        assert "startup_script" in summ["operator_defaults_withheld"] and summ["env_names"] == []
    finally:
        settings.routing_launch_defaults = saved


def test_forced_account_key_provider_blocked_without_override():
    ec.reset()
    for forces in ("YES", "UNKNOWN", None):
        class Forced(ec.Fake):
            CAPABILITIES = ec.fake_caps(forces) if forces else ec.Capabilities()   # None: not declared
        adapters.register("syn_d", Forced)
        try:
            ec.mode("LIVE")
            ec.enable("syn_d", live=True)
            code, o = engine.route(ec.spec("syn_d", launch={"ssh_public_key": ec.pubkey(5)}), ec.key(21))
            assert o["status"] == "pending_approval" and not ec.calls("provision"), (forces, o["status"])
            assert o["ssh_access"]["operator_access"].startswith("blocked:"), o["ssh_access"]
            rr, q = o["route_request_id"], o["quote"]["quote_id"]
            r = approve_quiet(rr, q)
            assert r == ("refused", "operator_access_override_required"), r
            assert approve_quiet(rr, q, allow_provider_account_keys=True) == ("refused", "reason_required")
            assert not ec.calls("provision")
            code, res = engine.approve(rr, OPERATOR, quote_id=q, allow_provider_account_keys=True,
                                       reason="partner accepts provider account keys")
            assert res["status"] == "provisioned"
            row = ec.dep(res["deployment"]["deployment_id"])
            assert row.operator_access == "provider_forced_account_key:override_by:operator", row.operator_access
            assert res["ssh_access"]["operator_access"] == row.operator_access
            assert any(x["action"] == "override_operator_access" for x in control.recent_log(10))
            ec.reset()
        finally:
            adapters.register("syn_d", ec.Fake)


def test_api_rejects_private_keys_without_echo():
    ec.reset()
    ec.mode("SUPERVISED")
    ec.enable("syn_a")
    main, c = ec._client()
    _, priv = _rsa_pub(2048)
    try:
        ec._as(main, ec.key(22))
        for field in ("ssh_public_key", "ssh_key", "ssh_private_key"):
            r = c.post("/v1/route", json={**ec.BODY, "launch": {field: priv}}, headers={"Idempotency-Key": f"pk-{field}"})
            assert r.status_code == 422, (field, r.status_code)
            body = r.text
            assert "PRIVATE KEY" not in body and priv.splitlines()[1][:30] not in body, (field, body[:300])
        r = c.post("/v1/route", json={**ec.BODY, "launch": {"ssh_public_key": _rsa_pub(1024)[0]}},
                   headers={"Idempotency-Key": "weak"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "ssh_public_key_invalid"
        assert ec.count("route_requests", "request::text LIKE '%PRIVATE%'") == 0
        r = c.post("/v1/route", json={**ec.BODY, "launch": {"ssh_public_key": ec.pubkey(7)}},
                   headers={"Idempotency-Key": "good"})
        assert r.status_code == 202, r.text
        ap = r.json()["data"]["approval"]
        assert ap["ssh_access"]["customer_key_fingerprint"].startswith("SHA256:") and ap["ssh_access"]["operator_access"] == "NONE"
        assert ap["runtime"]["effective_max_runtime_minutes"] == 60
    finally:
        main.app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# F. lifecycle timestamps
# --------------------------------------------------------------------------

def test_requested_termination_at_while_launch_unresolved():
    ec.reset()
    ec.mode("LIVE")
    ec.enable("syn_a", live=True)
    ec.Fake.MODE["syn_a"] = "timeout"
    code, o = engine.route(ec.spec("syn_a"), ec.key(23))
    dep_id = o["deployment"]["deployment_id"]
    assert ec.dep(dep_id).status == "provider_timeout"
    out = deployments.terminate(dep_id, ec.key(23))
    row = ec.dep(dep_id)
    assert row.requested_termination_at is not None and out["requested_termination_at"] == row.requested_termination_at.isoformat()
    for k in ("provider_created_at", "provider_running_at", "provider_terminated_at", "billable_start", "billable_end",
              "billable_basis", "effective_max_runtime_minutes", "runtime_ceiling_source", "ssh_access"):
        assert k in out, k
    first = row.requested_termination_at
    deployments.terminate(dep_id, ec.key(23))
    assert ec.dep(dep_id).requested_termination_at == first, "the first request time is kept"


# --------------------------------------------------------------------------
# I. validation gate
# --------------------------------------------------------------------------

def test_validation_gate_each_condition():
    ec.reset()
    ec.validation_ready("syn_c")
    base = dict(gpu_count=1, hourly_price=1.2, runtime_minutes=30, admin=True)
    assert validation.preconditions("syn_c", **base)["ok"], validation.preconditions("syn_c", **base)["failed"]

    def fails(code, **kw):
        out = validation.preconditions("syn_c", **{**base, **kw})
        codes = {f["code"] for f in out["failed"]}
        assert code in codes and all(f["reason"] for f in out["failed"]), (code, out["failed"])

    fails("one_gpu", gpu_count=2)
    fails("price_cap", hourly_price=3.01)
    fails("runtime_cap", runtime_minutes=31)
    fails("runtime_cap", deadline_set=False)
    fails("admin_approval", admin=False)
    settings.validation_allowed_providers = ["lambda"]
    fails("provider_allowed")
    settings.validation_allowed_providers = ["syn_c"]
    control.set_mode("LIVE", reason="t", by="t")
    fails("mode_supervised")
    control.set_mode("SUPERVISED", reason="t", by="t")
    control.kill_provider("syn_c", "incident", "t")
    fails("provider_kill_switch")
    control.unkill_provider("syn_c", "ok", "t")
    with normalize.SessionLocal.begin() as s:   # drills older than the window
        s.execute(text("UPDATE execution_control_log SET at = at - interval '8 days' WHERE action IN "
                       "('kill_all','set_mode','kill_provider','unkill_provider')"))
    fails("global_kill_switch")
    fails("provider_kill_switch")
    ec.validation_ready("syn_c")
    control.record_job_health("routing_tracker", ok=False, error="RuntimeError: db down")
    fails("tracker_worker")
    control.record_job_health("routing_tracker", ok=True)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE execution_controls SET value = jsonb_set(value, '{last_ok_at}', to_jsonb(CAST(:t AS text))) "
                       "WHERE key = 'job:reconcile'"), {"t": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
    fails("reconciliation_worker")
    control.record_job_health("reconcile", ok=True)
    from store.reconcile import ReconciliationRun
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:   # a pass where only syn_c's list failed
        s.add(ReconciliationRun(started_at=now, finished_at=now, trigger="test", provider=None, status="partial",
                                providers={"syn_c": {"listed": 0, "list_errors": 1}}, findings=[], counts={}))
    fails("reconciliation_worker")
    with normalize.SessionLocal.begin() as s:   # a pass where only ANOTHER provider failed: fine for syn_c
        s.add(ReconciliationRun(started_at=now + timedelta(seconds=1), finished_at=now + timedelta(seconds=1),
                                trigger="test", provider=None, status="partial",
                                providers={"syn_c": {"listed": 0, "list_errors": 0, "credentials": 1},
                                           "vast": {"error": "boom"}}, findings=[], counts={}))
    assert validation.preconditions("syn_c", **base)["ok"]
    item = checklist.reconciliation_running("syn_c")
    assert item["status"] == "green", item
    assert checklist.reconciliation_running("vast")["status"] == "red"
    control.ops_channel_configured = lambda: False
    fails("ops_alerts")
    control.ops_channel_configured = lambda: True
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM execution_control_log WHERE action = 'ops_test_alert'"))
    fails("ops_alerts")
    ec.validation_ready("syn_c")
    # an active validation deployment blocks the next one at start; the refusal lists every failed condition
    rr = engine.create_validation_route("syn_c", None, by="operator")
    engine.approve(rr, OPERATOR, quote_id=deployments.for_request(rr).quote_id)
    try:
        validation.start_validation("syn_c", by="operator")
        raise AssertionError("second validation started")
    except validation.PreconditionsFailed as exc:
        assert "one_active_validation" in {f["code"] for f in exc.failed}
    # the gate is re-checked at approval: a precondition broken after start blocks the approval
    deployments.terminate(deployments.for_request(rr).deployment_id, OPERATOR)
    ec.confirm_terminated(deployments.for_request(rr).deployment_id)
    rr2 = engine.create_validation_route("syn_c", None, by="operator")
    control.record_job_health("reconcile", ok=False, error="boom")
    r = approve_quiet(rr2, deployments.for_request(rr2).quote_id)
    assert r == ("refused", "validation_preconditions_failed") and deployments.for_request(rr2).status == "pending_approval"


def test_ops_test_alert_and_preconditions_endpoints():
    ec.reset()
    ec.validation_ready("syn_c")
    from alerts import ops
    real = ops.alert
    main, c = ec._client()
    try:
        ec._as(main, OPERATOR)
        ops.alert = lambda *a, **k: {"recorded": True, "delivered": False}
        r = c.post("/v1/admin/ops/test-alert", json={"reason": "drill"})
        assert r.status_code == 200 and r.json()["data"]["delivered"] is False and r.json()["data"]["ok"] is False, r.text
        ops.alert = lambda *a, **k: {"recorded": True, "delivered": True}
        r = c.post("/v1/admin/ops/test-alert", json={"reason": "drill"})
        assert r.json()["data"]["delivered"] is True and r.json()["data"]["ok"] is True
        r = c.get("/v1/admin/execution/validation/preconditions", params={"provider": "syn_c"})
        assert r.status_code == 200 and {x["code"] for x in r.json()["data"]["checks"]} >= set(validation.PRECONDITIONS)
        ec._as(main, ec.key(7))
        assert c.post("/v1/admin/ops/test-alert", json={}).status_code == 403
    finally:
        ops.alert = real
        main.app.dependency_overrides.clear()


def test_checklist_tracker_item():
    import jobs
    saved = dict(jobs.JOBS)
    try:
        jobs.JOBS.pop("routing_tracker", None)
        with normalize.SessionLocal.begin() as s:
            s.execute(text("DELETE FROM execution_controls WHERE key = 'job:routing_tracker'"))
        assert checklist.tracker_running()["status"] == "unknown"
        control.record_job_health("routing_tracker", ok=True)
        assert checklist.tracker_running()["status"] == "green"
        control.record_job_health("routing_tracker", ok=False, error="x")
        assert checklist.tracker_running()["status"] == "red"
        ids = [i["id"] for i in checklist.checklist("syn_c", probe=False)["items"]]
        assert "tracker_running" in ids and "reconciliation_running" in ids
        # heartbeats from the real wrapper
        j = jobs.Job("routing_tracker", lambda: {"ok": 1}, 60)
        jobs.JOBS["routing_tracker"] = j
        control.install_job_heartbeats(("routing_tracker",))
        j.fn()
        assert control.job_health("routing_tracker", 60)["ok"]
    finally:
        jobs.JOBS.clear()
        jobs.JOBS.update(saved)


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
    saved_cfg = control.ops_channel_configured
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
        print("\nconcurrency proof:")
        for k, v in RESULTS.items():
            print(f"  {k}: {v}")
        print(f"\n{len(tests) - failed}/{len(tests)} passed")
        if failed:
            sys.exit(1)
    finally:
        control.ops_channel_configured = saved_cfg
        for p in ec.SYN:
            adapters.unregister(p)
        settings.routing_live_provisioning = False
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)


if __name__ == "__main__":
    main()
