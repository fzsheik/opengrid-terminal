"""Request-level security shared by main.py's middleware and accounts/: deployment detection,
the client IP, CSRF, login throttling, anonymous rate limits and HTML security headers.
See methodology/security.md for the threat model behind each rule.

    deployed()            fail-closed deployment detection (any signal counts)
    client_ip(request)    X-Forwarded-For only when settings.trust_proxy_headers
    csrf_violation(req)   None, or why a state-changing request with ambient credentials is refused
    login_locked(ip) / login_failed(ip)       per-IP basic-auth failure throttle (in-process)
    public_allowed(ip)    per-IP token bucket for anonymous PUBLIC_PAGES visitors
    html_headers(path, body) -> dict           CSP (with hashes of the page's own inline scripts) etc.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import re
import threading
import time
from collections import deque
from urllib.parse import urlparse

from config import settings

log = logging.getLogger(__name__)

ENVIRONMENTS = ("development", "production")
_LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
_warned_env = False


# ---------------------------------------------------------------- deployment detection

def deployment_signals() -> list[str]:
    """Every reason to believe this process is deployed. Empty = local development."""
    out = []
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        out.append("RAILWAY_ENVIRONMENT is set")
    env = (settings.environment or "").strip().lower()
    if env == "production":
        out.append("environment=production")
    elif env not in ENVIRONMENTS:
        out.append(f"environment={env!r} is not 'development'")  # unknown values fail closed
    try:
        host = (urlparse(settings.database_url).hostname or "").lower()
    except ValueError:
        host = "?"
    if host not in _LOCAL_DB_HOSTS:
        out.append(f"DATABASE_URL host {host!r} is not local")
    return out


def deployed() -> bool:
    """True if ANY signal says deployed. Failing open (treating a server as dev) would run it with a
    derived pepper, a derived encryption key and possibly no password; failing closed only costs a
    developer one env var."""
    global _warned_env
    sig = deployment_signals()
    if sig and os.environ.get("RAILWAY_ENVIRONMENT") and settings.environment != "production" and not _warned_env:
        log.warning("running on Railway without ENVIRONMENT=production: set it (deploy/make_env.py writes it)")
        _warned_env = True
    return bool(sig)


# ---------------------------------------------------------------- client IP

def _ip(value: str) -> str | None:
    value = (value or "").strip().strip('"')
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    elif value.count(":") == 1:  # ipv4:port
        value = value.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request) -> str | None:
    """The caller's IP. X-Forwarded-For is client-controlled except for the entries our own proxies
    append on the RIGHT, so with trust_proxy_headers we take the entry trusted_proxy_count hops from
    the right (Railway's edge appends exactly one), never the left-most."""
    if request is None:
        return None
    peer = request.client.host if request.client else None
    if settings.trust_proxy_headers:
        hops = [h for h in (request.headers.get("x-forwarded-for") or "").split(",") if h.strip()]
        n = max(1, settings.trusted_proxy_count)
        if len(hops) >= n:
            ip = _ip(hops[-n])
            if ip:
                return ip[:64]
    return peer[:64] if peer else None


# ---------------------------------------------------------------- CSRF

CSRF_HEADER = "x-opengrid-request"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _netloc(url: str) -> str:
    try:
        u = urlparse(url)
    except ValueError:
        return ""
    if not u.hostname:
        return ""
    default = {"http": 80, "https": 443}.get(u.scheme)
    port = u.port
    return u.hostname.lower() + (f":{port}" if port and port != default else "")


def _host(request) -> str:
    h = (request.headers.get("host") or "").lower()
    for suffix in (":80", ":443"):
        if h.endswith(suffix):
            h = h[: -len(suffix)]
    return h


def csrf_violation(request) -> str | None:
    """For a request authenticated by AMBIENT credentials (the browser re-sends cached basic auth, or
    none at all in open dev): refuse state changes that a cross-site page could have triggered.

      - the custom header X-OpenGrid-Request: 1 is required. A cross-site page can only send it with
        fetch(), which forces a CORS preflight, and no CORS is configured, so the preflight fails;
        a plain <form> cannot set headers at all.
      - when the browser says where the request came from (Origin / Sec-Fetch-Site), it must be us.
    Bearer API keys are exempt (not ambient: a page cannot make the browser attach one)."""
    if request.method.upper() in SAFE_METHODS:
        return None
    if request.headers.get(CSRF_HEADER) != "1":
        return ("cross-site request refused: state-changing requests authenticated by the site login must "
                "send the header 'X-OpenGrid-Request: 1' (the OpenGrid web app does; API clients should use "
                "an API key: Authorization: Bearer opg_...)")
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return f"cross-site request refused: Sec-Fetch-Site is {site!r}"
    origin = request.headers.get("origin")
    if origin is not None:
        mine = {_host(request), _netloc(settings.public_base_url)} - {""}
        if _netloc(origin) not in mine:
            return "cross-site request refused: Origin does not match this site"
    return None


def open_dev_host_ok(request) -> bool:
    """Open development (no APP_PASSWORD) answers only to Hosts a DNS-rebinding page cannot produce.

    Without a password the site is OPERATOR to anyone who can reach it. A web page on evil.example that
    re-points its own name at 127.0.0.1 is, to the browser, same-origin with it: Origin and Host both say
    evil.example, so the CSRF rule cannot tell. Its requests still carry Host: evil.example, so: only
    IP literals, localhost names, Starlette's test host and our own public host are answered."""
    host = (request.headers.get("host") or "").strip().lower()
    if host.startswith("["):
        name = host[1:host.find("]")] if "]" in host else host
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    mine = (urlparse(settings.public_base_url).hostname or "").lower()
    return name in ("localhost", "testserver") or name.endswith(".localhost") or (bool(mine) and name == mine)


# ---------------------------------------------------------------- login throttle

_fail_lock = threading.Lock()
_failures: dict[str, deque] = {}
clock = time.monotonic  # tests may replace


def _prune(q: deque, now: float) -> None:
    window = settings.login_failure_window_seconds
    while q and now - q[0] > window:
        q.popleft()


def login_locked(ip: str | None) -> int:
    """Seconds until this IP may try the site password again (0 = not locked)."""
    key = ip or "?"
    now = clock()
    with _fail_lock:
        q = _failures.get(key)
        if not q:
            return 0
        _prune(q, now)
        if len(q) < settings.login_max_failures:
            return 0
        return max(1, int(settings.login_failure_window_seconds - (now - q[0])) + 1)


def login_failed(ip: str | None) -> None:
    key = ip or "?"
    now = clock()
    with _fail_lock:
        if len(_failures) > 50_000:  # bounded memory under a spray of source addresses
            _failures.clear()
        q = _failures.setdefault(key, deque())
        _prune(q, now)
        q.append(now)
        if len(q) == settings.login_max_failures:
            log.warning("basic auth: %s failed logins from %s; locked for %ss", len(q), key,
                        settings.login_failure_window_seconds)


def is_basic(header: str) -> bool:
    return header.lower().startswith("basic ")


# ---------------------------------------------------------------- anonymous rate limit

def public_allowed(ip: str | None):
    """Token bucket per client IP for anonymous PUBLIC_PAGES traffic. Returns a ratelimit.Decision."""
    from accounts import ratelimit

    return ratelimit.take(("ip", ip or "?"), "public", max(1, settings.public_rate_limit_per_minute))


def reset() -> None:
    with _fail_lock:
        _failures.clear()


# ---------------------------------------------------------------- HTML security headers

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)
_NON_JS = re.compile(r"""\btype\s*=\s*["']?(application/(ld\+)?json|text/template)""", re.I)
# FastAPI's /docs and /redoc pages load Swagger UI / ReDoc from this CDN (operator-only pages).
_DOCS_CDN = "https://cdn.jsdelivr.net"
_DOCS_PATHS = ("/docs", "/redoc", "/docs/oauth2-redirect")

COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}


def inline_script_hashes(html: str) -> list[str]:
    """'sha256-...' for each inline executable <script> in the page, so the CSP can allow exactly
    the page's own boot scripts and nothing an injection adds."""
    out = []
    for attrs, body in _SCRIPT.findall(html):
        if re.search(r"\bsrc\s*=", attrs, re.I) or _NON_JS.search(attrs):
            continue
        out.append("'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode() + "'")
    return sorted(set(out))


_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
_trusted_cache: dict = {}


def trusted_script_hashes() -> frozenset:
    """Hashes of the inline scripts in OUR templates (web/*.html), re-read when a file changes.
    Only these may be allowed: hashing whatever inline script a response happens to contain would
    also allow a script injected into it (the CSP would then stop nothing)."""
    try:
        files = sorted(f for f in os.listdir(_WEB) if f.endswith(".html"))
        stamp = tuple((f, os.stat(os.path.join(_WEB, f)).st_mtime) for f in files)
    except OSError:
        return frozenset()
    if _trusted_cache.get("stamp") != stamp:
        out = set()
        for f in files:
            with open(os.path.join(_WEB, f), encoding="utf-8", newline="") as fh:
                raw = fh.read()
            # as stored (FileResponse sends the bytes) and with newlines normalized (read_text templates)
            for variant in {raw, raw.replace("\r\n", "\n"), raw.replace("\r\n", "\n").replace("\n", "\r\n")}:
                out.update(inline_script_hashes(variant))
        _trusted_cache.update(stamp=stamp, hashes=frozenset(out))
    return _trusted_cache["hashes"]


def csp(path: str, html: str) -> str:
    page = inline_script_hashes(html)
    if path not in _DOCS_PATHS:  # FastAPI generates the /docs boot scripts itself (no user data, operator only)
        allowed = trusted_script_hashes()
        page = [h for h in page if h in allowed]
    hashes = " ".join(page)
    script = f"'self' {hashes}".strip()
    style = "'self' 'unsafe-inline'"   # the UI sets style attributes; styles cannot run script
    img = "'self' data:"
    extra = ""
    if path in _DOCS_PATHS:
        script += f" {_DOCS_CDN}"
        style += f" {_DOCS_CDN}"
        img += " https://fastapi.tiangolo.com"
        extra = "; worker-src 'self' blob:"
    return (f"default-src 'self'; script-src {script}; style-src {style}; img-src {img}; font-src 'self' data:; "
            f"connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
            f"frame-ancestors 'none'{extra}")
