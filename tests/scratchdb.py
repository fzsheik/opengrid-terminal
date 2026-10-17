"""A throwaway database for tests: created and dropped over SQL, so no Postgres CLI is needed.

The server comes from TEST_PG_URL (default: localhost, current OS user), never the app's database.

Importing this module also ISOLATES the test process from real providers (every DB test imports it, directly or
via bench_fixtures), whatever the developer's .env holds:
    * provider credentials (and webhook URLs, S3 / LLM keys) are blanked in os.environ BEFORE config is imported
      and on config.settings if it already was; tests then set their own fake keys + mock transports.
    * httpx's real transports refuse every non-loopback host (RealNetworkBlocked, an httpx.ConnectError so the
      code under test sees "offline", printed to stderr and kept in BLOCKED so it is never silent).
OPENGRID_TEST_ALLOW_NETWORK=1 turns the guard off (never with real keys).
"""

import os
import sys

from sqlalchemy import create_engine, text

SERVER = os.environ.get("TEST_PG_URL", "postgresql+psycopg://localhost:5432")


def url(name: str) -> str:
    return f"{SERVER}/{name}"


def _admin(sql: str) -> None:
    engine = create_engine(url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def drop(name: str) -> None:
    _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def create(name: str) -> str:
    """Drop and recreate `name`; returns its URL."""
    drop(name)
    _admin(f'CREATE DATABASE "{name}"')
    return url(name)


# --------------------------------------------------------------------------
# Isolation from real providers (see the module docstring)
# --------------------------------------------------------------------------

_SECRET_FIELDS = ("salad_api_key", "salad_org", "hyperstack_api_key", "lambda_api_key", "runpod_api_key",
                  "digitalocean_api_key", "shadeform_api_key", "vast_api_key", "verda_client_id",
                  "verda_client_secret", "anthropic_api_key", "job_artifact_s3_access_key_id",
                  "job_artifact_s3_secret_access_key", "ops_alert_webhook_url", "deployments_webhook_url",
                  "market_webhook_url", "news_webhook_url")
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")
BLOCKED: list[str] = []


def blank_credentials() -> None:
    for f in _SECRET_FIELDS:
        os.environ[f.upper()] = ""            # beats the .env file (pydantic-settings: env first)
    cfg = sys.modules.get("config")
    if cfg is not None and hasattr(cfg, "settings"):
        for f in _SECRET_FIELDS:
            if hasattr(cfg.settings, f):
                setattr(cfg.settings, f, None)


def _guard_network() -> None:
    import httpx

    class RealNetworkBlocked(httpx.ConnectError):
        pass

    def refuse(request):
        host = request.url.host or ""
        if host in _LOOPBACK:
            return None
        msg = f"TEST ISOLATION: refused a real network call to {request.method} {request.url.scheme}://{host}"
        BLOCKED.append(f"{request.method} {host}")
        print(msg, file=sys.stderr, flush=True)
        return RealNetworkBlocked(msg, request=request)

    sync_orig, async_orig = httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request
    if getattr(sync_orig, "_og_guard", False):
        return

    def handle_request(self, request):
        exc = refuse(request)
        if exc is not None:
            raise exc
        return sync_orig(self, request)

    async def handle_async_request(self, request):
        exc = refuse(request)
        if exc is not None:
            raise exc
        return await async_orig(self, request)

    handle_request._og_guard = True
    httpx.HTTPTransport.handle_request = handle_request
    httpx.AsyncHTTPTransport.handle_async_request = handle_async_request
    globals()["RealNetworkBlocked"] = RealNetworkBlocked


def _report_blocked() -> None:
    if BLOCKED:
        print(f"TEST ISOLATION: {len(BLOCKED)} real network call(s) refused: {', '.join(sorted(set(BLOCKED)))}",
              file=sys.stderr, flush=True)


blank_credentials()
if os.environ.get("OPENGRID_TEST_ALLOW_NETWORK") != "1":
    _guard_network()
    import atexit
    atexit.register(_report_blocked)
