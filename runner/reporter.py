"""Runner -> OpenGrid callback client and on-instance supervisor. STDLIB ONLY (runs on the system python3 of a
fresh GPU image, before/without the training venv).

Wire format (JOBS_BRIEF "Runner callback API"; documented in methodology/runner.md):
  Authorization: Bearer ogj_<job_id>.<token>          (the ONLY credential; job-scoped, expiring, revocable)
  POST {base}/v1/jobs/{job}/runner/events     {"events": [{"seq": int, "type": phase|log|metric|gpu|result|error,
                                                          "at": iso8601, "data": {...}}, ...]}
  PUT  {base}/v1/jobs/{job}/runner/artifacts/{name}   raw bytes; X-Content-SHA256: <hex>
  POST {base}/v1/jobs/{job}/runner/complete   {"outcome": succeeded|failed|oom|timeout, "summary": {...}}

Delivery: every event gets a monotonically increasing seq (persisted counter, shared by every process on the
instance via a lock dir) and is written to an on-disk spool BEFORE any network attempt; a batch is deleted from
the spool only after a 2xx. The server ignores duplicate seqs, so retrying a batch whose response was lost is
safe. Transient failures (network, 408/429/5xx) retry with exponential backoff + full jitter; 401/403 (token
expired/revoked) and 409 (job already terminal) stop network delivery — the spool is kept for diagnosis.
The token is never printed: it is read from a root-only file, kept out of argv/env/log lines, and scrubbed
from any forwarded child output.

CLI (used by the bootstrap script):
  python3 -m runner.reporter emit     <common> --type phase --name gpu_detected [--kv k=v]... [--kv-file k=path]...
  python3 -m runner.reporter run      <common> --out DIR [--max-seconds N] -- <trainer command...>
  python3 -m runner.reporter complete <common> --outcome failed [--code X] [--message M]
  python3 -m runner.reporter flush    <common>
  python3 -m runner.reporter retry    <common>      (flush + re-send an undelivered complete)
  <common> = --base-url URL --job JOB_ID --token-file PATH --state-dir DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EVENT_TYPES = ("phase", "log", "metric", "gpu", "result", "error")
OUTCOMES = ("succeeded", "failed", "oom", "timeout")
_PROGRESS = re.compile(r"\s*\d{1,3}(\.\d+)?%\s*")
ARTIFACT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
UPLOAD_ARTIFACTS = ("model.pt", "config.json", "metrics.json", "env.json", "manifest.json")
MAX_LINE = 4000            # server truncates at 4 KB; we stay under it
MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 512 * 1024   # server bounds bodies at 1 MB
MAX_LOG_LINES = 5000       # beyond this, child output is counted, not forwarded
EXIT_CODE_OUTCOME = {0: "succeeded", 2: "failed", 3: "oom", 4: "timeout", 5: "failed"}
EXIT_CODE_REASON = {2: "training_failed", 3: "out_of_memory", 4: "training_timed_out", 5: "cuda_unavailable"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def diag(msg: str) -> None:
    """Local diagnostics (stderr -> /var/log/opengrid-job.log). Never pass secrets here."""
    print(f"[opengrid-reporter {now_iso()}] {msg}", file=sys.stderr, flush=True)


def parse_line(line: str):
    """A trainer stdout line -> (type, data, at) if it is a structured event, else None."""
    s = line.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return None
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("og") != 1 or obj.get("type") not in EVENT_TYPES:
        return None
    data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    at = obj.get("at") if isinstance(obj.get("at"), str) else now_iso()
    return obj["type"], data, at


# ---------------------------------------------------------------------------------------------- HTTP
class TransientError(Exception):
    """Retry later (network, timeout, 408/425/429/5xx)."""


class PermanentError(Exception):
    """This request will never succeed as sent (400/404/413/422, redirect...)."""


class AuthError(PermanentError):
    """401/403: token expired, revoked or wrong. Stop talking to the server."""


class JobTerminalError(PermanentError):
    """409: the job is already terminal; OpenGrid no longer accepts callbacks."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a redirect would drop the body / leak the header to another host
        return None


