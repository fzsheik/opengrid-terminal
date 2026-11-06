"""The public customer docs (docs-site/, docs.tryopengrid.com) say only what the API does.

    - the committed schema and sidebar files are what api/docs_openapi.py generates from the code today;
    - the schema publishes only customer routes: Data API GETs and the read-only POSTs, no operator writes;
    - every route the docs' pages name (`GET /v1/...`) exists, and every internal link resolves to a page;
    - every error code the API can put in `docs_url` has its anchor on the Errors page;
    - CORS: the data host answers the docs origin only (preflights included, so the playground works with a key),
      without credentials, and the website host is untouched.

Run:  .venv/bin/python tests/test_docs_site.py   (no database: nothing here reaches a route that reads one)
Regenerate after an API change:  .venv/bin/python -m api.docs_openapi
"""

import json
import os
import re
import sys
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from api import contract, docs_openapi  # noqa: E402
from config import settings  # noqa: E402

DOCS = ROOT / "docs-site"
CONTENT = DOCS / "content" / "docs"
SPEC = json.loads((DOCS / "openapi.json").read_text())
PAGES = {p: p.read_text() for p in CONTENT.rglob("*.mdx")}
DOCS_ORIGIN = settings.docs_base_url.rstrip("/")
DATA_HOST = "data.tryopengrid.com"
# Routes the docs site serves besides its pages (docs-site/app/).
SITE_ROUTES = {"/llms.txt", "/llms-full.txt", "/openapi.json", "/quickstart.md", "/sitemap.xml", "/robots.txt"}


def ops():
    return [(m, p, o) for p, item in SPEC["paths"].items() for m, o in item.items()]


def page_urls() -> set[str]:
    urls = set()
    for p in PAGES:
        rel = p.relative_to(CONTENT).with_suffix("")
        parts = [x for x in rel.parts if not (x.startswith("(") and x.endswith(")"))]
        if parts and parts[-1] == "index":
            parts = parts[:-1]
        urls.add("/" + "/".join(parts))
    for m, p, o in ops():
        urls.add(f"/api-reference/{docs_openapi.slug(o['tags'][0])}/{o['operationId']}")
    return urls


def test_generated_files_match_the_code():
    for path, text in docs_openapi.files().items():
        assert path.exists() and path.read_text() == text, \
            f"{path.relative_to(ROOT)} is stale: run .venv/bin/python -m api.docs_openapi"


def test_only_customer_routes_are_published():
    assert ops(), "empty schema"
    for m, p, o in ops():
        assert contract.is_data_path(p), p
        assert m == "get" or (m == "post" and p in docs_openapi.PUBLIC_POST), f"{m} {p} is not a customer route"
        assert o["tags"] and o["tags"][0] in docs_openapi.TAG_ORDER and o["summary"] and o["operationId"], p
        assert any(x["name"] == contract.HEADER for x in o["parameters"]), p
    paths = {f"{m} {p}" for m, p, o in ops()}
    for operator_write in ("post /v1/events/track", "post /v1/news/refresh", "post /v1/news/reclassify"):
        assert operator_write not in paths, operator_write
    ids = [o["operationId"] for m, p, o in ops()]
    assert len(ids) == len(set(ids)), "operationIds name the reference pages: they must be unique"
    assert SPEC["servers"] == [{"url": f"https://{DATA_HOST}"}]
    assert "bearer" in SPEC["components"]["securitySchemes"]


def test_recorded_shapes_are_documented():
    snapshot = json.loads((ROOT / "tests" / "contracts" / f"{contract.LATEST}.json").read_text())
    typed = 0
    for m, p, o in ops():
        data = (o["responses"]["200"].get("content", {}).get("application/json", {}).get("schema", {})
                .get("properties", {}).get("data", {}))
        typed += "type" in data
    assert typed >= len(snapshot) - 2, f"only {typed} of {len(snapshot)} recorded routes have a typed response"
    markets = SPEC["paths"]["/v1/markets"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    row = markets["properties"]["data"]["items"]["properties"]
    assert row["price"]["properties"]["median"]["type"] == "number" and row["gpu"]["type"] == "string"


def test_routes_named_in_the_docs_exist():
    # A template's parameter matches any one segment, so both `/v1/markets/{gpu}` and a concrete id pass.
    known = [re.compile("^" + m.upper() + " " + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(p)) + "$")
             for m, p, o in ops()]
    for page, text in PAGES.items():
        for method, path in re.findall(r"`(GET|POST) (/v1/[^`?\s]*)", text):
            assert any(k.match(f"{method} {path}") for k in known), \
                f"{page.relative_to(DOCS)} names {method} {path}, which the schema does not have"


def test_internal_links_resolve():
    urls = page_urls() | SITE_ROUTES
    for page, text in PAGES.items():
        for link in re.findall(r"\]\((/[^)\s]*)\)|href=\"(/[^\"]*)\"", text):
            link = next(x for x in link if x)
            target = link.split("#", 1)[0] or "/"
            assert target in urls, f"{page.relative_to(DOCS)} links to {link}, which is not a page"


def test_every_error_code_has_an_anchor():
    errors = PAGES[CONTENT / "(docs)" / "errors.mdx"]
    anchors = set(re.findall(r"^### ([a-z_]+)$", errors, re.M))
    codes = set(contract._CODE.values()) | {"unknown_api_version", "invalid_cursor", "unknown_series"}
    assert codes <= anchors, f"Errors page lacks {sorted(codes - anchors)}"
    url = contract.error_body(422, code="invalid_parameter")["error"]["docs_url"]
    assert url == f"{DOCS_ORIGIN}/errors#invalid_parameter", url


def test_cors_for_the_docs_playground_only():
    old = settings.data_api_host
    settings.data_api_host = DATA_HOST
    try:
        c = TestClient(main.app)
        pre = {"Origin": DOCS_ORIGIN, "Access-Control-Request-Method": "GET",
               "Access-Control-Request-Headers": "authorization,opengrid-version"}
        r = c.options("/v1/markets", headers={**pre, "Host": DATA_HOST})
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.headers["access-control-allow-origin"] == DOCS_ORIGIN
        assert "access-control-allow-credentials" not in r.headers, "the playground never sends cookies"
        assert "authorization" in r.headers["access-control-allow-headers"].lower()

        r = c.options("/v1/markets", headers={**pre, "Host": DATA_HOST, "Origin": "https://evil.example"})
        assert r.headers.get("access-control-allow-origin") is None, "only the docs origin"

        r = c.get("/v1/markets", headers={"Host": DATA_HOST, "Origin": DOCS_ORIGIN})
        assert r.status_code == 401 and r.json()["error"]["code"] == "authentication_required"
        assert r.headers["access-control-allow-origin"] == DOCS_ORIGIN, "the playground must be able to read errors"
        assert "x-request-id" in r.headers["access-control-expose-headers"].lower()

        r = c.options("/v1/markets", headers={**pre, "Host": "terminal.tryopengrid.com"})
        assert r.headers.get("access-control-allow-origin") is None, "the website host is not opened to the docs"
    finally:
        settings.data_api_host = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("all passed")
