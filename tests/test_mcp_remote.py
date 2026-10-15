"""The hosted MCP endpoint (/mcp, api/mcp_remote.py) against the REAL app served by uvicorn on 127.0.0.1.

Scratch DB og_test_mcp_remote; real API keys made by accounts.keys; the official `mcp` client over streamable
HTTP (legacy initialize handshake and auto/discover). Checks: tool list (no local-file tools; the new ones
present), market search, the SANDBOX demo (simulated: a sandbox job row, no deployment, no provider), cancel
without confirm refused, scopes enforced through the caller's key, prompts, auth (401 without a key, with a bad
key, with basic auth only, anonymous even with PUBLIC_PAGES on; 403 cross-origin), the key never echoed, and no
deadlock: 48 concurrent tool calls (more than the gate admits and than anyio's 40 worker threads) all finish.

Run:  .venv/bin/python tests/test_mcp_remote.py
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
os.environ["POLLER_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_fixtures as bf  # noqa: E402

import anyio  # noqa: E402
import httpx  # noqa: E402
import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

import main  # noqa: E402
from accounts import keys, ratelimit  # noqa: E402
from api import mcp_remote  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_mcp_remote"
G = "NVIDIA H100 80GB SXM5"
ALL_SCOPES = ["data:read", "route:preview", "route:execute", "deployments:read"]
S = None
SECRETS: list[str] = []      # every key made here: none may appear in any error text
SEEN: list[str] = []         # every error / refusal text received
KEY_IDS: dict[str, int] = {}


class LiveServer:
    def __init__(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        # lifespan off (no init_db against the app's own DB, no background jobs): the gate starts the MCP
        # session manager on the first request, which this also tests.
        cfg = uvicorn.Config(main.app, host="127.0.0.1", port=self.port, lifespan="off", log_config=None,
                             access_log=False, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="mcp-uvicorn", daemon=True)
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


SRV: LiveServer | None = None


def mkkey(scopes=ALL_SCOPES) -> str:
    k = keys.create_key(bf.fresh_account(), "mcp-test", scopes, rate_limit_per_minute=5000)
    SECRETS.append(k["secret"])
    KEY_IDS[k["secret"]] = k["id"]
    return k["secret"]


def mcp_url() -> str:
    return SRV.url + "/mcp"


def session(key: str, fn, mode: str = "legacy"):
    async def go():
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60)
        async with http:
            async with Client(streamable_http_client(mcp_url(), http_client=http), mode=mode,
                              read_timeout_seconds=60) as c:
                return await fn(c)
    return anyio.run(go)


def text(r) -> str:
    t = " ".join(getattr(b, "text", "") for b in r.content)
    if r.is_error:
        SEEN.append(t)
    return t


HDR = {"content-type": "application/json", "accept": "application/json, text/event-stream",
       "mcp-protocol-version": "2025-06-18"}


def rpc(method: str, params: dict | None = None, *, headers: dict | None = None, id_: int = 1) -> httpx.Response:
    r = httpx.post(mcp_url(), json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
                   headers={**HDR, **(headers or {})}, timeout=60)
    if r.status_code >= 400:
        SEEN.append(r.text + json.dumps(dict(r.headers)))
    return r


# ---------------------------------------------------------------- setup

def setup():
    global S, SRV
    import logging
    for name in ("mcp", "opengrid.access", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
    S = bf.db(DB)
    settings.api_key_pepper = "test-pepper-mcp-remote"
    settings.app_password = None
    settings.public_pages = False
    keys.reset_cache()
    ratelimit.reset()
    bf.market(G, [bf.listing("lambda", 2.49, gpu=G, region="us-east-1", lid="lambda:gpu_1x_h100_sxm5"),
                  bf.listing("runpod", 2.19, gpu=G, region="us-east", lid="runpod:h100")])
    SRV = LiveServer().start()


# ---------------------------------------------------------------- protocol + tools

def test_initialize_list_tools_and_prompts():
    key = mkkey()

    async def fn(c):
        tools = (await c.list_tools()).tools
        prompts = (await c.list_prompts()).prompts
        p = await c.get_prompt("cheapest_gpu", {"gpu": "H100"})
        return c.server_info, c.protocol_version, tools, prompts, p

    for mode in ("legacy", "auto"):
        info, version, tools, prompts, p = session(key, fn, mode)
        names = {t.name for t in tools}
        assert info is not None and info.name == "opengrid", info
        assert "opengrid_analyze_project" not in names, names                      # reads local files: local only
        assert {"opengrid_get_job_logs", "opengrid_start_demo", "opengrid_search_gpus", "opengrid_cancel_job",
                "opengrid_recommend_compute"} <= names, names
        assert len(names) == 12, names
        rec = next(t for t in tools if t.name == "opengrid_recommend_compute")
        assert "project_path" not in rec.input_schema["properties"], rec.input_schema
        for t in tools:
            for bad in ("launch", "provision", "submit", "execute", "shell"):
                assert bad not in t.name, t.name
        demo = next(t for t in tools if t.name == "opengrid_start_demo")
        assert "SIMULATED" in demo.description and "FREE" in demo.description
        a = demo.annotations
        assert a.read_only_hint is False and a.destructive_hint is False and a.idempotent_hint is True
        assert {x.name for x in prompts} == {"cheapest_gpu", "try_free_demo"}, prompts  # size_my_project: local only
        assert "opengrid_search_gpus" in p.messages[0].content.text and "H100" in p.messages[0].content.text
        print(f"   {mode}: protocol {version}, {len(names)} tools, {len(prompts)} prompts")


def test_search_gpus_through_the_callers_key():
    key = mkkey(["data:read"])

    async def fn(c):
        return await c.call_tool("opengrid_search_gpus", {"model": "H100", "limit": 5})

    r = session(key, fn)
    assert not r.is_error, text(r)
    sc = r.structured_content
    assert sc["price_kind"] == "observed" and sc["returned"] >= 1, sc
    assert any("h100" in (x.get("slug") or "").lower() for x in sc["items"]), sc["items"]
    # the inner /v1 call was made with this key (usage logged against it)
    from accounts import usage
    usage.flush()
    n = bf.scalar("SELECT count(*) FROM api_key_usage WHERE key_id = :k AND path LIKE '/v1/gpus%'",
                  k=_key_id(key))
    assert n >= 1, n


def test_start_demo_is_a_simulated_sandbox_job():
    key = mkkey()
    before = bf.scalar("SELECT count(*) FROM deployments")

    async def fn(c):
        r1 = await c.call_tool("opengrid_start_demo", {"epochs": 2, "idempotency_key": "mcp-remote-demo-0001"})
        r2 = await c.call_tool("opengrid_start_demo", {"epochs": 2, "idempotency_key": "mcp-remote-demo-0001"})
        jid = (r1.structured_content or {}).get("job", {}).get("job_id")
        st = await c.call_tool("opengrid_get_job_status", {"job_id": jid, "log_lines": 5}) if jid else None
        lg = await c.call_tool("opengrid_get_job_logs", {"job_id": jid, "limit": 10}) if jid else None
        cancel = await c.call_tool("opengrid_cancel_job", {"job_id": jid}) if jid else None
        return r1, r2, st, lg, cancel

    r1, r2, st, lg, cancel = session(key, fn)
    assert not r1.is_error, text(r1)
    sc = r1.structured_content
    jid = sc["job"]["job_id"]
    assert sc["simulated"] is True and sc["job"].get("sandbox") is True, sc
    assert sc["real_run_url"].endswith("/jobs/new?preset=mnist&real=1"), sc
    assert any("SIMULATED" in n for n in sc["notes"])
    assert not r2.is_error and r2.structured_content["job"]["job_id"] == jid       # same key: same job
    assert bf.scalar("SELECT count(*) FROM jobs WHERE job_id = :j AND sandbox", j=jid) == 1
    assert bf.scalar("SELECT count(*) FROM jobs WHERE name = 'opengrid-cli-demo'") == 1
    assert bf.scalar("SELECT count(*) FROM deployments") == before                 # nothing provisioned
    assert not st.is_error and st.structured_content["simulated"] is True, text(st)
    assert not lg.is_error and lg.structured_content["job_id"] == jid, text(lg)
    assert cancel.is_error and "confirm=true" in text(cancel), text(cancel)
    print(f"   demo job {jid}: sandbox, status {st.structured_content['job'].get('status')}")


def test_scopes_and_errors_come_from_the_api():
    key = mkkey(["data:read"])

    async def fn(c):
        demo = await c.call_tool("opengrid_start_demo", {})
        jobs = await c.call_tool("opengrid_list_jobs", {})
        bad = await c.call_tool("opengrid_get_gpu_specs", {"gpu": "no-such-gpu-xyz"})
        return demo, jobs, bad

    n0 = bf.scalar("SELECT count(*) FROM jobs")
    demo, jobs, bad = session(key, fn)
    assert demo.is_error and "route:execute" in text(demo), text(demo)
    assert jobs.is_error and "deployments:read" in text(jobs), text(jobs)
    assert bad.is_error and text(bad)
    assert bf.scalar("SELECT count(*) FROM jobs") == n0


# ---------------------------------------------------------------- auth

def _all_401(r: httpx.Response):
    assert r.status_code == 401, (r.status_code, r.text)
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer"), r.headers
    assert r.headers["content-type"].startswith("application/json") and r.json()["detail"]


def test_auth_refusals():
    good = mkkey()
    _all_401(rpc("tools/list"))                                                    # no key
    bad = "opg_live_" + "x" * 40
    SECRETS.append(bad)
    _all_401(rpc("tools/list", headers={"authorization": f"Bearer {bad}"}))        # unknown key
    _all_401(rpc("tools/list", headers={"authorization": "Bearer not-a-key-123456"}))
    _all_401(rpc("tools/list", headers={"authorization": "Bearer"}))
    old = settings.app_password, settings.public_pages
    try:
        settings.app_password = "site-pass-123"
        basic = "Basic " + base64.b64encode(f"{settings.app_user}:site-pass-123".encode()).decode()
        _all_401(rpc("tools/list", headers={"authorization": basic}))              # the site login: never
        settings.public_pages = True
        _all_401(rpc("tools/list"))                                                # anonymous, PUBLIC_PAGES on
        r = httpx.get(mcp_url(), timeout=10)
        assert r.status_code == 401, r.status_code
        r = rpc("tools/list", headers={"authorization": f"Bearer {good}"})        # a key still works
        assert r.status_code == 200 and r.json()["result"]["tools"], r.text
    finally:
        settings.app_password, settings.public_pages = old
    r = rpc("tools/list", headers={"authorization": f"Bearer {good}", "origin": "https://evil.example"})
    assert r.status_code == 403, r.status_code
    r = rpc("tools/list", headers={"authorization": f"Bearer {good}", "origin": SRV.url})  # same origin
    assert r.status_code == 200, r.text
    keys.revoke_key(_key_id(good))
    _all_401(rpc("tools/list", headers={"authorization": f"Bearer {good}"}))        # revoked
    assert "revoked" in SEEN[-1]


def _key_id(secret: str) -> int:
    return KEY_IDS[secret]


def test_unavailable_mcp_is_503_not_a_crash():
    gate = mcp_remote.GATE
    saved = gate.cli
    try:
        gate.cli = None
        r = rpc("tools/list", headers={"authorization": f"Bearer {mkkey()}"})
        assert r.status_code == 503 and r.json()["detail"], r.text
    finally:
        gate.cli = saved
    assert httpx.get(SRV.url + "/health", timeout=10).status_code == 200


# ---------------------------------------------------------------- no deadlock

def _burst(key: str, n: int = 48):
    h = {"authorization": f"Bearer {key}"}

    def one(i):
        try:
            r = rpc("tools/call", {"name": "opengrid_get_gpu_specs", "arguments": {"gpu": "h100-80gb-sxm5"}},
                    headers=h, id_=i)
        except httpx.HTTPError as e:
            return (0, f"client {type(e).__name__}")
        if r.status_code != 200 or r.json()["result"].get("isError"):
            return (r.status_code, r.text[:300])
        return None

    t0 = time.monotonic()
    with ThreadPoolExecutor(n) as ex:
        bad = [b for b in ex.map(one, range(n)) if b]
    return bad, time.monotonic() - t0


def _on_server_loop(fn):
    import asyncio

    loop = mcp_remote.GATE._running.loop
    return asyncio.run_coroutine_threadsafe(fn(), loop).result(30)


def _threads(n: int):
    async def go():
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = n
    return go


def test_concurrent_calls_cannot_deadlock():
    """Tool threads and the app's sync endpoints share anyio's worker threads. Shrink that pool to 6: with the
    gate's admission limit (6 // 2 = 3) 48 concurrent calls all finish. Without it (counterfactual: admission
    opened to 1000) the tool threads take every worker thread and their own inner /v1 requests starve until
    the in-process timeout, i.e. the deadlock the limit exists to prevent."""
    import asyncio

    key = mkkey()
    bad, dt = _burst(key)                                    # default pool (40 threads)
    assert not bad, bad[:3]
    print(f"   48 concurrent tool calls, 40 worker threads: {dt:.1f}s")
    old_timeout = mcp_remote.INNER_TIMEOUT
    try:
        _on_server_loop(_threads(6))
        _on_server_loop(mcp_remote.GATE.shutdown)            # the next request starts a fresh manager
        assert rpc("tools/list", headers={"authorization": f"Bearer {key}"}).status_code == 200
        assert mcp_remote.GATE._running.sem._value == 3, mcp_remote.GATE._running.sem._value
        bad, dt = _burst(key)
        assert not bad, bad[:3]
        assert dt < 30, dt
        print(f"   48 concurrent tool calls, 6 worker threads, admission 3: {dt:.1f}s, all ok")

        mcp_remote.INNER_TIMEOUT = 2.0                       # counterfactual: no admission limit

        async def open_gate():
            mcp_remote.GATE._running.sem = asyncio.Semaphore(1000)
        _on_server_loop(open_gate)
        bad, dt = _burst(key, 12)
        print(f"   counterfactual (admission 1000, 6 worker threads): {len(bad)}/12 calls failed "
              f"(starved until the {mcp_remote.INNER_TIMEOUT:g}s timeout) in {dt:.1f}s")
        assert bad and any("timed out" in b[1] for b in bad), bad[:3]
    finally:
        mcp_remote.INNER_TIMEOUT = old_timeout
        _on_server_loop(_threads(40))
        _on_server_loop(mcp_remote.GATE.shutdown)
    assert rpc("tools/list", headers={"authorization": f"Bearer {key}"}).status_code == 200
    assert mcp_remote.GATE._running.sem._value == mcp_remote.MAX_CONCURRENT


def test_transport_refuses_the_loop_thread():
    import asyncio

    async def go():
        t = mcp_remote.InProcessTransport(main.app, asyncio.get_running_loop(), None)
        try:
            t.handle_request(httpx.Request("GET", "http://x/health"))
        except RuntimeError as e:
            return str(e)
        return None

    assert "deadlock" in (asyncio.run(go()) or "")


def test_no_key_in_any_error():
    assert SEEN, "expected some refusals to inspect"
    for t in SEEN:
        for s in SECRETS:
            assert s not in t, "an API key was echoed"


TESTS = [test_initialize_list_tools_and_prompts, test_search_gpus_through_the_callers_key,
         test_start_demo_is_a_simulated_sandbox_job, test_scopes_and_errors_come_from_the_api, test_auth_refusals,
         test_unavailable_mcp_is_503_not_a_crash, test_concurrent_calls_cannot_deadlock,
         test_transport_refuses_the_loop_thread, test_no_key_in_any_error]


def main_() -> int:
    failed = 0
    try:
        setup()
        for t in TESTS:
            try:
                t()
                print(f"ok {t.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}")
                traceback.print_exc()
    finally:
        if SRV is not None:
            SRV.stop()
        if S is not None:
            bf.drop(S, DB)
    print("all passed" if not failed else f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_())
