"""Structured logging, request tracing, secret redaction and in-process metrics.

    observability.install(app)     called once by main.py (outermost middleware)

Context
    Every log line written while handling a request (or inside `bind(...)`) carries
    request_id, route_request_id, deployment_id, provider, account_id. The request-id
    middleware accepts a caller's X-Request-ID (if it is a sane token) or makes one, and
    returns it on the response. Code that learns an id mid-request adds it:

        observability.set(route_request_id=rr_id)            # rest of this request/thread
        with observability.bind(deployment_id=d, provider=p): # a block (jobs, loops)
            log.info("terminating")

    account_id is read lazily from the principal accounts.auth leaves on request.state,
    so no handler has to set it. Sync handlers run in a worker thread with a copy of the
    context, which is why set() inside a handler is visible to its own log lines.

Log format
    settings.log_json (None = JSON when deployed, text in dev). JSON lines carry ts, level,
    logger, msg, the context ids, and every `extra=` field (the execution core logs each
    provider call on logger "opengrid.provider" with extra provider/op/status/latency_ms).

Redaction (a filter on every handler, plus a last pass over each formatted line)
    Authorization headers (Bearer/Basic), opg_ API keys, Fernet keys and tokens,
    key=value secrets (api_key, token, secret, password, client_secret, ...), known
    provider key shapes (DigitalOcean dop_v1_, RunPod rpa_, Lambda secret_, Salad,
    AWS AKIA, Stripe-style sk_live_, whsec_), PEM private keys, and the literal values
    of every secret setting in config.py. Dict extras with a secret-looking key are
    replaced whole. Over-redaction is accepted; a leaked key is not.

Metrics (in-process; the app is a single process by design)
    counters and latency histograms (p50/p95 over the last 2000 samples per series):
    api_requests{route,method,status}, api_latency_ms{route}, provider_calls{provider,op,
    outcome}, provider_latency_ms{provider,op}, db_latency_ms (a SELECT 1 every 60 s),
    plus anything code records with metrics.inc()/observe(). DB-derived counts (route
    previews, launches, approvals, ...) are computed on read by api/execution.py.

Trace
    trace(id) assembles route request -> decision -> quotes -> approval -> provision
    attempts -> deployment events -> termination -> reconciliation -> billing -> feedback
    from the database, in time order. Tables other agents own are read defensively.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from decimal import Decimal

from config import settings

log = logging.getLogger(__name__)

CONTEXT_FIELDS = ("request_id", "route_request_id", "deployment_id", "provider", "account_id")
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("opengrid_log_ctx", default={})
_state: contextvars.ContextVar[dict | None] = contextvars.ContextVar("opengrid_req_state", default=None)
_RID_OK = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
INSTALLED = {"middleware": False, "logging": False, "json": False}


# ---------------------------------------------------------------- context

def new_request_id() -> str:
    return "req_" + secrets.token_hex(12)


def current() -> dict:
    """The context ids in force now (account_id resolved from the request's principal)."""
    out = {k: v for k, v in _ctx.get().items() if v is not None}
    if "account_id" not in out:
        st = _state.get()
        who = st.get("principal") if isinstance(st, dict) else None
        if who is not None and getattr(who, "account_id", None) is not None:
            out["account_id"] = who.account_id
    return out


def set(**kw) -> None:  # noqa: A001 - observability.set(...) reads well at call sites
    """Add ids to the current context (this request / thread) until it ends."""
    _ctx.set({**_ctx.get(), **{k: v for k, v in kw.items() if k in CONTEXT_FIELDS}})


@contextlib.contextmanager
def bind(**kw):
    """Ids for a block: `with bind(deployment_id=d): ...`."""
    token = _ctx.set({**_ctx.get(), **{k: v for k, v in kw.items() if k in CONTEXT_FIELDS}})
    try:
        yield
    finally:
        _ctx.reset(token)


_base_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    """Snapshot the context onto a private attribute: setting `provider` etc. here would make
    logging refuse `extra={"provider": ...}` (makeRecord forbids overwriting). The filter and
    formatters read it; an id passed explicitly in `extra` wins."""
    record = _base_factory(*args, **kwargs)
    record._og_ctx = current()
    return record


def context_of(record: logging.LogRecord) -> dict:
    ctx = dict(getattr(record, "_og_ctx", None) or {})
    for k in CONTEXT_FIELDS:
        v = record.__dict__.get(k)
        if v is not None:
            ctx[k] = v
    return ctx


# ---------------------------------------------------------------- redaction

R = "[REDACTED]"
_SECRET_NAME = (r"api[_-]?key|apikey|x-api-key|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
                r"token|secret|client[_-]?secret|secret[_-]?key|password|passwd|pwd|pepper|private[_-]?key|"
                r"encryption[_-]?key|credentials?|cookie|set-cookie|session[_-]?id|signature")
SECRET_KEY = re.compile(rf"(?i)^({_SECRET_NAME})$|(_key|_secret|_token|_password|_pepper)$")
PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM private keys, whole block.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), R),
    # Authorization headers in any rendering: "Authorization: Bearer x", {'authorization': 'Basic x'}.
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)(?!(?:(?:bearer|basic|token)\s+)?\[REDACTED\])(?:(bearer|basic|token)\s+)?[^\s'\",}]+"),
     lambda m: m.group(1) + (m.group(2) + " " if m.group(2) else "") + R),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), lambda m: m.group(1) + " " + R),
    # OpenGrid API keys.
    (re.compile(r"\bopg_[A-Za-z0-9_\-]{6,}"), "opg_" + R),
    # Fernet tokens (ciphertext) and Fernet keys (32 bytes urlsafe-base64 = 43 chars + '=').
    (re.compile(r"\bgAAAAA[A-Za-z0-9_\-]{20,}={0,2}"), R),
    (re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{43}=(?![A-Za-z0-9_\-=])"), R),
    # Provider and common key shapes.
    (re.compile(r"\bdo[por]_v1_[a-f0-9]{20,}"), R),                     # DigitalOcean
    (re.compile(r"\brpa_[A-Za-z0-9]{16,}"), R),                         # RunPod
    (re.compile(r"\bsecret_[A-Za-z0-9_.\-]{16,}"), R),                  # Lambda
    (re.compile(r"\bsalad_cloud_[A-Za-z0-9_\-]{8,}"), R),              # Salad
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), R),                           # AWS access key id
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"), R),  # Stripe-style
    (re.compile(r"\bwhsec_[A-Za-z0-9_\-]{10,}"), R),                    # webhook secrets
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), R),                   # GitHub
    (re.compile(r"\b[a-f0-9]{64}\b"), R),                                # Vast-style 64-hex keys (and digests)
    # key=value / "key": "value" with a secret-looking key name.
    (re.compile(rf"(?i)\b({_SECRET_NAME})(['\"]?\s*[:=]\s*['\"]?)(?!\[REDACTED\])([^\s'\"&,;}}\]]+)"),
     lambda m: m.group(1) + m.group(2) + R),
]

