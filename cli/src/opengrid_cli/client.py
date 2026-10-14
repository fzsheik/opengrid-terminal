"""Thin HTTP client for the OpenGrid API (shared by the `opengrid` CLI and the local MCP server).

    from opengrid_cli.client import Client
    c = Client()                       # OPENGRID_URL / OPENGRID_API_KEY / stored key
    r = c.get("/v1/gpus", params={"limit": 1000})
    r.data, r.meta, r.request_id

Rules this module keeps:
  - The API key comes from OPENGRID_API_KEY, else the 0600 credentials file written by `opengrid auth login`.
    It is sent only as `Authorization: Bearer ...` and is never printed: every error message passes through
    `redact()`, which removes the key itself and anything shaped like an OpenGrid key or bearer token.
  - Only idempotent GETs are retried (connect errors, timeouts, 429/502/503/504), with a small bounded backoff.
    POSTs are sent once; mutations carry an Idempotency-Key so a manual re-run is safe.
  - Response bodies are read with a hard size cap (default 8 MiB); bigger responses are refused, not truncated.
  - Every response carries the server's X-Request-ID; errors include it so a support request can quote it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:8000"
PRODUCTION_URL = "https://opengrid-terminal-production.up.railway.app"
ENV_URL = "OPENGRID_URL"
ENV_KEY = "OPENGRID_API_KEY"
ENV_CONFIG_DIR = "OPENGRID_CONFIG_DIR"
CREDENTIALS_FILE = "credentials.json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
RETRY_STATUSES = (429, 502, 503, 504)

# Exit codes (documented in README.md and `opengrid --help`).
EXIT_OK = 0
EXIT_ERROR = 1          # API error (4xx/5xx other than below), invalid response, a doctor check failed
EXIT_USAGE = 2          # bad arguments (argparse)
EXIT_AUTH = 3           # 401/403: no key, invalid key, or the key lacks the needed scope
EXIT_NOT_FOUND = 4      # 404: unknown GPU, provider, job
EXIT_UNREACHABLE = 5    # connection refused, DNS, timeout, TLS
EXIT_REFUSED = 6        # a safety rule refused: confirmation missing, not sandbox, path outside allowed roots
EXIT_INTERRUPTED = 130


def version() -> str:
    try:
        from importlib.metadata import version as v

        return v("opengrid-cli")
    except Exception:
        return "0.1.0"


# ---------------------------------------------------------------- redaction

_REDACTED = "[REDACTED]"
_PATTERNS = (
    re.compile(r"\bopg_[A-Za-z0-9_\-]{4,}"),
    re.compile(r"\bogj_[A-Za-z0-9_.\-]{4,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{6,}"),
)


def redact(text: Any, secret: str | None = None) -> str:
    """`text` with the given secret and anything shaped like an OpenGrid key / bearer token removed."""
    s = str(text)
    if secret:
        s = s.replace(secret, _REDACTED)
    s = _PATTERNS[0].sub("opg_" + _REDACTED, s)
    s = _PATTERNS[1].sub("ogj_" + _REDACTED, s)
    s = _PATTERNS[2].sub(lambda m: m.group(1) + " " + _REDACTED, s)
    return s


def key_hint(key: str | None) -> str | None:
    """A non-secret label for a key: its public prefix (the first 13 chars the server also shows), never more."""
    if not key:
        return None
    return key[:13] + "..." if key.startswith("opg_live_") and len(key) > 20 else "opg_..."


# ---------------------------------------------------------------- configuration and stored credentials

def config_dir() -> Path:
    if os.environ.get(ENV_CONFIG_DIR):
        return Path(os.environ[ENV_CONFIG_DIR]).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "opengrid"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "opengrid"


def credentials_path() -> Path:
    return config_dir() / CREDENTIALS_FILE


def load_stored_key() -> str | None:
    p = credentials_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        key = json.loads(raw).get("api_key")
    except (ValueError, AttributeError):
        return None
    return key if isinstance(key, str) and key else None


def store_key(key: str) -> Path:
    """Write the key to the credentials file with mode 0600 (directory 0700). Returns the path."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    p = d / CREDENTIALS_FILE
    tmp = d / f".{CREDENTIALS_FILE}.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"api_key": key, "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(p, 0o600)
    return p


def delete_stored_key() -> bool:
    p = credentials_path()
    if p.exists():
        p.unlink()
        return True
    return False


