"""PostHog: inert until POSTHOG_KEY is set; then the public key + ingest host reach the page (never the personal key),
the CSP gains exactly that ingest origin in connect-src (script-src stays 'self'), and the admin Value page reads
page views / visitors / top pages server-side (analytics/posthog.py), admin only, cached, errors without the key.

Run:  .venv/bin/python tests/test_posthog.py
"""

import json
import os
import re
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from analytics import posthog  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_posthog"
normalize.SessionLocal = fixtures.session(scratchdb.create(DB))
client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
KEY, SECRET, HOST = "phc_testpublickey123", "phx_super_secret_personal_key", "https://eu.i.posthog.com"


def configure(key=KEY, project="4242", personal=SECRET, host=HOST):
    settings.posthog_key, settings.posthog_project_id, settings.posthog_host = key, project, host
    settings.posthog_personal_api_key = SecretStr(personal) if personal else None
    posthog.reset()


def unconfigure():
    configure(key=None, project=None, personal=None, host="https://us.i.posthog.com")


def boot_of(html: str) -> dict:
    m = re.search(r'<script id="og-boot" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def csp_of(r) -> dict:
    return {p.split(" ", 1)[0]: p.split(" ", 1)[1] if " " in p else "" for p in
            (x.strip() for x in r.headers["content-security-policy"].split(";"))}


def test_off_by_default_changes_nothing():
    unconfigure()
    r = client.get("/gpus")
    assert r.status_code == 200
    assert "posthog" not in boot_of(r.text), "no key: nothing in the boot data"
    assert csp_of(r)["connect-src"] == "'self'", csp_of(r)
    assert "/static/vendor/posthog.js" not in r.text, "the library is never in the page; core.js loads it on demand"
    d = client.get("/v1/admin/posthog").json()["data"]
    assert d["configured"] is False and d["tracking"] is False
    assert d["missing"] == ["POSTHOG_KEY", "POSTHOG_PROJECT_ID", "POSTHOG_PERSONAL_API_KEY"], d


def test_configured_page_gets_only_the_public_key_and_one_origin():
    configure()
    try:
        r = client.get("/gpus")
        assert boot_of(r.text)["posthog"] == {"key": KEY, "host": HOST}
        assert SECRET not in r.text and "personal" not in json.dumps(boot_of(r.text)), "personal key never in a page"
        c = csp_of(r)
        assert c["connect-src"] == f"'self' {HOST}", c
        assert c["script-src"].startswith("'self'") and "posthog" not in c["script-src"], "script stays same-origin"
        assert "posthog" not in c["img-src"] and "posthog" not in c["default-src"]
        js = client.get("/static/vendor/posthog.js")
        assert js.status_code == 200 and "posthog-js 1." in js.text[:200], "vendored library served from /static"
    finally:
        unconfigure()


def _mock(handler):
    calls = []

    def wrapped(req):
        calls.append(req)
        return handler(req)
    posthog.new_client = lambda: httpx.Client(transport=httpx.MockTransport(wrapped), timeout=1)
    return calls


def test_admin_numbers_from_hogql_cached_and_admin_only():
    configure()
    real = posthog.new_client

    def ok(req):
        q = json.loads(req.content)["query"]["query"]
        if "GROUP BY path" in q:
            return httpx.Response(200, json={"results": [["/gpus", 40, 12], ["/", 30, 20]]})
        return httpx.Response(200, json={"results": [[70, 25, 300, 90]]})
    calls = _mock(ok)
    try:
        r = client.get("/v1/admin/posthog")
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["configured"] and not d.get("error"), d
        assert d["last_7_days"] == {"pageviews": 70, "visitors": 25} and d["last_30_days"] == {"pageviews": 300, "visitors": 90}
        assert d["top_pages_7_days"][0] == {"path": "/gpus", "views": 40, "visitors": 12}
        assert d["project_url"] == "https://eu.posthog.com/project/4242" and d["cached"] is False
        assert len(calls) == 2
        req = calls[0]
        assert str(req.url) == "https://eu.posthog.com/api/projects/4242/query/", req.url
        assert req.headers["authorization"] == f"Bearer {SECRET}"
        assert json.loads(req.content)["query"]["kind"] == "HogQLQuery"
        assert SECRET not in r.text, "the response never carries the personal key"
        # cached: no second round trip within 10 minutes; ?refresh=true forces one
        assert client.get("/v1/admin/posthog").json()["data"]["cached"] is True and len(calls) == 2
        assert client.get("/v1/admin/posthog?refresh=true").json()["data"]["cached"] is False and len(calls) == 4
        # admin only: anonymous and the read-only viewer login never reach it
        settings.app_password, settings.viewer_user, settings.viewer_password = "s3cret", "vc", "look-only"
        try:
            assert client.get("/v1/admin/posthog").status_code == 401
            assert client.get("/v1/admin/posthog", auth=("vc", "look-only")).status_code == 403
            assert client.get("/v1/admin/posthog", auth=("opengrid", "s3cret")).status_code == 200
        finally:
            settings.app_password = settings.viewer_user = settings.viewer_password = None
    finally:
        posthog.new_client = real
        unconfigure()


def test_errors_are_short_and_never_leak_the_key():
    configure()
    real = posthog.new_client
    try:
        for status, hint in ((401, "POSTHOG_PERSONAL_API_KEY"), (403, "Query: Read"), (404, "POSTHOG_PROJECT_ID")):
            _mock(lambda req, s=status: httpx.Response(s, json={"detail": f"bad key {SECRET}"}))
            posthog.reset()
            d = client.get("/v1/admin/posthog").json()["data"]
            assert d["error"].startswith(f"PostHog answered HTTP {status}") and hint in d["error"], d
            assert SECRET not in json.dumps(d), "upstream body (which echoes the key) is never passed on"

        def boom(req):
            raise httpx.ConnectTimeout(f"timed out talking to {req.url} with {SECRET}")
        _mock(boom)
        posthog.reset()
        d = client.get("/v1/admin/posthog").json()["data"]
        assert d["error"] == "PostHog could not be reached (ConnectTimeout)" and SECRET not in json.dumps(d), d
        assert "cached" not in d, "errors are not cached"
    finally:
        posthog.new_client = real
        unconfigure()


def test_tracking_without_admin_numbers():
    configure(project=None, personal=None)
    try:
        d = client.get("/v1/admin/posthog").json()["data"]
        assert d["configured"] is False and d["tracking"] is True
        assert d["missing"] == ["POSTHOG_PROJECT_ID", "POSTHOG_PERSONAL_API_KEY"], d
        assert "posthog" in boot_of(client.get("/gpus").text), "events still flow with only the public key"
    finally:
        unconfigure()


def test_app_host_for_us_and_eu():
    for ingest, app in (("https://us.i.posthog.com", "https://us.posthog.com"), ("https://eu.i.posthog.com", "https://eu.posthog.com"),
                        ("https://ph.example.com", "https://ph.example.com")):
        settings.posthog_host = ingest
        assert settings.posthog_app_host == app, (ingest, settings.posthog_app_host)
    settings.posthog_host = "https://us.i.posthog.com"


if __name__ == "__main__":
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print("ok ", name)
        print("all passed")
    finally:
        normalize.SessionLocal.kw["bind"].dispose()
        scratchdb.drop(DB)
