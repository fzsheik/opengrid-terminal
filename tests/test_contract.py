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
import gpu_ids  # noqa: E402
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


def _gpu_fields(v):
    """Every value of a `gpu` key, anywhere in a body; also checks gpu_name sits beside each public id."""
    if isinstance(v, dict):
        g = v.get("gpu")
        if isinstance(g, str):
            if g in gpu_ids.BY_ID:
                assert v.get("gpu_name") == gpu_ids.BY_ID[g], v
            yield g
        for x in v.values():
            yield from _gpu_fields(x)
    elif isinstance(v, list):
        for x in v:
            yield from _gpu_fields(x)


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
        leaks = [g for g in _gpu_fields(body) if g in gpu_ids.GPU_IDS]
        if leaks:
            bad.append(f"canonical name in a gpu field {p}: {leaks[:2]}")
        if isinstance(body, dict) and isinstance(body.get("data"), list) and "next_cursor" not in body.get("meta", {}):
            bad.append(f"list without cursor paging {p}")  # every Data API list pages by cursor
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
    assert err["docs_url"] == "https://docs.tryopengrid.com/errors#invalid_parameter"


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

    main.app.router.routes.insert(0, __import__("fastapi").routing.APIRoute("/v1/macro/__boom", boom))
    try:
        err = assert_error(TestClient(main.app, raise_server_exceptions=False).get("/v1/macro/__boom"), 500)
        assert err["type"] == "api_error" and "secret internals" not in err["message"]
    finally:
        main.app.router.routes.pop(0)


def test_key_pin():
    from sqlalchemy import text

    from accounts import accounts, keys

    a = accounts.create_account("pinned-co")
    k = keys.create_key(a["id"], "pin", ["data:read"])
    assert k["api_version"] == contract.LATEST, k
    auth = {"Authorization": f"Bearer {k['secret']}"}
    old = contract.VERSIONS
    contract.VERSIONS = ("2026-01-01",) + old  # pretend an older version is still served
    try:
        with normalize.SessionLocal.begin() as s:
            s.execute(text("UPDATE api_keys SET api_version = '2026-01-01' WHERE id = :i"), {"i": k["id"]})
        keys.reset_cache()
        r = client.get("/v1/gpus", headers=auth)
        assert r.status_code == 200 and r.headers["opengrid-version"] == "2026-01-01", r.headers
        assert r.json()["meta"]["api_version"] == "2026-01-01"
        r = client.get("/v1/gpus", headers={**auth, "OpenGrid-Version": contract.LATEST})
        assert r.headers["opengrid-version"] == contract.LATEST, "the header beats the pin"
        err = assert_error(client.get("/v1/gpus/not-a-gpu", headers=auth), 404)
        assert err["code"] == "not_found" and r.headers["opengrid-version"]
    finally:
        contract.VERSIONS = old
    assert client.get("/v1/gpus").headers["opengrid-version"] == contract.LATEST, "no key, no header: latest"


def test_data_host_is_key_only():
    from accounts import accounts, keys

    old = settings.data_api_host
    settings.data_api_host = "data.test"
    try:
        dc = TestClient(main.app, base_url="https://data.test")
        err = assert_error(dc.get("/v1/gpus"), 401)
        assert err["code"] == "authentication_required" and "Bearer" in err["message"]
        assert assert_error(dc.get("/v1/jobs"), 404)["code"] == "not_found", "Compute API is not on the data host"
        assert assert_error(dc.get("/"), 404)["code"] == "not_found", "no website on the data host"
        assert dc.get("/health").status_code == 200
        k = keys.create_key(accounts.create_account("host-co")["id"], "h", ["data:read"])
        r = dc.get("/v1/gpus", headers={"Authorization": f"Bearer {k['secret']}"})
        assert r.status_code == 200 and r.json()["meta"]["api_version"] == contract.LATEST
        assert_error(dc.get("/v1/gpus", headers={"Authorization": "Bearer opg_live_wrong"}), 401)
        assert client.get("/v1/gpus").status_code == 200, "the website host is unchanged"
    finally:
        settings.data_api_host = old