def credentials_mode_ok(p: Path | None = None) -> bool | None:
    """True if the credentials file is not readable by group/others (None if there is no file)."""
    p = p or credentials_path()
    if not p.exists():
        return None
    if sys.platform == "win32":
        return True
    return not (p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def resolve_url(url: str | None = None) -> str:
    return (url or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")


def resolve_key(key: str | None = None) -> tuple[str | None, str]:
    """(key, source) where source is 'argument' | 'env' | 'file' | 'none'."""
    if key:
        return key, "argument"
    env = os.environ.get(ENV_KEY, "").strip()
    if env:
        return env, "env"
    stored = load_stored_key()
    if stored:
        return stored, "file"
    return None, "none"


# ---------------------------------------------------------------- errors

# The scope each private endpoint needs, so a 401/403 can say exactly what is missing.
_SCOPES = (
    ("POST", re.compile(r"^/v1/route/preview$"), "route:preview"),
    ("POST", re.compile(r"^/v1/jobs/[^/]+/cancel$"), "route:execute"),
    ("POST", re.compile(r"^/v1/jobs$"), "route:execute"),
    ("POST", re.compile(r"^/v1/jobs/preview$"), "route:preview"),
    ("GET", re.compile(r"^/v1/jobs/readiness$"), "route:preview"),
    ("GET", re.compile(r"^/v1/jobs/templates$"), "data:read"),
    ("GET", re.compile(r"^/v1/jobs(/.*)?$"), "deployments:read"),
    ("GET", re.compile(r"^/v1/route/.+$"), "route:preview"),
    ("GET", re.compile(r"^/v1/me$"), "any valid key"),
    ("POST", re.compile(r"^/v1/recommend(/estimate)?$"), "data:read"),
    ("GET", re.compile(r"^/v1/.*$"), "data:read"),
)


def scope_for(method: str, path: str) -> str | None:
    for m, rx, scope in _SCOPES:
        if m == method.upper() and rx.match(path):
            return scope
    return None


class OpenGridError(Exception):
    exit_code = EXIT_ERROR

    def __init__(self, message: str, *, status: int | None = None, request_id: str | None = None,
                 code: str | None = None, detail: Any = None, path: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.request_id = request_id
        self.code = code
        self.detail = detail
        self.path = path

    def __str__(self) -> str:
        s = self.message
        if self.request_id:
            s += f" (request id {self.request_id})"
        return s

    def as_dict(self) -> dict:
        return {"error": {"type": type(self).__name__, "message": self.message, "status": self.status,
                          "code": self.code, "request_id": self.request_id, "path": self.path,
                          "exit_code": self.exit_code}}


class AuthError(OpenGridError):
    exit_code = EXIT_AUTH


class NotFoundError(OpenGridError):
    exit_code = EXIT_NOT_FOUND


class UnreachableError(OpenGridError):
    exit_code = EXIT_UNREACHABLE


class RefusedError(OpenGridError):
    """A local safety rule refused the action (nothing was sent)."""
    exit_code = EXIT_REFUSED


class ResponseTooLarge(OpenGridError):
    pass


def _detail_text(detail: Any) -> tuple[str, str | None]:
    """FastAPI detail -> (text, code). detail may be a string, {code, message}, or a validation list."""
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or json.dumps(detail)[:500]), detail.get("code")
    if isinstance(detail, list):
        parts = []
        for d in detail[:5]:
            if isinstance(d, dict):
                loc = ".".join(str(x) for x in d.get("loc", []) if x not in ("body", "query"))
                parts.append(f"{loc}: {d.get('msg')}" if loc else str(d.get("msg")))
            else:
                parts.append(str(d))
        return "; ".join(parts), "validation_error"
    return str(detail)[:500], None


# ---------------------------------------------------------------- client

@dataclass
class Response:
    status: int
    data: Any
    meta: dict = field(default_factory=dict)
    request_id: str | None = None
    headers: dict = field(default_factory=dict)
    raw: Any = None


class Client:
    def __init__(self, url: str | None = None, api_key: str | None = None, *, timeout: float = DEFAULT_TIMEOUT,
                 retries: int = 2, max_bytes: int = MAX_RESPONSE_BYTES, transport: httpx.BaseTransport | None = None,
                 use_stored_key: bool = True):
        self.base_url = resolve_url(url)
        if api_key is None and not use_stored_key:
            env = os.environ.get(ENV_KEY, "").strip()
            self.api_key, self.key_source = (env, "env") if env else (None, "none")
        else:
            self.api_key, self.key_source = resolve_key(api_key)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.max_bytes = max_bytes
        headers = {"User-Agent": f"opengrid-cli/{version()}", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._http = httpx.Client(base_url=self.base_url, headers=headers, transport=transport,
                                  timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                                  follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def _redact(self, text: Any) -> str:
        return redact(text, self.api_key)

    # -------- requests

    def get(self, path: str, params: dict | None = None) -> Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        return self._request("GET", path, params=params, retry=True)

    def post(self, path: str, body: dict | None = None, *, idempotency_key: str | None = None,
             anonymous_ok: bool = False) -> Response:
        """`anonymous_ok`: a pure-computation POST (no writes) that the server allows without a key. Without a key,
        it carries X-OpenGrid-Request: 1, the header the server's CSRF rule asks deliberate (non-form) clients for.
        Mutations never send it, so they cannot ride on a local dev server's open operator access."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        if anonymous_ok and not self.api_key:
            headers["X-OpenGrid-Request"] = "1"
        return self._request("POST", path, json_body=body if body is not None else {}, headers=headers or None,
                             retry=False)

    def _request(self, method: str, path: str, *, params=None, json_body=None, headers=None, retry: bool) -> Response:
        rid = "cli_" + uuid.uuid4().hex[:20]
        hdrs = {"X-Request-ID": rid, **(headers or {})}
        attempts = 1 + (self.retries if retry else 0)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                with self._http.stream(method, path, params=params, json=json_body, headers=hdrs) as r:
                    body = self._read_bounded(r, path)
                    status, resp_headers = r.status_code, dict(r.headers)
            except httpx.TimeoutException as e:
                last_exc = UnreachableError(f"timed out after {self.timeout:g}s calling {method} {path} on "
                                            f"{self.base_url}", path=path, request_id=rid)
                last_exc.__cause__ = e
            except httpx.TransportError as e:
                last_exc = UnreachableError(f"cannot reach OpenGrid at {self.base_url} ({type(e).__name__}: "
                                            f"{self._redact(e)}); is the server running? set --url or "
                                            f"{ENV_URL}", path=path, request_id=rid)
                last_exc.__cause__ = e
            else:
                server_rid = resp_headers.get("x-request-id") or rid
                if retry and status in RETRY_STATUSES and attempt < attempts - 1:
                    time.sleep(self._backoff(attempt, resp_headers.get("retry-after")))
                    continue
                return self._finish(method, path, status, body, resp_headers, server_rid)
            if attempt < attempts - 1:
                time.sleep(self._backoff(attempt, None))
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(5.0, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return min(4.0, 0.5 * (2 ** attempt))

    def _read_bounded(self, r: httpx.Response, path: str) -> bytes:
        declared = r.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise ResponseTooLarge(f"response from {path} is {int(declared)} bytes, over the "
                                   f"{self.max_bytes}-byte limit", path=path, status=r.status_code,
                                   request_id=r.headers.get("x-request-id"))
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf.extend(chunk)
            if len(buf) > self.max_bytes:
                raise ResponseTooLarge(f"response from {path} exceeded the {self.max_bytes}-byte limit",
                                       path=path, status=r.status_code, request_id=r.headers.get("x-request-id"))
        return bytes(buf)

    def _finish(self, method, path, status, body: bytes, headers: dict, rid: str | None) -> Response:
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None
            if 200 <= status < 300:
                raise OpenGridError(f"{method} {path}: the server returned non-JSON content", status=status,
                                    request_id=rid, path=path)
        if 200 <= status < 300:
            if isinstance(payload, dict) and "data" in payload and set(payload) <= {"data", "meta"}:
                return Response(status, payload["data"], payload.get("meta") or {}, rid, headers, payload)
            return Response(status, payload, {}, rid, headers, payload)
        detail = payload.get("detail") if isinstance(payload, dict) else (body[:300].decode("utf-8", "replace")
                                                                           if body else None)
        text, code = _detail_text(detail)
        text = self._redact(text)
        scope = scope_for(method, path)
        if status == 401:
            if not self.api_key:
                need = f"an OpenGrid API key with scope {scope!r}" if scope and scope != "any valid key" \
                    else "an OpenGrid API key"
                msg = (f"{method} {path} needs {need}. Set {ENV_KEY} or run `opengrid auth login` "
                       f"(server said: {text or 'unauthorized'})")
            else:
                msg = (f"the API key was rejected by {self.base_url} (server said: {text or 'unauthorized'}); "
                       f"check {ENV_KEY} / `opengrid auth status`"
                       + (f". This call needs scope {scope!r}" if scope and scope != "any valid key" else ""))
            raise AuthError(msg, status=status, request_id=rid, code=code or "unauthorized", path=path)
        if status == 403:
            msg = f"{method} {path} was refused: {text or 'forbidden'}"
            if scope and scope != "any valid key":
                msg += f". This needs an API key with scope {scope!r}"
            raise AuthError(msg, status=status, request_id=rid, code=code or "forbidden", path=path)
        if status == 404:
            raise NotFoundError(text or f"{path} not found", status=status, request_id=rid, code=code, path=path)
        if status == 429:
            ra = headers.get("retry-after")
            raise OpenGridError(f"rate limited: {text}" + (f"; retry after {ra}s" if ra else ""), status=status,
                                request_id=rid, code=code or "rate_limited", path=path)
        if status >= 500:
            raise OpenGridError(f"OpenGrid server error {status} on {method} {path}"
                                + (f": {text}" if text else ""), status=status, request_id=rid, code=code,
                                path=path)
        raise OpenGridError(f"{method} {path} failed ({status}): {text}", status=status, request_id=rid,
                            code=code, detail=self._redact_obj(detail), path=path)

    def _redact_obj(self, obj: Any) -> Any:
        if obj is None:
            return None
        try:
            return json.loads(self._redact(json.dumps(obj)))
        except (TypeError, ValueError):
            return self._redact(obj)


def new_idempotency_key() -> str:
    return "cli-" + uuid.uuid4().hex
