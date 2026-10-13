"""OpenGrid Jobs: state machine, sandbox (SIMULATED) flow, the real path through the EXISTING router / approval /
launch / tracker / reconcile with the real Lambda adapter against a fake Lambda API (tests/bench_fixtures.LambdaSim),
runner callbacks, termination + reconciliation, and the failure paths.

Scratch DB og_test_jobs (re-created once; accounts fresh per scenario). No network, no uvicorn, no real provider.
Run:  .venv/Scripts/python tests/test_jobs.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_fixtures as bf  # noqa: E402
from bench_fixtures import LambdaSim, q, scalar  # noqa: E402

import httpx  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import OPERATOR, Principal  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import adapters, deployments, engine, reconcile, tracker  # noqa: E402
from sqlalchemy import text  # noqa: E402

from jobs import orchestrator as O  # noqa: E402
from jobs import states as S  # noqa: E402
from jobs import sync, tokens  # noqa: E402
from jobs import artifacts as A  # noqa: E402

DB = "og_test_jobs"
G = "NVIDIA H100 80GB SXM5"
SKU = bf.SKU
PRICE = 2.49
LAMBDA_KEY = "test-lambda-key-jobs-0123"
SCOPES = frozenset({"data:read", "route:preview", "route:execute", "deployments:read", "deployments:write"})
PK = bf.PK

S_ = None
SIM: "JobSim | None" = None


class JobSim(LambdaSim):
    """LambdaSim + the SSH-key list/delete endpoints the per-deployment key cleanup uses."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.keys: dict[str, str] = {}
        self.price_cents = int(round(PRICE * 100))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/api/v1/ssh-keys" and method == "POST":
            body = json.loads(request.content)
            self.log.append((method, path, {"name": body.get("name")}))
            kid = "k-" + hashlib.sha256(body["name"].encode()).hexdigest()[:6]
            self.keys[kid] = body["name"]
            return httpx.Response(200, json={"data": {"id": kid, "name": body["name"], "public_key": body["public_key"]}})
        if path == "/api/v1/ssh-keys" and method == "GET":
            return httpx.Response(200, json={"data": [{"id": k, "name": n, "public_key": PK} for k, n in self.keys.items()]})
        if path.startswith("/api/v1/ssh-keys/") and method == "DELETE":
            self.keys.pop(path.rsplit("/", 1)[-1], None)
            return httpx.Response(200, json={"data": {}})
        return super().__call__(request)

    def user_data(self) -> list[str]:
        return [b.get("user_data") for b in self.creates() if b.get("user_data")]


# --------------------------------------------------------------------------
# World
# --------------------------------------------------------------------------

def world(*, mode="SUPERVISED", validated=True, supervised=True, price=PRICE, boot_polls=0) -> JobSim:
    """Fresh scratch DB tables (once), fresh fake Lambda, market, controls and settings."""
    global S_, SIM
    if S_ is None:
        S_ = bf.db(DB)
    normalize.SessionLocal = S_
    with S_.begin() as s:
        for t in ("jobs", "job_events", "job_runner_events", "job_logs", "job_metrics", "job_artifacts", "job_tokens",
                  "deployments", "deployment_events", "provision_attempts", "quotes", "idempotency_keys",
                  "execution_records", "route_requests", "routing_decisions", "provider_resources",
                  "orphan_resources", "deployment_watch", "usage_slices", "usage_records", "ops_alert_state",
                  "reconciliation_runs"):
            try:
                s.execute(text(f"DELETE FROM {t}"))
            except Exception:  # noqa: BLE001
                raise
    row = bf.listing("lambda", price, gpu=G, region="us-east-1", lid="lambda:" + SKU)
    row.sku = SKU
    bf.market(G, [row])
    SIM = JobSim(boot_polls=boot_polls)
    adapters.TRANSPORTS["lambda"] = SIM.transport()
    settings.lambda_api_key = LAMBDA_KEY
    settings.api_key_pepper = "test-pepper-jobs"
    settings.job_callback_base_url = "https://jobs.test.opengrid.example"
    settings.ops_alert_webhook_url = None
    settings.ops_alert_webhook_secret = None
    settings.deployments_webhook_url = None
    settings.routing_max_attempts = 1
    settings.runtime_default_minutes = 60
    settings.job_artifact_store = "postgres"
    settings.job_bootstrap_timeout_minutes = 20
    settings.default_max_active_deployments = 50
    A._OVERRIDE.clear()
    bf.mode(mode)
    bf.flags("lambda", validated=validated, live=False, supervised=supervised)
    reconcile._JOB_FAILS.clear()
    return SIM


