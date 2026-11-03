"""Data API edge contract (api/contract.py, docs/API_CONTRACT.md sections 3-4), swept over every route.

    - every Data API GET route answers without a 500, carries OpenGrid-Version, and either
      {data, meta{request_id, api_version}} or {error{type, code, message, param, request_id, docs_url}};
    - an unknown OpenGrid-Version is a 400 before the route runs; a known one is echoed;
    - validation errors, the site gate's 401, and an unhandled exception all come back in the error shape;
    - Compute API paths keep FastAPI's {"detail"} (out of scope for now).

Run:  .venv/bin/python tests/test_contract.py
"""

import os
import re
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from api import contract  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_contract"
normalize.SessionLocal = fixtures.session(scratchdb.create(DB))
fixtures.seed(normalize.SessionLocal, days=10)
client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
STD = {"type", "code", "message", "param", "request_id", "docs_url"}
SAMPLE = {"gpu": "h100-80gb-sxm5", "provider": "syn_alpha", "index_id": "h100-80gb-sxm5", "family": "h100",
          "kind": "price", "name": "indices"}


def data_get_paths() -> list[str]:
    out = []
    for path, item in main.app.openapi()["paths"].items():  # app.routes nests included routers
        if "get" in item and contract.is_data_path(path):
            out.append(re.sub(r"\{([a-z_]+)\}", lambda m: SAMPLE.get(m.group(1), "x"), path))
    return sorted(set(out))


def assert_error(r, status=None):
    assert status is None or r.status_code == status, (r.status_code, r.text[:300])
    err = r.json()["error"]
    assert set(err) >= STD, err
    assert err["request_id"] == r.headers["x-request-id"], (err, r.headers)
    assert err["type"] and err["code"] and err["message"], err
    return err


def test_every_data_route():
    paths = data_get_paths()
    assert len(paths) >= 60, len(paths)
    bad = []
    for p in paths:
        r = client.get(p)
        if r.status_code >= 500 or r.headers.get("opengrid-version") != contract.LATEST:
            bad.append(f"{r.status_code} {p}: {r.text[:200]}")
            continue
        if "json" not in r.headers.get("content-type", ""):
            continue  # a markdown methodology document, say
        if r.status_code >= 400:
            assert_error(r)
            continue
        body = r.json()
        if isinstance(body, dict) and "meta" in body:
            meta = body["meta"]
            if meta.get("request_id") != r.headers["x-request-id"] or meta.get("api_version") != contract.LATEST:
                bad.append(f"meta {p}: {meta}")
    assert not bad, "\n".join(bad)


def test_version_header():
    r = client.get("/v1/gpus", headers={"OpenGrid-Version": "1999-01-01"})
    err = assert_error(r, 400)
    assert err["code"] == "unknown_api_version" and err["param"] == "OpenGrid-Version" and contract.LATEST in err["message"]
    assert assert_error(client.get("/v1/gpus", headers={"OpenGrid-Version": "latest"}), 400)["code"] == "unknown_api_version"
    r = client.get("/v1/gpus", headers={"OpenGrid-Version": contract.LATEST})
    assert r.status_code == 200 and r.headers["opengrid-version"] == contract.LATEST
    assert r.json()["meta"]["api_version"] == contract.LATEST


def test_error_shapes():
    err = assert_error(client.get("/v1/gpus/not-a-gpu"), 404)
    assert err["code"] == "not_found" and "not-a-gpu" in err["message"] and err["type"] == "invalid_request"
    err = assert_error(client.get("/v1/gpus?limit=0"), 422)
    assert err["code"] == "invalid_parameter" and err["param"] == "limit", err
    assert err["docs_url"].endswith("/api#error-invalid_parameter")


def test_site_gate_401_is_shaped():
    old_pw, old_pub = settings.app_password, settings.public_pages
    settings.app_password, settings.public_pages = "test-pw", False
    try:
        err = assert_error(TestClient(main.app).get("/v1/gpus"), 401)
        assert err["type"] == "authentication" and err["code"] == "authentication_required"
        # Compute API: unchanged for now (plain text from the gate)
        r = TestClient(main.app).get("/v1/jobs")
        assert r.status_code == 401 and "error" not in r.text
    finally:
        settings.app_password, settings.public_pages = old_pw, old_pub


def test_unhandled_exception_is_shaped():
    def boom():
        raise RuntimeError("secret internals")

    main.app.router.routes.insert(0, __import__("fastapi").routing.APIRoute("/v1/live/__boom", boom))
    try:
        err = assert_error(TestClient(main.app, raise_server_exceptions=False).get("/v1/live/__boom"), 500)
        assert err["type"] == "api_error" and "secret internals" not in err["message"]
    finally:
        main.app.router.routes.pop(0)


def test_compute_api_keeps_detail():
    r = client.get("/v1/jobs/job-doesnotexist")
    assert r.status_code >= 400 and "error" not in r.json(), r.text
    assert "opengrid-version" not in r.headers


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
