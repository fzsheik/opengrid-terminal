"""Shared test fixtures: contract-shaped payloads, a mock OpenGrid API (httpx.MockTransport), an analyzer stub.

Plain scripts, no pytest:  uv run --project cli python cli/tests/test_cli.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import traceback
import types
from pathlib import Path

import httpx

# Test the source tree even when the editable install's .pth is skipped (macOS UF_HIDDEN files, see README).
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SECRET = "opg_live_SeCrEtT0kenDoNotPrint0123456789abcdefghijkl"
BAD_KEY = "opg_live_BADbadBADbadBADbad0123456789abcdefghijklmn"
LIMITED_KEY = "opg_live_LimitedScopesOnly0123456789abcdefghijklmn"
BASE = "http://og.test"

_TMP = tempfile.mkdtemp(prefix="opengrid-cli-tests-")
os.environ["OPENGRID_CONFIG_DIR"] = str(Path(_TMP) / "config")
os.environ["OPENGRID_URL"] = BASE
os.environ.pop("OPENGRID_API_KEY", None)
os.environ.pop("OPENGRID_ALLOWED_ROOTS", None)


# ---------------------------------------------------------------- analyzer stub (the real one is maintained separately)

def install_analyzer_stub() -> types.ModuleType:
    m = types.ModuleType("opengrid_cli.analyze")
    m.calls = []
    m.result = None  # a test may set a full analyzer result to return instead of the default one

    def analyze(root, *, exclude=(), max_file_bytes=256000, max_files=400, dry_run=False):
        m.calls.append(("analyze", str(root), tuple(exclude), dry_run))
        if m.result is not None:
            return json.loads(json.dumps(m.result))
        n = int(os.environ.get("STUB_FILES", "2"))
        return {
            "root": str(Path(root).resolve()),
            "requirements": {
                "workload_type": "fine_tuning", "frameworks": ["pytorch", "peft"],
                "model": {"name": "meta-llama/Llama-3.1-8B", "parameters_b": 8.03},
                "precision": "bf16", "training": {"method": "lora", "seq_len": 2048},
                "evidence": [{"field": "model.parameters_b", "value": 8.03, "source": "config.json:12",
                              "confidence": "high"}],
                "source": "analyzer"},
            "files_inspected": [{"path": f"src/file_{i}.py", "bytes": 100 + i, "reason": "training script"}
                                for i in range(n)],
            "files_skipped": [{"path": ".env", "reason": "secret file"}],
            "signals": [{"kind": "framework", "value": "peft", "source": "requirements.txt:4"}],
            "questions": [], "warnings": [], "analyzer_version": "1"}

    def plan(root, *, exclude=(), max_file_bytes=256000, max_files=400):
        m.calls.append(("plan", str(root), tuple(exclude)))
        return {"root": str(root), "files": [{"path": "train.py", "reason": "training script"}],
                "would_skip": [{"path": ".env", "reason": "secret file"}]}

    m.analyze, m.plan = analyze, plan
    sys.modules["opengrid_cli.analyze"] = m
    return m


# ---------------------------------------------------------------- contract-shaped payloads

def env(data, **meta):
    return {"data": data, "meta": {"as_of": "2026-10-13T12:00:00+00:00", **meta}}


HW_H100 = {"vendor": "NVIDIA", "architecture": "Hopper", "vram_gb": 80, "memory_type": "HBM3",
           "memory_bandwidth_tbps": 3.35, "bf16_tflops_dense": 989.5, "form_factor": "SXM"}
HW_A100 = {"vendor": "NVIDIA", "architecture": "Ampere", "vram_gb": 80, "memory_bandwidth_tbps": 2.04,
           "bf16_tflops_dense": 312, "form_factor": "SXM"}
HW_MI300 = {"vendor": "AMD", "architecture": "CDNA3", "vram_gb": 192, "form_factor": "OAM"}

GPUS = [
    {"slug": "h100-80gb-sxm5", "name": "NVIDIA H100 80GB SXM5", "hardware": HW_H100, "low": 1.3, "median": 4.3,
     "high": 12.29, "providers": 16, "available_listings": 17, "priced_listings": 47},
    {"slug": "a100-80gb-sxm4", "name": "NVIDIA A100 80GB SXM4", "hardware": HW_A100, "low": 1.0, "median": 1.96,
     "high": 3.5, "providers": 11, "available_listings": 12, "priced_listings": 30},
    {"slug": "mi300x-192gb", "name": "AMD Instinct MI300X 192GB", "hardware": HW_MI300, "low": 2.1, "median": 3.0,
     "high": 4.0, "providers": 3, "available_listings": 0, "priced_listings": 4},
    {"slug": "l4-24gb", "name": "NVIDIA L4 24GB", "hardware": {"vendor": "NVIDIA", "vram_gb": 24}, "low": None,
     "median": None, "high": None, "providers": 0, "available_listings": 0, "priced_listings": 0},
]

GPU_DETAIL = {"slug": "h100-80gb-sxm5", "name": "NVIDIA H100 80GB SXM5", "hardware": HW_H100,
              "market": {"low": 1.3, "median": 4.3, "high": 12.29, "providers": 3,
                         "listings": {"live": 61, "priced": 47, "available": 17, "availability_unknown": 30,
                                      "sold_out": 14},
                         "score": {"label": "highly fragmented"},
                         "by_provider": [{"provider": "runpod", "price": 3.99, "region": None, "available": True,
                                          "listings": 2, "change_24h": {"pct": 0.0}},
                                         {"provider": "lium", "price": 1.3, "region": "Toronto", "available": True,
                                          "listings": 8},
                                         {"provider": "lambda", "price": 2.99, "region": "us-east-1",
                                          "available": None, "listings": 1}]},
              "alternatives": {"alternatives": [{"gpu": "NVIDIA H100 80GB PCIe"}]}}

FAMILY = {"id": "H100", "slug": "h100", "name": "H100 family", "variants": [
    {"gpu": "NVIDIA H100 80GB SXM5", "slug": "h100-80gb-sxm5", "low": 1.3, "median": 4.3, "providers": 16,
     "available_listings": 17}]}

COMPARE = {"type": "gpu", "a": {"name": "NVIDIA H100 80GB SXM5", "slug": "h100-80gb-sxm5", "hardware": HW_H100,
                                "market": {"low": 1.3, "median": 4.3, "high": 12.29, "providers": 16},
                                "listings": {"available": 17, "priced": 47}, "score": {"label": "fragmented"},
                                "capability": {"per_gb_vram_hour": {"median": 0.054, "low": 0.016}}},
           "b": {"name": "NVIDIA A100 80GB SXM4", "slug": "a100-80gb-sxm4", "hardware": HW_A100,
                 "market": {"low": 1.0, "median": 1.96, "high": 3.5, "providers": 11},
                 "listings": {"available": 12, "priced": 30}, "score": {"label": "fragmented"},
                 "capability": {"per_gb_vram_hour": {"median": 0.0245}}},
           "note": "different GPUs are different products", "shared_dimensions": ["vram_gb"]}

PROVIDERS = [{"provider": "lambda", "meta": {"display_name": "Lambda", "provider_class": "neocloud",
                                             "source_type": "public_api"},
              "coverage": {"gpus": 9, "live_listings": 20, "priced_listings": 20, "region_groups": ["US"]},
              "feed_health": {"last_ok_fetch": "2026-10-13T11:58:00+00:00", "failure_rate_24h": 0.0}}]

CONTEXT = {"gpu": "NVIDIA H100 80GB SXM5", "current": {"lowest": 1.3, "median": 3.99, "highest": 6.88, "providers": 13},
           "label": None, "label_reason": "history does not cover the window", "summaries": ["lowest is 4% above low"],
           "metrics": {"lowest": {"historical": {"low": {"value": 1.25}, "high": {"value": 1.75}, "since": "2026-10-04",
                                                 "samples": 220}}}}

BEST = {"gpu": "NVIDIA H100 80GB SXM5", "mode": "BALANCED", "candidates_total": 16,
        "candidates": [{"rank": i + 1, "provider": f"p{i}", "gpu": "NVIDIA H100 80GB SXM5", "region": "US",
                        "gpu_count": 1, "price_per_gpu_hour": 1.3 + i, "available": True, "age_seconds": 400,
                        "provisionable": i == 2, "score": 0.9 - i / 10, "explanation": "cheap"} for i in range(8)],
        "exclusions_by_code": {"stale": 96}}

ESTIMATE = {"needs_gpu": True, "needs_gpu_reason": "pytorch + 8B model", "workload_type": "fine_tuning",
            "memory": {"method": "training_lora", "components_gb": {"weights": 16.06, "activations": 6.2},
                       "min_total_gb": 24.3, "recommended_gb": 29.2, "range_gb": [21.0, 34.0], "per_gpu_gb": 29.2,
                       "confidence": "medium", "assumptions": ["micro batch 4 assumed"], "kind": "estimated"},
            "gpu_count": {"min": 1, "suggested": 1, "reason": "fits one GPU"},
            "interconnect": {"needed": False, "reason": "single GPU"},
            "runtime": {"known": False, "reason": "no benchmark for this workload; runtime not estimated"},
            "questions": [{"field": "training.method", "question": "LoRA or QLoRA?", "why": "memory"}],
            "methodology": "/methodology/recommendations"}

OPTION = {"option_id": "h100-80gb-sxm5:lambda:us-east-1:1", "tier": "balanced", "gpu": "NVIDIA H100 80GB SXM5",
          "gpu_slug": "h100-80gb-sxm5", "vram_gb": 80, "gpu_count": 1, "provider": "lambda",
          "provider_display": "Lambda", "region": "us-east-1", "price_per_gpu_hour": 2.99,
          "price_per_instance_hour": 2.99, "price_kind": "observed", "available": True,
          "observed_at": "2026-10-13T11:53:00+00:00", "age_seconds": 420, "fresh": True,
          "memory_fit": {"needed_per_gpu_gb": 29.2, "vram_gb": 80, "headroom_pct": 63.5, "fits": True},
          "cost": {"billing_unit": "per_minute", "scenarios": [{"hours": 1, "total_usd": 2.99,
                                                                "assumption": "1 hour of runtime"}],
                   "budget_hours": 16.7, "unknown_costs": ["storage", "egress"], "kind": "estimated"},
          "why": ["fits 29 GB estimate with 64% headroom"], "limitations": ["runtime not benchmarked"],
          "execution": {"executable": False, "reasons": [{"code": "auth", "reason": "sign in / API key required"}],
                        "workload_supported": False, "route_preview_url": "/route?gpu=h100-80gb-sxm5&count=1"}}

RECOMMENDATION = {"estimate": ESTIMATE,
                  "picks": {"cheapest_market": {"option_id": OPTION["option_id"], "reason": "cheapest fresh fit"},
                            "best_for_workload": {"option_id": OPTION["option_id"], "reason": "balanced"},
                            "best_executable": None},
                  "options": [dict(OPTION, option_id=f"opt-{i}") for i in range(12)] + [OPTION],
                  "excluded": [{"gpu": "NVIDIA L4 24GB", "code": "insufficient_vram", "reason": "24 GB < 29.2 GB"}],
                  "questions": [], "notes": ["Prices are observed list prices, not quotes."],
                  "as_of": "2026-10-13T12:00:00+00:00", "methodology": "/methodology/recommendations"}

JOB = {"job_id": "job-a37ddf8cf1c1c13f", "name": "opengrid-first-training-test", "template": "pytorch-mnist",
       "job_type": "training", "status": "training_completed", "outcome": "simulated", "finished": True,
       "sandbox": True, "simulated": True, "simulated_note": "SIMULATED", "runner_phase": "saving_artifacts",
       "selection": {"provider": "lium", "gpu": "NVIDIA RTX A6000 48GB", "price_per_gpu_hour": 0.42},
       "resource": {"disposition": "simulated_none", "possibly_billing": False},
       "estimates": {"kind": "estimated", "est_max_total_usd": 0.42}}

ROUTE = {"route_request_id": "rr_0123456789abcdef", "preview": True, "gpu": "NVIDIA H100 80GB SXM5", "count": 1,
         "mode": "BALANCED", "region_group": None,
         "selected": {"provider": "lium", "region": "Toronto", "price_per_gpu_hour": 1.3, "available": True,
                      "provisionable": False, "explanation": "70% below median", "raw_secret_field": "x"},
         "can_provision_selected": False,
         "best_provisionable": {"provider": "runpod", "region": None, "price_per_gpu_hour": 3.99},
         "alternatives": [{"provider": f"alt{i}", "price_per_gpu_hour": 2 + i} for i in range(9)],
         "quote": {"kind": "quote", "price_per_gpu_hour": 1.3, "hours": 1, "total_usd": 1.3},
         "quote_record": {"quote_id": "q_0123abcd"}, "execution_mode": "PREVIEW_ONLY",
         "live_provisioning_enabled": False, "exclusions_total": 3, "exclusions_by_code": {"stale": 3}}


# ---------------------------------------------------------------- the mock API

PRIVATE = ("/v1/me", "/v1/jobs", "/v1/route")


class MockAPI:
    """Routes requests like the real server: public GETs anonymous; private paths need a valid key + scope."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.overrides: dict = {}   # path -> callable(request) -> httpx.Response
        self.keys = {SECRET: ["data:read", "route:preview", "deployments:read", "route:execute"],
                     LIMITED_KEY: ["data:read"]}
        self.gpus = GPUS
        self.job_response = JOB

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(r.content or b"{}") for r in self.requests if r.url.path == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        rid = request.headers.get("x-request-id") or "req_mock"
        h = {"x-request-id": rid}
        if path in self.overrides:
            return self.overrides[path](request)
        auth = request.headers.get("authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else None
        scopes = None
        if key is not None and path.startswith("/v1/"):  # the server checks keys only on /v1
            if key not in self.keys:
                return httpx.Response(401, json={"detail": "invalid API key"}, headers=h)
            scopes = self.keys[key]
        if path.startswith(PRIVATE):
            if key is None:
                return httpx.Response(401, json={"detail": "OpenGrid API key required"}, headers=h)
            need = ("route:preview" if path.startswith("/v1/route") else "route:execute"
                    if request.method == "POST" else "deployments:read" if path.startswith("/v1/jobs") else None)
            if need and need not in scopes:
                return httpx.Response(403, json={"detail": f"this key lacks the {need!r} scope"}, headers=h)
        q = request.url.params
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "db": "up"}, headers=h)
        if path == "/openapi.json":
            return httpx.Response(200, json={"info": {"title": "opengrid-terminal", "version": "0.1.0"},
                                             "paths": {"/v1/recommend": {}, "/v1/recommend/estimate": {}}}, headers=h)
        if path == "/v1/gpus":
            return httpx.Response(200, json=env(self.gpus, kind="observed", total=len(self.gpus)), headers=h)
        if path == "/v1/gpus/h100-80gb-sxm5":
            return httpx.Response(200, json=env(GPU_DETAIL, kind="inferred"), headers=h)
        if path == "/v1/gpus/h100":
            return httpx.Response(200, json=env(FAMILY, kind="family", resolved_as="family", note="a family"), headers=h)
        if path.startswith("/v1/gpus/"):
            return httpx.Response(404, json={"detail": f"unknown GPU or GPU family {path[9:]!r}"}, headers=h)
        if path == "/v1/compare":
            return httpx.Response(200, json=env(COMPARE), headers=h)
        if path == "/v1/providers":
            return httpx.Response(200, json=env(PROVIDERS), headers=h)
        if path.endswith("/context") and path.startswith("/v1/markets/"):
            return httpx.Response(200, json=env(CONTEXT), headers=h)
        if path.startswith("/v1/best/"):
            lim = int(q.get("limit", 10))
            return httpx.Response(200, json=env(dict(BEST, candidates=BEST["candidates"][:lim])), headers=h)
        if path == "/v1/me":
            return httpx.Response(200, json=env({"principal": "api_key", "account": {"name": "acme"},
                                                 "key": {"prefix": key[:13]}, "scopes": scopes}), headers=h)
        if path == "/v1/recommend/estimate":
            return httpx.Response(200, json=env(ESTIMATE, kind="estimated"), headers=h)
        if path == "/v1/recommend":
            return httpx.Response(200, json=env(RECOMMENDATION), headers=h)
        if path == "/v1/route/preview":
            return httpx.Response(200, json=env(ROUTE, kind="inferred"), headers=h)
        if path == "/v1/jobs" and request.method == "GET":
            return httpx.Response(200, json=env([JOB]), headers=h)
        if path == "/v1/jobs" and request.method == "POST":
            if not request.headers.get("idempotency-key"):
                return httpx.Response(422, json={"detail": {"code": "idempotency_key_required", "message": "x"}})
            return httpx.Response(201, json=env(self.job_response, simulated=True), headers=h)
        if path.startswith("/v1/jobs/") and path.endswith("/logs"):
            return httpx.Response(200, json=env({"items": [{"id": 1, "at": "2026-10-13T12:00:00", "stream": "stdout",
                                                            "line": "epoch 1 loss 0.1"}], "next_after": 1,
                                                 "more": False, "simulated": True}), headers=h)
        if path.startswith("/v1/jobs/") and path.endswith("/cancel"):
            if not request.headers.get("idempotency-key"):
                return httpx.Response(422, json={"detail": {"code": "idempotency_key_required", "message": "x"}})
            return httpx.Response(200, json=env(dict(JOB, cancel={"requested": False, "note": "nothing to cancel"})),
                                  headers=h)
        if path.startswith("/v1/jobs/job-"):
            return httpx.Response(200, json=env(JOB, simulated=True), headers=h)
        return httpx.Response(404, json={"detail": "not found"}, headers=h)


# ---------------------------------------------------------------- running the CLI in-process

def run_cli(argv: list[str], api: MockAPI, *, key: str | None = None, tty: bool = False, stdin: str = "") -> tuple[int, str, str]:
    from opengrid_cli import cli

    old_key = os.environ.pop("OPENGRID_API_KEY", None)
    if key:
        os.environ["OPENGRID_API_KEY"] = key
    cli.TRANSPORT = api.transport()
    real_tty, real_stdin = cli.stdin_is_tty, sys.stdin
    cli.stdin_is_tty = lambda: tty
    sys.stdin = io.StringIO(stdin)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 2
    finally:
        cli.stdin_is_tty, sys.stdin = real_tty, real_stdin
        cli.TRANSPORT = None
        os.environ.pop("OPENGRID_API_KEY", None)
        if old_key is not None:
            os.environ["OPENGRID_API_KEY"] = old_key
    return code, out.getvalue(), err.getvalue()


def run_tests(tests: list) -> None:
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    if failed:
        print(f"{failed} failed")
        sys.exit(1)
    print("all passed")