def _all_pages(path, **params):
    rows, cursor = [], None
    for _ in range(1000):
        r = client.get(path, params={**params, **({"cursor": cursor} if cursor else {})})
        assert r.status_code == 200, r.text
        rows += r.json()["data"]
        cursor = r.json()["meta"]["next_cursor"]
        if not cursor:
            return rows
    raise AssertionError("cursor never ended")


def test_listings_match_the_table_and_page_completely():
    from sqlalchemy import text

    with normalize.SessionLocal() as s:
        want = {(r.provider, r.listing_id) for r in s.execute(text(
            "SELECT provider, listing_id FROM compute_listings WHERE visibility = 'public'"))}
        obs = s.execute(text("SELECT count(*) FROM listing_observations o JOIN compute_listings c "
                             "USING (provider, listing_id) WHERE c.visibility = 'public'")).scalar()
    got = _all_pages("/v1/listings", limit=3)
    assert len(got) == len(want) and {(r["provider"], r["listing_id"]) for r in got} == want
    prices = [r["price_per_gpu_hour"] for r in got if r["price_per_gpu_hour"] is not None]
    assert prices == sorted(prices), "cheapest first"
    row = got[0]
    assert row["currency"] == "USD" and "trust" in row and row["region_group"] and row["observed_at"].endswith("Z")
    hist = _all_pages("/v1/listings/history", limit=7)
    keys_ = [(r["observed_at"], r["provider"], r["listing_id"]) for r in hist]
    assert obs > 7 and len(hist) == obs and len(set(keys_)) == obs, "keyset cursor: every change once"
    assert [k[0] for k in keys_] == sorted((k[0] for k in keys_), reverse=True), "newest first"
    assert assert_error(client.get("/v1/listings/history", params={"cursor": "junk"}), 400)["code"] == "invalid_cursor"


def test_listings_as_of():
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    t = datetime.now(timezone.utc) - timedelta(days=3)
    got = _all_pages("/v1/listings", as_of=t.isoformat(), limit=50)
    assert got and all(r["trust"] is None and r["original"] is None for r in got)
    with normalize.SessionLocal() as s:  # independent: the last recorded price at or before t, per listing
        for r in got[:10]:
            want = s.execute(text("SELECT price_per_gpu_hour, observed_at FROM listing_observations WHERE provider = :p "
                                  "AND listing_id = :l AND observed_at <= :t ORDER BY observed_at DESC LIMIT 1"),
                             {"p": r["provider"], "l": r["listing_id"], "t": t}).one()
            assert r["price_per_gpu_hour"] == (None if want[0] is None else float(want[0])), (r, want)
            assert datetime.fromisoformat(r["observed_at"].replace("Z", "+00:00")) <= t
    assert client.get("/v1/listings", params={"as_of": "2000-01-01T00:00:00Z"}).json()["data"] == []
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert assert_error(client.get("/v1/listings", params={"as_of": future}), 422)["param"] == "as_of"


