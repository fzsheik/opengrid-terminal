"""One probe of one target. Never raises: every outcome, including DNS failure, comes back as a result dict.

Timings (ms, None when the phase did not happen):
    dns_ms      our own getaddrinfo, timed separately (the OS resolver cache can make this near zero on repeat probes)
    connect_ms  TCP connect (httpcore resolves again inside it, usually from cache)
    tls_ms      TLS handshake
    ttfb_ms     request sent -> response headers received
    total_ms    start -> response headers received (the body is never read)
A fresh connection is made per probe (no keep-alive), so the connect and TLS phases are real every time.

error_class: dns | connect_timeout | connect_failed | timeout | connection_refused | unreachable | tls | reset | protocol | http_5xx |
             http_4xx | http_status | other. `ok` is true exactly when the status is in the target's accept list.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from config import settings
from probes.targets import Target

TIMEOUT_S = 10.0
USER_AGENT = f"OpenGridTerminal-Probe/1.0 (+{settings.public_base_url})"


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, follow_redirects=False,
                             timeout=httpx.Timeout(TIMEOUT_S), limits=httpx.Limits(max_keepalive_connections=0))


def _ms(a: float | None, b: float | None) -> int | None:
    return None if a is None or b is None else max(0, int(round((b - a) * 1000)))


def classify_exception(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return "timeout"
    chain, e = [], exc
    while e is not None and len(chain) < 6:
        chain.append(e)
        e = e.__cause__ or e.__context__
    text = " ".join(str(x) for x in chain).lower()
    if any(isinstance(x, ssl.SSLError) for x in chain) or "certificate" in text or "ssl" in text or "tls" in text:
        return "tls"
    if any(isinstance(x, socket.gaierror) for x in chain) or "name or service not known" in text or "nodename nor servname" in text:
        return "dns"
    if "refused" in text:
        return "connection_refused"
    if "unreachable" in text or "no route" in text:
        return "unreachable"
    if isinstance(exc, httpx.ConnectError):
        return "connect_failed"
    if isinstance(exc, (httpx.ReadError, httpx.WriteError)) or "reset" in text:
        return "reset"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "protocol"
    return "other"


def status_class(status: int) -> str:
    return "http_5xx" if status >= 500 else "http_4xx" if status >= 400 else "http_status"


async def measure(c: httpx.AsyncClient, t: Target, location: str) -> dict:
    observed = datetime.now(timezone.utc)
    out = {"target_id": t.id, "location": location, "observed_at": observed, "ok": False, "status_code": None,
           "error_class": None, "error": None, "dns_ms": None, "connect_ms": None, "tls_ms": None, "ttfb_ms": None,
           "total_ms": None, "resolved_ip": None}
    parts = urlsplit(t.url)
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    start = time.perf_counter()
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        out["dns_ms"] = _ms(start, time.perf_counter())
        out["resolved_ip"] = infos[0][4][0]
    except Exception as exc:  # noqa: BLE001
        out.update(error_class="dns", error=f"{type(exc).__name__}: {exc}"[:200], total_ms=_ms(start, time.perf_counter()))
        return out
    ev: dict[str, float] = {}

    async def trace(name: str, info) -> None:
        ev[name] = time.perf_counter()

    t_req = time.perf_counter()
    try:
        async with c.stream("GET", t.url, extensions={"trace": trace}) as resp:
            got = time.perf_counter()
            out["status_code"] = resp.status_code
    except Exception as exc:  # noqa: BLE001
        out.update(error_class=classify_exception(exc), error=f"{type(exc).__name__}: {exc}"[:200],
                   total_ms=_ms(start, time.perf_counter()))
    else:
        out.update(connect_ms=_ms(ev.get("connection.connect_tcp.started"), ev.get("connection.connect_tcp.complete")),
                   tls_ms=_ms(ev.get("connection.start_tls.started"), ev.get("connection.start_tls.complete")),
                   ttfb_ms=_ms(ev.get("http11.send_request_headers.started") or ev.get("http2.send_request_headers.started") or t_req, got),
                   total_ms=_ms(start, got))
        if out["status_code"] in t.accept:
            out["ok"] = True
        else:
            out.update(error_class=status_class(out["status_code"]), error=f"HTTP {out['status_code']} (accepted: {', '.join(map(str, t.accept))})")
    return out