def who(account_id: int | None = None) -> Principal:
    aid = account_id or bf.fresh_account()
    return Principal(kind="api_key", account_id=aid, key_id=aid * 10, scopes=SCOPES)


def body(**kw) -> dict:
    b = {"name": "opengrid-first-training-test", "job_type": "training", "template": "pytorch-mnist",
         "config": {"epochs": 2, "batch_size": 128, "learning_rate": 0.001, "seed": 42},
         "resources": {"gpu": "auto", "gpu_count": 1, "min_vram_gb": None, "region": "US", "strict_region": False,
                       "allow_spot": False},
         "routing": {"strategy": "CHEAPEST", "weights": None},
         "limits": {"max_price_per_gpu_hour_usd": 3, "max_total_cost_usd": 2, "max_runtime_minutes": 30},
         "ssh_public_key": None, "sandbox": False}
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(b.get(k), dict):
            b[k] = {**b[k], **v}
        else:
            b[k] = v
    return b


def job_row(job_id):
    from store.jobs import Job
    with normalize.SessionLocal() as s:
        return s.get(Job, job_id)


def dep_row(job_id):
    j = job_row(job_id)
    return deployments.load_row(j.deployment_id) if j.deployment_id else None


def token_for(job_id) -> str:
    """The token the provider received in user_data, recomputed from the stored nonce (test-side only)."""
    nonce = q("SELECT nonce FROM job_tokens WHERE job_id = :j AND revoked_at IS NULL ORDER BY id DESC LIMIT 1",
              j=job_id)[0][0]
    return tokens.derive(job_id, nonce)


def approve(job_id):
    j = job_row(job_id)
    d = deployments.load_row(j.deployment_id)
    return engine.approve(j.route_request_id, OPERATOR, quote_id=d.quote_id)


def workers(n=1, minutes=None):
    for _ in range(n):
        tracker.track()
        reconcile.run_once()
        sync.run_all()


def submit_real(w=None, **kw):
    w = w or who()
    code, out = O.submit(body(**kw), w)
    return code, out, w


def runner_events(job_id, evs, *, token_id=None):
    out = sync.ingest(job_id, evs, token_id=token_id)
    sync.sync_job(job_id)
    return out


def happy_events(start=1, epochs=2):
    seq = [start - 1]

    def e(typ, **data):
        seq[0] += 1
        return {"seq": seq[0], "type": typ, "at": "2026-10-13T10:00:00Z", "data": data}

    evs = [e("phase", phase="bootstrap_started", template="pytorch-mnist"),
           e("phase", phase="gpu_detected", gpus="NVIDIA H100 80GB HBM3, 550.54.15, 81559 MiB, 9.0\n"),
           e("phase", phase="torch_installed", env_source="pinned",
             versions=json.dumps({"python": "3.10.12", "torch": "2.4.1+cu121", "torchvision": "0.19.1+cu121"})),
           e("phase", phase="cuda_available", cuda=json.dumps({"cuda_available": True, "device_count": 1,
                                                                "name": "NVIDIA H100 80GB HBM3", "capability": "9.0"})),
           e("phase", phase="device_selected", device="cuda", gpu_name="NVIDIA H100 80GB HBM3",
             compute_capability="9.0", torch="2.4.1+cu121"),
           e("phase", phase="dataset_ready", dataset="MNIST"),
           e("phase", phase="training_started", device="cuda", param_device="cuda:0", parameters=1199882)]
    for ep in range(1, epochs + 1):
        evs.append(e("metric", epoch=ep, train_loss=0.21 / ep, test_loss=0.06 / ep, test_accuracy=0.98 + ep / 1000,
                     batch_device="cuda:0", samples_per_second=41000.0))
        evs.append(e("log", stream="stdout", line=f"epoch {ep} done"))
    evs.append(e("phase", phase="saving_artifacts"))
    return evs, seq[0]