def test_exports():
    import gzip
    import json
    from datetime import date, datetime, time, timedelta, timezone

    from sqlalchemy import text

    day = date.today() - timedelta(days=2)
    d0 = datetime.combine(day, time.min, tzinfo=timezone.utc)
    cat = {r["dataset"]: r for r in client.get("/v1/exports").json()["data"]}
    assert set(cat) == {"listings", "listing_changes", "index_levels", "fixings"} and cat["listings"]["first_date"]

    def rows(name):
        r = client.get(f"/v1/exports/{name}", params={"date": day.isoformat()})
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip", r.text[:200]
        return [json.loads(x) for x in gzip.decompress(r.content).decode().splitlines() if x]

    snap = rows("listings")
    end = (d0 + timedelta(days=1) - timedelta(microseconds=1)).isoformat()
    assert snap == _all_pages("/v1/listings", as_of=end, limit=1000), "the day's snapshot is the as_of read"
    changes = rows("listing_changes")
    with normalize.SessionLocal() as s:
        n = s.execute(text("SELECT count(*) FROM listing_observations o JOIN compute_listings c USING (provider, listing_id) "
                           "WHERE c.visibility = 'public' AND o.observed_at >= :a AND o.observed_at < :b"),
                      {"a": d0, "b": d0 + timedelta(days=1)}).scalar()
    assert n > 0 and len(changes) == n and all(c["observed_at"].endswith("Z") and "gpu_name" in c for c in changes)
    rows("index_levels"), rows("fixings")
    today = client.get("/v1/exports/listings", params={"date": date.today().isoformat()})
    assert assert_error(today, 422)["param"] == "date"
    assert client.get("/v1/exports/nope", params={"date": day.isoformat()}).status_code == 422


def test_version_downgrade_steps():
    # A fake breaking change on 2027-01-01: /v1/markets rows rename providers -> provider_count. A key pinned to
    # the current version (or a request asking for it) gets the old field back; the new version gets the new one.
    def rename(rows):
        return [{**{k: v for k, v in r.items() if k != "providers"}, "provider_count": r["providers"]} for r in rows]

    def back(data, path):
        return [{**{k: v for k, v in r.items() if k != "provider_count"}, "providers": r["provider_count"]}
                for r in data] if path == "/v1/markets" else data

    old_versions, old_steps, real = contract.VERSIONS, list(contract.STEPS), contract.public_shape
    contract.VERSIONS = old_versions + ("2027-01-01",)
    contract.STEPS.append(("2027-01-01", back))
    contract.public_shape = lambda v: rename(real(v)) if isinstance(v, list) and v and "spread" in v[0] else real(v)
    try:
        new = client.get("/v1/markets", headers={"OpenGrid-Version": "2027-01-01"}).json()["data"]
        old = client.get("/v1/markets", headers={"OpenGrid-Version": old_versions[-1]}).json()["data"]
        assert "provider_count" in new[0] and "providers" not in new[0]
        assert "providers" in old[0] and "provider_count" not in old[0] and old[0]["providers"] == new[0]["provider_count"]
    finally:
        contract.VERSIONS, contract.STEPS[:], contract.public_shape = old_versions, old_steps, real


def test_data_api_openapi():
    spec = client.get("/v1/openapi.json").json()
    assert spec["info"]["title"] == "OpenGrid Data API" and spec["info"]["version"] == contract.LATEST
    ps = set(spec["paths"])
    assert {"/v1/gpus", "/v1/markets/{gpu}", "/v1/listings", "/v1/exports/{dataset}"} <= ps
    assert all(contract.is_data_path(p) for p in ps), "only Data API routes"
    assert not any(p.startswith(("/v1/jobs", "/v1/admin", "/internal", "/v1/ops")) for p in ps)
    old_pw, old_pub = settings.app_password, settings.public_pages
    settings.app_password, settings.public_pages = "test-pw", True
    try:
        assert TestClient(main.app).get("/v1/openapi.json").status_code == 401, "never anonymous"
    finally:
        settings.app_password, settings.public_pages = old_pw, old_pub


