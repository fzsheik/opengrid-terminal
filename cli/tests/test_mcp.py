"""MCP server tests, in-process (mcp.Client connected directly to the MCPServer), against a mock API.

    uv run --project cli python cli/tests/test_mcp.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anyio  # noqa: E402
import httpx  # noqa: E402

from _support import BAD_KEY, BASE, LIMITED_KEY, SECRET, MockAPI, install_analyzer_stub, run_tests  # noqa: E402

STUB = install_analyzer_stub()

from mcp import Client as MCPClient  # noqa: E402

from opengrid_cli import mcp_server  # noqa: E402
from opengrid_cli.client import Client  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="og-mcp-root-")).resolve()
PROJECT = ROOT / "proj"
PROJECT.mkdir()
(PROJECT / "train.py").write_text("SECRET_FILE_CONTENT = 1\n")
OUTSIDE = Path(tempfile.mkdtemp(prefix="og-mcp-outside-")).resolve()
(ROOT / "link-out").symlink_to(OUTSIDE, target_is_directory=True)

EXPECTED = {"opengrid_search_gpus", "opengrid_compare_gpus", "opengrid_get_gpu_specs", "opengrid_get_market_context",
            "opengrid_analyze_project", "opengrid_estimate_requirements", "opengrid_recommend_compute",
            "opengrid_preview_route", "opengrid_list_jobs", "opengrid_get_job_status", "opengrid_cancel_job"}


def server(api: MockAPI | None = None, key: str | None = None):
    api = api or MockAPI()
    return mcp_server.build_server(lambda: Client(BASE, api_key=key, transport=api.transport(), retries=0,
                                                  use_stored_key=False)), api


def call(srv, name: str, args: dict):
    async def go():
        async with MCPClient(srv) as c:
            return await c.call_tool(name, args)
    return anyio.run(go)


def tools(srv):
    async def go():
        async with MCPClient(srv) as c:
            return (await c.list_tools()).tools
    return anyio.run(go)


def text(r) -> str:
    return " ".join(getattr(b, "text", "") for b in r.content)


# ---------------------------------------------------------------- discovery: names, schemas, annotations

def test_tool_list_schemas_and_annotations():
    ts = {t.name: t for t in tools(server()[0])}
    assert set(ts) == EXPECTED, set(ts) ^ EXPECTED
    for name, t in ts.items():
        assert t.input_schema.get("type") == "object", name
        assert t.output_schema and t.output_schema.get("type") == "object", name
        assert t.annotations is not None and t.title, name
        assert len(t.description) > 80, name
        assert "instructions" in t.description or "UNTRUSTED" in t.description or "untrusted" in t.description, name
    for name in EXPECTED - {"opengrid_preview_route", "opengrid_cancel_job"}:
        a = ts[name].annotations
        assert a.read_only_hint is True and a.destructive_hint is False, name
    assert ts["opengrid_analyze_project"].annotations.open_world_hint is False
    assert ts["opengrid_search_gpus"].annotations.open_world_hint is True
    p = ts["opengrid_preview_route"].annotations
    assert p.read_only_hint is False and p.destructive_hint is False and "preview ticket" in ts["opengrid_preview_route"].description
    c = ts["opengrid_cancel_job"].annotations
    assert c.read_only_hint is False and c.destructive_hint is True and c.idempotent_hint is True
    assert "confirm" in ts["opengrid_cancel_job"].input_schema["properties"]
    for name in ("opengrid_search_gpus", "opengrid_get_gpu_specs", "opengrid_recommend_compute"):
        assert "observed" in ts[name].description and "runtime" in ts[name].description, name
    s = ts["opengrid_search_gpus"].input_schema["properties"]
    assert s["limit"]["maximum"] == 50 and s["limit"]["minimum"] == 1 and s["model"]["anyOf"][0]["maxLength"] == 64
    assert ts["opengrid_get_job_status"].input_schema["properties"]["job_id"]["pattern"].startswith("^job-")
    for n in ts:
        for bad in ("launch", "provision", "submit", "execute", "run_code", "shell"):
            assert bad not in n, n


# ---------------------------------------------------------------- market tools

def test_search_gpus_structured_and_truncated():
    api = MockAPI()
    api.gpus = [dict(api.gpus[0], slug=f"gpu-{i}", name=f"NVIDIA GPU {i}") for i in range(300)]
    srv, _ = server(api)
    r = call(srv, "opengrid_search_gpus", {"limit": 5})
    assert not r.is_error, text(r)
    sc = r.structured_content
    assert sc["returned"] == 5 and sc["total_matched"] == 300 and sc["truncated"] is True
    assert any("showing 5 of 300" in n for n in sc["notes"]) and sc["price_kind"] == "observed"
    r = call(srv, "opengrid_search_gpus", {"limit": 500})
    assert r.is_error  # schema bound
    r = call(srv, "opengrid_search_gpus", {"vendor": "Nope"})
    assert r.is_error


def test_specs_compare_context():
    srv, _ = server()
    r = call(srv, "opengrid_get_gpu_specs", {"gpu": "h100-80gb-sxm5", "max_providers": 2})
    sc = r.structured_content
    assert not r.is_error and sc["kind"] == "gpu" and sc["providers_truncated"] is True and len(sc["cheapest_by_provider"]) == 2
    r = call(srv, "opengrid_get_gpu_specs", {"gpu": "h100"})
    assert r.structured_content["kind"] == "family" and r.structured_content["variants"]
    r = call(srv, "opengrid_get_gpu_specs", {"gpu": "nope"})
    assert r.is_error and "unknown GPU" in text(r)
    r = call(srv, "opengrid_get_gpu_specs", {"gpu": "../admin"})
    assert r.is_error  # pattern refuses path tricks before any request
    r = call(srv, "opengrid_compare_gpus", {"gpu_a": "h100-80gb-sxm5", "gpu_b": "a100-80gb-sxm4"})
    assert not r.is_error and r.structured_content["a"]["slug"] == "h100-80gb-sxm5"
    r = call(srv, "opengrid_get_market_context", {"gpu": "h100-80gb-sxm5", "max_candidates": 3})
    sc = r.structured_content
    assert len(sc["best_available_now"]) == 3 and sc["candidates_truncated"] is False or sc["candidates_total"] == 16


def test_provider_failure_is_error():
    api = MockAPI()
    api.overrides["/v1/gpus"] = lambda r: httpx.Response(503, json={"detail": "upstream down"})
    srv, _ = server(api)
    r = call(srv, "opengrid_search_gpus", {})
    assert r.is_error and "503" in text(r) and "upstream down" in text(r)

    def boom(request):
        raise httpx.ConnectError("refused")
    api2 = MockAPI()
    api2.handle = boom
    srv2, _ = server(api2)
    r = call(srv2, "opengrid_search_gpus", {})
    assert r.is_error and "cannot reach OpenGrid" in text(r)

    def crash(request):
        raise RuntimeError(f"internal detail with {SECRET}")
    api3 = MockAPI()
    api3.handle = crash
    srv3, _ = server(api3, key=SECRET)
    r = call(srv3, "opengrid_search_gpus", {})
    assert r.is_error and SECRET not in text(r) and "internal detail" not in text(r)


def test_invalid_credentials_never_leak():
    srv, _ = server(key=BAD_KEY)
    r = call(srv, "opengrid_list_jobs", {})
    assert r.is_error and "rejected" in text(r) and BAD_KEY not in text(r)
    api = MockAPI()
    api.overrides["/v1/jobs"] = lambda req: httpx.Response(401, json={"detail": f"invalid key {SECRET}"})
    srv, _ = server(api, key=SECRET)
    r = call(srv, "opengrid_list_jobs", {})
    assert r.is_error and SECRET not in text(r) and "[REDACTED]" in text(r)
    srv, _ = server()  # no key at all
    r = call(srv, "opengrid_list_jobs", {})
    assert r.is_error and "deployments:read" in text(r)
    srv, _ = server(key=LIMITED_KEY)
    r = call(srv, "opengrid_preview_route", {"gpu": "h100-80gb-sxm5"})
    assert r.is_error and "route:preview" in text(r) and LIMITED_KEY not in text(r)


# ---------------------------------------------------------------- local analysis: allowed roots only

def test_analyze_inside_root():
    os.chdir(ROOT)
    srv, api = server()
    r = call(srv, "opengrid_analyze_project", {"path": str(PROJECT)})
    assert not r.is_error, text(r)
    sc = r.structured_content
    assert sc["requirements"]["model"]["parameters_b"] == 8.03 and sc["files_skipped"][0]["path"] == ".env"
    assert "SECRET_FILE_CONTENT" not in json.dumps(sc) and api.requests == []  # local only, no contents
    r = call(srv, "opengrid_analyze_project", {"path": "proj"})  # relative to the server's cwd
    assert not r.is_error


def test_analyze_outside_root_refused():
    os.chdir(ROOT)
    srv, _ = server()
    for p in ("/etc", str(OUTSIDE), str(PROJECT / ".." / ".."), "../", str(ROOT / "link-out"), "~", "/",
              str(PROJECT / "train.py"), str(ROOT / "missing")):
        r = call(srv, "opengrid_analyze_project", {"path": p})
        assert r.is_error, p
        assert "outside the allowed roots" in text(r) or "not a directory" in text(r), (p, text(r))
    n = len(STUB.calls)
    call(srv, "opengrid_analyze_project", {"path": "/etc"})
    assert len(STUB.calls) == n  # the analyzer was never called for a refused path


def test_allowed_roots_env_and_dangerous_cwd():
    os.chdir("/")
    try:
        assert mcp_server.allowed_roots() == []
        srv, _ = server()
        r = call(srv, "opengrid_analyze_project", {"path": str(PROJECT)})
        assert r.is_error and "OPENGRID_ALLOWED_ROOTS" in text(r)
        os.environ["OPENGRID_ALLOWED_ROOTS"] = os.pathsep.join([str(PROJECT), "/"])
        assert mcp_server.allowed_roots() == [PROJECT]  # "/" is never accepted as a root
        r = call(srv, "opengrid_analyze_project", {"path": str(PROJECT)})
        assert not r.is_error
        r = call(srv, "opengrid_analyze_project", {"path": str(ROOT)})
        assert r.is_error
    finally:
        os.environ.pop("OPENGRID_ALLOWED_ROOTS", None)
        os.chdir(ROOT)


def test_oversized_analysis_truncated():
    os.chdir(ROOT)
    os.environ["STUB_FILES"] = "5000"
    try:
        srv, _ = server()
        r = call(srv, "opengrid_analyze_project", {"path": str(PROJECT)})
        sc = r.structured_content
        assert sc["truncated"] is True and len(sc["files_inspected"]) <= 100
        assert len(json.dumps(sc)) <= mcp_server.MAX_RESULT_CHARS + 2000
    finally:
        os.environ.pop("STUB_FILES", None)


def test_shrink_bounds_any_payload():
    data = {"a": [{"line": "x" * 1000} for _ in range(500)], "b": "ok"}
    out, t = mcp_server.shrink(data, 20000)
    assert t and len(json.dumps(out)) <= 20000 and out["b"] == "ok"
    out, t = mcp_server.shrink({"s": "y" * 100000}, 20000)
    assert t and len(json.dumps(out)) <= 20000


# ---------------------------------------------------------------- recommendation engine tools

def test_estimate_and_recommend():
    os.chdir(ROOT)
    srv, api = server()
    req = {"model": {"parameters_b": 7}, "training": {"method": "lora"}, "workload_type": "fine_tuning"}
    r = call(srv, "opengrid_estimate_requirements", {"requirements": req})
    assert not r.is_error, text(r)
    assert r.structured_content["estimate"]["memory"]["kind"] == "estimated"
    assert api.bodies("/v1/recommend/estimate")[0] == {"model": {"parameters_b": 7.0}, "training": {"method": "lora"},
                                                       "workload_type": "fine_tuning"}
    r = call(srv, "opengrid_estimate_requirements", {"requirements": {"model": {"parameters_b": 7}, "file": "x"}})
    assert r.is_error  # contract mirror forbids extra fields
    r = call(srv, "opengrid_recommend_compute", {"requirements": req, "limit": 3})
    sc = r.structured_content
    assert not r.is_error and len(sc["recommendation"]["options"]) == 3 and sc["truncated"] is True
    assert sc["recommendation"]["picks"]["best_executable"] is None
    r = call(srv, "opengrid_recommend_compute", {"project_path": str(PROJECT)})
    sc = r.structured_content
    assert not r.is_error and sc["analysis"]["files_inspected"] == 2 and sc["requirements_sent"]["source"] == "analyzer"
    body = api.bodies("/v1/recommend")[-1]
    assert "SECRET_FILE_CONTENT" not in json.dumps(body) and "files_inspected" not in body
    assert call(srv, "opengrid_recommend_compute", {}).is_error
    assert call(srv, "opengrid_recommend_compute", {"requirements": req, "project_path": str(PROJECT)}).is_error
    r = call(srv, "opengrid_recommend_compute", {"project_path": "/etc"})
    assert r.is_error and "outside the allowed roots" in text(r)


# ---------------------------------------------------------------- route preview, jobs, cancel

def test_preview_route():
    srv, api = server(key=SECRET)
    r = call(srv, "opengrid_preview_route", {"gpu": "h100-80gb-sxm5", "count": 2, "region": "US"})
    sc = r.structured_content
    assert not r.is_error and sc["provisioned"] is False and sc["route_request_id"] == "rr_0123456789abcdef"
    assert len(sc["alternatives"]) == 5 and any("never provisions" in n for n in sc["notes"])
    assert api.bodies("/v1/route/preview")[0]["count"] == 2
    assert call(srv, "opengrid_preview_route", {"gpu": "h100-80gb-sxm5", "mode": "USER_DEFINED"}).is_error


def test_jobs_and_logs_bounded():
    api = MockAPI()
    api.overrides["/v1/jobs/job-a37ddf8cf1c1c13f/logs"] = lambda req: httpx.Response(200, json={"data": {
        "items": [{"id": i, "at": "t", "stream": "stdout", "line": "z" * 5000} for i in range(400)],
        "next_after": 399, "more": True}, "meta": {}})
    srv, _ = server(api, key=SECRET)
    r = call(srv, "opengrid_list_jobs", {"limit": 5})
    assert not r.is_error and r.structured_content["items"][0]["job_id"] == "job-a37ddf8cf1c1c13f"
    r = call(srv, "opengrid_get_job_status", {"job_id": "job-a37ddf8cf1c1c13f", "log_lines": 200})
    sc = r.structured_content
    assert not r.is_error and sc["simulated"] is True and sc["logs_more"] is True
    assert len(json.dumps(sc)) <= mcp_server.MAX_RESULT_CHARS + 2000 and all(len(x["line"]) <= 2000 for x in sc["logs"])
    assert call(srv, "opengrid_get_job_status", {"job_id": "job-../../x"}).is_error


def test_cancel_needs_confirm():
    srv, api = server(key=SECRET)
    r = call(srv, "opengrid_cancel_job", {"job_id": "job-a37ddf8cf1c1c13f"})
    assert r.is_error and "confirm=true" in text(r) and api.requests == []
    r = call(srv, "opengrid_cancel_job", {"job_id": "job-a37ddf8cf1c1c13f", "confirm": False})
    assert r.is_error and api.requests == []
    r = call(srv, "opengrid_cancel_job", {"job_id": "job-a37ddf8cf1c1c13f", "confirm": True,
                                          "idempotency_key": "mcp-retry-0001"})
    assert not r.is_error, text(r)
    [req] = api.requests
    assert req.method == "POST" and req.url.path.endswith("/cancel") and req.headers["idempotency-key"] == "mcp-retry-0001"
    r = call(srv, "opengrid_cancel_job", {"job_id": "job-a37ddf8cf1c1c13f", "confirm": True})
    assert api.requests[-1].headers["idempotency-key"].startswith("cli-")
    assert call(srv, "opengrid_cancel_job", {"job_id": "job-a37ddf8cf1c1c13f", "confirm": True,
                                             "idempotency_key": "bad key with spaces"}).is_error


def test_http_mode_refuses_non_loopback():
    from opengrid_cli.client import RefusedError

    try:
        mcp_server.run(http=True, host="0.0.0.0", port=9)
        raise AssertionError("expected refusal")
    except RefusedError as e:
        assert "no authentication" in str(e)


if __name__ == "__main__":
    os.chdir(ROOT)
    run_tests([v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)])