_known: dict = {"at": 0.0, "values": ()}


def _known_secrets() -> tuple[str, ...]:
    """Literal values of secret settings (re-read every 30 s; tests change settings)."""
    now = time.monotonic()
    if now - _known["at"] > 30:
        vals = []
        for name, value in vars(settings).items():
            if isinstance(value, str) and len(value) >= 6 and SECRET_KEY.search(name):
                vals.append(value)
            if name == "app_password" and isinstance(value, str) and len(value) >= 4:
                vals.append(value)
        _known.update(at=now, values=tuple(sorted(builtins_set(vals), key=len, reverse=True)))
    return _known["values"]


def builtins_set(it):
    import builtins

    return builtins.set(it)


def refresh_known_secrets() -> None:
    _known["at"] = 0.0


def redact(text: str) -> str:
    if not text:
        return text
    for v in _known_secrets():
        if v in text:
            text = text.replace(v, R)
    for pat, rep in PATTERNS:
        text = pat.sub(rep, text)
    return text


def redact_value(v, depth: int = 0):
    """Structured redaction: secret-named keys replaced whole, strings pattern-redacted."""
    if depth > 6:
        return v
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, dict):
        return {k: (R if isinstance(k, str) and SECRET_KEY.search(k) and val not in (None, "", [], {})
                    else redact_value(val, depth + 1)) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [redact_value(x, depth + 1) for x in v]
    return v


_STD = builtins_set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}