def test_replaces_the_old_first_ui_routes():
    """The Data API now carries everything the retired /market, /market/detail and /listings gave the website."""
    import market
    import normalize as nz
    from gpu_ids import GPU_IDS

    # /market  ->  /v1/markets?trend=true (change, trend, who is lowest / highest)
    ov = market.overview(24, 48)
    r = client.get("/v1/markets", params={"trend": True, "hours": 24, "points": 48, "limit": 1000}).json()
    by_old = {g["gpu"]: g for g in ov["gpus"]}
    assert len(r["meta"]["trend_times"]) == len(ov["times"]) and r["data"]  # each call takes its own "now"
    for row in r["data"]:
        old = by_old.get(row["gpu_name"])
        assert old is not None, row["gpu_name"]
        assert len(row["trend"]) == len(old["spark"]) and all(
            (a is None and b is None) or abs(a - b) < 1e-9 for a, b in zip(row["trend"], old["spark"]))
        assert (row["change"]["pct"] is None and old["change_pct"] is None) or abs(row["change"]["pct"] - old["change_pct"]) < 1e-9
        assert (row["change"]["since"] is None) == (old["change_since"] is None)
        assert row["low_provider"] == old["lowest_provider"] and row["high_provider"] == old["highest_provider"]
        assert abs(row["price"]["low"] - old["lowest"]) < 1e-6, (row["gpu_name"], row["price"], old["lowest"])
    assert "trend" not in client.get("/v1/markets", params={"limit": 1}).json()["data"][0], "trend is opt-in"

    # /market/detail  ->  /v1/markets/{gpu}/history/providers
    gpu = r["data"][0]["gpu_name"]
    d = client.get(f"/v1/markets/{r['data'][0]['gpu']}/history/providers", params={"hours": 24}).json()["data"]
    old_d = market.detail(gpu, 24)
    assert [p["provider"] for p in d["providers"]] == [p["provider"] for p in old_d["providers"]]
    assert d["low"] == old_d["lowest"] and d["high"] == old_d["highest"] and len(d["times"]) == len(old_d["times"])
    assert all(p["series"] == o["series"] and p["now"] == o["now"] for p, o in zip(d["providers"], old_d["providers"]))

    # /listings  ->  /v1/listings?previous=true (the price before the newest change)
    prev = {(x["provider"], x["listing_id"]): x for x in nz.listings()}
    rows = _all_pages("/v1/listings", limit=500, previous=True)
    assert len(rows) == len(prev) and rows
    for x in rows:
        o = prev[(x["provider"], x["listing_id"])]
        pc = x["price_change"]
        want = o.get("previous_price_per_gpu_hour")
        assert (pc["previous_price_per_gpu_hour"] if pc else None) == (None if want is None else float(want)), x["listing_id"]
    assert all(x["price_change"] is None for x in _all_pages("/v1/listings", limit=500)), "previous is opt-in"

    # the old routes are gone; the retired first UI redirects to the GPU table
    for gone in ("/market", "/market/detail?gpu=x", "/listings", "/changes", "/history", "/reference-prices", "/polling", "/mapping"):
        assert client.get(gone).status_code in (404, 401), gone
    rc = client.get("/classic", follow_redirects=False)
    assert rc.status_code == 308 and rc.headers["location"] == "/gpus"
    assert {"/internal/polling", "/internal/mapping"} <= set(main.app.openapi()["paths"])  # moved, not dropped


def test_etag_and_cache_headers():
    r = client.get("/v1/gpus")
    tag = r.headers["etag"]
    assert r.status_code == 200 and tag.startswith('"') and r.headers["cache-control"] == contract.CACHE_CONTROL
    assert "Authorization" in r.headers["vary"] and "OpenGrid-Version" in r.headers["vary"]
    assert client.get("/v1/gpus").headers["etag"] == tag, "as_of / request_id change; the ETag does not"
    nm = client.get("/v1/gpus", headers={"If-None-Match": tag})
    assert nm.status_code == 304 and nm.content == b"" and nm.headers["etag"] == tag
    assert client.get("/v1/gpus", headers={"If-None-Match": '"other"'}).status_code == 200
    assert client.get("/v1/gpus?limit=2").headers["etag"] != tag, "a different body, a different ETag"
    err = client.get("/v1/gpus/not-a-gpu")
    assert err.status_code == 404 and "etag" not in err.headers
    old = contract.VERSIONS
    contract.VERSIONS = ("2026-01-01",) + old
    try:
        assert client.get("/v1/gpus", headers={"OpenGrid-Version": "2026-01-01"}).headers["etag"] != tag, \
            "another version is another shape"
    finally:
        contract.VERSIONS = old


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