ART = {"model.pt": b"\x80\x02fake-state-dict" * 10, "config.json": b'{"epochs": 2}', "metrics.json": b'{"final": {}}',
       "env.json": b'{"gpu_name": "NVIDIA H100 80GB HBM3"}', "manifest.json": b"{}"}


def upload_all(job_id, names=None):
    out = {}
    for n, data in ART.items():
        if names is not None and n not in names:
            continue
        meta = A.store().put(job_id, n, data, hashlib.sha256(data).hexdigest())
        sync.on_artifact(job_id, meta)
        out[n] = {"sha256": meta["sha256"], "bytes": meta["size"]}
    return out


def result_event(seq, manifest):
    return {"seq": seq, "type": "result", "at": "2026-10-13T10:05:00Z",
            "data": {"outcome": "succeeded", "train_loss": 0.1, "test_loss": 0.03, "test_accuracy": 0.982,
                     "param_l2_change": 12.5, "device": "cuda", "artifacts": manifest}}


def to_running(job_id):
    """Approve (the existing endpoint's engine.approve) and let the tracker see the instance running."""
    code, out = approve(job_id)
    assert out["launch"]["launched"], out
    workers(2)
    j = job_row(job_id)
    assert j.status == "initializing", j.status
    return out


def drive_to_end(job_id, n=6):
    for _ in range(n):
        if job_row(job_id).status == "reconciled":
            break
        workers(1)
    return job_row(job_id)


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------

def test_state_machine():
    assert set(S.ALLOWED) == set(S.STATES)
    assert S.can("pending_approval", "approved") and not S.can("reconciled", "training")
    assert not S.can("training", "pending_approval"), "never backwards to pre-launch"
    assert S.path_to("pending_approval", "initializing") == ["provisioning", "initializing"]
    assert S.path_to("reconciled", "training") == []
    for st in S.FINISHED:
        assert S.ALLOWED[st] == (), f"{st} must be absorbing"
    for st in S.WORKLOAD_FAILED:
        assert "terminating" in S.ALLOWED[st], f"a failed workload still terminates its GPU ({st})"
        assert st not in S.FINISHED, "a failed workload is never finished while its GPU may exist"
    world()
    from store.jobs import Job
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        j = Job(job_id="job-00000000aaaa0001", name="t", job_type="training", template="pytorch-mnist", config={},
                spec={}, status="created", sandbox=False, created_at=now, updated_at=now)
        s.add(j)
        s.flush()
        assert S.transition(s, j, "validating", actor="system", reason="x")
        assert not S.transition(s, j, "validating", actor="system"), "same state is a no-op"
        try:
            S.transition(s, j, "reconciled", actor="system")
            raise AssertionError("illegal transition accepted")
        except S.IllegalJobTransition:
            pass
        try:
            S.transition(s, j, "quoted", actor="robot")
            raise AssertionError("unknown actor accepted")
        except ValueError:
            pass
        S.transition(s, j, "validation_failed", actor="system", reason="bad", outcome="failed", failure_code="x")
        assert j.finished_at is not None and j.outcome == "failed"
    n = scalar("SELECT count(*) FROM job_events WHERE job_id = 'job-00000000aaaa0001'")
    assert n == 2, n


# --------------------------------------------------------------------------
# Preview / sandbox
# --------------------------------------------------------------------------

def test_preview_shapes():
    world()
    out = O.preview(body(), who())
    for k in ("market_best", "execution_ready", "alternatives", "exclusions", "blockers", "estimate", "submission",
              "request", "template"):
        assert k in out, k
    assert out["execution_ready"]["provider"] == "lambda", out["execution_ready"]
    assert out["submission"]["can_submit"] and out["submission"]["would"] == "pending_approval", out["submission"]
    assert out["submission"]["ssh"]["mode"] == "ephemeral"
    assert "ssh_public_key" not in out["request"]
    assert SIM.creates() == [], "preview never launches"
    # nothing executable: PREVIEW_ONLY says exactly why
    world(mode="PREVIEW_ONLY")
    out = O.preview(body(), who())
    assert out["execution_ready"] is None and out["market_best"] is not None
    r = out["submission"]["refusal"]
    assert r["code"] == "execution_disabled", r
    # simulated adapter: provider_unvalidated
    world(validated=False)
    out = O.preview(body(), who())
    assert out["submission"]["refusal"]["code"] == "provider_unvalidated", out["submission"]
    assert any("run the supervised validation first" in b["reason"] for b in out["blockers"]), out["blockers"]


