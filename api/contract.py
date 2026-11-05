"""The OpenGrid Data API contract at the HTTP edge (docs/API_CONTRACT.md sections 3 and 4).

On Data API paths (market intelligence; DATA_AREAS below) one pure ASGI middleware:
    * resolves the API version: `OpenGrid-Version` header, else the version pinned to the API key, else latest;
      an unknown header value is a 400 before any route runs;
    * echoes the version used in the `OpenGrid-Version` response header (and api.common.envelope puts it, with
      the request id, in `meta`);
    * rewrites EVERY error response into one shape, whoever produced it (HTTPException, validation, the site gate,
      rate limits, an unhandled exception):
          {"error": {"type", "code", "message", "param", "request_id", "docs_url"}}

The Compute API (jobs, routing, reservations, accounts...) keeps FastAPI's {"detail": ...} for now.
"""

import json
import logging
import re
from datetime import datetime

import observability
from config import settings

log = logging.getLogger(__name__)

# Dated versions, oldest first. A breaking change appends a date here and adds its downgrade step (api/versions/).
VERSIONS = ("2026-11-03",)
LATEST = VERSIONS[-1]
HEADER = "OpenGrid-Version"
# Breaking changes, oldest first: (version that introduced it, downgrade(data, path) -> data in the previous shape).
# A request pinned to an older version gets every newer step's downgrade applied, newest first. Empty until the first
# breaking change; add the step in api/versions/<date>.py and register it here with its date in VERSIONS.
STEPS: list[tuple[str, object]] = []
CACHE_CONTROL = "private, max-age=60"  # prices change per poll (minutes); 60 s keeps a client's view fresh

# First path segment after /v1/ of every Data API area. Shrinks to the contract's route list as routes move.
DATA_AREAS = frozenset({
    "gpus", "families", "providers", "regions", "markets", "listings",
    "reserved", "indices", "fixings", "capacity", "costs", "fx", "reliability",
    "events", "news", "timeline", "macro", "methodology", "exports", "openapi.json",
})

_TYPE = {401: "authentication", 403: "permission", 429: "rate_limit"}
_CODE = {400: "bad_request", 401: "authentication_required", 403: "permission_denied", 404: "not_found",
         405: "method_not_allowed", 409: "conflict", 413: "payload_too_large", 415: "unsupported_media_type",
         422: "invalid_parameter", 429: "rate_limited", 500: "internal_error", 502: "bad_gateway",
         503: "unavailable", 504: "timeout"}
_STD = ("type", "code", "message", "param", "request_id", "docs_url")


def on_data_host(host: str) -> bool:
    return bool(settings.data_api_host) and host.split(":", 1)[0].lower() == settings.data_api_host.lower()


def is_data_path(path: str) -> bool:
    if not path.startswith("/v1/"):
        return False
    return path[4:].split("/", 1)[0] in DATA_AREAS


def error_type(status: int) -> str:
    return _TYPE.get(status) or ("api_error" if status >= 500 else "invalid_request")


def error_body(status: int, *, code: str | None = None, message: str | None = None, param: str | None = None,
               request_id: str | None = None, **extra) -> dict:
    code = code or _CODE.get(status) or ("internal_error" if status >= 500 else "bad_request")
    err = {"type": error_type(status), "code": code, "message": message or f"HTTP {status}", "param": param,
           "request_id": request_id, "docs_url": f"{settings.public_base_url.rstrip('/')}/api#error-{code}"}
    err.update({k: v for k, v in extra.items() if k not in _STD})
    return {"error": err}


def from_response(status: int, raw: bytes, content_type: str, request_id: str | None) -> dict:
    """Any error body (FastAPI detail, our {code, message}, a validation list, plain text) -> the error shape."""
    body = None
    if "json" in content_type:
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):  # already in shape
        return error_body(status, request_id=request_id, **{k: v for k, v in body["error"].items() if k != "request_id"})
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if body is None and raw:
        detail = raw.decode("utf-8", "replace").strip()
    if isinstance(detail, dict):
        extra = {k: v for k, v in detail.items() if k not in ("code", "message", "param", "detail")}
        return error_body(status, code=detail.get("code"), message=detail.get("message") or detail.get("detail"),
                          param=detail.get("param"), request_id=request_id, **extra)
    if isinstance(detail, list):  # FastAPI validation errors
        parts, param = [], None
        for e in detail:
            loc = [str(x) for x in (e.get("loc") or []) if x not in ("query", "path", "body", "header")]
            param = param or (loc[-1] if loc else None)
            parts.append(f"{'.'.join(loc)}: {e.get('msg')}" if loc else str(e.get("msg")))
        return error_body(status, message="; ".join(parts) or None, param=param, request_id=request_id)
    return error_body(status, message=str(detail) if detail not in (None, "") else None, request_id=request_id)


def resolve(state: dict) -> str | None:
    """Header, else the key's pin, else latest. None outside a Data API request."""
    if not state.get("data_api"):
        return None
    pin = getattr(state.get("principal"), "api_version", None)
    # shortcut: a pin that is no longer served falls back to latest; give it its own 400 when a version is retired
    return state.get("api_version_asked") or (pin if pin in VERSIONS else None) or LATEST


