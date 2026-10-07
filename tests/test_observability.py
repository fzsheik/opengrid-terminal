"""Observability: request ids (in/out, propagation into every log record of the request, threads),
JSON log format, secret redaction (every pattern, msg/args/extras/tracebacks), metrics counting.

No database needed. Run:  .venv/Scripts/python tests/test_observability.py
"""

import io
import json
import logging
import os
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
os.environ["POLLER_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import observability as obs  # noqa: E402
from config import settings  # noqa: E402

app = FastAPI()
app.add_middleware(obs.RequestContextMiddleware)
log = logging.getLogger("test.obs")


@app.get("/t/{thing}")
def handler(thing: str):  # sync: runs in a worker thread with a copy of the context
    obs.set(deployment_id=f"dep-{thing}", provider="lambda")
    log.info("handling %s", thing, extra={"step": "one"})
    return {"ok": True, "ctx": obs.current()}


@app.get("/boom")
def boom():
    raise RuntimeError("password=hunter22 failed")


class Capture:
    """A handler with the real filter + formatter, as configure_logging installs them."""

    def __init__(self, fmt=None):
        self.stream = io.StringIO()
        self.h = logging.StreamHandler(self.stream)
        self.h.addFilter(obs.RedactFilter())
        self.h.setFormatter(fmt or obs.JsonFormatter())
        self.records = []
        outer = self

        class Rec(logging.Handler):
            def emit(self, record):
                outer.records.append(record)
        self.rec = Rec()

    def __enter__(self):
        root = logging.getLogger()
        root.addHandler(self.h)
        root.addHandler(self.rec)
        self._lvl = root.level
        root.setLevel(logging.INFO)
        return self

    def __exit__(self, *a):
        root = logging.getLogger()
        root.removeHandler(self.h)
        root.removeHandler(self.rec)
        root.setLevel(self._lvl)

    def lines(self):
        return [json.loads(x) for x in self.stream.getvalue().splitlines() if x.strip()]

    def text(self):
        return self.stream.getvalue()


def setup():
    obs.configure_logging(json_lines=False)  # installs the record factory
    obs.metrics.reset()


def test_request_id_in_and_out():
    setup()
    c = TestClient(app)
    r = c.get("/t/a", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    assert r.json()["ctx"]["request_id"] == "abc-123"
    r = c.get("/t/b")
    assert r.headers["x-request-id"].startswith("req_") and len(r.headers["x-request-id"]) == 28
    bad = c.get("/t/c", headers={"X-Request-ID": "has spaces; <evil>"})
    assert bad.headers["x-request-id"].startswith("req_"), "an unsafe id is replaced, not echoed"
    long = c.get("/t/d", headers={"X-Request-ID": "x" * 200})
    assert long.headers["x-request-id"].startswith("req_")
    assert obs.current() == {}, "context does not leak out of the request"


def test_context_propagates_into_records():
    setup()
    c = TestClient(app)
    with Capture() as cap:
        c.get("/t/zz", headers={"X-Request-ID": "rid-42"})
    mine = [r for r in cap.records if r.name == "test.obs"]
    assert mine and mine[0].request_id == "rid-42"
    assert mine[0].deployment_id == "dep-zz" and mine[0].provider == "lambda"
    access = [r for r in cap.records if r.name == "opengrid.access"]
    assert access and access[0].request_id == "rid-42" and access[0].route == "/t/{thing}"
    lines = cap.lines()
    j = next(x for x in lines if x["logger"] == "test.obs")
    assert j["request_id"] == "rid-42" and j["deployment_id"] == "dep-zz" and j["step"] == "one"
    assert j["msg"] == "handling zz" and j["level"] == "INFO" and j["ts"].endswith("+00:00")


def test_bind_and_account_from_principal():
    setup()
    with Capture() as cap:
        with obs.bind(route_request_id="rr_1", deployment_id="dep-9"):
            log.info("inside")
            with obs.bind(provider="vast"):
                log.info("nested")
        log.info("outside")
    by = {r.getMessage(): r for r in cap.records}
    assert by["inside"].route_request_id == "rr_1" and by["inside"].provider is None
    assert by["nested"].provider == "vast" and by["nested"].deployment_id == "dep-9"
    assert by["outside"].route_request_id is None
    # account_id is read lazily from the principal on request.state
    from accounts.auth import Principal

    state = {"principal": Principal(kind="api_key", account_id=77, key_id=1)}
    tok = obs._state.set(state)
    try:
        assert obs.current()["account_id"] == 77
    finally:
        obs._state.reset(tok)


def test_json_format_and_exception():
    setup()
    c = TestClient(app, raise_server_exceptions=False)
    with Capture() as cap:
        log.info("plain", extra={"n": 3, "when": __import__("datetime").datetime(2026, 1, 1)})
        try:
            raise ValueError("token=abcdef123456 broke")
        except ValueError:
            log.exception("failed")
        r = c.get("/boom")
    assert r.status_code == 500 and r.headers.get("x-request-id", "").startswith("req_") or r.status_code == 500
    lines = cap.lines()
    plain = next(x for x in lines if x["msg"] == "plain")
    assert plain["n"] == 3 and plain["when"].startswith("2026-01-01")
    exc = next(x for x in lines if x["msg"] == "failed")
    assert "ValueError" in exc["exc"] and "abcdef123456" not in exc["exc"], exc["exc"]
    assert "hunter22" not in cap.text()
    assert obs.metrics.counter("api_requests", route="/boom", status=500) == 1


SECRETS = {
    "bearer": ("Authorization: Bearer opg_live_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg", "AbCdEfGhIjKlMnOp"),
    "basic": ("authorization: Basic b3BlbmdyaWQ6c3VwZXJzZWNyZXQ=", "b3BlbmdyaWQ6c3VwZXJzZWNyZXQ"),
    "header_dict": ("{'Authorization': 'Bearer xyzTOKENxyz123'}", "xyzTOKENxyz123"),
    "opg_key": ("key opg_live_ZZZZyyyyXXXXwwww1111 used", "ZZZZyyyyXXXXwwww1111"),
    "fernet_key": ("CREDENTIALS_ENCRYPTION_KEY " + "q" * 20 + "W" * 23 + "=", "q" * 20 + "W" * 23),
    "fernet_token": ("blob gAAAAABlZ0123456789abcdefghijklmnopqrstuv== end", "gAAAAABlZ0123456789abcdefghij"),
    "api_key_kv": ("api_key=sk-12345-secret-value", "sk-12345-secret-value"),
    "json_secret": ('{"client_secret": "verda-cs-998877"}', "verda-cs-998877"),
    "password": ("login password: hunter2hunter2", "hunter2hunter2"),
    "token_kv": ("token=tok_abcdefabcdef", "tok_abcdefabcdef"),
    "digitalocean": ("using dop_v1_" + "a1" * 32, "a1a1a1a1a1a1a1a1a1a1"),
    "runpod": ("rpa_ABCDEFGHIJKLMNOPQRSTUVWX123", "ABCDEFGHIJKLMNOPQRSTUVWX"),
    "lambda": ("secret_mykey_0123456789abcdef.0123456789", "mykey_0123456789abcdef"),
    "vast_hex": ("vast key " + "0123456789abcdef" * 4, "0123456789abcdef" * 4),
    "aws": ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    "stripe": ("sk_live_51HxxxxxxxxxxABCDEF", "51HxxxxxxxxxxABCDEF"),
    "whsec": ("whsec_abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
    "salad": ("salad_cloud_user_ABCDEFGHIJK", "user_ABCDEFGHIJK"),
    "pem": ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n-----END OPENSSH PRIVATE KEY-----",
            "b3BlbnNzaC1rZXktdjEAAAA"),
}


def test_redaction_every_pattern():
    for name, (text, secret) in SECRETS.items():
        out = obs.redact(text)
        assert secret not in out, (name, out)
        assert "[REDACTED]" in out, (name, out)
    # idempotent: a second pass (filter, then formatter) leaves redacted text alone
    for text, _ in SECRETS.values():
        once = obs.redact(text)
        assert obs.redact(once) == once, (once, obs.redact(once))
    # a logger name ending in a secret-ish word is not a key=value pair
    with Capture(obs.TextFormatter("%(name)s: %(message)s")) as cap:
        logging.getLogger("accounts.credentials").warning("credentials_encryption_key unset: using a dev key")
    assert "credentials_encryption_key unset" in cap.text(), cap.text()
    # harmless text survives
    keep ="GET /v1/deployments/dep-abc123 200 in 12ms for provider lambda, request req_1234"
    assert obs.redact(keep) == keep


def test_redaction_through_logging():
    setup()
    with Capture() as cap:
        log.info("calling %s with %s", "lambda", "Authorization: Bearer abcdefgh12345678")
        log.info("creds", extra={"api_key": "plainsecret-1", "headers": {"Authorization": "Basic Zm9vOmJhcg=="},
                                 "body": {"nested": {"password": "pw-123456"}, "items": ["token=t0k3n-abc"]},
                                 "note": "opg_live_leakedkey123456"})
    t = cap.text()
    for s in ("abcdefgh12345678", "plainsecret-1", "Zm9vOmJhcg", "pw-123456", "t0k3n-abc", "leakedkey123456"):
        assert s not in t, (s, t)
    rec = next(r for r in cap.records if r.getMessage() == "creds")
    assert rec.api_key == "[REDACTED]" and rec.body["nested"]["password"] == "[REDACTED]"


def test_known_setting_values_redacted():
    old = settings.api_key_pepper
    settings.api_key_pepper = "Pp-unusual-value-778899"
    obs.refresh_known_secrets()
    try:
        assert "Pp-unusual-value-778899" not in obs.redact("pepper is Pp-unusual-value-778899 ok")
        with Capture(obs.TextFormatter("%(message)s")) as cap:
            log.warning("leak %s", "Pp-unusual-value-778899")
        assert "Pp-unusual-value-778899" not in cap.text() and "[REDACTED]" in cap.text()
    finally:
        settings.api_key_pepper = old
        obs.refresh_known_secrets()


def test_metrics_counting():
    setup()
    c = TestClient(app)
    for x in ("a", "b", "c"):
        c.get(f"/t/{x}")
    c.get("/nope")
    assert obs.metrics.counter("api_requests", route="/t/{thing}") == 3, obs.metrics.snapshot()
    assert obs.metrics.counter("api_requests", route="/t/{thing}", status=200) == 3
    assert obs.metrics.counter("api_requests", route="<unmatched>", status=404) == 1
    snap = obs.metrics.snapshot()
    h = next(x for x in snap["histograms"] if x["name"] == "api_latency_ms" and x["labels"]["route"] == "/t/{thing}")
    assert h["count"] == 3 and h["p50"] is not None and h["p95"] >= h["p50"]
    assert not any("/t/a" in json.dumps(x["labels"]) for x in snap["counters"]), "raw paths never become labels"
    assert obs.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) in (5, 6) and obs.percentile([], 0.5) is None
    assert obs.percentile(list(range(1, 101)), 0.95) == 95


def test_provider_call_metrics_from_logs():
    setup()
    logging.getLogger("routing.adapters").info("provider_call", extra={
        "provider": "lambda", "method": "GET", "path": "/instances/123456", "status": 200, "latency_ms": 40})
    logging.getLogger("routing.adapters").info("provider_call", extra={
        "provider": "lambda", "method": "POST", "path": "/instance-operations/launch", "status": 503, "latency_ms": 900})
    logging.getLogger("routing.adapters").info("something else", extra={"provider": "lambda"})
    logging.getLogger("opengrid.provider").info("call", extra={"provider": "vast", "op": "provision",
                                                               "outcome": "unknown", "latency_ms": 20000})
    assert obs.metrics.counter("provider_calls", provider="lambda", op="GET /instances/:id", outcome="ok") == 1
    assert obs.metrics.counter("provider_calls", provider="lambda", outcome="server_error") == 1
    assert obs.metrics.counter("provider_calls", provider="lambda") == 2, "only provider_call lines count"
    assert obs.metrics.counter("provider_ops", provider="vast", op="provision", outcome="unknown") == 1
    assert obs.metrics.counter("provider_calls", provider="vast") == 0, "verbs and HTTP calls are kept apart"
    logging.getLogger("opengrid.provider").info("p", extra={"provider": "vast", "op": "status", "status": "running"})
    assert obs.metrics.counter("provider_ops", provider="vast", op="status", outcome="running") == 1
    obs.record_provider_call("runpod", "GET /pods", "ok", 12)
    assert obs.metrics.counter("provider_calls", provider="runpod") == 1


def test_main_app_installs_middleware():
    import main

    c = TestClient(main.app)
    r = c.get("/v1/scopes", headers={"X-Request-ID": "main-1"})
    assert r.headers.get("x-request-id") == "main-1", dict(r.headers)
    assert obs.INSTALLED["middleware"] and obs.INSTALLED["logging"]
    root = logging.getLogger()
    assert all(any(isinstance(f, obs.RedactFilter) for f in h.filters) for h in root.handlers)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} passed")