class Client:
    def __init__(self, base_url: str, job_id: str, token: str, *, timeout: float = 20.0, max_attempts: int = 6,
                 backoff_base: float = 1.0, backoff_cap: float = 60.0, sleep=time.sleep, rng=None):
        self.base = base_url.rstrip("/")
        self.job_id = job_id
        self._auth = f"Bearer ogj_{job_id}.{token}"
        self._token = token
        self.timeout, self.max_attempts = timeout, max_attempts
        self.backoff_base, self.backoff_cap = backoff_base, backoff_cap
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.disabled_reason = None   # set on auth failure / terminal job: no further network calls
        self._disabled_cls = PermanentError
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self):
        return f"Client(base={self.base!r}, job={self.job_id!r}, token=***)"

    def scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter: U(0.5, 1.5) x base x 2^attempt, capped."""
        return min(self.backoff_cap, self.backoff_base * (2 ** attempt)) * (0.5 + self.rng.random())

    def _once(self, method: str, path: str, body: bytes, headers: dict):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("User-Agent", "opengrid-runner")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(65536)
        except urllib.error.HTTPError as e:
            code = e.code
            detail = e.read(2000).decode("utf-8", "replace") if e.fp else ""
            msg = f"{method} {path} -> HTTP {code} {self.scrub(detail)[:300]}"
            if code in (401, 403):
                raise AuthError(msg) from None
            if code == 409:
                raise JobTerminalError(msg) from None
            if code in (408, 425, 429) or code >= 500:
                raise TransientError(msg) from None
            raise PermanentError(msg) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise TransientError(f"{method} {path} -> {type(e).__name__}: {self.scrub(str(e))[:200]}") from None

    def request(self, method: str, path: str, body: bytes, headers: dict, *, attempts: int | None = None):
        if self.disabled_reason:
            raise self._disabled_cls(f"delivery disabled: {self.disabled_reason}")
        attempts = attempts or self.max_attempts
        last = None
        for i in range(attempts):
            try:
                status, data = self._once(method, path, body, headers)
                if 300 <= status < 400:
                    raise PermanentError(f"{method} {path} -> HTTP {status} (redirects refused)")
                return status, data
            except (AuthError, JobTerminalError) as e:
                self.disabled_reason, self._disabled_cls = str(e), type(e)
                raise
            except TransientError as e:
                last = e
                if i + 1 < attempts:
                    delay = self.backoff(i)
                    diag(f"transient: {e}; retry {i + 1}/{attempts - 1} in {delay:.1f}s")
                    self.sleep(delay)
        raise last

    # -- endpoints
    def post_events(self, events: list, *, attempts=None):
        body = json.dumps({"events": events}, separators=(",", ":")).encode()
        return self.request("POST", f"/v1/jobs/{self.job_id}/runner/events", body,
                            {"Content-Type": "application/json"}, attempts=attempts)

    def put_artifact(self, name: str, data: bytes, *, attempts=None):
        if not ARTIFACT_NAME.match(name):
            raise PermanentError(f"artifact name not allowed: {name!r}")
        return self.request("PUT", f"/v1/jobs/{self.job_id}/runner/artifacts/{name}", data,
                            {"Content-Type": "application/octet-stream", "X-Content-SHA256": sha256_bytes(data)},
                            attempts=attempts)

    def complete(self, outcome: str, summary: dict, *, attempts=None):
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        body = json.dumps({"outcome": outcome, "summary": summary}, separators=(",", ":"), default=str).encode()
        return self.request("POST", f"/v1/jobs/{self.job_id}/runner/complete", body,
                            {"Content-Type": "application/json"}, attempts=attempts)


# ---------------------------------------------------------------------------------------------- spool
class Spool:
    """Durable, cross-process event queue: state_dir/seq (last seq) + state_dir/spool/<seq>.json."""

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        self.spool = self.dir / "spool"
        self.spool.mkdir(parents=True, exist_ok=True)
        self._lockdir = self.dir / "seq.lock"
        self._tlock = threading.Lock()

    def _lock(self, timeout=30.0):
        t0 = time.monotonic()
        while True:
            try:
                os.mkdir(self._lockdir)   # atomic on POSIX and Windows
                return
            except FileExistsError:
                try:  # a crashed holder must not wedge the job forever
                    if time.time() - self._lockdir.stat().st_mtime > 30:
                        os.rmdir(self._lockdir)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError("spool seq lock")
                time.sleep(0.02)

    def _unlock(self):
        try:
            os.rmdir(self._lockdir)
        except FileNotFoundError:
            pass

    def add(self, type_: str, data: dict, at: str | None = None) -> dict:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"event type must be one of {EVENT_TYPES}")
        with self._tlock:
            self._lock()
            try:
                p = self.dir / "seq"
                seq = (int(p.read_text()) if p.exists() else 0) + 1
                ev = {"seq": seq, "type": type_, "at": at or now_iso(), "data": data}
                tmp = self.spool / f".{seq:012d}.tmp"
                tmp.write_text(json.dumps(ev, separators=(",", ":"), default=str), encoding="utf-8")
                os.replace(tmp, self.spool / f"{seq:012d}.json")
                tmp_seq = self.dir / ".seq.tmp"
                tmp_seq.write_text(str(seq))
                os.replace(tmp_seq, p)
            finally:
                self._unlock()
        return ev

    def pending(self) -> list:
        out = []
        for f in sorted(self.spool.glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                f.rename(f.with_suffix(".bad"))   # never block the queue on a corrupt file
        return out

    def remove(self, seqs) -> None:
        for s in seqs:
            try:
                (self.spool / f"{int(s):012d}.json").unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------------------------- reporter
def _bound(data: dict) -> dict:
    """Keep single events well under the server's 1 MB body / 4 KB log line limits."""
    s = json.dumps(data, default=str)
    if len(s) <= 64 * 1024:
        return data
    return {"truncated": True, "original_bytes": len(s), "preview": s[:4000]}