def test_sandbox_end_to_end():
    world(mode="PREVIEW_ONLY")           # nothing real can run: the sandbox still works, simulated
    code, out = O.submit(body(sandbox=True), who())
    assert code == 201, code
    assert out["sandbox"] and out["simulated"] and "SIMULATED" in out["simulated_note"]
    assert out["status"] == "training_completed" and out["outcome"] == "simulated", (out["status"], out["outcome"])
    assert out["finished"] and out["resource"]["disposition"] == "simulated_none"
    assert out["resource"]["deployment_id"] is None and out["approval"] is None
    assert all(c["status"] == "simulated" for c in out["checks"]), [c for c in out["checks"] if c["status"] != "simulated"]
    assert SIM.log == [], "a sandbox job never calls the provider"
    m = O.metrics(out["job_id"], who(out and job_row(out["job_id"]).account_id))
    assert m["simulated"] and all(p["source"] == "simulated" for s in m["series"].values() for p in s)
    arts = O.artifacts_list(out["job_id"], OPERATOR)
    assert [a["name"] for a in arts] == ["simulated-report.json"] and arts[0]["simulated"]
    lg = O.logs(out["job_id"], OPERATOR, after=None, limit=100)
    assert any("[SIMULATED]" in i["line"] for i in lg["items"])
    assert scalar("SELECT count(*) FROM deployments") == 0
    assert scalar("SELECT count(*) FROM job_runner_events WHERE source <> 'simulated'") == 0


def test_refusals_are_not_faked():
    world(mode="PREVIEW_ONLY")
    try:
        O.submit(body(), who())
        raise AssertionError("submitted with execution disabled")
    except O.JobRefused as r:
        assert r.code == "execution_disabled" and r.status == 422 and r.extra["blockers"], r.detail()
    assert scalar("SELECT count(*) FROM jobs") == 0, "a refused submission creates no job"
    world()
    try:
        O.submit(body(limits={"max_total_cost_usd": 0.5}), who())
        raise AssertionError("over budget accepted")
    except O.JobRefused as r:
        assert r.code in ("budget_exceeded", "no_eligible_provider"), r.detail()
    try:
        O.submit(body(job_type="inference"), who())
        raise AssertionError("unsupported type accepted")
    except O.JobRefused as r:
        assert r.code == "unsupported_workload_type", r.detail()
    try:
        O.submit(body(config={"epochs": 999}), who())
        raise AssertionError("out-of-bounds config accepted")
    except O.JobRefused as r:
        assert r.code == "config_out_of_bounds", r.detail()
    assert SIM.creates() == []


# --------------------------------------------------------------------------
# The real path (fake Lambda)
# --------------------------------------------------------------------------

class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage() + " " + json.dumps(record.__dict__, default=str))
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


def db_text_dump() -> str:
    tables = [r[0] for r in q("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")]
    out = []
    with normalize.SessionLocal() as s:
        for t in tables:
            for row in s.execute(text(f'SELECT t::text FROM "{t}" t')):
                out.append(row[0])
    return "\n".join(out)


