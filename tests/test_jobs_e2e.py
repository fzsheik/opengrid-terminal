"""SIMULATION of the founder's first paid MNIST job, end to end through the REAL plumbing (one isolated test).

    preview -> submit (Idempotency-Key, replay) -> the EXISTING admin approval POST /v1/route/{rr}/approve ->
    routing/deployments.launch renders the bootstrap -> fake Lambda (tests/bench_fixtures LambdaSim behind
    httpx.MockTransport, driven through the REAL LambdaAdapter) receives user_data -> the REAL runner
    (runner/reporter.py + runner/mnist_train.py, CPU torch venv) calls back over real HTTP to the app served by
    uvicorn on 127.0.0.1 inside this process -> artifacts / metrics / logs -> the existing terminate -> tracker /
    reconcile / job_sync -> reconciled, cost records.

What is SIMULATED here (labelled, never presented as a real run):
  * the provider: LambdaSim (no real Lambda call; no network except 127.0.0.1);
  * "validated + supervised" for Lambda and execution mode SUPERVISED exist ONLY in the scratch database
    og_test_jobs_e2e and in this process's in-memory settings (a TEST FIXTURE; nothing real is flagged);
  * the GPU: this machine has none. Two jobs therefore:
      job R  - the REAL runner + REAL trainer on CPU (--allow-cpu, 2000-sample subset, 1 epoch) against a real
               (non-sandbox) job. The server must REFUSE to call it a success (no GPU metrics: the rule of
               tests/test_jobs_security.test_real_metrics_require_a_gpu) -> training_failed / metrics_missing,
               still terminated and reconciled. Its artifacts are real and persisted.
      job A  - the founder's job: the REAL reporter client (runner.reporter Client/Spool/Reporter over real HTTP)
               delivers GPU-SHAPED SYNTHETIC events (marked "synthetic" in their data) plus the real artifact
               bytes produced by job R's CPU run -> the happy path to reconciled / succeeded.
If the CPU torch venv is missing, job R is SKIPPED loudly and job A uploads synthetic artifact bytes.

Run:  PYTHONIOENCODING=utf-8 .venv/Scripts/python tests/test_jobs_e2e.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
os.environ["POLLER_ENABLED"] = "false"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_jobs as T  # noqa: E402  (shared world: scratch DB, JobSim fake Lambda, market, controls)

T.DB = "og_test_jobs_e2e"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import main  # noqa: E402
from accounts import accounts as acc  # noqa: E402
from accounts import keys, ratelimit  # noqa: E402
from bench_fixtures import q, scalar  # noqa: E402
from config import settings  # noqa: E402
from jobs import tokens  # noqa: E402
from routing import deployments  # noqa: E402
from runner import bootstrap  # noqa: E402
from runner import reporter as R  # noqa: E402

_SCRATCH = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "claude" / \
    "C--Users-elsha-OneDrive-Desktop-opengrid-terminal" / "04091b5b-5d13-4253-a8ec-0031d0b7b618" / "scratchpad"
TORCH_PY = os.environ.get("OG_TORCHENV") or str(_SCRATCH / "torchenv" / "Scripts" / "python.exe")
MNIST_DATA = os.environ.get("OG_MNIST_DATA") or str(_SCRATCH / "cpu_run" / "data")
HAPPY_PATH = ["provisioning", "initializing", "training", "saving_artifacts", "training_completed", "terminating",
              "termination_confirmed", "reconciliation_pending", "reconciled"]
MOJIBAKE = ("â€", "Ã", "Â", "�")   # UTF-8 read as cp1252/latin-1, replacement char
T0 = time.monotonic()


def step(msg: str) -> None:
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", flush=True)


# --------------------------------------------------------------------------
# The app on a real localhost port (test-scoped thread; shut down in finally)
# --------------------------------------------------------------------------

class LiveServer:
    def __init__(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        # lifespan off: like TestClient without `with` (no init_db against the app's DB, no background jobs)
        cfg = uvicorn.Config(main.app, host="127.0.0.1", port=self.port, lifespan="off", log_config=None,
                             access_log=True, log_level="info")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="e2e-uvicorn", daemon=True)
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)
        assert not self.thread.is_alive(), "uvicorn thread did not stop"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage() + " " + json.dumps(record.__dict__, default=str))
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def mkkey(scopes, account_id=None, **kw):
    aid = account_id or T.bf.fresh_account()
    k = keys.create_key(aid, "e2e", scopes, rate_limit_per_minute=1000, **kw)
    return {"Authorization": f"Bearer {k['secret']}"}, aid


def ik(h):
    return {**h, "Idempotency-Key": str(uuid.uuid4())}


def parse_user_data(ud: str) -> tuple[str, str]:
    """TEST-ONLY: what the GPU would read from its user_data (token + callback base URL)."""
    tok = re.search(r"<<'OG_TOKEN_EOF'\n([A-Za-z0-9_-]+)\nOG_TOKEN_EOF", ud).group(1)
    base = re.search(r"^OG_BASE_URL='([^']+)'$", ud, re.M).group(1)
    return tok, base


def transitions(jid):
    return [r[0] for r in q("SELECT to_status FROM job_events WHERE job_id = :j AND "
                            "from_status IS DISTINCT FROM to_status ORDER BY id", j=jid)]


def happy(tr):
    """reconciliation_pending is legitimately skipped (jobs/states.py: termination_confirmed -> reconciled) when the
    cost was already reconciled by the time job_sync saw the confirmed termination; normalise for comparison."""
    tr = list(tr)
    if "termination_confirmed" in tr and "reconciliation_pending" not in tr:
        tr.insert(tr.index("termination_confirmed") + 1, "reconciliation_pending")
    return tr


def drive(jid, want="reconciled", n=10):
    for _ in range(n):
        if T.job_row(jid).status == want:
            break
        T.workers(1)          # tracker.track + reconcile.run_once + sync.run_all (the job_sync step)
    return T.job_row(jid)


def no_mojibake(label, text):
    """Checks the DECODED text (a JSON body is re-serialised with ensure_ascii=False, so escaped mojibake
    is caught too)."""
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False)
    except ValueError:
        pass
    bad = [m for m in MOJIBAKE if m in text]
    if bad:
        i = text.find(bad[0])
        raise AssertionError(f"mojibake {bad} in {label}: ...{text[max(0, i - 120):i + 80]!r}...")


def approve_http(c, admin, job):
    rr, qid = job["resource"]["route_request_id"], job["approval"]["quote_id"]
    h = ik(admin)
    r = c.post(f"/v1/route/{rr}/approve", headers=h, json={"quote_id": qid})
    assert r.status_code == 200, r.text
    r2 = c.post(f"/v1/route/{rr}/approve", headers=h, json={"quote_id": qid})
    assert r2.status_code == 200 and r2.headers.get("Idempotent-Replayed") == "true", (r2.status_code, r2.text)
    return r.json()


def submit_http(c, h, body=None):
    r = c.post("/v1/jobs", headers=ik(h), json=body or T.body())
    assert r.status_code == 202, r.text
    return r.json()["data"]


# --------------------------------------------------------------------------
# The test
# --------------------------------------------------------------------------

def test_first_paid_job_e2e():
    sim = T.world()                     # scratch DB og_test_jobs_e2e; LambdaSim; market; SUPERVISED + flags
    settings.app_password = None
    settings.public_pages = False
    settings.environment = "development"
    keys.reset_cache()
    ratelimit.reset()
    acc.reset_cache()
    srv = LiveServer().start()
    # The rendered bootstrap must call back to THIS server (http allowed only for localhost, dev only).
    settings.job_callback_base_url = srv.url
    step(f"app serving on {srv.url} (uvicorn thread, lifespan off, OPENGRID_NO_JOBS=1, POLLER_ENABLED=false)")

    cap = Capture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    quiet = [(h, h.level) for h in root.handlers if h is not cap]
    for h, _ in quiet:
        h.setLevel(logging.WARNING)     # console stays readable; the capture handler still sees DEBUG
    calls = {"terminate": 0}
    real_terminate = deployments.terminate

    def counting_terminate(*a, **kw):
        calls["terminate"] += 1
        return real_terminate(*a, **kw)

    deployments.terminate = counting_terminate
    sub_out: list[str] = []
    tmp = tempfile.TemporaryDirectory(prefix="og_e2e_")
    c = httpx.Client(base_url=srv.url, timeout=60)
    try:
        cust, cust_aid = mkkey(["route:preview", "route:execute", "deployments:read"])
        admin, _ = mkkey(["admin"], platform_admin=True)
        other, _ = mkkey(["route:preview", "route:execute", "deployments:read"])
        step(f"keys: customer account {cust_aid} (route:preview, route:execute, deployments:read); "
             "platform-admin key; a second tenant")

        # ---- 1. preview
        r = c.post("/v1/jobs/preview", headers=cust, json=T.body())
        assert r.status_code == 200, r.text
        pv = r.json()["data"]
        assert pv["market_best"] is not None and pv["execution_ready"] is not None, pv.keys()
        assert pv["execution_ready"]["provider"] == "lambda" and pv["submission"]["would"] == "pending_approval"
        no_mojibake("preview", r.text)
        step(f"preview: market_best={pv['market_best'].get('provider')} execution_ready="
             f"{pv['execution_ready']['provider']} est_max_total={pv['estimate'].get('est_max_total')}")

        # ---- 2. submit + replay
        key_a = str(uuid.uuid4())
        r1 = c.post("/v1/jobs", headers={**cust, "Idempotency-Key": key_a}, json=T.body())
        assert r1.status_code == 202, r1.text
        job_a = r1.json()["data"]
        ja = job_a["job_id"]
        assert job_a["status"] == "pending_approval" and job_a["approval"]["required"], job_a["status"]
        r2 = c.post("/v1/jobs", headers={**cust, "Idempotency-Key": key_a}, json=T.body())
        assert r2.status_code == 202 and r2.headers.get("Idempotent-Replayed") == "true"
        assert r2.json()["data"]["job_id"] == ja
        assert scalar("SELECT count(*) FROM jobs") == 1 and scalar("SELECT count(*) FROM deployments") == 1
        assert sim.creates() == [], "nothing launches before approval"
        d = T.dep_row(ja)
        assert (d.launch or {}).get("startup_script") == f"opengrid-job:{ja}", "the deployment keeps the placeholder"
        step(f"submit: job A {ja} pending_approval (rr {job_a['resource']['route_request_id']}); replay -> same job")

        # ---- 3. cancel path on a second job before approval
        job_c = submit_http(c, cust)
        jc = job_c["job_id"]
        r = c.post(f"/v1/jobs/{jc}/cancel", headers=ik(cust))
        assert r.status_code == 200 and r.json()["data"]["outcome"] == "cancelled", r.text
        assert T.dep_row(jc).status == "rejected"
        r = c.post(f"/v1/route/{job_c['resource']['route_request_id']}/approve", headers=ik(admin),
                   json={"quote_id": job_c["approval"]["quote_id"]})
        assert r.status_code == 409, (r.status_code, r.text)
        assert sim.creates() == []
        step(f"cancel: job C {jc} cancelled before approval -> route rejected; approving it now -> 409")

        # ---- 4. a customer key cannot approve; the admin approves through the existing endpoint
        r = c.post(f"/v1/route/{job_a['resource']['route_request_id']}/approve", headers=ik(cust),
                   json={"quote_id": job_a["approval"]["quote_id"]})
        assert r.status_code == 403, r.text
        out = approve_http(c, admin, job_a)
        assert out["data"]["launch"]["launched"], out["data"].get("launch")
        assert len(sim.creates()) == 1
        ud_a = sim.user_data()[0]
        assert ud_a.startswith("#!/bin/bash") and "opengrid-job:" not in ud_a, "the placeholder reached the provider"
        assert f"version={bootstrap.TEMPLATE_VERSION}" in ud_a and bootstrap.BUNDLE_SHA256 in ud_a
        ja_row = T.job_row(ja)
        assert ja_row.bootstrap_sha256 == hashlib.sha256(ud_a.encode()).hexdigest()
        assert ja_row.bootstrap_template_version == bootstrap.TEMPLATE_VERSION
        tok_a, base_a = parse_user_data(ud_a)
        assert base_a == srv.url and tok_a == T.token_for(ja)
        assert (T.dep_row(ja).launch or {}).get("startup_script") == f"opengrid-job:{ja}"
        T.workers(2)
        assert T.job_row(ja).status == "initializing", T.job_row(ja).status
        step(f"approve: launched via deployments.launch; LambdaSim got rendered user_data "
             f"({len(ud_a)} bytes, {bootstrap.TEMPLATE_VERSION}); job stores sha256 {ja_row.bootstrap_sha256[:12]} only")

        # ---- 5. tenant isolation
        for path in (f"/v1/jobs/{ja}", f"/v1/jobs/{ja}/logs", f"/v1/jobs/{ja}/metrics", f"/v1/jobs/{ja}/artifacts",
                     f"/v1/jobs/{ja}/artifacts/model.pt"):
            rb = c.get(path, headers=other)
            assert rb.status_code == 404, (path, rb.status_code, rb.text)
        assert c.post(f"/v1/jobs/{ja}/cancel", headers=ik(other)).status_code == 404
        step("tenant isolation: the second account gets 404 on job A and its logs/metrics/artifacts/cancel")

        # ---- 6. job R: the REAL runner on CPU against a real job -> the server refuses success
        work = Path(tmp.name)
        art_dir = None
        torch_ok = Path(TORCH_PY).exists()
        if not torch_ok:
            print(f"SKIP real-runner leg: no CPU torch venv at {TORCH_PY} (set OG_TORCHENV); job A uploads "
                  "synthetic artifact bytes instead", flush=True)
        else:
            job_r = submit_http(c, cust)
            jr = job_r["job_id"]
            approve_http(c, admin, job_r)
            ud_r = sim.user_data()[-1]
            tok_r, base_r = parse_user_data(ud_r)
            T.workers(2)
            assert T.job_row(jr).status == "initializing"
            (work / "r").mkdir()
            tf = work / "r" / "token"
            tf.write_text(tok_r)
            art_dir = work / "r" / "out"
            common = ["--base-url", base_r, "--job", jr, "--token-file", str(tf), "--state-dir",
                      str(work / "r" / "state")]
            env = {**os.environ, "OG_ENV_SOURCE": "local_cpu_test"}
            # what the bootstrap does first (stage 2), minus nvidia-smi/venv steps a CPU box cannot pass honestly
            p = subprocess.run([sys.executable, "-m", "runner.reporter", "emit", *common, "--type", "phase",
                                "--name", "bootstrap_started", "--kv", "template=pytorch-mnist",
                                "--kv", f"template_version={bootstrap.TEMPLATE_VERSION}",
                                "--kv", "note=e2e test: local CPU, no GPU"],
                               cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
            sub_out += [p.stdout, p.stderr]
            assert p.returncode == 0, p.stderr[-2000:]
            step(f"job R {jr}: real runner starting (reporter run -> mnist_train --allow-cpu, 2000 train / "
                 "1000 test, 1 epoch)")
            t1 = time.monotonic()
            # the same command line the bootstrap uses (runner/bootstrap.py stage 2), plus the CPU/test flags
            p = subprocess.run([sys.executable, "-m", "runner.reporter", "run", *common, "--out", str(art_dir),
                                "--max-seconds", "600", "--", TORCH_PY, "-m", "runner.mnist_train", "--allow-cpu",
                                "--epochs", "1", "--limit-train", "2000", "--limit-test", "1000",
                                "--num-workers", "0", "--out", str(art_dir), "--data", MNIST_DATA],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=900, env=env)
            sub_out += [p.stdout, p.stderr]
            assert p.returncode == 0, p.stderr[-3000:]
            step(f"job R: runner exited 0 after {time.monotonic() - t1:.1f}s (locally 'succeeded' on CPU)")
            jrr = T.job_row(jr)
            assert jrr.outcome == "failed" and jrr.failure_code == "metrics_missing", (jrr.status, jrr.failure_code)
            g = c.get(f"/v1/jobs/{jr}", headers=cust).json()["data"]
            ck = {x["key"]: x["status"] for x in g["checks"]}
            assert ck["cuda_available"] == "fail" and ck["real_metrics"] == "fail", ck
            assert ck["training_ran"] == "pass" and ck["artifacts_persisted"] == "pass", ck
            dr = T.dep_row(jr)
            assert len(sim.terminates(dr.provider_instance_id)) == 1, "a refused job still terminates its GPU"
            jrr = drive(jr)
            assert jrr.status == "reconciled" and jrr.outcome == "failed", (jrr.status, jrr.outcome)
            tr = transitions(jr)
            assert happy(tr[tr.index("training"):]) == ["training", "saving_artifacts", "training_failed",
                                                        "terminating", "termination_confirmed",
                                                        "reconciliation_pending", "reconciled"], tr
            arts = {a["name"]: a for a in c.get(f"/v1/jobs/{jr}/artifacts", headers=cust).json()["data"]}
            manifest = json.loads((art_dir / "manifest.json").read_text(encoding="utf-8"))
            for n, m in manifest.items():
                assert arts[n]["sha256"] == m["sha256"], n
            real_metrics = json.loads((art_dir / "metrics.json").read_text(encoding="utf-8"))
            r = c.post(f"/v1/jobs/{jr}/runner/events", headers={"Authorization": tokens.header_value(jr, tok_r)},
                       json={"events": [{"seq": 9999, "type": "log", "data": {"line": "late"}}]})
            assert r.status_code in (401, 409), r.text
            step(f"job R: server refused success honestly (failure metrics_missing; cuda_available/real_metrics "
                 f"fail); real CPU acc={real_metrics['final']['test_accuracy']:.4f}; terminated once; reconciled "
                 f"(failed); {len(arts)} real artifacts persisted with matching sha256")

        # ---- 7. job A: the real reporter client, GPU-SHAPED SYNTHETIC events, over real HTTP
        if art_dir is None:
            art_dir = work / "synthetic"
            art_dir.mkdir()
            for n, data in T.ART.items():
                (art_dir / n).write_bytes(data)
            (art_dir / "manifest.json").write_text(json.dumps(
                {n: {"sha256": hashlib.sha256(d_).hexdigest(), "bytes": len(d_)} for n, d_ in T.ART.items()
                 if n != "manifest.json"}), encoding="utf-8")
        client = R.Client(base_a, ja, tok_a, timeout=20, max_attempts=3, backoff_base=0.2)
        rep = R.Reporter(client, R.Spool(work / "a_state"))
        evs, _ = T.happy_events(epochs=2)
        for e in evs:
            data = dict(e["data"])
            data["synthetic"] = "e2e test: GPU-shaped event (this machine has no GPU)"
            rep.emit(e["type"], data, e["at"], flush=False)
        assert rep.flush(), "events not delivered"
        assert T.job_row(ja).status == "saving_artifacts", T.job_row(ja).status
        up = rep.upload_artifacts(art_dir)
        assert not up["failed"], up["failed"]
        manifest_a = {n: m for n, m in up["uploaded"].items() if n != "manifest.json"}
        rep.emit("result", {"outcome": "succeeded", "train_loss": 0.1, "test_loss": 0.03, "test_accuracy": 0.982,
                            "param_l2_change": 12.5, "device": "cuda", "artifacts": manifest_a,
                            "synthetic": "e2e test: GPU-shaped result"})
        rep.phase("artifacts_uploaded", uploaded=sorted(up["uploaded"]))
        rep.phase("training_completed", test_accuracy=0.982)
        assert rep.complete("succeeded", {"exit_code": 0, "synthetic": True}), "complete not delivered"
        ja_row = T.job_row(ja)
        assert ja_row.outcome == "succeeded", (ja_row.status, ja_row.failure_code, ja_row.failure_reason)
        da = T.dep_row(ja)
        assert len(sim.terminates(da.provider_instance_id)) == 1, "runner complete -> exactly one provider terminate"
        step(f"job A: {len(evs)} events + {len(up['uploaded'])} artifacts + result + complete via runner.reporter "
             f"over HTTP; outcome succeeded; provider terminate sent")

        ja_row = drive(ja)
        assert ja_row.status == "reconciled" and ja_row.outcome == "succeeded", (ja_row.status, ja_row.outcome)
        tr = transitions(ja)
        assert tr[:4] == ["created", "validating", "quoted", "pending_approval"], tr
        assert happy(tr[tr.index("provisioning"):]) == HAPPY_PATH, tr
        assert len(sim.terminates(da.provider_instance_id)) == 1, "never terminated twice"
        step("job A: transitions " + " -> ".join(tr))

        g = c.get(f"/v1/jobs/{ja}", headers=cust)
        assert g.status_code == 200
        no_mojibake("GET /v1/jobs/{id}", g.text)
        no_mojibake("GET /v1/jobs", c.get("/v1/jobs", headers=cust).text)
        # The server sends correct UTF-8 (RFC 8259) with Content-Type "application/json" and no charset; a client
        # that decodes it as latin-1/cp1252 (e.g. Windows PowerShell 5.1 Invoke-WebRequest) shows "â€”"
        # for every em dash. Show what such a client would see, without failing on it (it is the client's decode).
        non_ascii = sorted({ch for body in (g.text, c.get("/v1/jobs/templates", headers=mkkey(["data:read"])[0]).text)
                            for ch in json.dumps(json.loads(body), ensure_ascii=False) if ord(ch) > 127})
        g.content.decode("utf-8")
        step(f"mojibake: none in decoded JSON; content-type {g.headers.get('content-type')!r}; non-ASCII chars "
             f"served: {non_ascii}; a latin-1 decode would show e.g. {'\u2014'.encode().decode('latin-1')!r}")
        if torch_ok:
            no_mojibake("GET /v1/jobs/{R}", c.get(f"/v1/jobs/{jr}", headers=cust).text)
        no_mojibake("jobs.selection (DB)", json.dumps([r[0] for r in q("SELECT selection FROM jobs")],
                                                      ensure_ascii=False, default=str))
        j = g.json()["data"]
        assert j["resource"]["disposition"] == "terminated_confirmed", j["resource"]
        bad = [x for x in j["checks"] if x["status"] != "pass"]
        assert not bad, bad
        assert j["bootstrap"]["sha256"] == hashlib.sha256(ud_a.encode()).hexdigest()

        # ---- 8. metrics, logs (cursor), artifacts (download + sha256 + loads in torch)
        m = c.get(f"/v1/jobs/{ja}/metrics", headers=cust).json()["data"]
        assert [e["epoch"] for e in m["epochs"]] == [1, 2] and "train_loss" in m["series"] and not m["simulated"], m
        a = c.get(f"/v1/jobs/{ja}/logs?limit=1", headers=cust).json()["data"]
        assert len(a["items"]) == 1 and a["more"], a
        b = c.get(f"/v1/jobs/{ja}/logs?after={a['next_after']}&limit=100", headers=cust).json()["data"]
        assert b["items"] and b["items"][0]["id"] > a["items"][0]["id"]
        arts = {x["name"]: x for x in c.get(f"/v1/jobs/{ja}/artifacts", headers=cust).json()["data"]}
        assert set(R.UPLOAD_ARTIFACTS) <= set(arts), arts.keys()
        r = c.get(f"/v1/jobs/{ja}/artifacts/model.pt", headers=cust)
        local = (art_dir / "model.pt").read_bytes()
        assert r.status_code == 200 and r.content == local
        assert r.headers["x-artifact-sha256"] == hashlib.sha256(local).hexdigest() == arts["model.pt"]["sha256"]
        assert r.headers["content-disposition"].startswith("attachment")
        for path in (f"/v1/jobs/{ja}/artifacts", f"/v1/jobs/{ja}/artifacts/model.pt"):
            assert c.get(path, headers=other).status_code == 404
        if torch_ok:
            dl = work / "downloaded_model.pt"
            dl.write_bytes(r.content)
            code = ("import json; from runner.mnist_train import load_model, evaluate, test_loader; "
                    f"m = load_model(r'{dl}'); "
                    f"print(json.dumps(evaluate(m, test_loader(r'{MNIST_DATA}', limit_test=1000, seed=42), 'cpu')))")
            p = subprocess.run([TORCH_PY, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300)
            assert p.returncode == 0, p.stderr[-2000:]
            again = json.loads(p.stdout.strip().splitlines()[-1])
            assert again["test_accuracy"] == real_metrics["final"]["test_accuracy"], (again, real_metrics["final"])
            step(f"artifacts: downloaded model.pt (sha256 ok) loads in torch and reproduces acc "
                 f"{again['test_accuracy']:.4f}")
        step(f"metrics {len(m['epochs'])} epochs; logs cursor {a['next_after']} -> {len(b['items'])} more lines")

        # ---- 9. token after terminal; tokens/script nowhere in the DB or logs
        r = c.post(f"/v1/jobs/{ja}/runner/events", headers={"Authorization": tokens.header_value(ja, tok_a)},
                   json={"events": [{"seq": 9999, "type": "log", "data": {"line": "late"}}]})
        assert r.status_code in (401, 409), r.text
        assert scalar("SELECT count(*) FROM job_tokens WHERE revoked_at IS NULL") == 0
        dump = T.db_text_dump()
        logs_text = "\n".join(cap.lines + sub_out)
        toks = [tok_a] + ([tok_r] if torch_ok else [])
        for t in toks:
            assert t not in dump, "runner token stored in the database"
            assert t not in logs_text, "runner token in captured logs / runner output"
        assert "OG_B64_EOF" not in dump and "OG_TOKEN_EOF" not in dump, "rendered bootstrap stored"
        assert "OG_B64_EOF" not in logs_text, "rendered bootstrap logged"
        step(f"secrets: {len(toks)} runner tokens absent from {len(dump)} chars of DB text and {len(cap.lines)} "
             f"log records + runner output; late callback refused ({r.status_code})")

        # ---- 10. cost / usage records
        dep_ids = [da.deployment_id] + ([T.dep_row(jr).deployment_id] if torch_ok else [])
        for dep_id in dep_ids:
            row = q("SELECT status, reconciled_at, reconciliation IS NOT NULL FROM deployments "
                    "WHERE deployment_id = :d", d=dep_id)[0]
            assert row[0] == "terminated" and row[1] is not None and row[2], row
            assert scalar("SELECT count(*) FROM execution_records WHERE deployment_id = :d", d=dep_id) == 1
        slices = scalar("SELECT count(*) FROM usage_slices WHERE deployment_id = :d", d=da.deployment_id)
        recs = q("SELECT amount_usd FROM usage_records WHERE deployment_id = :d", d=da.deployment_id) \
            if scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = 'usage_records' "
                      "AND column_name = 'amount_usd'") else q("SELECT id FROM usage_records WHERE deployment_id = :d",
                                                                d=da.deployment_id)
        assert slices >= 1, "no usage slice metered"
        rec = q("SELECT reconciliation FROM deployments WHERE deployment_id = :d", d=da.deployment_id)[0][0]
        inv = T.bf.invariants(dep_ids, sim=sim)
        assert inv["creates"] == len(dep_ids) and inv["alive"] == 0, inv
        step(f"cost: execution_records + reconciliation for {len(dep_ids)} deployments; job A usage_slices={slices} "
             f"usage_records={len(recs)}; reconciliation keys={sorted(rec)[:8]}; invariants {inv}")
        step(f"deployments.terminate calls (all callers) = {calls['terminate']}; provider terminate per instance = 1")
    finally:
        deployments.terminate = real_terminate
        root.removeHandler(cap)
        root.setLevel(old_level)
        for h, lvl in quiet:
            h.setLevel(lvl)
        c.close()
        srv.stop()
        tmp.cleanup()
        step("uvicorn stopped; temp dir removed")


def main_() -> int:
    ok = True
    try:
        test_first_paid_job_e2e()
        print("PASS test_first_paid_job_e2e")
    except Exception:  # noqa: BLE001
        ok = False
        print("FAIL test_first_paid_job_e2e")
        traceback.print_exc()
    finally:
        if T.S_ is not None:
            T.bf.drop(T.S_, T.DB)
    print(f"total {time.monotonic() - T0:.1f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main_())
