"""The hosted MCP endpoint: /mcp. An assistant connects with a URL and an OpenGrid API key; nothing to install.

    POST https://<host>/mcp     Authorization: Bearer opg_live_...     (MCP streamable HTTP, stateless, JSON)

Same tools as the local `opengrid mcp` server (opengrid_cli.mcp_server.build_server(remote=True)), minus the
ones that read local files. Every tool reaches the REST API through opengrid_cli.client.Client, acting with
the CALLER's key, so scopes, ownership, rate limits and usage logging are exactly those of the /v1 API.

Auth (the gate below, before the MCP app sees the request):
  - only `Authorization: Bearer opg_...`, verified by accounts.keys.verify; anything else is 401 JSON with
    `WWW-Authenticate: Bearer`. The site login (basic auth) and the open dev server are NEVER accepted here, and
    PUBLIC_PAGES does not open /mcp: a Bearer key is not ambient, so the endpoint has no CSRF surface;
  - main.require_password lets /mcp through to this gate, like Bearer requests to /v1;
  - the MCP envelope request itself is rate-limited as a read (accounts/ratelimit.py); each tool's own /v1
    calls are classified and counted normally;
  - error texts are fixed strings or the server's own auth details, passed through redact(): never the key.

How tools reach the API (and why it cannot deadlock):
  The SDK runs sync tool functions in anyio worker threads (mcp.server.mcpserver.utilities.func_metadata:
  `anyio.to_thread.run_sync`), not on the event loop. The tool's Client uses InProcessTransport: the worker
  thread hands the request to the app's OWN event loop (asyncio.run_coroutine_threadsafe, through
  httpx.ASGITransport, i.e. the full middleware stack) and blocks only itself while the loop serves it. No
  network hop, no port to discover. The transport refuses to run on the loop thread (that would deadlock).
  Thread starvation: MCP tool threads and the app's sync endpoints share anyio's default limiter (40
  threads). A tool thread waits on an inner request that may need a thread too, so the gate admits at most
  admission_limit() MCP requests at once (MAX_CONCURRENT = 8, never more than half the pool; others wait up
  to QUEUE_WAIT_SECONDS, then 503): tool threads can never hold every token, so inner requests always find
  one. tests/test_mcp_remote.py shows both sides: 48 concurrent calls on a 6-thread pool all finish, and with
  the limit removed they starve until the in-process timeout.

If `mcp` / opengrid_cli cannot be imported, the app still starts (warning logged) and /mcp answers 503.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from accounts import security
from config import settings

log = logging.getLogger("opengrid.mcp_remote")
# The SDK logs every stateless request ("Terminating session") and every tool error at INFO: keep the app's
# log readable (requests are already in opengrid.access; tool errors reach the caller).
logging.getLogger("mcp").setLevel(logging.WARNING)

PATH = "/mcp"
MAX_CONCURRENT = 8          # well under anyio's default 40 worker threads: see the module docstring
QUEUE_WAIT_SECONDS = 15.0
INNER_TIMEOUT = 30.0        # one in-process /v1 call
SCOPE_KEY = "opengrid.mcp_caller"


def _import_cli():
    """opengrid_cli is installed as a package (pyproject path source). A repo checkout whose editable .pth is
    skipped (macOS `hidden` flag, see cli/README.md) falls back to the same source tree."""
    try:
        from opengrid_cli import client, mcp_server
    except ImportError:
        src = Path(__file__).resolve().parent.parent / "cli" / "src"
        if not (src / "opengrid_cli" / "mcp_server.py").exists():
            raise
        sys.path.append(str(src))
        from opengrid_cli import client, mcp_server
    import mcp  # noqa: F401  (the SDK itself; mcp_server imports it lazily)

    return client, mcp_server


@dataclass(frozen=True)
class Caller:
    key: str
    ip: str | None
    loop: asyncio.AbstractEventLoop


_caller: contextvars.ContextVar[Caller | None] = contextvars.ContextVar("opengrid_mcp_caller", default=None)


class InProcessTransport(httpx.BaseTransport):
    """A sync httpx transport that serves each request through the ASGI app on its event loop.

    Call it from a worker thread only: it blocks the calling thread until the loop has answered."""

    def __init__(self, app, loop: asyncio.AbstractEventLoop, client_ip: str | None, timeout: float = 30.0):
        self.app, self.loop, self.timeout = app, loop, timeout
        self.client = (client_ip or "127.0.0.1", 0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            raise RuntimeError("InProcessTransport used on the event loop thread: it would deadlock")
        fut = asyncio.run_coroutine_threadsafe(self._send(request), self.loop)
        try:
            status, headers, body = fut.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise httpx.ReadTimeout("in-process request timed out", request=request) from None
        return httpx.Response(status, headers=headers, content=body, request=request)

    async def _send(self, request: httpx.Request):
        t = httpx.ASGITransport(app=self.app, raise_app_exceptions=False, client=self.client)
        r = await t.handle_async_request(request)
        try:
            body = b"".join([chunk async for chunk in r.stream])  # raw bytes; the sync client decodes
        finally:
            await r.aclose()
        return r.status_code, r.headers.multi_items(), body


class CallerContext:
    """MCPServer middleware: exposes the gate's verified caller to the tool functions (a context variable,
    copied into the worker thread that runs each sync tool)."""

    async def __call__(self, ctx, call_next):
        req = getattr(ctx, "request", None)
        scope = getattr(req, "scope", None) or {}
        token = _caller.set(scope.get(SCOPE_KEY))
        try:
            return await call_next(ctx)
        finally:
            _caller.reset(token)


def _deny(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    h = dict(headers or {})
    if status == 401:
        h["WWW-Authenticate"] = "Bearer"
    return JSONResponse({"detail": detail}, status_code=status, headers=h)


def _origin_ok(request: Request) -> bool:
    """DNS rebinding / Origin. The SDK's Host allowlist is OFF on purpose: this is a public host (Railway plus
    any custom domain), and DNS rebinding only matters for servers that trust the network instead of a
    credential; here every request needs a Bearer key a browser never attaches by itself. What we do check:
    a browser-sent Origin must be this site's own (the request's Host, or settings.public_base_url). Native MCP
    clients send no Origin. No CORS headers are ever sent, so other origins cannot read responses anyway."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    netloc = urlparse(origin).netloc.lower()
    return bool(netloc) and netloc in {(request.headers.get("host") or "").lower(),
                                       urlparse(settings.public_base_url).netloc.lower()}