def test_real_path_happy():
    world()
    cap = Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        code, out, w = submit_real()
        assert code == 202, code
        jid = out["job_id"]
        assert out["status"] == "pending_approval", out["status"]
        assert out["approval"]["required"] and out["approval"]["approve"].startswith("POST /v1/route/rr")
        assert out["resource"]["disposition"] == "not_launched_yet"
        assert out["ssh"]["mode"] == "ephemeral" and out["ssh"]["fingerprint"].startswith("SHA256:")
        assert SIM.creates() == [], "nothing launches before approval"
        d = dep_row(jid)
        assert d.status == "pending_approval" and d.purpose == "customer"
        assert (d.launch or {}).get("startup_script") == f"opengrid-job:{jid}", d.launch
        # the budget binds through the existing max-price cap: quote x billed runtime <= max_total_cost
        rr = q("SELECT request FROM route_requests WHERE id = :r", r=d.route_request_id)[0][0]
        assert rr["max_price_per_gpu_hour"] * 0.5 <= 2.0 + 1e-6, rr["max_price_per_gpu_hour"]
        # approval: the EXISTING engine.approve -> launch renders the bootstrap
        to_running(jid)
        assert len(SIM.creates()) == 1
        ud = SIM.user_data()[0]
        assert ud.startswith("#!/bin/bash") and "opengrid-job:" not in ud
        j = job_row(jid)
        assert j.bootstrap_sha256 == hashlib.sha256(ud.encode()).hexdigest()
        assert j.bootstrap_template_version and j.bootstrap_template_version.startswith("pytorch-mnist@")
        tok = token_for(jid)
        assert tok in ud, "the provider received the derived token"
        # runner callbacks
        evs, last = happy_events()
        r = runner_events(jid, evs)
        assert r["accepted"] == len(evs) and r["duplicates"] == 0
        assert job_row(jid).status == "saving_artifacts", job_row(jid).status
        r = runner_events(jid, evs[:3])
        assert r["accepted"] == 0 and r["duplicates"] == 3, "duplicate seq ignored"
        manifest = upload_all(jid)
        runner_events(jid, [result_event(last + 1, manifest)])
        res = sync.complete(jid, {"outcome": "succeeded", "summary": {"exit_code": 0}})
        assert res["job_status"] in ("training_completed", "terminating"), res
        j = job_row(jid)
        assert j.outcome == "succeeded", j.outcome
        d = dep_row(jid)
        assert d.status == "terminating" and d.terminate_requested_at is not None, d.status
        assert len(SIM.terminates()) == 1, "complete -> the existing terminate"
        j = drive_to_end(jid)
        assert j.status == "reconciled", j.status
        assert j.outcome == "succeeded" and j.finished_at is not None
        d = dep_row(jid)
        assert d.status == "terminated" and d.reconciled_at is not None
        full = O.get(jid, w)
        st = {c["key"]: c["status"] for c in full["checks"]}
        assert all(v == "pass" for v in st.values()), st
        assert full["resource"]["disposition"] == "terminated_confirmed"
        # tokens revoked at the end; late callbacks refused
        assert scalar("SELECT count(*) FROM job_tokens WHERE job_id = :j AND revoked_at IS NULL", j=jid) == 0
        try:
            tokens.verify(tokens.header_value(jid, tok), jid)
            raise AssertionError("revoked token accepted")
        except HTTPException as exc:
            assert exc.status_code == 401 and exc.detail["code"] == "runner_token_revoked", exc.detail
        # the token / rendered script are nowhere in the DB or the logs
        dump = db_text_dump()
        assert tok not in dump, "runner token stored in the database"
        assert "OG_B64_EOF" not in dump, "rendered bootstrap stored in the database"
        logs = "\n".join(cap.lines)
        assert tok not in logs, "runner token logged"
        assert "OG_B64_EOF" not in logs, "rendered bootstrap logged"
        assert len(SIM.creates()) == 1
        inv = bf.invariants([j.deployment_id], sim=SIM)
        assert inv["creates"] == 1
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)


def test_customer_key():
    world()
    code, out, w = submit_real(ssh_public_key=PK)
    assert out["ssh"]["mode"] == "customer" and out["ssh"]["fingerprint"].startswith("SHA256:")
    assert "ssh_public_key" not in out["request"]


def test_bootstrap_timeout():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    to_running(jid)
    from datetime import datetime, timezone
    later = datetime.now(timezone.utc) + timedelta(minutes=settings.job_bootstrap_timeout_minutes + 1)
    r = sync.sync_job(jid, now=later)
    j = job_row(jid)
    assert j.failure_code == "bootstrap_timeout" and j.outcome == "failed", (j.status, j.failure_code)
    assert dep_row(jid).terminate_requested_at is not None and len(SIM.terminates()) == 1, r
    j = drive_to_end(jid)
    assert j.status == "reconciled" and j.outcome == "failed"


def _run_until_training(jid):
    to_running(jid)
    evs, last = happy_events()
    runner_events(jid, evs[:-1])
    assert job_row(jid).status == "training"
    return last