class Reporter:
    def __init__(self, client: Client, spool: Spool):
        self.client, self.spool = client, spool
        self._flush_lock = threading.Lock()

    def emit(self, type_: str, data: dict, at: str | None = None, *, flush=True) -> dict:
        ev = self.spool.add(type_, _bound(data), at)
        if flush:
            self.flush(attempts=2)
        return ev

    def phase(self, name: str, **data):
        return self.emit("phase", {"phase": name, **data})

    def flush(self, attempts=None) -> bool:
        """Send every spooled event in seq order, in bounded batches. True when the spool is empty."""
        with self._flush_lock:
            pending = self.spool.pending()
            while pending:
                batch, size = [], 0
                for ev in pending:
                    n = len(json.dumps(ev, default=str)) + 1
                    if batch and (len(batch) >= MAX_BATCH_EVENTS or size + n > MAX_BATCH_BYTES):
                        break
                    batch.append(ev)
                    size += n
                try:
                    self.client.post_events(batch, attempts=attempts)
                except (AuthError, JobTerminalError) as e:
                    diag(f"events not delivered ({len(pending)} spooled kept): {e}")
                    return False
                except PermanentError as e:
                    if self.client.disabled_reason:
                        return False
                    # The server refused this batch as malformed; it will never be accepted. Keep a copy.
                    diag(f"events batch rejected permanently, moved aside: {e}")
                    for ev in batch:
                        f = self.spool.spool / f"{ev['seq']:012d}.json"
                        if f.exists():
                            f.rename(f.with_suffix(".rejected"))
                    pending = pending[len(batch):]
                    continue
                except TransientError as e:
                    diag(f"OpenGrid unreachable, {len(pending)} events spooled for retry: {e}")
                    return False
                self.spool.remove(ev["seq"] for ev in batch)
                pending = pending[len(batch):]
            return True

    def upload_artifacts(self, out_dir, names=UPLOAD_ARTIFACTS) -> dict:
        uploaded, failed = {}, {}
        for name in names:
            p = Path(out_dir) / name
            if not p.is_file():
                failed[name] = "missing"
                continue
            data = p.read_bytes()
            digest = sha256_bytes(data)
            try:
                self.client.put_artifact(name, data)
                uploaded[name] = {"sha256": digest, "bytes": len(data)}
            except (PermanentError, TransientError) as e:
                failed[name] = str(e)[:300]
        return {"uploaded": uploaded, "failed": failed}

    def complete(self, outcome: str, summary: dict) -> bool:
        self.flush(attempts=4)
        try:
            self.client.complete(outcome, summary, attempts=max(self.client.max_attempts, 8))
            self._mark_done(outcome)
            return True
        except JobTerminalError:
            self._mark_done(outcome)
            return True   # already terminal (e.g. cancelled): nothing more to say
        except (PermanentError, TransientError) as e:
            diag(f"complete not delivered: {e}")
            (self.spool.dir / "complete.pending.json").write_text(
                json.dumps({"outcome": outcome, "summary": summary}, default=str), encoding="utf-8")
            return False


    def _mark_done(self, outcome):
        """complete.done tells the bootstrap that a complete reached OpenGrid (or the job was already terminal)."""
        (self.spool.dir / "complete.done").write_text(outcome, encoding="utf-8")

    def retry_pending(self) -> bool:
        """True when nothing is left to deliver (spool empty and no undelivered complete)."""
        flushed = self.flush()
        p = self.spool.dir / "complete.pending.json"
        if not p.exists():
            return flushed
        body = json.loads(p.read_text(encoding="utf-8"))
        try:
            self.client.complete(body["outcome"], body["summary"])
        except JobTerminalError:
            pass
        except (PermanentError, TransientError) as e:
            diag(f"complete still not delivered: {e}")
            return False
        self._mark_done(body["outcome"])
        p.unlink()
        return flushed