def admission_limit() -> int:
    """MCP requests admitted at once: MAX_CONCURRENT, and never more than half of anyio's worker threads (the
    pool the tool threads share with the app's sync endpoints), so inner requests always find a thread."""
    import anyio.to_thread

    tokens = int(anyio.to_thread.current_default_thread_limiter().total_tokens)
    return max(1, min(MAX_CONCURRENT, tokens // 2))


class _Running:
    """One MCP server + session manager bound to one event loop (the SDK's manager runs once per instance)."""

    def __init__(self, gate: "Gate", loop: asyncio.AbstractEventLoop):
        _, mcp_server = gate.cli
        self.loop = loop
        self.server = mcp_server.build_server(gate.client_factory, remote=True,
                                              site_url=settings.public_base_url, middleware=[CallerContext()])
        self.server.streamable_http_app(stateless_http=True, json_response=True)  # creates the session manager
        self.manager = self.server.session_manager
        self.sem = asyncio.Semaphore(admission_limit())
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.error: BaseException | None = None
        # a fresh context: the manager's tasks must not inherit the first request's context variables
        self.task = loop.create_task(self._run(), context=contextvars.Context())

    async def _run(self):
        try:
            async with self.manager.run():
                self.ready.set()
                await self.stop.wait()
        except asyncio.CancelledError:  # the server is shutting down
            self.error = RuntimeError("stopped")
            raise
        except Exception as e:  # noqa: BLE001
            self.error = e
            log.exception("hosted MCP session manager stopped")
        finally:
            self.ready.set()


class Gate:
    """The ASGI app at /mcp: authenticate, admit, then hand the request to the MCP session manager."""

    def __init__(self, app):
        self.app = app
        self.cli: tuple[Any, Any] | None = None
        self.unavailable: str | None = None
        self._running: _Running | None = None
        try:
            self.cli = _import_cli()
        except Exception as e:  # noqa: BLE001
            self.unavailable = f"{type(e).__name__}: {e}"
            log.warning("hosted MCP endpoint disabled (/mcp answers 503): cannot import the MCP server (%s)",
                        self.unavailable)

    # -------- tools' API client: the caller's key, through the app itself

    def client_factory(self):
        client, _ = self.cli
        c = _caller.get()
        if c is None:
            raise client.AuthError("no authenticated caller for this MCP request", code="unauthorized")
        return client.Client(settings.public_base_url, api_key=c.key, use_stored_key=False, retries=0,
                             timeout=INNER_TIMEOUT,
                             transport=InProcessTransport(self.app, c.loop, c.ip, INNER_TIMEOUT))

    # -------- lifecycle

    async def _ensure(self) -> _Running:
        loop = asyncio.get_running_loop()
        r = self._running
        if r is None or r.loop is not loop:
            r = self._running = _Running(self, loop)
        await r.ready.wait()
        if r.error is not None:
            self._running = None
            raise RuntimeError("the hosted MCP session manager is not running")
        return r

    async def startup(self) -> None:
        if self.cli is None:
            return
        try:
            await self._ensure()
        except Exception as e:  # noqa: BLE001  (never take the site down for /mcp; it retries per request)
            self._running = None
            log.warning("hosted MCP endpoint failed to start (%s); /mcp will retry on demand", type(e).__name__)

    async def shutdown(self) -> None:
        r, self._running = self._running, None
        if r is not None and r.loop is asyncio.get_running_loop():
            r.stop.set()
            try:
                await asyncio.wait_for(r.task, 10)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                r.task.cancel()

    # -------- requests

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        resp = await self._check(request)
        if resp is not None:
            return await resp(scope, receive, send)
        try:
            r = await self._ensure()
        except Exception:  # noqa: BLE001
            return await _deny(503, "the MCP endpoint is unavailable right now; try again later")(scope, receive, send)
        try:
            await asyncio.wait_for(r.sem.acquire(), QUEUE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return await _deny(503, "the MCP endpoint is busy; try again shortly",
                               {"Retry-After": "5"})(scope, receive, send)
        try:
            if r.stop.is_set() or r.error is not None:  # shut down while this request waited
                return await _deny(503, "the MCP endpoint is restarting; try again")(scope, receive, send)
            await r.manager.handle_request(scope, receive, send)
        finally:
            r.sem.release()

    async def _check(self, request: Request) -> JSONResponse | None:
        if self.cli is None:
            return _deny(503, "the hosted MCP endpoint is not available on this server")
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            return _deny(401, "an OpenGrid API key is required: send Authorization: Bearer opg_... "
                              "(the site login is not accepted here)")
        if not _origin_ok(request):
            return _deny(403, "cross-origin requests to /mcp are refused")
        token = auth[7:].strip()
        from accounts import keys

        try:
            await run_in_threadpool(keys.verify, token, request)
        except HTTPException as e:
            client, _ = self.cli
            detail = client.redact(e.detail if isinstance(e.detail, str) else "unauthorized", token)
            headers = {k: v for k, v in (e.headers or {}).items() if k.lower() != "www-authenticate"}
            return _deny(e.status_code, detail, headers)
        except Exception as e:  # noqa: BLE001  (database down, ...): never echo exception text
            log.warning("MCP key check failed: %s", type(e).__name__)
            return _deny(503, "cannot verify the API key right now; try again shortly")
        scope = request.scope
        scope[SCOPE_KEY] = Caller(key=token, ip=security.client_ip(request), loop=asyncio.get_running_loop())
        return None


GATE: Gate | None = None


def install(app) -> Gate:
    """Called by main.py before the page router: the /mcp route (every method) served by the gate."""
    global GATE
    GATE = Gate(app)
    app.router.routes.append(Route(PATH, GATE, include_in_schema=False))
    return GATE
