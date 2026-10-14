"""Real protocol tests: spawn `opengrid mcp` as a subprocess and talk to it with the official `mcp` client.

A local mock OpenGrid API (http.server on 127.0.0.1, contract-shaped payloads) stands in for the server, so the
test needs no network and no running OpenGrid.

    uv run --project cli python cli/tests/test_mcp_stdio.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anyio  # noqa: E402
import httpx  # noqa: E402

from _support import SRC, MockAPI, run_tests  # noqa: E402

from mcp import Client, StdioServerParameters  # noqa: E402

API = MockAPI()


class Handler(BaseHTTPRequestHandler):
    def _do(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        req = httpx.Request(self.command, f"http://og.test{self.path}", headers=dict(self.headers), content=body)
        resp = API.handle(req)
        data = resp.content
        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() not in ("content-length", "transfer-encoding"):
                self.send_header(k, v)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _do

    def log_message(self, *a):
        pass


HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()
MOCK_URL = f"http://127.0.0.1:{HTTPD.server_address[1]}"
OPENGRID = str(Path(sys.executable).parent / ("opengrid.exe" if os.name == "nt" else "opengrid"))
WORKDIR = Path(tempfile.mkdtemp(prefix="og-stdio-")).resolve()
CHILD_ENV = {
    "OPENGRID_URL": MOCK_URL,
    "OPENGRID_CONFIG_DIR": str(WORKDIR / "config"),   # no stored key
    "PYTHONPATH": str(SRC),                            # the source tree, even if the editable .pth is skipped
}


def params(*extra: str) -> StdioServerParameters:
    assert Path(OPENGRID).exists(), f"console script missing: {OPENGRID} (run `uv sync --project cli`)"
    return StdioServerParameters(command=OPENGRID, args=["mcp", *extra], env=CHILD_ENV, cwd=str(WORKDIR))


def session(mode: str):
    async def go():
        async with Client(params(), mode=mode, read_timeout_seconds=30) as c:
            info = c.server_info
            tl = (await c.list_tools()).tools
            r = await c.call_tool("opengrid_search_gpus", {"model": "H100", "limit": 3})
            bad = await c.call_tool("opengrid_analyze_project", {"path": "/etc"})
            unauth = await c.call_tool("opengrid_list_jobs", {})
            return c.protocol_version, info, tl, r, bad, unauth
    return anyio.run(go)


def check(result) -> None:
    version, info, tl, r, bad, unauth = result
    assert info is not None and info.name == "opengrid", info
    names = {t.name for t in tl}
    assert len(names) == 11 and "opengrid_search_gpus" in names and "opengrid_cancel_job" in names, names
    assert all(t.output_schema for t in tl)
    assert not r.is_error, r
    sc = r.structured_content
    assert sc["items"][0]["slug"] == "h100-80gb-sxm5" and sc["price_kind"] == "observed", sc
    assert json.loads(r.content[0].text)["items"][0]["slug"] == "h100-80gb-sxm5"
    assert bad.is_error and "outside the allowed roots" in bad.content[0].text
    assert unauth.is_error and "deployments:read" in unauth.content[0].text


def test_stdio_initialize_handshake():
    n = len(API.requests)
    res = session("legacy")
    check(res)
    assert any(q.url.path == "/v1/gpus" for q in API.requests[n:])  # the child really called the API
    print(f"   protocol (legacy initialize): {res[0]}")


def test_stdio_discover():
    res = session("auto")
    check(res)
    print(f"   protocol (auto / server discover): {res[0]}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_loopback_and_origin_validation():
    port = _free_port()
    proc = subprocess.Popen([OPENGRID, "mcp", "--http", "--port", str(port)], env={**os.environ, **CHILD_ENV},
                            cwd=str(WORKDIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        hdr = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        evil = httpx.post(url, json=body, headers={**hdr, "origin": "https://evil.example"}, timeout=10)
        assert evil.status_code in (400, 403), evil.status_code
        rebind = httpx.post(url, json=body, headers={**hdr, "host": "attacker.example"}, timeout=10)
        assert rebind.status_code in (400, 421, 403), rebind.status_code

        async def go():
            async with Client(url, read_timeout_seconds=30) as c:
                r = await c.call_tool("opengrid_search_gpus", {"limit": 2})
                return r
        r = anyio.run(go)
        assert not r.is_error and r.structured_content["returned"] == 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    run_tests([test_stdio_initialize_handshake, test_stdio_discover, test_http_loopback_and_origin_validation])