def test_training_failure_terminates():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    _run_until_training(jid)
    sync.complete(jid, {"outcome": "failed", "summary": {"reason": "training_failed", "exit_code": 2}})
    j = job_row(jid)
    assert j.failure_code == "training_failed" and j.outcome == "failed"
    assert len(SIM.terminates()) == 1, "a training failure ALSO terminates"
    j = drive_to_end(jid)
    assert j.status == "reconciled" and j.outcome == "failed"
    full = O.get(jid, w)
    assert full["resource"]["disposition"] == "terminated_confirmed"


def test_oom():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    last = _run_until_training(jid)
    runner_events(jid, [{"seq": last + 1, "type": "error", "at": None,
                         "data": {"code": "oom", "message": "CUDA out of memory"}}])
    j = job_row(jid)
    assert j.failure_code == "out_of_memory", (j.status, j.failure_code)
    assert len(SIM.terminates()) == 1
    sync.complete(jid, {"outcome": "oom", "summary": {}})
    assert drive_to_end(jid).status == "reconciled"


def test_cuda_init_failed():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    to_running(jid)
    runner_events(jid, [{"seq": 1, "type": "phase", "data": {"phase": "bootstrap_started"}},
                        {"seq": 2, "type": "error", "data": {"code": "cuda_unavailable", "message": "no CUDA"}}])
    j = job_row(jid)
    assert j.failure_code == "cuda_init_failed" and j.status in ("bootstrap_failed", "terminating"), (j.status, j.failure_code)
    assert {c["key"]: c["status"] for c in O.get(jid, w)["checks"]}["cuda_available"] == "fail"
    assert drive_to_end(jid).status == "reconciled"


def test_artifact_upload_failure():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    last = _run_until_training(jid)
    manifest = upload_all(jid, names=("config.json",))
    runner_events(jid, [result_event(last + 1, manifest)])
    sync.complete(jid, {"outcome": "succeeded", "summary": {}})
    j = job_row(jid)
    assert j.failure_code == "artifact_upload_failed" and j.outcome == "failed", (j.status, j.failure_code)
    assert "model.pt" in (j.failure_reason or "")
    assert len(SIM.terminates()) == 1
    assert drive_to_end(jid).outcome == "failed"


def test_no_success_without_result():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    _run_until_training(jid)
    upload_all(jid)
    sync.complete(jid, {"outcome": "succeeded", "summary": {}})
    j = job_row(jid)
    assert j.outcome == "failed" and j.failure_code == "result_missing", (j.outcome, j.failure_code)


def test_cancel_before_launch():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    code, res = O.cancel(jid, w)
    assert code == 200 and res["cancel"]["before_launch"], res["cancel"]
    j = job_row(jid)
    assert j.status == "approval_rejected" and j.outcome == "cancelled" and j.finished_at is not None
    assert dep_row(jid).status == "rejected"
    assert SIM.creates() == []
    try:
        approve(jid)
        raise AssertionError("approved a cancelled job")
    except HTTPException as exc:
        assert exc.status_code == 409, exc.detail
    assert SIM.creates() == []


def test_cancel_after_launch():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    _run_until_training(jid)
    code, res = O.cancel(jid, w)
    assert code == 202 and not res["cancel"]["before_launch"], res["cancel"]
    j = job_row(jid)
    assert j.outcome == "cancelled" and j.status in ("cancelling", "terminating"), j.status
    assert len(SIM.terminates()) == 1
    code2, res2 = O.cancel(jid, w)
    assert len(SIM.terminates()) == 1, "a second cancel does not re-terminate"
    j = drive_to_end(jid)
    assert j.status == "reconciled" and j.outcome == "cancelled", (j.status, j.outcome)


def test_ambiguous_provider_timeout():
    world()
    SIM.fault("launch", "timeout_after")         # the provider created it, the answer was lost
    code, out, w = submit_real()
    jid = out["job_id"]
    code, res = approve(jid)
    assert code == 202, res
    sync.sync_job(jid)
    assert job_row(jid).status == "provider_timeout", job_row(jid).status
    code, res = approve(jid)                      # a second approve never launches again
    assert res.get("already_approved"), res
    assert len(SIM.creates()) == 1
    workers(2)                                     # reconciliation adopts it by name
    assert len(SIM.creates()) == 1, "no second launch"
    j = job_row(jid)
    assert j.status in ("initializing", "provisioning"), j.status
    sync.complete(jid, {"outcome": "failed", "summary": {}})
    assert drive_to_end(jid, 8).status == "reconciled"
    assert len(SIM.creates()) == 1


