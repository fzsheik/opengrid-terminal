"""CLI tests against a mock OpenGrid API (httpx.MockTransport). No network, no real server.

    uv run --project cli python cli/tests/test_cli.py
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from _support import (BAD_KEY, BASE, LIMITED_KEY, SECRET, MockAPI, install_analyzer_stub, run_cli,  # noqa: E402
                      run_tests)

STUB = install_analyzer_stub()
PROJECT = tempfile.mkdtemp(prefix="og-project-")
(Path(PROJECT) / "train.py").write_text("import torch\n")


def js(out: str) -> dict:
    return json.loads(out)


# ---------------------------------------------------------------- parsing and JSON output

def test_help_and_version():
    code, out, _ = run_cli(["--help"], MockAPI())
    assert code == 0 and "exit codes" in out and "No command can spend money" in out
    code, out, _ = run_cli(["version", "--json"], MockAPI())
    assert code == 0 and js(out)["data"]["opengrid_cli"]


def test_no_spend_commands_exist():
    from opengrid_cli.cli import build_parser

    p = build_parser()
    names = set()
    for a in p._subparsers._group_actions:
        for name, sp in a.choices.items():
            names.add(name)
            for sa in getattr(sp, "_subparsers", None)._group_actions if sp._subparsers else []:
                names.update(sa.choices)
    for forbidden in ("launch", "execute", "submit", "provision", "approve", "run"):
        assert forbidden not in names, forbidden
    code, _, err = run_cli(["route", "execute", "h100"], MockAPI())
    assert code == 2, code


def test_gpu_search_json_and_filters():
    api = MockAPI()
    code, out, _ = run_cli(["gpu", "search", "--json", "--vendor", "nvidia", "--min-vram", "40"], api)
    d = js(out)
    assert code == 0 and d["ok"] and d["command"] == "gpu search"
    slugs = [x["slug"] for x in d["data"]["items"]]
    assert slugs == ["h100-80gb-sxm5", "a100-80gb-sxm4"], slugs
    assert d["data"]["price_kind"] == "observed" and d["data"]["truncated"] is False
    assert all("authorization" not in r.headers for r in api.requests)  # public data: no key sent
    code, out, _ = run_cli(["--json", "gpu", "search", "--max-price", "1.5", "--sort", "price"], api)
    assert [x["slug"] for x in js(out)["data"]["items"]] == ["a100-80gb-sxm4", "h100-80gb-sxm5"]
    code, out, _ = run_cli(["gpu", "search", "--available", "--json", "--limit", "1"], api)
    d = js(out)["data"]
    assert d["returned"] == 1 and d["total_matched"] == 2 and d["truncated"] is True
    code, out, _ = run_cli(["gpu", "search", "--model", "H100"], api)
    assert code == 0 and "h100-80gb-sxm5" in out and "observed" in out


def test_gpu_show_compare_market_providers():
    api = MockAPI()
    code, out, _ = run_cli(["gpu", "show", "h100-80gb-sxm5", "--json"], api)
    d = js(out)["data"]
    assert code == 0 and d["kind"] == "gpu" and d["hardware"]["vram_gb"] == 80
    assert [p["provider"] for p in d["cheapest_by_provider"]] == ["lium", "lambda", "runpod"]  # cheapest first
    code, out, _ = run_cli(["gpu", "show", "h100-80gb-sxm5", "--providers", "1"], api)
    assert "first 1 providers" in out and "not a workload benchmark" in out
    code, out, _ = run_cli(["gpu", "show", "h100"], api)
    assert code == 0 and "GPU family" in out and "h100-80gb-sxm5" in out
    code, out, err = run_cli(["gpu", "show", "nope"], api)
    assert code == 4 and "unknown GPU" in err and "request id" in err
    code, out, _ = run_cli(["gpu", "show", "nope", "--json"], api)
    e = js(out)
    assert code == 4 and e["ok"] is False and e["error"]["status"] == 404 and e["error"]["exit_code"] == 4
    code, out, _ = run_cli(["gpu", "compare", "h100-80gb-sxm5", "a100-80gb-sxm4", "--json"], api)
    d = js(out)["data"]
    assert d["a"]["median_price_per_capability_hour"]["per_gb_vram_hour"] == 0.054 and d["b"]["slug"] == "a100-80gb-sxm4"
    code, out, _ = run_cli(["gpu", "market", "h100-80gb-sxm5", "--limit", "3", "--json"], api)
    d = js(out)["data"]
    assert len(d["best_available_now"]) == 3 and d["current"]["lowest"] == 1.3
    code, out, _ = run_cli(["providers"], api)
    assert code == 0 and "Lambda" in out


def test_path_segments_are_escaped():
    api = MockAPI()
    run_cli(["gpu", "show", "../../v1/me"], api)
    assert api.requests[-1].url.raw_path == b"/v1/gpus/..%2F..%2Fv1%2Fme", api.requests[-1].url.raw_path
    n = len(api.requests)
    code, _, err = run_cli(["gpu", "show", ".."], api)
    assert code == 6 and len(api.requests) == n, err


# ---------------------------------------------------------------- auth: missing / invalid key, never printed

def test_missing_key_gives_scope_message():
    code, out, err = run_cli(["jobs", "list"], MockAPI())
    assert code == 3, code
    assert "deployments:read" in err and "OPENGRID_API_KEY" in err and "opengrid auth login" in err, err
    code, out, err = run_cli(["route", "preview", "h100-80gb-sxm5"], MockAPI())
    assert code == 3 and "route:preview" in err


def test_invalid_key_and_scope():
    code, out, err = run_cli(["jobs", "list"], MockAPI(), key=BAD_KEY)
    assert code == 3 and "rejected" in err and BAD_KEY not in out + err, err
    code, out, err = run_cli(["route", "preview", "h100-80gb-sxm5", "--json"], MockAPI(), key=LIMITED_KEY)
    e = js(out)
    assert code == 3 and "route:preview" in e["error"]["message"] and LIMITED_KEY not in out + err


def test_key_never_printed():
    api = MockAPI()
    for argv in (["doctor"], ["doctor", "--json"], ["auth", "status"], ["auth", "status", "--json"],
                 ["jobs", "list", "--json"], ["jobs", "status", "job-a37ddf8cf1c1c13f"], ["gpu", "search"],
                 ["route", "preview", "h100-80gb-sxm5"], ["mcp", "config", "claude"], ["mcp", "config", "vscode"]):
        code, out, err = run_cli(argv, api, key=SECRET)
        assert SECRET not in out + err, argv
        assert SECRET[13:] not in out + err, argv
    assert any(r.headers.get("authorization") == f"Bearer {SECRET}" for r in api.requests)


def test_server_echoing_the_key_is_redacted():
    api = MockAPI()
    api.overrides["/v1/jobs"] = lambda r: httpx.Response(
        422, json={"detail": f"bad header Authorization: Bearer {SECRET} (key {SECRET})"})
    code, out, err = run_cli(["jobs", "list"], api, key=SECRET)
    assert code == 1 and SECRET not in out + err and "[REDACTED]" in err, err
    code, out, err = run_cli(["jobs", "list", "--json"], api, key=SECRET)
    assert SECRET not in out + err


def test_redact_function():
    from opengrid_cli.client import key_hint, redact

    s = redact(f"x {SECRET} y Bearer abcdefghijkl ogj_job-1.tok123456", None)
    assert SECRET not in s and "abcdefghijkl" not in s and "tok123456" not in s
    assert redact("my-secret-value", "my-secret-value") == "[REDACTED]"
    assert key_hint(SECRET) == SECRET[:13] + "..." and len(key_hint(SECRET)) < 20


def test_auth_login_stores_0600_and_never_prints():
    api = MockAPI()
    code, out, err = run_cli(["auth", "login", "--stdin"], api, stdin=SECRET + "\n")
    assert code == 0, err
    from opengrid_cli.client import credentials_path, load_stored_key

    p = credentials_path()
    assert p.exists() and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert load_stored_key() == SECRET and SECRET not in out + err
    code, out, err = run_cli(["auth", "status", "--json"], api)
    d = js(out)["data"]
    assert d["key_source"] == "file" and "route:preview" in d["scopes"] and SECRET not in out
    code, out, _ = run_cli(["jobs", "list", "--json"], api)  # the stored key is used
    assert code == 0
    code, out, _ = run_cli(["auth", "logout"], api)
    assert code == 0 and not p.exists()
    code, out, err = run_cli(["auth", "login", "--stdin"], api, stdin=BAD_KEY + "\n")
    assert code == 3 and not p.exists() and BAD_KEY not in out + err
    code, out, err = run_cli(["auth", "login", "--stdin"], api, stdin="not-a-key\n")
    assert code == 6 and not p.exists()
    code, out, err = run_cli(["auth", "login"], api, tty=False)
    assert code == 6 and "--stdin" in err


# ---------------------------------------------------------------- analyze / estimate / recommend and --yes

def test_analyze_local_only():
    api = MockAPI()
    code, out, _ = run_cli(["analyze", PROJECT, "--json", "--exclude", "data/**"], api)
    d = js(out)["data"]
    assert code == 0 and d["requirements"]["model"]["parameters_b"] == 8.03 and api.requests == []
    assert STUB.calls[-1][2] == ("data/**",)
    code, out, _ = run_cli(["analyze", PROJECT], api)
    assert "nothing was sent" in out and ".env" in out and "config.json:12" in out and api.requests == []
    code, out, _ = run_cli(["analyze", PROJECT, "--dry-run"], api)
    assert code == 0 and "WOULD be read" in out and "train.py" in out and STUB.calls[-1][0] == "plan"
    assert api.requests == []


def test_estimate_requires_yes_when_not_a_tty():
    api = MockAPI()
    code, out, err = run_cli(["estimate", PROJECT], api, tty=False)
    assert code == 6 and "--yes" in err and api.requests == [], (code, err)
    code, out, err = run_cli(["estimate", PROJECT, "--json"], api, tty=False)
    assert code == 6 and js(out)["error"]["code"] == "confirmation_required" and api.requests == []


def test_estimate_prompt_answers():
    api = MockAPI()
    code, out, err = run_cli(["estimate", PROJECT], api, tty=True, stdin="n\n")
    assert code == 6 and api.requests == [] and "nothing was sent" in err
    code, out, err = run_cli(["estimate", PROJECT], api, tty=True, stdin="y\n")
    assert code == 0 and len(api.requests) == 1, err
    assert "requirements to send" in out and "memory (estimated" in out and "runtime: not estimated" in out


def test_estimate_sends_only_requirements():
    api = MockAPI()
    code, out, err = run_cli(["estimate", PROJECT, "--yes", "--json", "--budget", "50", "--method", "qlora"], api)
    assert code == 0, err
    [body] = api.bodies("/v1/recommend/estimate")
    allowed = {"workload_type", "frameworks", "model", "precision", "quantization", "training", "inference",
               "constraints", "hardware_hints", "evidence", "source"}
    assert set(body) <= allowed, body
    assert body["training"]["method"] == "qlora" and body["constraints"]["budget_usd"] == 50  # flags override
    assert body["model"]["parameters_b"] == 8.03 and body["source"] == "analyzer"
    assert "import torch" not in json.dumps(body)
    d = js(out)["data"]
    assert d["estimate"]["memory"]["kind"] == "estimated" and d["requirements_sent"] == body
    assert "will_send" in err  # what is sent is shown (stderr in --json mode)


def test_estimate_flags_only_no_prompt():
    api = MockAPI()
    code, out, err = run_cli(["estimate", "--params", "7", "--method", "lora", "--json"], api, tty=False)
    assert code == 0, err
    [body] = api.bodies("/v1/recommend/estimate")
    assert body == {"workload_type": "fine_tuning", "model": {"parameters_b": 7.0}, "training": {"method": "lora"},
                    "source": "api"}, body
    code, out, err = run_cli(["estimate"], api)
    assert code == 6 and "nothing to send" in err


def test_recommend_human_and_json():
    api = MockAPI()
    code, out, err = run_cli(["recommend", "--params", "8", "--method", "lora", "--limit", "5"], api)
    assert code == 0, err
    for s in ("cheapest market", "best executable", "[balanced]", "headroom", "cost 1 h", "1 hour of runtime",
              "not included: storage, egress", "not executable", "sign in / API key required", "excluded:",
              "Nothing was launched", "observed"):
        assert s in out, s
    assert api.bodies("/v1/recommend")[0]["limit"] == 5
    code, out, err = run_cli(["recommend", PROJECT, "--json"], api, tty=False)
    assert code == 6
    code, out, err = run_cli(["recommend", PROJECT, "--json", "--yes"], api, tty=False)
    d = js(out)["data"]
    assert code == 0 and d["recommendation"]["picks"]["best_executable"] is None


# ---------------------------------------------------------------- route preview, jobs, demo

def test_route_preview_says_it_records_a_ticket():
    api = MockAPI()
    code, out, err = run_cli(["route", "preview", "h100-80gb-sxm5", "--count", "2", "--region", "US",
                              "--max-price", "3"], api, key=SECRET)
    assert code == 0, err
    assert "preview ticket" in out and "NEVER provisions" in out and "provisioned: no" in out
    [body] = api.bodies("/v1/route/preview")
    assert body == {"gpu": "h100-80gb-sxm5", "count": 2, "mode": "BALANCED", "region": "US",
                    "max_price_per_gpu_hour": 3.0}
    code, out, err = run_cli(["route", "preview", "h100-80gb-sxm5", "--json"], api, key=SECRET)
    d = js(out)["data"]
    assert d["provisioned"] is False and len(d["alternatives"]) == 5 and "raw_secret_field" not in d["selected"]
    assert "NEVER provisions" in err  # notice on stderr keeps stdout pure JSON


def test_jobs_read_commands():
    api = MockAPI()
    code, out, _ = run_cli(["jobs", "list", "--json"], api, key=SECRET)
    assert code == 0 and js(out)["data"]["items"][0]["job_id"] == "job-a37ddf8cf1c1c13f"
    code, out, _ = run_cli(["jobs", "status", "job-a37ddf8cf1c1c13f"], api, key=SECRET)
    assert code == 0 and "SIMULATED" in out
    code, out, _ = run_cli(["jobs", "logs", "job-a37ddf8cf1c1c13f", "--follow", "--interval", "0"], api, key=SECRET)
    assert code == 0 and "epoch 1 loss" in out and "training_completed" in out
    code, out, err = run_cli(["jobs", "status", "../admin"], api, key=SECRET)
    assert code == 6 and "not a job id" in err


def test_jobs_cancel_needs_yes_and_idempotency_key():
    api = MockAPI()
    code, out, err = run_cli(["jobs", "cancel", "job-a37ddf8cf1c1c13f"], api, key=SECRET, tty=True, stdin="y\n")
    assert code == 6 and "--yes" in err and api.requests == []
    code, out, err = run_cli(["jobs", "cancel", "job-a37ddf8cf1c1c13f", "--yes", "--json"], api, key=SECRET)
    assert code == 0, err
    [req] = [r for r in api.requests if r.url.path.endswith("/cancel")]
    assert req.method == "POST" and req.headers["idempotency-key"].startswith("cli-")
    assert js(out)["data"]["idempotency_key"] == req.headers["idempotency-key"]
    code, out, err = run_cli(["jobs", "cancel", "job-a37ddf8cf1c1c13f", "--yes", "--idempotency-key",
                              "retry-key-123"], api, key=SECRET)
    assert code == 0 and api.requests[-1].headers["idempotency-key"] == "retry-key-123"
    code, out, err = run_cli(["jobs", "cancel", "job-a37ddf8cf1c1c13f", "--yes"], api, key=LIMITED_KEY)
    assert code == 3 and "route:execute" in err


def test_demo_train_is_sandbox_only():
    api = MockAPI()
    code, out, err = run_cli(["demo", "train", "--epochs", "2"], api, key=SECRET)
    assert code == 0, err
    [body] = api.bodies("/v1/jobs")
    assert body["sandbox"] is True and body["template"] == "pytorch-mnist" and body["config"]["epochs"] == 2
    assert "SIMULATED" in out and "no GPU" in out
    assert [r for r in api.requests if r.method == "POST"][0].headers.get("idempotency-key")
    api.job_response = dict(api.job_response, sandbox=False, simulated=False)  # a server that did NOT sandbox it
    api.overrides["/v1/jobs"] = lambda r: httpx.Response(201, json={"data": api.job_response, "meta": {}})
    code, out, err = run_cli(["demo", "train"], api, key=SECRET)
    assert code == 1 and "did not confirm" in err
    code, _, _ = run_cli(["demo", "train", "--sandbox=false"], MockAPI(), key=SECRET)
    assert code == 2  # there is no way to ask for a non-sandbox run


# ---------------------------------------------------------------- mcp config, doctor

from opengrid_cli import hosts  # noqa: E402


def test_mcp_config_snippets():
    for host, needles in (
            ("claude", ("${OPENGRID_API_KEY:-}", "Bearer ${OPENGRID_API_KEY}", "--header 'Authorization: Bearer")),
            ("cursor", ("${env:OPENGRID_API_KEY}", "Bearer ${env:OPENGRID_API_KEY}")),
            ("codex", ('env_vars = ["OPENGRID_API_KEY"]', 'bearer_token_env_var = "OPENGRID_API_KEY"',
                       "--bearer-token-env-var OPENGRID_API_KEY")),
            ("vscode", ("${input:opengrid-api-key}", "Bearer ${input:opengrid-api-key}"))):
        code, out, err = run_cli(["mcp", "config", host], MockAPI(), key=SECRET)
        assert code == 0 and SECRET not in out + err, host
        for n in needles:
            assert n in out, (host, n)
        assert "uvx --from" in out or '"uvx"' in out, host
        code, out, _ = run_cli(["mcp", "config", host, "--json"], MockAPI(), key=SECRET)
        d = js(out)["data"]
        assert d["config"] and SECRET not in out
        assert [o["mode"] for o in d["options"]] == ["hosted", "uvx", "installed"], host
        assert d["mode"] == "hosted" and d["url"] == BASE  # OPENGRID_URL
        for o in d["options"]:
            if o["format"] == "json":
                json.loads(o["config"])
            assert not re.search(r"opg_live_[A-Za-z0-9]", o["config"] + (o["cli"] or ""))  # never a literal key
        assert f"{BASE}/mcp" in d["options"][0]["config"], d["options"][0]
    saved = os.environ.pop("OPENGRID_URL")
    try:  # without --url / OPENGRID_URL the snippets point at production, not a local dev server
        code, out, _ = run_cli(["mcp", "config", "codex", "--json"], MockAPI())
        assert 'url = "https://opengrid-terminal-production.up.railway.app/mcp"' in js(out)["data"]["config"]
    finally:
        os.environ["OPENGRID_URL"] = saved
    code, out, _ = run_cli(["mcp", "config", "vscode", "--json", "--mode", "installed"], MockAPI())
    d = js(out)["data"]
    cfg = json.loads(d["config"])
    assert len(d["options"]) == 1 and cfg["inputs"][0]["password"] is True and cfg["servers"]["opengrid"]["type"] == "stdio"
    code, out, _ = run_cli(["mcp", "config", "vscode", "--json", "--mode", "hosted"], MockAPI())
    cfg = json.loads(js(out)["data"]["config"])
    assert cfg["servers"]["opengrid"]["type"] == "http" and cfg["inputs"][0]["password"] is True
    code, out, _ = run_cli(["mcp", "config", "claude", "--json", "--mode", "uvx", "--url", "http://localhost:8000"],
                           MockAPI())
    cfg = json.loads(js(out)["data"]["config"])["mcpServers"]["opengrid"]
    assert cfg["command"] == "uvx" and cfg["args"] == ["--from", hosts.GIT_SPEC, "opengrid", "mcp"]
    assert hosts.GIT_SPEC.endswith("github.com/fzsheik/opengrid-terminal#subdirectory=cli")
    assert cfg["env"]["OPENGRID_URL"] == "http://localhost:8000"
    code, out, _ = run_cli(["mcp", "config", "claude", "--from-repo", "--json"], MockAPI())
    d = js(out)["data"]
    cfg = json.loads(d["config"])["mcpServers"]["opengrid"]
    assert d["mode"] == "installed" and cfg["command"] == "uv" and cfg["args"][-2:] == ["opengrid", "mcp"]
    code, out, err = run_cli(["mcp", "config", "claude", "--mode", "nope"], MockAPI())
    assert code == 2


def test_version_flag():
    code, out, err = run_cli(["--version"], MockAPI())
    assert code == 0 and out.startswith("opengrid "), (code, out, err)


def test_doctor():
    code, out, err = run_cli(["doctor", "--json"], MockAPI(), key=SECRET)
    d = js(out)["data"]
    checks = {c["check"]: c for c in d["checks"]}
    assert checks["health"]["ok"] and checks["auth"]["scopes"] and checks["mcp"]["ok"], checks
    assert checks["capabilities"]["capabilities"]["route:preview"] is True
    assert len(checks["mcp"]["tools"]) == 13
    code, out, err = run_cli(["doctor"], MockAPI())
    assert "anonymous" in out
    code, out, err = run_cli(["doctor"], MockAPI(), key=BAD_KEY)
    assert code == 1 and "[FAIL] auth" in out and BAD_KEY not in out + err


def test_unreachable_exit_code():
    from opengrid_cli import cli

    def boom(request):
        raise httpx.ConnectError("connection refused")

    api = MockAPI()
    api.handle = boom
    code, out, err = run_cli(["gpu", "search"], api)
    assert code == 5 and "cannot reach OpenGrid" in err and BASE in err, err
    code, out, err = run_cli(["doctor", "--json"], api)
    assert code == 5 and js(out)["data"]["ok"] is False
    assert cli.TRANSPORT is None


# ---------------------------------------------------------------- client behaviour

def test_client_retries_only_gets():
    from opengrid_cli.client import Client, OpenGridError

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"detail": "busy"}, headers={"retry-after": "0"})
        return httpx.Response(200, json={"data": [1], "meta": {}}, headers={"x-request-id": "req_abc"})

    c = Client(BASE, transport=httpx.MockTransport(flaky))
    r = c.get("/v1/gpus")
    assert r.data == [1] and calls["n"] == 2 and r.request_id == "req_abc"
    calls["n"] = 0
    try:
        c.post("/v1/recommend", {})
        raise AssertionError("POST should not be retried")
    except OpenGridError as e:
        assert calls["n"] == 1 and e.status == 503


def test_client_bounded_response():
    from opengrid_cli.client import Client, ResponseTooLarge

    big = b'{"data": "' + b"x" * 5000 + b'"}'
    c = Client(BASE, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=big)), max_bytes=1000)
    try:
        c.get("/v1/gpus")
        raise AssertionError("expected ResponseTooLarge")
    except ResponseTooLarge as e:
        assert "limit" in str(e)


def test_client_validation_detail_and_request_id():
    from opengrid_cli.client import Client, OpenGridError

    t = httpx.MockTransport(lambda r: httpx.Response(422, json={"detail": [{"loc": ["body", "count"], "msg": "too big"}]},
                                                     headers={"x-request-id": "req_x1"}))
    try:
        Client(BASE, transport=t).post("/v1/route/preview", {})
    except OpenGridError as e:
        assert "count: too big" in str(e) and "req_x1" in str(e)
    else:
        raise AssertionError


def test_workload_contract_mirror():
    from opengrid_cli import workload

    v = workload.validate({"model": {"parameters_b": 7}, "training": {"method": "lora"}, "constraints": {"allow_spot": False}})
    assert v == {"model": {"parameters_b": 7.0}, "training": {"method": "lora"}, "constraints": {"allow_spot": False}}
    try:
        workload.validate({"model": {"parameters_b": 7}, "file_contents": "x"})
        raise AssertionError("extra fields must be rejected")
    except Exception as e:
        assert "file_contents" in str(e)


if __name__ == "__main__":
    os.chdir(PROJECT)
    run_tests([v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)])