# ---------------------------------------------------------------------------------------------- supervisor
def _gpu_sample():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total,"
                              "temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    keys = ("index", "utilization_gpu_pct", "memory_used_mib", "memory_total_mib", "temperature_c", "power_w")
    return {"gpus": [dict(zip(keys, (c.strip() for c in line.split(","))))
                     for line in out.stdout.strip().splitlines() if line.strip()]}


def run_supervised(rep: Reporter, cmd: list, *, out_dir, max_seconds=None, flush_every=2.0,
                   gpu_every=15.0, env=None) -> dict:
    """Run the trainer, forward its events/log lines, enforce the wall clock, upload artifacts, post complete."""
    t0 = time.monotonic()
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONUNBUFFERED"] = "1"
    for k in list(child_env):
        if k.startswith("OG_TOKEN"):
            child_env.pop(k)
    popen_kw = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env, **popen_kw)
    state = {"lines": 0, "dropped": 0, "result": None, "error": None, "phases": []}
    stop = threading.Event()
    lock = threading.Lock()

    def reader(stream, name):
        for line in stream:
            line = rep.client.scrub(line.rstrip("\r\n"))
            parsed = parse_line(line) if name == "stdout" else None
            if parsed:
                type_, data, at = parsed
                if type_ == "result":
                    state["result"] = data
                elif type_ == "error":
                    state["error"] = data
                elif type_ == "phase":
                    state["phases"].append(data.get("phase"))
                rep.emit(type_, data, at, flush=False)
                continue
            if not line.strip() or (_PROGRESS.fullmatch(line) and not line.strip().startswith("100")):
                continue   # download progress ticks ("0.3%", "0.7%", ...) are noise, not logs
            with lock:
                state["lines"] += 1
                if state["lines"] > MAX_LOG_LINES:
                    state["dropped"] += 1
                    continue
            rep.emit("log", {"stream": name, "line": line[:MAX_LINE]}, flush=False)

    def flusher():
        last_gpu = 0.0
        have_smi = shutil.which("nvidia-smi") is not None
        while not stop.wait(flush_every):
            rep.flush(attempts=1)
            if have_smi and gpu_every and time.monotonic() - last_gpu >= gpu_every:
                last_gpu = time.monotonic()
                s = _gpu_sample()
                if s:
                    rep.emit("gpu", s, flush=False)

    threads = [threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True),
               threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True),
               threading.Thread(target=flusher, daemon=True)]
    for t in threads:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=max_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        diag(f"wall-clock limit {max_seconds}s reached; stopping trainer")
        _kill(proc)
    for t in threads[:2]:
        t.join(timeout=30)
    stop.set()
    threads[2].join(timeout=30)
    if state["dropped"]:
        rep.emit("log", {"stream": "reporter", "line": f"{state['dropped']} further log lines not forwarded"},
                 flush=False)

    code = proc.returncode
    if timed_out:
        outcome, reason = "timeout", "training_timed_out"
    else:
        outcome = EXIT_CODE_OUTCOME.get(code, "failed")
        reason = EXIT_CODE_REASON.get(code, "training_failed" if code else None)
    summary = {"exit_code": code, "runtime_seconds": round(time.monotonic() - t0, 3), "reason": reason,
               "env_source": os.environ.get("OG_ENV_SOURCE", "unknown"),
               "result": state["result"], "error": state["error"]}

    # Artifacts: upload whatever exists (partial outputs help diagnose failures too).
    rep.phase("uploading_artifacts")
    names = UPLOAD_ARTIFACTS if outcome == "succeeded" else tuple(
        n for n in UPLOAD_ARTIFACTS if (Path(out_dir) / n).is_file())
    up = rep.upload_artifacts(out_dir, names)
    summary["artifacts"] = up
    if outcome == "succeeded":
        manifest_ok = _manifest_matches(out_dir, up["uploaded"])
        if up["failed"] or not manifest_ok:
            outcome, reason = "failed", "artifact_upload_failed"
            summary["reason"] = reason
            summary["manifest_ok"] = manifest_ok
    rep.phase("artifacts_uploaded", uploaded=sorted(up["uploaded"]), failed=up["failed"])
    if outcome == "succeeded":
        rep.phase("training_completed", test_accuracy=(state["result"] or {}).get("test_accuracy"))
    else:
        rep.phase("training_failed", outcome=outcome, reason=reason, exit_code=code,
                  error=(state["error"] or {}).get("code"))
    delivered = rep.complete(outcome, summary)
    return {"outcome": outcome, "summary": summary, "complete_delivered": delivered}