def test_deadline_timeout():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    _run_until_training(jid)
    d = dep_row(jid)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE deployments SET terminate_deadline_at = now() - interval '1 minute' "
                       "WHERE deployment_id = :d"), {"d": d.deployment_id})
    sync.sync_job(jid)
    j = job_row(jid)
    assert j.failure_code == "training_timed_out" and j.status == "training_timed_out", (j.status, j.failure_code)
    assert len(SIM.terminates()) == 0, "the deadline itself is the reconciler's"
    j = drive_to_end(jid, 8)
    assert j.status == "reconciled" and j.outcome == "failed"
    assert dep_row(jid).termination_reason in ("deadline", "max_runtime", "auto_terminate", "deadline_reached",
                                                "max_runtime_reached") or dep_row(jid).status == "terminated"


def test_worker_restart_rebuild():
    """No job_sync ran while the approval, launch and boot happened: one pass rebuilds the job from the DB."""
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    approve(jid)
    tracker.track()
    tracker.track()
    assert dep_row(jid).status == "running"
    assert job_row(jid).status in ("pending_approval", "approved"), job_row(jid).status
    import importlib
    importlib.reload(sync)                       # a fresh process: nothing in memory survives
    sync.run_all()
    j = job_row(jid)
    assert j.status == "initializing", j.status
    kinds = [r[0] for r in q("SELECT to_status FROM job_events WHERE job_id = :j ORDER BY id", j=jid)]
    assert "provisioning" in kinds and kinds[-1] == "initializing", kinds


def test_out_of_order_and_late_callbacks():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    to_running(jid)
    evs, last = happy_events()
    runner_events(jid, list(reversed(evs)))          # out of order: accepted, job moves forward only
    assert job_row(jid).status == "saving_artifacts", job_row(jid).status
    assert scalar("SELECT count(*) FROM job_runner_events WHERE job_id = :j", j=jid) == len(evs)
    sync.complete(jid, {"outcome": "failed", "summary": {}})
    r = sync.complete(jid, {"outcome": "succeeded", "summary": {}})
    assert r["replayed"], "a second complete changes nothing"
    assert job_row(jid).outcome == "failed"
    drive_to_end(jid)
    try:
        tokens.verify(tokens.header_value(jid, "0" * 64), jid)
        raise AssertionError("bad token accepted")
    except HTTPException as exc:
        assert exc.status_code == 401


def test_s3_backend_fake():
    world()

    class FakeS3:
        def __init__(self):
            self.objects = {}

        def put_object(self, Bucket, Key, Body, ContentType=None):
            self.objects[(Bucket, Key)] = Body

        def get_object(self, Bucket, Key):
            import io
            return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

        def delete_object(self, Bucket, Key):
            self.objects.pop((Bucket, Key), None)

    fake = FakeS3()
    st = A.S3Store(fake, bucket="og-bucket", prefix="jobs/")
    A._OVERRIDE["s3"] = st
    settings.job_artifact_store = "s3"
    try:
        data = b"weights"
        m = A.store().put("job-00000000000000s3", "model.pt", data, hashlib.sha256(data).hexdigest()) \
            if False else st.put("job-0000000000000a53", "model.pt", data, hashlib.sha256(data).hexdigest())
        assert m["backend"] == "s3" and ("og-bucket", "jobs/job-0000000000000a53/model.pt") in fake.objects
        assert scalar("SELECT bytes IS NULL FROM job_artifacts WHERE job_id = 'job-0000000000000a53'") is True
        meta, got = st.get("job-0000000000000a53", "model.pt")
        assert got == data
        assert st.delete("job-0000000000000a53", "model.pt") and not fake.objects
        # unconfigured s3: unavailable, never silently falls back
        A._OVERRIDE.clear()
        settings.job_artifact_s3_bucket = None
        h = A.health()
        assert not h["ok"] and h["backend"] == "s3", h
        try:
            A.store().put("job-0000000000000a53", "model.pt", data, hashlib.sha256(data).hexdigest())
            raise AssertionError("put on an unconfigured s3 store")
        except A.ArtifactStoreUnavailable as e:
            assert e.code == "artifact_storage_unavailable" and e.status == 503
    finally:
        settings.job_artifact_store = "postgres"
        A._OVERRIDE.clear()