class RedactFilter(logging.Filter):
    """On every handler: render the message once, redact it and all extras and the traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_og_redacted", False):
            return True
        ctx = context_of(record)
        for k in CONTEXT_FIELDS:  # materialize for %(request_id)s-style formats and later handlers
            if k not in record.__dict__:
                setattr(record, k, ctx.get(k))
        try:
            msg = record.getMessage()
        except Exception:  # a bad format string must not lose the line
            msg = f"{record.msg} {record.args}"
        record.msg, record.args = redact(msg), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for k, v in list(record.__dict__.items()):
            if k in _STD or k in CONTEXT_FIELDS or k.startswith("_og"):
                continue
            if SECRET_KEY.search(k) and v not in (None, ""):
                setattr(record, k, R)
            else:
                setattr(record, k, redact_value(v))
        record._og_redacted = True
        return True


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (set, frozenset, tuple)):
        return list(v)
    return str(v)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        out.update(context_of(record))
        for k, v in record.__dict__.items():
            if k not in _STD and k not in CONTEXT_FIELDS and not k.startswith("_og") and k not in out:
                out[k] = v
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            out["exc"] = record.exc_text
        if record.stack_info:
            out["stack"] = record.stack_info
        return redact(json.dumps(out, default=_jsonable, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    """Dev format: the usual line plus the ids that are set, then a redaction pass."""

    def format(self, record: logging.LogRecord) -> str:
        # Redact the message and traceback, not the whole line: a logger name such as
        # "accounts.credentials:" followed by text would otherwise read as a key=value secret.
        try:
            msg = record.getMessage()
        except Exception:
            msg = f"{record.msg} {record.args}"
        record.msg, record.args = redact(msg), None
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        base = super().format(record)
        ctx = context_of(record)
        ids = " ".join(f"{k}={ctx[k]}" for k in CONTEXT_FIELDS if ctx.get(k) is not None)
        return f"{base} [{ids}]" if ids else base


def json_enabled() -> bool:
    if settings.log_json is not None:
        return bool(settings.log_json)
    return bool(os.environ.get("RAILWAY_ENVIRONMENT"))


_FILTER = RedactFilter()


def _all_handlers():
    seen = []
    loggers = [logging.getLogger()] + [lg for lg in logging.Logger.manager.loggerDict.values()
                                       if isinstance(lg, logging.Logger)]
    for lg in loggers:
        for h in lg.handlers:
            if h not in seen:
                seen.append(h)
    return seen


def configure_logging(json_lines: bool | None = None) -> None:
    """Context on every record, redaction on every handler, JSON or text on the root handlers."""
    json_lines = json_enabled() if json_lines is None else json_lines
    if logging.getLogRecordFactory() is not _record_factory:
        logging.setLogRecordFactory(_record_factory)
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    fmt = JsonFormatter() if json_lines else TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in root.handlers:
        if not isinstance(h, _ProviderCallHandler):
            h.setFormatter(fmt)
    if json_lines:  # uvicorn's own handlers too, so access and error lines are JSON as well
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            for h in logging.getLogger(name).handlers:
                h.setFormatter(fmt)
    for h in _all_handlers():
        if _FILTER not in h.filters:
            h.addFilter(_FILTER)
    INSTALLED.update(logging=True, json=bool(json_lines))


# ---------------------------------------------------------------- metrics

class _Hist:
    __slots__ = ("samples", "count", "total", "max")

    def __init__(self):
        self.samples, self.count, self.total, self.max = deque(maxlen=2000), 0, 0.0, 0.0

    def add(self, v: float):
        self.samples.append(v)
        self.count += 1
        self.total += v
        self.max = max(self.max, v)


def percentile(values, q: float) -> float | None:
    """Nearest-rank on sorted values (q in 0..1)."""
    vals = sorted(values)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, int(round(q * (len(vals) - 1)))))
    return float(vals[k])


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters: dict[tuple, int] = {}
        self.hists: dict[tuple, _Hist] = {}
        self.started_at = datetime.now(timezone.utc)

    @staticmethod
    def _key(name, labels):
        return (name, tuple(sorted((k, str(v)) for k, v in labels.items())))

    def inc(self, name: str, n: int = 1, **labels) -> None:
        k = self._key(name, labels)
        with self._lock:
            self.counters[k] = self.counters.get(k, 0) + n

    def observe(self, name: str, value: float, **labels) -> None:
        k = self._key(name, labels)
        with self._lock:
            h = self.hists.get(k)
            if h is None:
                h = self.hists[k] = _Hist()
            h.add(float(value))

    def counter(self, name: str, **labels) -> int:
        """Sum of a counter over series matching the given labels."""
        want = {(k, str(v)) for k, v in labels.items()}
        with self._lock:
            return sum(v for (n, ls), v in self.counters.items() if n == name and want <= builtins_set(ls))

    def snapshot(self) -> dict:
        with self._lock:
            counters = [{"name": n, "labels": dict(ls), "value": v} for (n, ls), v in self.counters.items()]
            hists = [{"name": n, "labels": dict(ls), "count": h.count, "window": len(h.samples),
                      "p50": percentile(h.samples, 0.5), "p95": percentile(h.samples, 0.95),
                      "mean": round(h.total / h.count, 3) if h.count else None, "max": h.max}
                     for (n, ls), h in self.hists.items()]
        counters.sort(key=lambda c: (c["name"], -c["value"]))
        hists.sort(key=lambda c: (c["name"], -c["count"]))
        return {"since": self.started_at.isoformat(), "counters": counters, "histograms": hists,
                "note": "in-process since the last restart; percentiles over the last 2000 samples per series"}

    def reset(self) -> None:
        with self._lock:
            self.counters.clear()
            self.hists.clear()
            self.started_at = datetime.now(timezone.utc)


metrics = Metrics()


def _outcome(status) -> str:
    try:
        code = int(status)
    except (TypeError, ValueError):
        return str(status) if status else "error"
    return "ok" if code < 400 else "client_error" if code < 500 else "server_error"


def record_provider_call(provider: str, op: str, outcome: str, latency_ms: float | None = None, *,
                         kind: str = "http") -> None:
    """kind 'http': one HTTP request to a provider API (adapters); kind 'op': one execution verb
    (provision / status / terminate ...) as the core sees it, which may span several HTTP calls."""
    name = "provider_calls" if kind == "http" else "provider_ops"
    metrics.inc(name, provider=provider or "?", op=op or "?", outcome=outcome or "?")
    if latency_ms is not None:
        metrics.observe(name.replace("calls", "latency_ms").replace("ops", "op_latency_ms"), latency_ms,
                        provider=provider or "?", op=op or "?")


_ID_SEG = re.compile(r"/(?:\d+|[0-9a-fA-F-]{8,}|[A-Za-z0-9_-]*\d[A-Za-z0-9_-]{5,})(?=/|$)")
PROVIDER_LOGGERS = ("opengrid.provider", "routing.adapters")


def op_name(method, path) -> str:
    """'GET /instances/1234' -> 'GET /instances/:id' (bounded label cardinality)."""
    p = _ID_SEG.sub("/:id", str(path or "").split("?", 1)[0])[:80]
    return f"{method} {p}".strip() if method else (p or "?")


class _ProviderCallHandler(logging.Handler):
    """Counts provider calls from the structured provider-call log lines (opengrid.provider, routing.adapters)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == "routing.adapters" and record.msg != "provider_call":
                return
            g = record.__dict__.get
            op = g("op") or g("verb") or g("operation") or op_name(g("method"), g("path"))
            outcome = g("outcome") or _outcome(g("status") if g("status") is not None else g("http_status"))
            if g("error_kind") and outcome == "ok":
                outcome = str(g("error_kind"))
            lat = g("latency_ms")
            record_provider_call(g("provider") or "?", str(op), str(outcome),
                                 float(lat) if isinstance(lat, (int, float)) else None,
                                 kind="http" if record.name == "routing.adapters" else "op")
        except Exception:  # metrics must never break logging
            pass


