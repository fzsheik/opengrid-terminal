"""The password gate and the database URL a host provides.

Run:  .venv/bin/python tests/test_deploy.py
"""

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import main
from config import Settings, settings

client = TestClient(main.app)          # not used as a context manager: the poller is not started


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_database_url():
    f = lambda u: Settings(database_url=u).database_url  # noqa: E731
    assert f("postgres://u:p@host.internal:5432/railway") == "postgresql+psycopg://u:p@host.internal:5432/railway", "Railway/Heroku style"
    assert f("postgresql://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"
    assert f("postgresql+psycopg://localhost:5432/opengrid") == "postgresql+psycopg://localhost:5432/opengrid", "already right: untouched"
    assert f("postgres://u:p@h/db?sslmode=require").endswith("/db?sslmode=require"), "query string kept"


def test_gate():
    settings.app_user, settings.app_password = "opengrid", "s3cret-pass"
    try:
        for method, path in [("get", "/"), ("get", "/market"), ("get", "/listings"), ("get", "/raw"), ("get", "/static/app.js"),
                             ("get", "/static/logos/lium.png"), ("get", "/classic"), ("get", "/docs"), ("post", "/fetch"), ("post", "/normalize")]:
            r = getattr(client, method)(path)
            assert r.status_code == 401, f"{method.upper()} {path} must need the password, got {r.status_code}"
            assert "Basic" in r.headers["www-authenticate"], "the browser must be told to ask for a password"
        for bad in [basic("opengrid", "wrong"), basic("someone", "s3cret-pass"), basic("", ""), basic("opengrid", ""), {"Authorization": "Basic !!!notbase64"},
                    {"Authorization": "Bearer s3cret-pass"}, {"Authorization": "Basic"}, basic("opengrid", "s3cret-pass-extra"), basic("opengrid", "s3cret")]:
            assert client.get("/market", headers=bad).status_code == 401, f"rejected: {bad}"
        good = basic("opengrid", "s3cret-pass")
        assert client.get("/", headers=good).status_code == 200
        assert client.get("/static/app.js", headers=good).status_code == 200
        assert client.get("/static/logos/lium.png", headers=good).status_code == 200
        assert client.get("/health").status_code == 200, "the host's health check has no password"
        assert basic("opengrid", "pass:with:colons") and main.password_ok(basic("opengrid", "s3cret-pass")["Authorization"])
        settings.app_password = "pass:with:colons"
        assert client.get("/", headers=basic("opengrid", "pass:with:colons")).status_code == 200, "a password containing colons works"
        settings.app_password = "unicode-pässword"
        assert client.get("/", headers=basic("opengrid", "unicode-pässword")).status_code == 200, "a non-ascii password works"
    finally:
        settings.app_password = None


def test_open_when_unset():
    settings.app_password = None
    assert client.get("/").status_code == 200, "no password set (your own machine): open"


def test_refuses_to_run_deployed_without_password():
    import asyncio, os
    settings.app_password = None
    os.environ["RAILWAY_ENVIRONMENT"] = "production"
    try:
        async def start():
            async with main.lifespan(main.app):
                pass
        try:
            asyncio.run(start()); raise AssertionError("it started with no password")
        except RuntimeError as e:
            assert "APP_PASSWORD" in str(e)
    finally:
        del os.environ["RAILWAY_ENVIRONMENT"]


if __name__ == "__main__":
    for t in (test_database_url, test_gate, test_open_when_unset, test_refuses_to_run_deployed_without_password):
        t(); print(t.__name__, "ok")
