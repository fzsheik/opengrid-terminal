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

import observability
from config import settings

log = logging.getLogger(__name__)

# Dated versions, oldest first. A breaking change appends a date here and adds its downgrade step (api/versions/).
VERSIONS = ("2026-11-03",)
LATEST = VERSIONS[-1]
HEADER = "OpenGrid-Version"

# First path segment after /v1/ of every Data API area. Shrinks to the contract's route list as routes move.
DATA_AREAS = frozenset({
    "gpus", "families", "providers", "regions", "hardware", "markets", "spreads", "compare", "heatmaps", "history",
    "reserved", "indices", "fixings", "capacity", "cost", "fx", "uptime", "probes", "freshness", "trust",
    "events", "shifts", "news", "timeline", "overview", "tape", "opportunities", "live", "context", "methodology",
})

_TYPE = {401: "authentication", 403: "permission", 429: "rate_limit"}
_CODE = {400: "bad_request", 401: "authentication_required", 403: "permission_denied", 404: "not_found",
         405: "method_not_allowed", 409: "conflict", 413: "payload_too_large", 415: "unsupported_media_type",
         422: "invalid_parameter", 429: "rate_limited", 500: "internal_error", 502: "bad_gateway",
         503: "unavailable", 504: "timeout"}
_STD = ("type", "code", "message", "param", "request_id", "docs_url")


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


def current_version() -> str | None:
    """The API version of the request in progress (None outside a Data API request)."""
    st = observability._state.get()
    return resolve(st) if isinstance(st, dict) else None


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ContractMiddleware:
    """Pure ASGI, inside RequestContextMiddleware (needs the request id) and outside everything else."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_data_path(scope.get("path", "")):
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        rid = state.get("request_id")
        asked = next((v.decode("latin-1").strip() for k, v in scope.get("headers") or () if k == HEADER.lower().encode()), None)
        if asked is not None and asked not in VERSIONS:
            reason = "is not a date (YYYY-MM-DD)" if not _DATE.match(asked) else "is not a supported version"
            return await _send_json(send, 400, error_body(
                400, code="unknown_api_version", param=HEADER, request_id=rid,
                message=f"{HEADER} {asked!r} {reason}; supported: {', '.join(VERSIONS)}"), {})
        state["data_api"], state["api_version_asked"] = True, asked
        head = scope.get("method") == "HEAD"
        held: dict = {}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                version = resolve(state)  # the key (if any) is known by now
                headers = [h for h in message.get("headers", []) if h[0].lower() != HEADER.lower().encode()]
                headers.append((HEADER.lower().encode(), version.encode()))
                message = {**message, "headers": headers}
                if message["status"] >= 400 and not head:
                    held["start"], held["body"] = message, b""
                    return
                held["sent"] = True
                return await send(message)
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
