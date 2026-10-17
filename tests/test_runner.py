"""Runner (runs ON the GPU): reporter wire protocol, retries, offline spool, artifact hashing, supervisor, and
(if a separate CPU torch venv exists) a real MNIST smoke test. Runs in the app's .venv WITHOUT torch.

Everything here is LOCAL verification against a fake callback server (stdlib http.server): no GPU, no provider.

Run:  .venv/Scripts/python tests/test_runner.py
Optional CPU smoke test: set OG_TORCHENV to a python with torch==2.4.1/torchvision==0.19.1 (CPU wheels) (default:
the session scratch venv) and OG_MNIST_DATA to a directory with (or for) the MNIST download.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runner import reporter as R  # noqa: E402

TOKEN = "tok_" + "A1b2C3d4" * 5
JOB = "job_test1"
_SCRATCH = Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "claude" / \
    "C--Users-elsha-OneDrive-Desktop-opengrid-terminal" / "04091b5b-5d13-4253-a8ec-0031d0b7b618" / "scratchpad"


# ---------------------------------------------------------------------------------------------- fake OpenGrid
class FakeOpenGrid:
    """Implements the JOBS_BRIEF callback contract closely enough to test the client against it."""

    def __init__(self, token=TOKEN, job=JOB):
        self.token, self.job = token, job
        self.events, self.batches, self.artifacts, self.completes = {}, [], {}, []
        self.requests, self.plan = [], []     # plan: status codes to return for the next N requests
        self.terminal = False
        self.resume = None                    # bytes served at GET runner/resume (None = 404, not requested)
        self.lock = threading.Lock()
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj=None):
                body = json.dumps(obj or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                with srv.lock:
                    srv.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                    if srv.plan:
                        code = srv.plan.pop(0)
                        if code:
                            return self._reply(code, {"code": "planned"})
                if self.headers.get("Authorization") != f"Bearer ogj_{srv.job}.{srv.token}":
                    return self._reply(401, {"code": "bad_token"})
                if srv.terminal:
                    return self._reply(409, {"code": "job_terminal"})
                base = f"/v1/jobs/{srv.job}/runner/"
                if not self.path.startswith(base):
                    return self._reply(404)
                what = self.path[len(base):]
                if len(body) > 1_000_000 and what in ("events", "complete"):
                    return self._reply(413)
                if self.command == "POST" and what == "events":
                    evs = json.loads(body)["events"]
                    with srv.lock:
                        srv.batches.append([e["seq"] for e in evs])
                        for e in evs:
                            assert set(e) == {"seq", "type", "at", "data"}, e
                            assert e["type"] in R.EVENT_TYPES
                            srv.events.setdefault(e["seq"], e)   # duplicate seq ignored
                    return self._reply(200, {"accepted": len(evs)})
                if self.command == "PUT" and what.startswith("artifacts/"):
                    name = what.split("/", 1)[1]
                    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
                        return self._reply(422)
                    if hashlib.sha256(body).hexdigest() != self.headers.get("X-Content-SHA256"):
                        return self._reply(400, {"code": "sha256_mismatch"})
                    srv.artifacts[name] = body
                    return self._reply(200, {"sha256": hashlib.sha256(body).hexdigest()})
                if self.command == "POST" and what == "complete":
                    srv.completes.append(json.loads(body))
                    return self._reply(200)
                if self.command == "GET" and what == "resume":
                    if srv.resume is None:
                        return self._reply(404, {"code": "resume_not_requested"})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(srv.resume)))
                    self.send_header("X-Content-SHA256", hashlib.sha256(srv.resume).hexdigest())
                    self.end_headers()
                    self.wfile.write(srv.resume)
                    return None
                return self._reply(404)

            do_POST = do_PUT = do_GET = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _client(url, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("timeout", 5)
    return R.Client(url, JOB, TOKEN, **kw)


def _dead_url():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------------------------- pure parts
def test_parse_line():
    ev = R.parse_line('{"og": 1, "type": "metric", "at": "2026-10-13T00:00:00Z", "data": {"epoch": 1}}')
    assert ev == ("metric", {"epoch": 1}, "2026-10-13T00:00:00Z")
    assert R.parse_line("Downloading https://ossci-datasets.s3.amazonaws.com/mnist/x.gz") is None
    assert R.parse_line('{"og": 1, "type": "rm -rf", "data": {}}') is None, "unknown types are just logs"
    assert R.parse_line('{"type": "phase", "data": {}}') is None, "needs the og marker"
    assert R.parse_line("{not json}") is None
    t, d, at = R.parse_line('{"og": 1, "type": "phase", "data": "x"}')
    assert t == "phase" and d == {} and at.endswith("Z")


def test_spool_seq_is_monotonic_and_durable():
    with tempfile.TemporaryDirectory() as d:
        a, b = R.Spool(d), R.Spool(d)  # two processes sharing the state dir
        seqs = [a.add("phase", {"phase": "x"})["seq"], b.add("log", {"line": "y"})["seq"],
                a.add("metric", {"v": 1})["seq"]]
        assert seqs == [1, 2, 3]
        assert [e["seq"] for e in R.Spool(d).pending()] == [1, 2, 3], "survives a restart"
        a.remove([2])
        assert [e["seq"] for e in b.pending()] == [1, 3]
        assert R.Spool(d).add("phase", {})["seq"] == 4, "seq never reused after delivery"
        try:
            a.add("bogus", {})
            raise AssertionError("bad type accepted")
        except ValueError:
            pass
        # concurrent writers never collide
        out = []
        ths = [threading.Thread(target=lambda: out.extend(R.Spool(d).add("log", {})["seq"] for _ in range(20)))
               for _ in range(4)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        assert sorted(out) == list(range(5, 85))


def test_artifact_hashing():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "model.pt"
        p.write_bytes(b"\x00weights" * 1000)
        assert R.sha256_file(p) == hashlib.sha256(p.read_bytes()).hexdigest() == R.sha256_bytes(p.read_bytes())
        (Path(d) / "manifest.json").write_text(json.dumps({"model.pt": {"sha256": R.sha256_file(p)}}))
        assert R._manifest_matches(d, {"model.pt": {"sha256": R.sha256_file(p)}})
        assert not R._manifest_matches(d, {"model.pt": {"sha256": "0" * 64}})
        assert not R._manifest_matches(d, {})


def test_backoff_has_jitter_and_cap():
    import random
    c = _client("http://127.0.0.1:1", rng=random.Random(7), backoff_base=1.0, backoff_cap=60)
    ds = [c.backoff(i) for i in range(10)]
    assert 0.5 <= ds[0] <= 1.5 and 4 * 0.5 <= ds[2] <= 4 * 1.5
    assert max(ds) <= 90 and all(d <= 60 * 1.5 for d in ds), "capped"
    assert len(set(round(d, 6) for d in ds)) == len(ds), "jittered"


# ---------------------------------------------------------------------------------------------- against the server
def test_events_batch_seq_and_retry():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            delays = []
            rep = R.Reporter(_client(srv.url, sleep=delays.append), R.Spool(d))
            for i in range(5):
                rep.emit("metric", {"i": i}, flush=False)
            srv.plan = [503, 502]                           # two transient failures, then success
            assert rep.flush() is True
            assert len(delays) == 2 and delays[1] > 0
            assert sorted(srv.events) == [1, 2, 3, 4, 5] and R.Spool(d).pending() == []
            # a lost response: the server got the batch, the client retries it -> duplicates ignored
            rep.emit("phase", {"phase": "a"}, flush=False)
            ev = R.Spool(d).pending()
            rep.client.post_events(ev)
            assert rep.flush()
            assert sorted(srv.events) == [1, 2, 3, 4, 5, 6] and srv.batches[-1] == [6] == srv.batches[-2]
            # big backlogs go out in bounded batches, in seq order
            for i in range(250):
                rep.emit("log", {"line": "x" * 100}, flush=False)
            assert rep.flush()
            sizes = [len(b) for b in srv.batches[-3:]]
            assert sizes == [100, 100, 50], sizes
            assert sorted(srv.events) == list(range(1, 257))
            auth = {h.get("authorization") for _, _, h, _ in srv.requests}
            assert auth == {f"Bearer ogj_{JOB}.{TOKEN}"}
    finally:
        srv.close()


def test_offline_spool_then_recover():
    with tempfile.TemporaryDirectory() as d:
        dead = _dead_url()
        err = io.StringIO()
        with redirect_stderr(err):
            rep = R.Reporter(_client(dead, max_attempts=2), R.Spool(d))
            rep.phase("bootstrap_started", note="offline")
            rep.emit("metric", {"epoch": 1})
            assert rep.flush() is False
        assert [e["seq"] for e in R.Spool(d).pending()] == [1, 2], "kept on disk while OpenGrid is unreachable"
        assert TOKEN not in err.getvalue(), "token never in diagnostics"
        srv = FakeOpenGrid()
        try:
            rep2 = R.Reporter(_client(srv.url), R.Spool(d))   # e.g. the next process on the instance
            assert rep2.flush() is True
            assert sorted(srv.events) == [1, 2] and srv.events[1]["data"]["phase"] == "bootstrap_started"
            assert R.Spool(d).pending() == []
        finally:
            srv.close()


def test_auth_and_terminal_stop_delivery():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            bad = R.Reporter(R.Client(srv.url, JOB, "wrong_token_wrong_token", sleep=lambda s: None), R.Spool(d))
            err = io.StringIO()
            with redirect_stderr(err):
                bad.emit("phase", {"phase": "x"})
                assert bad.flush() is False and bad.client.disabled_reason
                n = len(srv.requests)
                bad.emit("phase", {"phase": "y"})
            assert len(srv.requests) == n, "no more calls once the token is rejected"
            assert len(R.Spool(d).pending()) == 2, "spool kept for diagnosis"
            assert "wrong_token" not in err.getvalue()
        with tempfile.TemporaryDirectory() as d:
            srv.terminal = True
            rep = R.Reporter(_client(srv.url), R.Spool(d))
            with redirect_stderr(io.StringIO()):
                rep.emit("phase", {"phase": "late"})
                assert rep.complete("succeeded", {}) is True, "409 = already terminal: nothing more to do"
    finally:
        srv.close()


def test_permanent_rejection_is_moved_aside():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            rep = R.Reporter(_client(srv.url), R.Spool(d))
            rep.emit("log", {"line": "a"}, flush=False)
            srv.plan = [422]
            with redirect_stderr(io.StringIO()):
                assert rep.flush() is True, "a malformed batch must not wedge the queue"
            assert list(Path(d, "spool").glob("*.rejected")), "but it is kept"
            rep.emit("log", {"line": "b"})
            assert 2 in srv.events
    finally:
        srv.close()


def test_artifact_upload_and_complete():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            out.mkdir()
            for n in R.UPLOAD_ARTIFACTS:
                (out / n).write_bytes(os.urandom(2048) if n == "model.pt" else b'{"ok": true}')
            rep = R.Reporter(_client(srv.url), R.Spool(Path(d) / "state"))
            srv.plan = [500]  # first PUT fails transiently
            with redirect_stderr(io.StringIO()):
                res = rep.upload_artifacts(out)
            assert res["failed"] == {} and set(res["uploaded"]) == set(R.UPLOAD_ARTIFACTS)
            for n in R.UPLOAD_ARTIFACTS:
                assert srv.artifacts[n] == (out / n).read_bytes()
                assert res["uploaded"][n]["sha256"] == hashlib.sha256(srv.artifacts[n]).hexdigest()
            puts = [h for m, p, h, _ in srv.requests if m == "PUT"]
            assert all(h.get("x-content-sha256") and h.get("content-type") == "application/octet-stream" for h in puts)
            try:
                rep.client.put_artifact("../etc/passwd", b"x")
                raise AssertionError("path name accepted")
            except R.PermanentError:
                pass
            assert rep.complete("succeeded", {"test_accuracy": 0.99}) is True
            assert srv.completes == [{"outcome": "succeeded", "summary": {"test_accuracy": 0.99}}]
            try:
                rep.client.complete("exploded", {})
                raise AssertionError
            except ValueError:
                pass
    finally:
        srv.close()


def test_undelivered_complete_is_retried():
    with tempfile.TemporaryDirectory() as d:
        with redirect_stderr(io.StringIO()):
            rep = R.Reporter(_client(_dead_url(), max_attempts=1), R.Spool(d))
            assert rep.complete("failed", {"reason": "bootstrap_failed"}) is False
        assert (Path(d) / "complete.pending.json").exists()
        srv = FakeOpenGrid()
        try:
            assert R.Reporter(_client(srv.url), R.Spool(d)).retry_pending() is True
            assert srv.completes == [{"outcome": "failed", "summary": {"reason": "bootstrap_failed"}}]
            assert not (Path(d) / "complete.pending.json").exists()
        finally:
            srv.close()


# ---------------------------------------------------------------------------------------------- supervisor
FAKE_TRAINER = r'''
import hashlib, json, sys, time, pathlib
mode, out = sys.argv[1], pathlib.Path(sys.argv[2])
def ev(t, **d): print(json.dumps({"og": 1, "type": t, "at": "2026-10-13T00:00:00Z", "data": d}), flush=True)
ev("phase", phase="training_started", device="cpu")
print("plain log line with secret %s" % sys.argv[3], flush=True)
print("to stderr", file=sys.stderr, flush=True)
print("0.3%\r0.7%\r100.0%", file=sys.stderr, flush=True)
if mode == "sleep":
    time.sleep(60)
if mode == "oom":
    ev("error", code="oom", message="CUDA out of memory"); sys.exit(3)
for e in (1, 2):
    ev("metric", epoch=e, train_loss=1.0 / e, test_accuracy=0.9 + e / 100)
    ev("phase", phase="epoch_completed", epoch=e)
    if mode == "ckpt":
        c = out / "checkpoints"; c.mkdir(parents=True, exist_ok=True)
        n = "ckpt-e%04d.pt" % e; (c / n).write_bytes(b"state-%d" % e * 100)
        sha = hashlib.sha256((c / n).read_bytes()).hexdigest() if e == 1 else "0" * 64   # e2: bytes changed
        ev("phase", phase="checkpoint_saved", epoch=e, name=n, sha256=sha, bytes=600)
        time.sleep(0.5)
out.mkdir(parents=True, exist_ok=True)
names = ("model.pt", "config.json", "metrics.json", "env.json")
for n in names: (out / n).write_bytes(n.encode() * 50)
(out / "manifest.json").write_text(json.dumps({n: {"sha256": hashlib.sha256((out / n).read_bytes()).hexdigest()} for n in names}))
ev("result", outcome="succeeded", test_accuracy=0.92)
'''


def _supervise(srv, d, mode, max_seconds=None):
    trainer = Path(d) / "fake_trainer.py"
    trainer.write_text(FAKE_TRAINER)
    rep = R.Reporter(_client(srv.url), R.Spool(Path(d) / "state"))
    with redirect_stderr(io.StringIO()):
        return R.run_supervised(rep, [sys.executable, str(trainer), mode, str(Path(d) / "out"), TOKEN],
                                out_dir=Path(d) / "out", max_seconds=max_seconds, flush_every=0.2, gpu_every=0)


def test_supervisor_success():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            res = _supervise(srv, d, "ok")
            assert res["outcome"] == "succeeded" and res["complete_delivered"], res
            seqs = sorted(srv.events)
            assert seqs == list(range(1, len(seqs) + 1)), "contiguous seq"
            evs = [srv.events[s] for s in seqs]
            phases = [e["data"].get("phase") for e in evs if e["type"] == "phase"]
            assert phases[0] == "training_started" and phases.count("epoch_completed") == 2
            assert phases[-3:] == ["uploading_artifacts", "artifacts_uploaded", "training_completed"], phases
            assert [e["data"]["epoch"] for e in evs if e["type"] == "metric"] == [1, 2]
            logs = [e["data"] for e in evs if e["type"] == "log"]
            assert {"stream": "stderr", "line": "to stderr"} in logs
            assert [l["line"] for l in logs if "%" in l["line"]] == ["100.0%"], "progress ticks dropped"
            assert all(TOKEN not in json.dumps(e) for e in evs), "token scrubbed from forwarded output"
            assert any("secret ***" in l["line"] for l in logs)
            assert set(srv.artifacts) == set(R.UPLOAD_ARTIFACTS)
            (c,) = srv.completes
            assert c["outcome"] == "succeeded" and c["summary"]["exit_code"] == 0
            assert c["summary"]["result"]["test_accuracy"] == 0.92
    finally:
        srv.close()


def test_supervisor_oom_and_timeout():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            res = _supervise(srv, d, "oom")
            assert res["outcome"] == "oom" and res["summary"]["reason"] == "out_of_memory"
            assert srv.completes[-1]["outcome"] == "oom"
            assert srv.completes[-1]["summary"]["error"]["code"] == "oom"
        with tempfile.TemporaryDirectory() as d:
            res = _supervise(srv, d, "sleep", max_seconds=3)
            assert res["outcome"] == "timeout" and res["summary"]["reason"] == "training_timed_out"
            assert res["summary"]["runtime_seconds"] < 30
            phases = [e["data"].get("phase") for e in srv.events.values() if e["type"] == "phase"]
            assert "training_failed" in phases
    finally:
        srv.close()


def test_supervisor_upload_failure_fails_job():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            trainer = Path(d) / "t.py"
            trainer.write_text(FAKE_TRAINER.replace('(out / "manifest.json")', '(out / "manifest.json")') +
                               '\n(out / "model.pt").unlink()\n')
            rep = R.Reporter(_client(srv.url), R.Spool(Path(d) / "state"))
            with redirect_stderr(io.StringIO()):
                res = R.run_supervised(rep, [sys.executable, str(trainer), "ok", str(Path(d) / "out"), "x"],
                                       out_dir=Path(d) / "out", flush_every=0.2, gpu_every=0)
            assert res["outcome"] == "failed" and res["summary"]["reason"] == "artifact_upload_failed", res
    finally:
        srv.close()


def test_supervisor_uploads_checkpoints_not_as_results():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            res = _supervise(srv, d, "ckpt")
            assert res["outcome"] == "succeeded", res
            assert srv.artifacts["ckpt-e0001.pt"] == b"state-1" * 100, "checkpoint uploaded via the artifacts PUT"
            assert "ckpt-e0002.pt" not in srv.artifacts, "bytes that differ from the trainer's sha256 are not sent"
            phases = [e["data"] for e in sorted(srv.events.values(), key=lambda e: e["seq"]) if e["type"] == "phase"]
            up = [p for p in phases if p["phase"] == "checkpoint_uploaded"]
            assert [p["name"] for p in up] == ["ckpt-e0001.pt"] and up[0]["sha256"] == hashlib.sha256(
                b"state-1" * 100).hexdigest(), phases
            bad = [p for p in phases if p["phase"] == "checkpoint_upload_failed"]
            assert bad and bad[0]["reason"] == "sha256_changed", bad
            # results are unaffected: the same five names, the checkpoint is neither required nor in the manifest
            (c,) = srv.completes
            assert set(c["summary"]["artifacts"]["uploaded"]) == set(R.UPLOAD_ARTIFACTS)
            assert c["summary"]["checkpoints_uploaded"] == 1
            i_up = next(i for i, p in enumerate(phases) if p["phase"] == "checkpoint_uploaded")
            i_res = next(i for i, p in enumerate(phases) if p["phase"] == "uploading_artifacts")
            assert i_up < i_res, "the checkpoint went up during training, before the results"
        # an unknown / path-like name is never read from disk or sent
        rep = R.Reporter(_client(srv.url), R.Spool(Path(tempfile.mkdtemp()) / "state"))
        assert rep.upload_checkpoint(tempfile.gettempdir(), {"name": "../model.pt"}) is False
        assert rep.upload_checkpoint(tempfile.gettempdir(), {"name": "ckpt-e9999.pt"}) is False   # missing: skipped
    finally:
        srv.close()


def test_fetch_resume_is_sha256_pinned():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            c = _client(srv.url, max_attempts=2)
            dest = Path(d) / "resume.pt"
            try:
                c.fetch_resume(dest, sha256="a" * 64)
                raise AssertionError("404 (resume not requested) accepted")
            except R.PermanentError:
                pass
            srv.resume = os.urandom(3000)
            good = hashlib.sha256(srv.resume).hexdigest()
            try:
                c.fetch_resume(dest, sha256="b" * 64)
                raise AssertionError("wrong bytes kept")
            except R.PermanentError:
                pass
            assert not dest.exists() and not list(Path(d).glob(".*part")), "nothing left behind on a mismatch"
            srv.plan = [503]   # transient: retried, the partial download starts over
            assert c.fetch_resume(dest, sha256=good) == {"bytes": 3000, "sha256": good}
            assert dest.read_bytes() == srv.resume
            gets = [h for m, p, h, _ in srv.requests if m == "GET"]
            assert gets and all(h.get("authorization", "").startswith("Bearer ogj_") for h in gets)
            try:
                c.fetch_resume(Path(d) / "x.pt", sha256=good, max_bytes=100)
                raise AssertionError("oversize accepted")
            except R.PermanentError:
                pass
            # CLI: downloads, then reports resume_downloaded (the token never printed)
            tf = Path(d) / "token"
            tf.write_text(TOKEN)
            p = subprocess.run([sys.executable, "-m", "runner.reporter", "fetch-resume", "--base-url", srv.url,
                                "--job", JOB, "--token-file", str(tf), "--state-dir", str(Path(d) / "state"),
                                "--dest", str(Path(d) / "cli.pt"), "--sha256", good, "--epoch", "2"],
                               cwd=ROOT, capture_output=True, text=True, timeout=60)
            assert p.returncode == 0, p.stderr[-2000:]
            assert TOKEN not in p.stdout + p.stderr
            assert (Path(d) / "cli.pt").read_bytes() == srv.resume
            ph = [e["data"] for e in srv.events.values() if e["type"] == "phase"]
            assert any(x["phase"] == "resume_downloaded" and x["epoch"] == "2" and x["sha256"] == good for x in ph)
    finally:
        srv.close()


def test_trainer_checkpoint_pure_parts():
    from runner import mnist_train as M
    assert "torch" not in sys.modules
    assert M.checkpoint_name(3) == "ckpt-e0003.pt" and re.fullmatch(R.CHECKPOINT_NAME, M.checkpoint_name(12))
    # every N epochs, never after the last one (that is model.pt), never with 0
    assert [e for e in range(1, 6) if M.should_checkpoint(e, 1, 5)] == [1, 2, 3, 4]
    assert [e for e in range(1, 7) if M.should_checkpoint(e, 2, 6)] == [2, 4]
    assert not any(M.should_checkpoint(e, 0, 5) for e in range(1, 6))
    assert not M.should_checkpoint(1, 1, 1)
    assert M.validate_config({})["checkpoint_every_epochs"] == 1
    assert M.validate_config({"checkpoint_every_epochs": 0})["checkpoint_every_epochs"] == 0
    for bad in ({"checkpoint_every_epochs": -1}, {"checkpoint_every_epochs": 21}, {"checkpoint_every_epochs": 1.5}):
        try:
            M.validate_config(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    cfg = M.validate_config({"epochs": 5})
    ck = {"format": M.CKPT_FORMAT, "template": M.TEMPLATE, "epoch": 2, "model": {}, "optimizer": {}}
    assert M.resume_start(ck, cfg) == 3
    for bad in ({**ck, "epoch": 5}, {**ck, "epoch": 0}, {**ck, "epoch": True}, {**ck, "template": "other"},
                {**ck, "format": "x"}, {k: v for k, v in ck.items() if k != "optimizer"}, ["not", "a", "dict"]):
        try:
            M.resume_start(bad, cfg)
            raise AssertionError(f"resumable: {bad}")
        except ValueError:
            pass
    with tempfile.TemporaryDirectory() as d:
        for e in (1, 2, 3):
            (Path(d) / M.checkpoint_name(e)).write_bytes(b"x")
        (Path(d) / "model.pt").write_bytes(b"x")
        assert M.prune_local(d) == ["ckpt-e0001.pt"]
        assert sorted(p.name for p in Path(d).iterdir()) == ["ckpt-e0002.pt", "ckpt-e0003.pt", "model.pt"]
    p = M._parse_args(["--out", "o", "--data", "d", "--resume", "r.pt", "--resume-sha256", "a" * 64])
    assert p.resume == "r.pt" and p.resume_sha256 == "a" * 64


def test_cli_emit_and_complete_never_print_token():
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "token"
            tf.write_text(TOKEN + "\n")
            (Path(d) / "gpu.txt").write_text("NVIDIA H100 80GB HBM3, 550.54.15, 81559 MiB, 9.0\n")
            common = ["--base-url", srv.url, "--job", JOB, "--token-file", str(tf), "--state-dir", str(Path(d) / "s")]
            p = subprocess.run([sys.executable, "-m", "runner.reporter", "emit", *common, "--type", "phase",
                                "--name", "gpu_detected", "--kv", "x=1", "--kv-file", f"gpus={Path(d) / 'gpu.txt'}"],
                               cwd=ROOT, capture_output=True, text=True, timeout=60)
            assert p.returncode == 0, p.stderr
            assert srv.events[1]["data"] == {"phase": "gpu_detected", "x": "1",
                                             "gpus": "NVIDIA H100 80GB HBM3, 550.54.15, 81559 MiB, 9.0\n"}
            p = subprocess.run([sys.executable, "-m", "runner.reporter", "complete", *common, "--outcome", "failed",
                                "--code", "bootstrap_failed", "--message", "nvidia-smi failed"],
                               cwd=ROOT, capture_output=True, text=True, timeout=60)
            assert p.returncode == 0 and srv.completes[-1]["summary"]["reason"] == "bootstrap_failed"
            assert srv.events[2]["data"]["phase"] == "bootstrap_failed"
            assert TOKEN not in p.stdout + p.stderr
            # wrong/malformed token file -> fails without echoing it
            tf.write_text("bad token; rm -rf /")
            p = subprocess.run([sys.executable, "-m", "runner.reporter", "flush", *common], cwd=ROOT,
                               capture_output=True, text=True, timeout=60)
            assert p.returncode != 0 and "rm -rf" not in p.stdout + p.stderr
    finally:
        srv.close()


def test_mnist_module_importable_without_torch():
    from runner import mnist_train as M
    assert "torch" not in sys.modules, "app server has no torch: mnist_train must import it lazily"
    assert M.validate_config({}) == M.DEFAULTS
    assert M.validate_config({"epochs": 2, "learning_rate": 0.01})["epochs"] == 2
    for bad in ({"epochs": 0}, {"epochs": 21}, {"epochs": 1.5}, {"epochs": True}, {"batch_size": 8},
                {"learning_rate": 5}, {"seed": -1}, {"cmd": "rm -rf /"}, {"limit_train": 0}):
        try:
            M.validate_config(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert M.OUTCOME_BY_EXIT[M.EXIT_OOM] == "oom" and R.EXIT_CODE_OUTCOME[M.EXIT_TIMEOUT] == "timeout"


# ---------------------------------------------------------------------------------------------- optional CPU run
def _torch_python():
    cand = os.environ.get("OG_TORCHENV") or str(_SCRATCH / "torchenv" / "Scripts" / "python.exe")
    return cand if Path(cand).exists() else None


def test_cpu_smoke_real_mnist():
    """LOCAL CPU verification (not a GPU run): reporter + real trainer + real MNIST, subset for speed."""
    py = _torch_python()
    if not py:
        print("  SKIP test_cpu_smoke_real_mnist: no CPU torch venv (set OG_TORCHENV)")
        return
    data = os.environ.get("OG_MNIST_DATA") or str(_SCRATCH / "cpu_run" / "data")
    srv = FakeOpenGrid()
    try:
        with tempfile.TemporaryDirectory() as d:
            tf = Path(d) / "token"
            tf.write_text(TOKEN)
            out = Path(d) / "out"
            p = subprocess.run(
                [sys.executable, "-m", "runner.reporter", "run", "--base-url", srv.url, "--job", JOB,
                 "--token-file", str(tf), "--state-dir", str(Path(d) / "state"), "--out", str(out),
                 "--max-seconds", "600", "--", py, "-m", "runner.mnist_train", "--allow-cpu", "--epochs", "1",
                 "--limit-train", "2000", "--limit-test", "1000", "--num-workers", "0",
                 "--out", str(out), "--data", data],
                cwd=ROOT, capture_output=True, text=True, timeout=900)
            assert p.returncode == 0, p.stderr[-3000:]
            (c,) = srv.completes
            assert c["outcome"] == "succeeded", c
            r = c["summary"]["result"]
            assert r["device"] == "cpu" and r["reload_matches"] is True and r["param_l2_change"] > 0
            assert r["test_accuracy"] > 0.80, r   # 2000 samples x 1 epoch; full-data numbers in methodology
            metrics = json.loads(srv.artifacts["metrics.json"])
            assert metrics["epochs"][0]["train_samples"] == 2000 and metrics["epochs"][0]["test_samples"] == 1000
            manifest = json.loads(srv.artifacts["manifest.json"])
            for n, m in manifest.items():
                assert hashlib.sha256(srv.artifacts[n]).hexdigest() == m["sha256"]
            env = json.loads(srv.artifacts["env.json"])
            assert env["torch"].startswith("2.4.1") and env["torchvision"].startswith("0.19.1")
            phases = [e["data"].get("phase") for e in sorted(srv.events.values(), key=lambda e: e["seq"])
                      if e["type"] == "phase"]
            for want in ("device_selected", "dataset_ready", "training_started", "epoch_completed",
                         "artifacts_uploaded", "training_completed"):
                assert want in phases, (want, phases)
            # independent reload in a fresh process reproduces the reported accuracy
            (out / "model_check.pt").write_bytes(srv.artifacts["model.pt"])
            code = ("import sys, json; from runner.mnist_train import load_model, evaluate, test_loader; "
                    f"m = load_model(r'{out / 'model_check.pt'}'); "
                    f"print(json.dumps(evaluate(m, test_loader(r'{data}', limit_test=1000, seed=42), 'cpu')))")
            q = subprocess.run([py, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300)
            assert q.returncode == 0, q.stderr[-2000:]
            again = json.loads(q.stdout.strip().splitlines()[-1])
            assert again["test_accuracy"] == r["test_accuracy"], (again, r)
            print(f"  cpu smoke: acc={r['test_accuracy']:.4f} reload={again['test_accuracy']:.4f} "
                  f"l2={r['param_l2_change']:.3f} events={len(srv.events)}")
    finally:
        srv.close()


def _trainer(py, out, data, *extra):
    p = subprocess.run([py, "-m", "runner.mnist_train", "--allow-cpu", "--limit-train", "1000", "--limit-test", "500",
                        "--num-workers", "0", "--out", str(out), "--data", data, *extra],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    evs = [json.loads(ln) for ln in p.stdout.splitlines() if ln.startswith('{"og": 1')]
    return p, evs


def test_cpu_checkpoint_resume_real_mnist():
    """LOCAL CPU verification: real trainer saves a checkpoint, a second process (a 'new machine') resumes from it."""
    py = _torch_python()
    if not py:
        print("  SKIP test_cpu_checkpoint_resume_real_mnist: no CPU torch venv (set OG_TORCHENV)")
        return
    data = os.environ.get("OG_MNIST_DATA") or str(_SCRATCH / "cpu_run" / "data")
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a", Path(d) / "b"
        p, evs = _trainer(py, a, data, "--epochs", "3", "--checkpoint-every-epochs", "1")
        assert p.returncode == 0, (p.stderr[-3000:], [e for e in evs if e["type"] == "error"])
        saved = [e["data"] for e in evs if e["type"] == "phase" and e["data"]["phase"] == "checkpoint_saved"]
        assert [s["epoch"] for s in saved] == [1, 2], "every epoch before the last"
        files = sorted(x.name for x in (a / "checkpoints").iterdir())
        assert files == ["ckpt-e0001.pt", "ckpt-e0002.pt"], files
        ck = a / "checkpoints" / "ckpt-e0001.pt"
        assert R.sha256_file(ck) == saved[0]["sha256"]
        manifest = json.loads((a / "manifest.json").read_text())
        assert not any(n.startswith("ckpt-") for n in manifest), "checkpoints are not results"
        # tampered pin -> refused before anything is loaded
        p, evs = _trainer(py, b, data, "--epochs", "3", "--resume", str(ck), "--resume-sha256", "0" * 64)
        assert p.returncode == 2 and any(e["type"] == "error" and e["data"]["code"] == "resume_failed" for e in evs)
        # resume on a "new machine" (fresh out dir) from epoch 1
        p, evs = _trainer(py, b, data, "--epochs", "3", "--resume", str(ck), "--resume-sha256", saved[0]["sha256"])
        assert p.returncode == 0, p.stderr[-3000:]
        (res,) = [e["data"] for e in evs if e["type"] == "phase" and e["data"]["phase"] == "resumed"]
        assert res["from_epoch"] == 1 and res["next_epoch"] == 2 and res["sha256"] == saved[0]["sha256"]
        assert [e["data"]["epoch"] for e in evs if e["type"] == "metric"] == [2, 3]
        (r,) = [e["data"] for e in evs if e["type"] == "result"]
        assert r["outcome"] == "succeeded" and r["resumed_from_epoch"] == 1 and r["param_l2_change"] > 0
        m = json.loads((b / "metrics.json").read_text())
        assert m["resumed_from"]["from_epoch"] == 1 and [e["epoch"] for e in m["prior_epochs"]] == [1]
        print(f"  cpu resume: from epoch 1 -> acc={r['test_accuracy']:.4f} l2(this run)={r['param_l2_change']:.3f}")


if __name__ == "__main__":
    for t in (test_parse_line, test_spool_seq_is_monotonic_and_durable, test_artifact_hashing,
              test_backoff_has_jitter_and_cap, test_events_batch_seq_and_retry, test_offline_spool_then_recover,
              test_auth_and_terminal_stop_delivery, test_permanent_rejection_is_moved_aside,
              test_artifact_upload_and_complete, test_undelivered_complete_is_retried, test_supervisor_success,
              test_supervisor_oom_and_timeout, test_supervisor_upload_failure_fails_job,
              test_supervisor_uploads_checkpoints_not_as_results, test_fetch_resume_is_sha256_pinned,
              test_trainer_checkpoint_pure_parts,
              test_cli_emit_and_complete_never_print_token, test_mnist_module_importable_without_torch,
              test_cpu_smoke_real_mnist, test_cpu_checkpoint_resume_real_mnist):
        t(); print(t.__name__, "ok")