def _manifest_matches(out_dir, uploaded: dict) -> bool:
    """The uploaded bytes must be exactly what the trainer hashed in manifest.json."""
    try:
        manifest = json.loads((Path(out_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return all(uploaded.get(n, {}).get("sha256") == m.get("sha256") for n, m in manifest.items())


def _kill(proc):
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=15)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        proc.wait()


# ---------------------------------------------------------------------------------------------- CLI
def _read_token(path) -> str:
    tok = Path(path).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", tok):
        raise SystemExit("token file malformed")   # never echo it
    return tok


def _common(ap):
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--timeout", type=float, default=20.0)


def _kv(pairs, files) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k] = v
    for p in files or []:
        k, _, path = p.partition("=")
        try:
            out[k] = Path(path).read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError as e:
            out[k] = f"<unreadable: {type(e).__name__}>"
    return out


def build(a) -> Reporter:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a.job):
        raise SystemExit("bad job id")
    client = Client(a.base_url, a.job, _read_token(a.token_file), timeout=a.timeout)
    return Reporter(client, Spool(a.state_dir))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="python -m runner.reporter")
    sub = ap.add_subparsers(dest="action", required=True)
    e = sub.add_parser("emit")
    _common(e)
    e.add_argument("--type", default="phase", choices=EVENT_TYPES)
    e.add_argument("--name", help="phase name (type=phase) or error code (type=error)")
    e.add_argument("--kv", action="append", help="key=value (string) added to data")
    e.add_argument("--kv-file", action="append", help="key=path: file contents (<=8000 chars) added to data")
    r = sub.add_parser("run")
    _common(r)
    r.add_argument("--out", required=True)
    r.add_argument("--max-seconds", type=float)
    c = sub.add_parser("complete")
    _common(c)
    c.add_argument("--outcome", required=True, choices=OUTCOMES)
    c.add_argument("--code")
    c.add_argument("--message")
    f = sub.add_parser("flush")
    _common(f)
    rt = sub.add_parser("retry", help="flush the spool and re-send a complete that was not delivered")
    _common(rt)
    a = ap.parse_args(argv)
    rep = build(a)

    if a.action == "emit":
        data = _kv(a.kv, a.kv_file)
        if a.name:
            data["phase" if a.type == "phase" else "code"] = a.name
        rep.emit(a.type, data)
        return 0
    if a.action == "flush":
        return 0 if rep.flush() else 1
    if a.action == "retry":
        return 0 if rep.retry_pending() else 1
    if a.action == "complete":
        if a.outcome != "succeeded":
            rep.phase(a.code or "failed", message=a.message)
        ok = rep.complete(a.outcome, {"reason": a.code, "message": a.message,
                                      "env_source": os.environ.get("OG_ENV_SOURCE", "unknown")})
        return 0 if ok else 1
    if not cmd:
        ap.error("run needs a command after --")
    res = run_supervised(rep, cmd, out_dir=a.out, max_seconds=a.max_seconds)
    diag(f"outcome={res['outcome']} complete_delivered={res['complete_delivered']}")
    return 0 if res["outcome"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
