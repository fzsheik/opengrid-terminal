"""OpenGrid Jobs HTTP API: envelope / error conventions, scopes, tenant isolation, Idempotency-Key (replay,
conflicting body, concurrent submits -> one job and one route), preview shapes, the approval through the EXISTING
POST /v1/route/{id}/approve, runner callbacks over HTTP with the job token, logs cursor, metrics, artifacts.

TestClient (OPENGRID_NO_JOBS=1, POLLER_ENABLED=false), scratch DB og_test_jobs_api, fake Lambda API. No network.
Run:  .venv/Scripts/python tests/test_jobs_api.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_jobs as T  # noqa: E402  (the shared world: scratch DB, fake Lambda, market, controls)

T.DB = "og_test_jobs_api"

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import normalize  # noqa: E402
from accounts import accounts as acc  # noqa: E402
from accounts import keys, ratelimit  # noqa: E402
from bench_fixtures import q, scalar  # noqa: E402
from config import settings  # noqa: E402
from jobs import tokens  # noqa: E402

HDR = {"X-OpenGrid-Request": "1"}
client = TestClient(main.app)
ALL = ["data:read", "route:preview", "route:execute", "deployments:read", "deployments:write"]


def setup(**kw):
    sim = T.world(**kw)
    settings.app_password = None
    settings.public_pages = False
    settings.environment = "development"
    keys.reset_cache()
    ratelimit.reset()
    acc.reset_cache()
    return sim


def key(scopes=ALL, account_id=None):
    aid = account_id or T.bf.fresh_account()
    k = keys.create_key(aid, "jobs-test", scopes, rate_limit_per_minute=1000)
    return {"Authorization": f"Bearer {k['secret']}"}, aid


def idem(h=None):
    return {**(h or {}), "Idempotency-Key": str(uuid.uuid4())}


def submit(h, body=None, ik=None):
    return client.post("/v1/jobs", headers={**h, "Idempotency-Key": ik or str(uuid.uuid4())}, json=body or T.body())


def runner(job_id):
    return {"Authorization": tokens.header_value(job_id, T.token_for(job_id))}


# --------------------------------------------------------------------------

def test_templates_and_readiness():
    setup()
    h, _ = key(["data:read"])
    r = client.get("/v1/jobs/templates", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["meta"]["methodology"] == "/methodology/jobs"
    t = d["data"][0]
    assert t["id"] == "pytorch-mnist" and t["artifacts"] and t["bootstrap_template_version"], t.keys()
    r = client.get("/v1/jobs/readiness", headers=h)
    assert r.status_code == 403, "readiness needs route:preview"
    h2, _ = key(["route:preview"])
    r = client.get("/v1/jobs/readiness", headers=h2)
    assert r.status_code == 200 and "blockers" in r.json()["data"], r.text


def test_scopes():
    setup()
    ro, _ = key(["data:read"])
    assert client.post("/v1/jobs/preview", headers=ro, json=T.body()).status_code == 403
    assert submit(ro).status_code == 403
    assert client.get("/v1/jobs", headers=ro).status_code == 403
    pv, _ = key(["route:preview"])
    assert client.post("/v1/jobs/preview", headers=pv, json=T.body()).status_code == 200
    assert submit(pv).status_code == 403, "submit needs route:execute"
    assert client.get("/v1/jobs").status_code in (200, 401)   # open dev = operator; deployed would need auth
    assert client.post("/v1/jobs", json=T.body()).status_code == 403, "CSRF: operator POST without the header"
    assert scalar("SELECT count(*) FROM jobs") == 0


def test_preview_and_validation_errors():
    setup()
    h, _ = key()
    r = client.post("/v1/jobs/preview", headers=h, json=T.body())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["meta"]["kind"] == "estimated"
    data = d["data"]
    assert data["execution_ready"]["provider"] == "lambda" and data["submission"]["would"] == "pending_approval"
    assert data["estimate"]["est_max_total"] <= 2.0
    r = client.post("/v1/jobs/preview", headers=h, json=T.body(template="pytorch-imagenet"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_template", r.text
    assert "message" in r.json()["detail"]
    r = client.post("/v1/jobs/preview", headers=h, json={**T.body(), "command": "rm -rf /"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_field", r.text
    r = client.post("/v1/jobs/preview", headers={**h, "Content-Type": "text/plain"}, content=json.dumps(T.body()))
    assert r.status_code == 415, r.text
    r = client.post("/v1/jobs/preview", headers=h, json=T.body(resources={"allow_spot": True}))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "spot_not_supported", r.text


def test_idempotency_replay_and_conflict():
    setup()
    h, _ = key()
    r = client.post("/v1/jobs", headers=h, json=T.body())
    assert r.status_code == 428 and r.json()["detail"]["code"] == "idempotency_key_required", r.text
    ik = str(uuid.uuid4())
    r1 = submit(h, ik=ik)
    assert r1.status_code == 202, r1.text
    r2 = submit(h, ik=ik)
    assert r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true"
    assert r2.json()["data"]["job_id"] == r1.json()["data"]["job_id"]
    r3 = submit(h, T.body(name="other-name"), ik=ik)
    assert r3.status_code == 422 and r3.json()["detail"]["code"] == "idempotency_key_reused", r3.text
    assert scalar("SELECT count(*) FROM jobs") == 1
    assert scalar("SELECT count(*) FROM deployments") == 1
    # a refusal is stored and replayed too
    setup(mode="PREVIEW_ONLY")
    h, _ = key()
    ik = str(uuid.uuid4())
    a, b = submit(h, ik=ik), submit(h, ik=ik)
    assert a.status_code == b.status_code == 422 and a.json() == b.json(), (a.text, b.text)
    assert a.json()["detail"]["code"] == "execution_disabled"


def test_concurrent_submits_one_job_one_route():
    setup()
    h, _ = key()
    ik = str(uuid.uuid4())
    out = []

    def go():
        c = TestClient(main.app)
        out.append(c.post("/v1/jobs", headers={**h, "Idempotency-Key": ik}, json=T.body()))

    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    codes = sorted(r.status_code for r in out)
    assert all(c in (202, 409) for c in codes) and 202 in codes, codes
    ids = {r.json()["data"]["job_id"] for r in out if r.status_code == 202}
    assert len(ids) == 1, ids
    assert scalar("SELECT count(*) FROM jobs") == 1
    assert scalar("SELECT count(*) FROM deployments") == 1
    assert scalar("SELECT count(*) FROM route_requests WHERE NOT preview") == 1


def test_tenant_isolation():
    setup()
    ha, _ = key()
    hb, _ = key()
    r = submit(ha)
    jid = r.json()["data"]["job_id"]
    assert client.get(f"/v1/jobs/{jid}", headers=ha).status_code == 200
    for path in (f"/v1/jobs/{jid}", f"/v1/jobs/{jid}/logs", f"/v1/jobs/{jid}/metrics", f"/v1/jobs/{jid}/artifacts",
                 f"/v1/jobs/{jid}/artifacts/model.pt"):
        rb = client.get(path, headers=hb)
        assert rb.status_code == 404, (path, rb.status_code, rb.text)
    assert client.get("/v1/jobs", headers=hb).json()["data"] == []
    assert [j["job_id"] for j in client.get("/v1/jobs", headers=ha).json()["data"]] == [jid]
    r = client.post(f"/v1/jobs/{jid}/cancel", headers=idem(hb))
    assert r.status_code == 404, r.text
    assert T.job_row(jid).cancel_requested_at is None
    # the operator sees everything
    assert client.get(f"/v1/jobs/{jid}").status_code == 200


def test_full_flow_over_http():
    setup()
    h, aid = key()
    r = submit(h)
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["meta"]["kind"] == "transaction"
    job = d["data"]
    jid, rr = job["job_id"], job["resource"]["route_request_id"]
    assert job["status"] == "pending_approval" and job["approval"]["required"]
    assert job["approval"]["approve"] == f"POST /v1/route/{rr}/approve"
    assert job["route"]["status"] == "pending_approval"
    # a customer key cannot approve (existing endpoint: admin)
    r = client.post(f"/v1/route/{rr}/approve", headers=idem(h), json={"quote_id": job["approval"]["quote_id"]})
    assert r.status_code == 403, r.text
    # the operator approves through the EXISTING endpoint
    r = client.post(f"/v1/route/{rr}/approve", headers=idem(HDR), json={"quote_id": job["approval"]["quote_id"]})
    assert r.status_code == 200, r.text
    assert len(T.SIM.creates()) == 1
    T.workers(2)
    r = client.get(f"/v1/jobs/{jid}", headers=h)
    assert r.json()["data"]["status"] == "initializing", r.json()["data"]["status"]
    assert r.json()["data"]["resource"]["disposition"] == "possibly_billing"
    # runner callbacks: the token only
    rh = runner(jid)
    evs, last = T.happy_events()
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh, json={"events": evs})
    assert r.status_code == 200 and r.json()["accepted"] == len(evs), r.text
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh, json={"events": evs[:2]})
    assert r.json()["duplicates"] == 2
    manifest = {}
    for name, data in T.ART.items():
        r = client.put(f"/v1/jobs/{jid}/runner/artifacts/{name}", headers={
            **rh, "Content-Type": "application/octet-stream", "X-Content-SHA256": hashlib.sha256(data).hexdigest()},
            content=data)
        assert r.status_code == 200, r.text
        manifest[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    r = client.put(f"/v1/jobs/{jid}/runner/artifacts/model.pt", headers={
        **rh, "X-Content-SHA256": hashlib.sha256(T.ART["model.pt"]).hexdigest()}, content=T.ART["model.pt"])
    assert r.status_code == 200 and r.json()["artifact"].get("replayed"), "same artifact again = idempotent"
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh, json={"events": [T.result_event(last + 1, manifest)]})
    assert r.status_code == 200
    r = client.post(f"/v1/jobs/{jid}/runner/complete", headers=rh, json={"outcome": "succeeded", "summary": {}})
    assert r.status_code == 200 and r.json()["outcome"] == "succeeded", r.text
    assert len(T.SIM.terminates()) == 1
    T.drive_to_end(jid)
    r = client.get(f"/v1/jobs/{jid}", headers=h)
    j = r.json()["data"]
    assert j["status"] == "reconciled" and j["outcome"] == "succeeded" and j["finished"], (j["status"], j["outcome"])
    assert j["resource"]["disposition"] == "terminated_confirmed"
    assert all(c["status"] == "pass" for c in j["checks"]), [c for c in j["checks"] if c["status"] != "pass"]
    assert [e["to"] for e in j["events"] if e["from"] != e["to"]][:4] == ["created", "validating", "quoted",
                                                                          "pending_approval"]
    # late callback: the job is finished (token revoked first)
    r = client.post(f"/v1/jobs/{jid}/runner/events", headers=rh, json={"events": [
        {"seq": 999, "type": "log", "data": {"line": "late"}}]})
    assert r.status_code in (401, 409), r.text
    # logs: cursor
    r = client.get(f"/v1/jobs/{jid}/logs?limit=1", headers=h)
    a = r.json()["data"]
    assert len(a["items"]) == 1 and a["more"]
    r = client.get(f"/v1/jobs/{jid}/logs?after={a['next_after']}&limit=100", headers=h)
    b = r.json()["data"]
    assert b["items"] and b["items"][0]["id"] > a["items"][0]["id"]
    # metrics
    m = client.get(f"/v1/jobs/{jid}/metrics", headers=h).json()["data"]
    assert [e["epoch"] for e in m["epochs"]] == [1, 2] and "train_loss" in m["series"] and not m["simulated"]
    # artifacts
    arts = client.get(f"/v1/jobs/{jid}/artifacts", headers=h).json()["data"]
    assert {a["name"] for a in arts} == set(T.ART)
    r = client.get(f"/v1/jobs/{jid}/artifacts/model.pt", headers=h)
    assert r.status_code == 200 and r.content == T.ART["model.pt"]
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-artifact-sha256"] == hashlib.sha256(T.ART["model.pt"]).hexdigest()
    assert r.headers["content-type"] == "application/octet-stream"
    # cancel a finished job: nothing to do
    r = client.post(f"/v1/jobs/{jid}/cancel", headers=idem(h))
    assert r.status_code == 200 and not r.json()["data"]["cancel"]["requested"]


def test_cancel_api():
    setup()
    h, _ = key()
    jid = submit(h).json()["data"]["job_id"]
    r = client.post(f"/v1/jobs/{jid}/cancel", headers=h)
    assert r.status_code == 428, r.text
    ik = str(uuid.uuid4())
    r = client.post(f"/v1/jobs/{jid}/cancel", headers={**h, "Idempotency-Key": ik})
    assert r.status_code == 200 and r.json()["data"]["outcome"] == "cancelled", r.text
    r2 = client.post(f"/v1/jobs/{jid}/cancel", headers={**h, "Idempotency-Key": ik})
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert T.dep_row(jid).status == "rejected"


def test_sandbox_api():
    setup(mode="PREVIEW_ONLY")
    h, _ = key()
    r = submit(h, T.body(sandbox=True))
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["meta"]["kind"] == "simulated" and d["meta"]["simulated"] is True
    assert d["data"]["outcome"] == "simulated" and d["data"]["resource"]["deployment_id"] is None
    jid = d["data"]["job_id"]
    g = client.get(f"/v1/jobs/{jid}", headers=h).json()
    assert g["meta"]["simulated"] and g["data"]["simulated"]
    assert T.SIM.log == []


def test_runner_routes_refuse_api_keys_and_site_login():
    setup()
    h, _ = key(ALL + ["admin"])
    jid = submit(h).json()["data"]["job_id"]
    # an API key (even with every scope), the operator's site login (CSRF header present), nothing at all:
    # all refused (401 by the token check; the bare site-login request is already stopped by CSRF, 403)
    for hh, codes in ((h, (401,)), (HDR, (401,)), ({}, (401, 403))):
        r = client.post(f"/v1/jobs/{jid}/runner/events", headers=hh, json={"events": []})
        assert r.status_code in codes, (hh, r.status_code, r.text)
        r = client.post(f"/v1/jobs/{jid}/runner/complete", headers=hh, json={"outcome": "succeeded"})
        assert r.status_code in codes, r.text
        r = client.put(f"/v1/jobs/{jid}/runner/artifacts/model.pt", headers=hh, content=b"x")
        assert r.status_code in codes, r.text
    assert scalar("SELECT count(*) FROM job_runner_events") == 0 and scalar("SELECT count(*) FROM job_artifacts") == 0


TESTS = [test_templates_and_readiness, test_scopes, test_preview_and_validation_errors,
         test_idempotency_replay_and_conflict, test_concurrent_submits_one_job_one_route, test_tenant_isolation,
         test_full_flow_over_http, test_cancel_api, test_sandbox_api, test_runner_routes_refuse_api_keys_and_site_login]


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