def public_shape(v):
    """Data API response body rules applied in one place (docs/API_CONTRACT.md sections 5-6), on a copy:
        * a `gpu` field holding a canonical name becomes the public id, with `gpu_name` beside it;
        * a `provider` field (already the id) gets `provider_name` (display name) beside it;
        * `index_id` is named `index` (like gpu / provider: the field is the id);
        * computed floats lose binary noise (12 significant digits: 2.4215999999999998 -> 2.4216);
        * UTC timestamps end in Z (2026-11-03T14:00:00Z, not +00:00)."""
    import gpu_ids
    import provider_meta

    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k == "gpu" and isinstance(x, str) and x in gpu_ids.GPU_IDS:
                out["gpu"] = gpu_ids.GPU_IDS[x]
                out["gpu_name"] = v.get("gpu_name", x)
            elif k == "index_id" and isinstance(x, str):
                out["index"] = x
            elif k == "provider" and isinstance(x, str) and x:
                out["provider"] = x
                out["provider_name"] = v.get("provider_name") or provider_meta.meta(x).display_name
            elif k not in out:  # gpu_name / provider_name already placed beside their id
                out[k] = public_shape(x)
        return out
    if isinstance(v, (list, tuple)):
        return [public_shape(x) for x in v]
    if isinstance(v, float) and v == v and v not in (float("inf"), float("-inf")):
        return float(f"{v:.12g}")
    if isinstance(v, datetime):
        v = v.isoformat()
    if isinstance(v, str) and v.endswith("+00:00") and _ISO.match(v):
        return v[:-6] + "Z"
    return v


def downgrade(data, version: str):
    """The latest body turned back into `version`'s shape (STEPS newer than it, newest first)."""
    st = observability._state.get()
    path = st.get("path", "") if isinstance(st, dict) else ""
    for introduced, step in reversed(STEPS):
        if introduced > version:  # ISO dates compare as strings
            data = step(data, path)
    return data


def set_etag(data, version: str) -> None:
    """Remember the body's ETag for the response headers (ContractMiddleware): a hash of the data and the version."""
    import hashlib

    st = observability._state.get()
    if isinstance(st, dict):
        raw = json.dumps([version, data], sort_keys=True, default=str, separators=(",", ":")).encode()
        st["etag"] = '"' + hashlib.sha256(raw).hexdigest()[:32] + '"'


def current_version() -> str | None:
    """The API version of the request in progress (None outside a Data API request)."""
    st = observability._state.get()
    return resolve(st) if isinstance(st, dict) else None


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


class ContractMiddleware:
    """Pure ASGI, inside RequestContextMiddleware (needs the request id) and outside everything else."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = headers_in = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or ()}
        state = scope.setdefault("state", {})
        rid = state.get("request_id")
        if on_data_host(headers.get("host", "")):
            # The Data API host: Data API routes only, and only with an API key (never the site login or anonymous).
            if path == "/health":
                return await self.app(scope, receive, send)
            if not is_data_path(path):
                return await _send_json(send, 404, error_body(
                    404, request_id=rid, message=f"no Data API route at {path}; see /v1/gpus"), {})
            if not headers.get("authorization", "").lower().startswith("bearer "):
                return await _send_json(send, 401, error_body(
                    401, request_id=rid, message="API key required: send Authorization: Bearer opg_live_..."),
                    {"WWW-Authenticate": "Bearer"})
        elif not is_data_path(path):
            return await self.app(scope, receive, send)
        asked = headers.get(HEADER.lower())
        asked = asked.strip() if asked is not None else None
        if asked is not None and asked not in VERSIONS:
            reason = "is not a date (YYYY-MM-DD)" if not _DATE.match(asked) else "is not a supported version"
            return await _send_json(send, 400, error_body(
                400, code="unknown_api_version", param=HEADER, request_id=rid,
                message=f"{HEADER} {asked!r} {reason}; supported: {', '.join(VERSIONS)}"), {})
        state["data_api"], state["api_version_asked"], state["path"] = True, asked, path
        head = scope.get("method") == "HEAD"
        held: dict = {}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                version = resolve(state)  # the key (if any) is known by now
                headers = [h for h in message.get("headers", []) if h[0].lower() != HEADER.lower().encode()]
                headers.append((HEADER.lower().encode(), version.encode()))
                if message["status"] == 200 and scope.get("method") in ("GET", "HEAD") and state.get("etag"):
                    # Caching: per client only (private): a shared cache would skip per-key limits and usage logs.
                    headers += [(b"etag", state["etag"].encode()), (b"cache-control", CACHE_CONTROL.encode()),
                                (b"vary", b"Authorization, OpenGrid-Version")]
                    if state["etag"] in (headers_in.get("if-none-match") or "").replace("W/", "").split(", "):
                        held["sent"] = held["not_modified"] = True
                        keep = [h for h in headers if h[0].lower() not in (b"content-type", b"content-length")]
                        return await send({**message, "status": 304, "headers": keep})
                message = {**message, "headers": headers}
                if message["status"] >= 400 and not head:
                    held["start"], held["body"] = message, b""
                    return
                held["sent"] = True
                return await send(message)
            if message["type"] == "http.response.body" and held.get("not_modified"):
                if not message.get("more_body"):
                    await send({"type": "http.response.body", "body": b""})
                return
            if message["type"] == "http.response.body" and "start" in held:
                held["body"] += message.get("body", b"")
                if message.get("more_body"):
                    return
                start = held.pop("start")
                hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start["headers"]}
                out = from_response(start["status"], held["body"], hdrs.get("content-type", ""), rid)
                keep = {k: v for k, v in hdrs.items() if k not in ("content-type", "content-length")}
                held["sent"] = True
                return await _send_json(send, start["status"], out, keep)
            return await send(message)

        try:
            await self.app(scope, receive, wrapped)
        except Exception:
            if held.get("sent"):
                raise
            log.exception("unhandled error on %s", scope.get("path"))
            await _send_json(send, 500, error_body(500, message="Internal error; quote the request_id when reporting it",
                                                   request_id=rid), {HEADER: resolve(state)})


async def _send_json(send, status: int, body: dict, headers: dict) -> None:
    raw = json.dumps(body).encode()
    hdrs = [(k.encode("latin-1"), str(v).encode("latin-1")) for k, v in headers.items()]
    hdrs += [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": raw})