_provider_handler = _ProviderCallHandler(level=logging.DEBUG)


def _attach_provider_handler():
    for name in PROVIDER_LOGGERS:
        lg = logging.getLogger(name)
        if _provider_handler not in lg.handlers:
            lg.addHandler(_provider_handler)
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)


_attach_provider_handler()


# ---------------------------------------------------------------- middleware

access_log = logging.getLogger("opengrid.access")
_QUIET = ("/static/", "/health")


def route_template(scope) -> str:
    """The matched route's path template ('/v1/deployments/{deployment_id}'), never the raw path."""
    route = scope.get("route")
    if getattr(route, "path", None):
        return route.path
    path = scope.get("path", "")
    if path.startswith("/static/"):
        return "/static/*"
    app = scope.get("app")
    try:  # plain Starlette routes (openapi, docs) do not set scope["route"]
        from starlette.routing import Match

        for r in getattr(getattr(app, "router", None), "routes", ()):
            if getattr(r, "path", None) and r.matches({**scope, "type": "http"})[0] == Match.FULL:
                return r.path
    except Exception:
        pass
    return "<unmatched>"


class RequestContextMiddleware:
    """Pure ASGI: request id in/out, context for the request's log lines, request metrics."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = None
        for k, v in scope.get("headers") or ():
            if k == b"x-request-id":
                rid = v.decode("latin-1").strip()
                break
        if not rid or not _RID_OK.match(rid):
            rid = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = rid
        t_ctx = _ctx.set({"request_id": rid})
        t_state = _state.set(state)
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = [h for h in message.get("headers", []) if h[0].lower() != b"x-request-id"]
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            ms = (time.perf_counter() - started) * 1000
            template = route_template(scope)
            method = scope.get("method", "GET")
            metrics.inc("api_requests", route=template, method=method, status=status["code"])
            metrics.observe("api_latency_ms", ms, route=template)
            path = scope.get("path", "")
            if not path.startswith(_QUIET):
                access_log.info("%s %s %s %.0fms", method, template, status["code"], ms,
                                extra={"http_method": method, "route": template, "status": status["code"],
                                       "latency_ms": round(ms, 1)})
            _state.reset(t_state)
            _ctx.reset(t_ctx)


# ---------------------------------------------------------------- DB latency job

def db_ping() -> dict:
    import normalize
    from sqlalchemy import text

    started = time.perf_counter()
    with normalize.SessionLocal() as s:
        s.execute(text("SELECT 1"))
    ms = (time.perf_counter() - started) * 1000
    metrics.observe("db_latency_ms", ms)
    return {"db_latency_ms": round(ms, 2)}


def _register_jobs():
    from jobs import job

    job("db_latency", every_seconds=60, initial_delay_seconds=15)(db_ping)


_register_jobs()


def install(app) -> None:
    """Called by main.py: request-id / context / metrics middleware (outermost) + logging setup."""
    app.add_middleware(RequestContextMiddleware)
    INSTALLED["middleware"] = True
    configure_logging()
    _attach_provider_handler()


# ---------------------------------------------------------------- schema helpers (other agents' tables)

def has_table(s, name: str) -> bool:
    from sqlalchemy import text

    return s.execute(text("SELECT to_regclass(:n)"), {"n": f"public.{name}"}).scalar() is not None


def columns(s, name: str) -> frozenset[str]:
    from sqlalchemy import text

    return frozenset(s.execute(text("SELECT column_name FROM information_schema.columns "
                                    "WHERE table_schema = 'public' AND table_name = :t"), {"t": name}).scalars())


def rows(s, table: str, where: str = "true", params: dict | None = None, order: str | None = None,
         limit: int = 1000) -> list[dict]:
    """Whole rows as JSON dicts (row_to_json), so callers survive added/renamed columns."""
    from sqlalchemy import text

    sql = f"SELECT row_to_json(t) FROM {table} t WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    sql += f" LIMIT {int(limit)}"
    return [r for (r,) in s.execute(text(sql), params or {})]


# ---------------------------------------------------------------- trace

def _at(row: dict, *names):
    for n in names:
        if row.get(n):
            return row[n]
    return None


def _compact_candidates(cands) -> list[dict]:
    keep = ("rank", "provider", "listing_id", "gpu", "price_per_gpu_hour", "score", "provisionable", "region")
    return [{k: c.get(k) for k in keep if k in c} for c in (cands or [])[:25]]


def trace(ident: str) -> dict | None:
    """The full chain for a route_request_id or a deployment_id, oldest step first. None if unknown."""
    import normalize

    unavailable: list[str] = []
    steps: list[dict] = []

    def add(at, step, **data):
        steps.append({"at": at, "step": step, **data})

    with normalize.SessionLocal() as s:
        if not has_table(s, "route_requests") or not has_table(s, "deployments"):
            return None
        rr_id, focus = None, None
        rr = rows(s, "route_requests", "id = :i", {"i": ident})
        if rr:
            rr_id = ident
        else:
            dep = rows(s, "deployments", "deployment_id = :i", {"i": ident})
            if not dep:
                return None
            focus, rr_id = ident, dep[0].get("route_request_id")
            rr = rows(s, "route_requests", "id = :i", {"i": rr_id}) if rr_id else []
        if rr:
            r = rr[0]
            add(r.get("created_at"), "route_request", route_request_id=r["id"], preview=r.get("preview"),
                mode=r.get("mode"), gpu=r.get("gpu"), status=r.get("status"), account_id=r.get("account_id"),
                key_id=r.get("key_id"), principal_kind=r.get("principal_kind"), request=r.get("request"),
                result=r.get("result"))
            for d in rows(s, "routing_decisions", "route_request_id = :i", {"i": rr_id}, "created_at, id"):
                add(d.get("created_at"), "decision", mode=d.get("mode"), weights=d.get("weights"),
                    selected_provider=d.get("selected_provider"), selected_listing_id=d.get("selected_listing_id"),
                    selected_observed_price_per_gpu_hour=d.get("selected_observed_price_per_gpu_hour"),
                    candidates_total=len(d.get("candidates") or []), candidates=_compact_candidates(d.get("candidates")),
                    exclusions_total=len(d.get("exclusions") or []), market_snapshot=d.get("market_snapshot"),
                    methodology_version=d.get("methodology_version"))
        if rr_id and has_table(s, "quotes") and "route_request_id" in columns(s, "quotes"):
            for q in rows(s, "quotes", "route_request_id = :i", {"i": rr_id}, "created_at"):
                add(q.get("created_at"), "quote", **{k: q.get(k) for k in (
                    "id", "provider", "listing_id", "gpu", "gpu_count", "region", "observed_price_per_gpu_hour",
                    "quote_price_per_gpu_hour", "est_hourly_cost", "est_total_cost", "price_source", "status",
                    "expires_at", "consumed_by_deployment_id", "fees", "taxes", "billing_unit") if k in q})
        elif rr_id:
            unavailable.append("quotes: table not present (execution core migration 0010 not applied)")
        deps = rows(s, "deployments", "route_request_id = :i", {"i": rr_id}, "created_at") if rr_id else \
            rows(s, "deployments", "deployment_id = :i", {"i": focus})
        dep_ids = [d["deployment_id"] for d in deps]
        for d in deps:
            did = d["deployment_id"]
            add(d.get("created_at"), "deployment_created", deployment_id=did, purpose=d.get("purpose"),
                provider=d.get("provider"), gpu=d.get("gpu"), gpu_count=d.get("gpu_count"), quote_id=d.get("quote_id"),
                credential_source=d.get("credential_source"), credential_ref=d.get("credential_ref"),
                client_name=d.get("client_name"), max_runtime_minutes=d.get("max_runtime_minutes"))
            if d.get("approved_at"):
                add(d["approved_at"], "approval", deployment_id=did, approved_by=d.get("approved_by"),
                    quote_id=d.get("quote_id"))
            for a in rows(s, "provision_attempts", "deployment_id = :i", {"i": did}, "started_at, id"):
                add(a.get("started_at"), "provision_attempt", deployment_id=did,
                    **{k: v for k, v in a.items() if k not in ("deployment_id", "route_request_id")})
            for e in rows(s, "deployment_events", "deployment_id = :i", {"i": did}, "at, id"):
                st = e.get("to_status")
                kind = ("termination" if st in ("terminating", "terminated", "termination_failed")
                        else "state_change")
                add(e.get("at"), kind, deployment_id=did, from_status=e.get("from_status"), to_status=st,
                    **{k: e.get(k) for k in ("actor", "reason", "evidence", "detail") if k in e})
            if d.get("reconciled_at") or d.get("reconciliation"):
                add(d.get("reconciled_at"), "cost_reconciliation", deployment_id=did,
                    reconciliation=d.get("reconciliation"), provider_reported_cost=d.get("provider_reported_cost"))
            if has_table(s, "orphan_resources") and "deployment_id" in columns(s, "orphan_resources"):
                for o in rows(s, "orphan_resources", "deployment_id = :i", {"i": did}):
                    add(_at(o, "detected_at", "created_at", "first_seen_at"), "orphan", deployment_id=did, **o)
            if has_table(s, "usage_records"):
                for u in rows(s, "usage_records", "deployment_id = :i", {"i": did}, "period_start"):
                    add(u.get("created_at"), "billing_usage", deployment_id=did, usage_record_id=u.get("id"),
                        period_start=u.get("period_start"), period_end=u.get("period_end"),
                        gpu_hours=u.get("gpu_hours"), provider_cost_usd=u.get("provider_cost_usd"), kind=u.get("kind"))
            if has_table(s, "usage_slices"):
                for u in rows(s, "usage_slices", "deployment_id = :i", {"i": did}, "period_start", 500):
                    add(u.get("created_at"), "usage_slice", deployment_id=did, **{k: u.get(k) for k in (
                        "period_start", "period_end", "running_seconds", "stopped_seconds", "unbilled_seconds",
                        "stopped_billing", "price_per_gpu_hour", "price_basis", "cost_usd", "kind",
                        "usage_record_id", "end_estimated", "final")})
            if has_table(s, "deployment_watch"):
                for w in rows(s, "deployment_watch", "deployment_id = :i", {"i": did}):
                    for al in w.get("alerts") or []:
                        add(al.get("at"), "alert", deployment_id=did, kind=al.get("kind"), message=al.get("message"))
            for x in rows(s, "execution_records", "deployment_id = :i", {"i": did}):
                add(x.get("updated_at"), "transaction_record", deployment_id=did, **{
                    k: x.get(k) for k in ("provision_ok", "attempts", "provision_latency_ms", "uptime_seconds",
                                          "interruptions", "termination_reason", "cost_basis", "provider_cost_usd",
                                          "quoted_price_per_gpu_hour", "actual_price_per_gpu_hour",
                                          "workload_completed") if k in x})
            if has_table(s, "deployment_feedback"):
                for f in rows(s, "deployment_feedback", "deployment_id = :i", {"i": did}):
                    add(_at(f, "updated_at", "created_at"), "feedback", deployment_id=did, **{
                        k: f.get(k) for k in ("would_have_chosen_provider", "price_better", "setup_easier",
                                              "would_route_next", "what_broke", "notes")})
        if dep_ids and has_table(s, "reconciliation_runs"):
            cols = columns(s, "reconciliation_runs")
            tcol = next((c for c in ("started_at", "created_at", "at", "finished_at") if c in cols), None)
            pat = "|".join(re.escape(d) for d in dep_ids)
            for run in rows(s, "reconciliation_runs", "row_to_json(t)::text ~ :p", {"p": pat},
                            f"{tcol} DESC" if tcol else None, limit=50):
                add(run.get(tcol) if tcol else None, "reconciliation_run", **run)
        elif dep_ids:
            unavailable.append("reconciliation_runs: table not present (adapters/reconciliation migration 0011 "
                               "not applied)")
        if has_table(s, "idempotency_keys") and "resource_id" in columns(s, "idempotency_keys"):
            ids = [i for i in [rr_id, *dep_ids] if i]
            for k in rows(s, "idempotency_keys", "resource_id = ANY(:ids)", {"ids": ids}, "created_at"):
                add(k.get("created_at"), "idempotency_key", scope=k.get("scope"), status=k.get("status"),
                    resource_id=k.get("resource_id"))

    def sort_key(st):
        a = st.get("at")
        if isinstance(a, str):
            try:
                a = datetime.fromisoformat(a)
            except ValueError:
                a = None
        if isinstance(a, datetime) and a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        return (a is None, a or datetime.min.replace(tzinfo=timezone.utc))

    steps.sort(key=sort_key)
    return {"id": ident, "focus_deployment_id": focus, "route_request_id": rr_id, "deployment_ids": dep_ids,
            "steps": redact_value(steps), "unavailable": unavailable,
            "note": "assembled from the database; times are as recorded by each component"}