def test_readiness():
    world(mode="PREVIEW_ONLY")
    r = O.readiness(who())
    codes = {b["code"] for b in r["blockers"]}
    assert not r["ready"] and "execution_mode" in codes and "provider_ready" in codes, codes
    world()
    r = O.readiness(OPERATOR)
    lam = next(p for p in r["providers"] if p["provider"] == "lambda")
    assert lam["ready"], lam
    assert next(c for c in r["checks"] if c["code"] == "artifact_store")["ok"]


def test_price_changed_and_gpu_unavailable():
    world()
    SIM.price_cents = 300                      # the live price moved 20% since the observed listing
    try:
        submit_real()
        raise AssertionError("launched on a moved price")
    except O.JobRefused as r:
        assert r.code == "price_changed" and r.extra.get("job_id"), r.detail()
        j = job_row(r.extra["job_id"])
        assert j.status == "no_eligible_provider" and j.failure_code == "price_changed", (j.status, j.failure_code)
    world()
    SIM.capacity = False                       # sold out on the live check
    try:
        submit_real()
        raise AssertionError("launched on an unavailable listing")
    except O.JobRefused as r:
        assert r.code == "gpu_unavailable", r.detail()
    assert SIM.creates() == []


def test_quote_expired_at_approval():
    world()
    code, out, w = submit_real()
    jid = out["job_id"]
    old_q = dep_row(jid).quote_id
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE quotes SET expires_at = now() - interval '1 second' WHERE id = :q"), {"q": old_q})
    try:
        approve(jid)
        raise AssertionError("approved an expired quote")
    except HTTPException as exc:
        assert exc.detail["code"] == "quote_expired", exc.detail
    assert SIM.creates() == []
    g = O.get(jid, w, sync_first=True)
    assert g["status"] in ("pending_approval", "quote_expired"), g["status"]
    new_q = dep_row(jid).quote_id
    assert new_q != old_q and g["approval"]["quote_id"] == new_q, "the job shows the fresh quote to approve"
    approve(jid)
    assert len(SIM.creates()) == 1


def test_live_mode_launches_with_the_bootstrap():
    world(mode="LIVE")
    bf.flags("lambda", validated=True, live=True, supervised=True)
    code, out, w = submit_real()
    jid = out["job_id"]
    assert len(SIM.creates()) == 1, "LIVE: no per-launch approval (validated + live-enabled provider)"
    j = job_row(jid)
    assert j.bootstrap_sha256 == hashlib.sha256(SIM.user_data()[0].encode()).hexdigest()
    assert j.deployment_id and j.route_request_id
    workers(2)
    assert job_row(jid).status == "initializing"


TESTS = [test_state_machine, test_preview_shapes, test_sandbox_end_to_end, test_refusals_are_not_faked,
         test_real_path_happy, test_customer_key, test_bootstrap_timeout, test_training_failure_terminates, test_oom,
         test_cuda_init_failed, test_artifact_upload_failure, test_no_success_without_result, test_cancel_before_launch,
         test_cancel_after_launch, test_ambiguous_provider_timeout, test_deadline_timeout, test_worker_restart_rebuild,
         test_out_of_order_and_late_callbacks, test_s3_backend_fake, test_readiness,
         test_price_changed_and_gpu_unavailable, test_quote_expired_at_approval, test_live_mode_launches_with_the_bootstrap]


def main(only=None) -> int:
    failed = 0
    try:
        for t in TESTS:
            if only and t.__name__ not in only:
                continue
            try:
                t()
                print(f"PASS {t.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}")
                traceback.print_exc()
    finally:
        if S_ is not None:
            bf.drop(S_, DB)
    print(f"{len(TESTS) - failed if not only else '-'} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
