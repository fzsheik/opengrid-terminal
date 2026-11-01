"""Crusoe: prices via Shadeform (unchanged); with a key pair, capacity from Crusoe's own signed Cloud API.

Fixtures:
    fixtures_providers/crusoe_api.json  GET api.cloud.crusoe.ai/v1 /capacities, /locations and
                                        /projects/{id}/compute/vms/types, fetched 2026-11-01 with a real key pair,
                                        trimmed (see its _note): every GPU (type, location) once, one key kept with
                                        all 8 of its repeated copies, 2 CPU rows; GPU types + 2 CPU types
    fixtures_capacity/crusoe.json       Shadeform's catalogue, Crusoe rows (the price source, and the no-key capacity)

Signing vectors were computed with client-go's own auth code (auth/auth.go generateMessageV1_0 + signMessageV1_0)
on the sample key pair printed in docs.crusoe.ai/reference/api. The docs' own example signature does not reproduce
under any reading of its inputs (it is base64 of a hex string), so it is not used.

Every network call goes through httpx.MockTransport; scratchdb refuses real hosts and blanks real keys.

Run:  .venv/bin/python tests/test_provider_crusoe.py
"""

import asyncio
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (blanks real keys, refuses real network)

import httpx  # noqa: E402

import mapping  # noqa: E402
from providers import PROVIDERS, shadeform  # noqa: E402
from providers.crusoe import (  # noqa: E402
    CAPACITIES, LOCATIONS, PROJECTS, TYPES, CrusoeProvider, auth_headers, canonical_query, signature,
)

HERE = Path(__file__).resolve().parent
T0 = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
SHADE = shadeform.BASE_URL + shadeform.ENDPOINT
# The sample key pair from docs.crusoe.ai/reference/api (not a real credential).
DOC_KID, DOC_SECRET = "gYFONy-6QKS1acgUEQrR4Q", "uZFGf918DmiBUwBWv8lnEg"
PID = "00000000-1111-2222-3333-444444444444"


def api() -> dict:
    return json.loads((HERE / "fixtures_providers" / "crusoe_api.json").read_text())


def shade_payload() -> dict:
    return json.loads((HERE / "fixtures_capacity" / "crusoe.json").read_text())[shadeform.ENDPOINT][0]


def snap(ep, payload, at=T0):
    return SimpleNamespace(endpoint=ep, payload=payload, fetched_at=at)


def with_api(api_at=T0, shade_at=T0, caps=None):
    a = api()
    return {SHADE: [snap(SHADE, shade_payload(), shade_at)],
            CAPACITIES: [snap(CAPACITIES, a["capacities"] if caps is None else caps, api_at)],
            LOCATIONS: [snap(LOCATIONS, a["locations"], api_at)],
            TYPES: [snap(TYPES, a["types"], api_at)]}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def test_signing_vectors():
    # (path, query, timestamp) -> signature, from client-go's Go code with the docs' sample secret.
    assert signature(DOC_SECRET, "GET", "/v1/capacities", "product_name=a100.8x&location=us-northcentral1-a",
                     "2022-03-01T01:23:45+09:00") == "iKuNhKlbPR3_RUNRTQJhikY-HNtmGKACr3_GV2LbEJM"
    assert signature(DOC_SECRET, "GET", "/v1/capacities", "", "2026-11-01T20:00:00Z") \
        == "kmM-w48t1ZcjJuMYIaW8x5Hqt-Gbdt9Qrj5tYqNTNAI"
    assert signature(DOC_SECRET, "get", "/v1/capacities",
                     "product_name=l40s-48gb.1x&location=us+east&product_name=a100-80gb.8x",
                     "2026-11-01T20:00:00Z") == "vrq83Xj-sGcfkS4pGKerMqFynZ8T6kbJaD-1fgoxwsY"
    assert signature(DOC_SECRET, "GET", "/v1alpha5/locations", "", "2022-03-01T01:23:45+09:00") \
        == "Dj6Jlqo0Bt-cJnFZiJonG-ogh4oowxA99R0WcZDHD-M", "the version prefix is part of the signed path"


def test_canonical_query():
    assert canonical_query("") == ""
    assert canonical_query("product_name=a100.8x&location=us-northcentral1-a") \
        == "location=us-northcentral1-a&product_name=a100.8x", "keys sorted"
    assert canonical_query("b=2&a=z&b=1") == "a=z&b=2&b=1", "a key's values keep their order"
    assert canonical_query("q=a%20b&&r=c+d&s=%7E") == "q=a+b&r=c+d&s=~", "Go's QueryEscape: space as +, ~ bare"
    assert raises(canonical_query, "a=1;b=2")


def test_auth_headers():
    h = auth_headers(DOC_KID, DOC_SECRET, "GET", "https://api.cloud.crusoe.ai/v1/capacities",
                     now=datetime(2026, 11, 1, 22, 0, tzinfo=timezone(timedelta(hours=2))))
    assert h == {"X-Crusoe-Timestamp": "2026-11-01T20:00:00Z",
                 "Authorization": f"Bearer 1.0:{DOC_KID}:kmM-w48t1ZcjJuMYIaW8x5Hqt-Gbdt9Qrj5tYqNTNAI"}
    assert DOC_SECRET not in str(h), "the secret never leaves the process"


def test_capacity_from_the_api():
    rows = CrusoeProvider.normalize_capacity(with_api())
    by = {(r.sku, r.region): r for r in rows}
    assert len(rows) == 20 == len(by), "one row per (type, location): the API's repeated copies collapse"
    assert all(r.source == CAPACITIES and r.provider == "crusoe" and r.observed_at == T0 for r in rows)
    assert all(r.available_count is None and r.total_count is None and r.count_unit is None for r in rows), \
        "quantity is a yes/no here, never a count"
    assert all(r.canonical_gpu_name for r in rows), "every Crusoe GPU type maps"
    assert sum(r.in_stock for r in rows) == 5
    l40s = by[("l40s-48gb.1x", "us-east1-a")]
    assert (l40s.in_stock, l40s.raw_value, l40s.gpu_count, l40s.country) == (True, "quantity=1", 1, "US")
    assert l40s.raw_gpu_name == "L40S-48GB" and l40s.canonical_gpu_name == "NVIDIA L40S 48GB"
    h100 = by[("h100-80gb-sxm-ib.8x", "eu-iceland1-a")]
    assert (h100.in_stock, h100.country, h100.gpu_count, h100.canonical_gpu_name) == \
        (False, "IS", 8, "NVIDIA H100 80GB SXM5")
    assert by[("mi355x-288gb-roce.8x", "us-east2-a")].canonical_gpu_name == "AMD Instinct MI355X 288GB"
    assert not any(r.sku.startswith(("c1a", "s1a")) for r in rows), "CPU types are skipped"


def test_capacity_joins_to_listings():
    raw = with_api()
    listings = {l.sku: l for l in CrusoeProvider.normalize(raw)}
    rows = CrusoeProvider.normalize_capacity(raw)
    joined = [r for r in rows if r.sku in listings]
    assert len(joined) == 15 and {r.sku for r in rows} - set(listings) == {
        "h100-80gb-sxm-ib.8x", "mi300x-192gb-ib.8x", "mi355x-288gb-roce.8x"}, "skus are the listing skus"
    for r in joined:
        assert r.canonical_gpu_name == listings[r.sku].canonical_gpu_name, r.sku
        assert r.gpu_count == listings[r.sku].gpu_count, r.sku


def test_quantity_edge_cases():
    caps = {"items": [
        {"type": "l40s-48gb.1x", "location": "us-east1-a", "quantity": 0},
        {"type": "l40s-48gb.1x", "location": "us-east1-a", "quantity": 2},   # copies disagree: any > 0 is in stock
        {"type": "l40s-48gb.2x", "location": "us-east1-a"},                  # no quantity: unknown
        {"type": "l40s-48gb.4x", "location": "ca-east1-a", "quantity": True},  # not an integer: unknown
        {"type": "c1a.2x", "location": "us-east1-a", "quantity": 1},
    ]}
    by = {r.sku: r for r in CrusoeProvider.normalize_capacity(with_api(caps=caps))}
    assert set(by) == {"l40s-48gb.1x", "l40s-48gb.2x", "l40s-48gb.4x"}
    assert by["l40s-48gb.1x"].in_stock is True and by["l40s-48gb.1x"].raw_value == "quantity=0,2"
    assert by["l40s-48gb.2x"].in_stock is None and by["l40s-48gb.4x"].in_stock is None
    assert by["l40s-48gb.4x"].country is None, "an unknown location prefix is not guessed"

    # Without the type catalogue, Shadeform's label for a listed type names the GPU; unlisted types are skipped.
    raw = with_api()
    del raw[TYPES]
    rows = CrusoeProvider.normalize_capacity(raw)
    assert {r.sku for r in rows} <= {l.sku for l in CrusoeProvider.normalize(raw)} and len(rows) == 15
    assert all(r.canonical_gpu_name for r in rows)


def test_one_source_per_provider():
    assert all(r.source == CAPACITIES for r in CrusoeProvider.normalize_capacity(with_api(shade_at=T0 + timedelta(hours=5))))
    old = CrusoeProvider.normalize_capacity(with_api(api_at=T0 - timedelta(hours=7)))
    assert old and all(r.source == shadeform.ENDPOINT for r in old), "a long-dead API falls back to Shadeform"
    no_key = {SHADE: [snap(SHADE, shade_payload())]}
    rows = CrusoeProvider.normalize_capacity(no_key)
    assert len(rows) == 31 and all(r.source == shadeform.ENDPOINT for r in rows), "no key: Shadeform's yes/no as before"
    assert CrusoeProvider.normalize(with_api()) == CrusoeProvider.normalize(no_key), "prices never read the API"


def test_fail_closed():
    assert raises(CrusoeProvider.normalize_capacity, with_api(caps={"data": []})), "a reshaped body fails"
    assert CrusoeProvider.normalize_capacity({}) == []


def _serve(status=None, bodies=None):
    """A mock Crusoe + Shadeform that checks every Crusoe signature the way the server would."""
    seen, a = [], api()
    full_caps = {"items": a["capacities"]["items"] + [
        {"type": "c1a.4x", "location": "us-east1-a", "quantity": 1, "num_slices": 1}]}
    replies = {"/v1/capacities": full_caps, "/v1/locations": a["locations"],
               "/v1/organizations/projects": {"items": [{"id": PID, "name": "default"}]},
               f"/v1/projects/{PID}/compute/vms/types": a["types"], **(bodies or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.shadeform.ai":
            return httpx.Response(200, json=shade_payload())
        assert request.url.host == "api.cloud.crusoe.ai" and request.method == "GET"
        ts = request.headers["x-crusoe-timestamp"]
        want = f"Bearer 1.0:kid-test:{signature(DOC_SECRET, 'GET', request.url.path, request.url.query.decode(), ts)}"
        if request.headers.get("authorization") != want:
            return httpx.Response(401, json={"code": "unauthenticated", "message": "bad signature"})
        if status and request.url.path in status:
            return httpx.Response(status[request.url.path], json={"code": "x", "message": "nope"})
        return httpx.Response(200, json=replies[request.url.path])

    def make(kid="kid-test", secret=DOC_SECRET):
        client = httpx.AsyncClient(base_url=shadeform.BASE_URL, transport=httpx.MockTransport(handler))
        return CrusoeProvider(client, access_key_id=kid, secret_key=secret)
    return make, seen


def test_fetch_with_keys():
    make, seen = _serve()
    raw = asyncio.run(make().fetch())
    assert [r.endpoint for r in raw] == [shadeform.ENDPOINT, CAPACITIES, LOCATIONS, TYPES] and all(r.ok for r in raw)
    assert PID not in json.dumps([r.model_dump(mode="json") for r in raw]), "the project id is never stored"
    gpu_rows = [i for i in api()["capacities"]["items"] if not i["type"].startswith(("c1a", "s1a"))]
    assert raw[1].payload == {"items": gpu_rows} and len(gpu_rows) == 27, "GPU rows only, verbatim (CPU rows dropped)"
    assert raw[2].payload == api()["locations"] and raw[3].payload == api()["types"]
    crusoe = [r for r in seen if r.url.host == "api.cloud.crusoe.ai"]
    assert [r.url.path for r in crusoe] == ["/v1/organizations/projects", f"/v1/projects/{PID}/compute/vms/types",
                                            "/v1/capacities", "/v1/locations"]
    assert all(r.method == "GET" for r in seen), "read-only: nothing is ever created"
    assert all("authorization" not in r.headers for r in seen if r.url.host == "api.shadeform.ai"), \
        "the Crusoe keys go only to Crusoe"
    assert not any(DOC_SECRET in str(r.url) + str(dict(r.headers)) for r in seen), "the secret is never sent"


def test_fetch_failures():
    # Bad keys: every Crusoe call is a recorded failure; prices still flow from Shadeform.
    make, _ = _serve()
    raw = asyncio.run(make(secret="AAAAAAAAAAAAAAAAAAAAAA").fetch())
    assert raw[0].ok and [r.ok for r in raw[1:]] == [False, False, False]
    assert all(r.status_code == 401 for r in raw[1:]) and raw[3].endpoint == TYPES
    assert not any(DOC_SECRET in (r.error or "") for r in raw)

    # Zero GPU rows, or a reshaped body: a failure, never an empty market.
    make, _ = _serve(bodies={"/v1/capacities": {"items": [{"type": "c1a.2x", "location": "us-east1-a", "quantity": 1}]}})
    caps = asyncio.run(make().fetch())[1]
    assert not caps.ok and caps.payload is None and "no GPU rows" in caps.error
    make, _ = _serve(bodies={"/v1/capacities": {"data": []}})
    assert not asyncio.run(make().fetch())[1].ok

    # The type catalogue is unreachable: capacity is still stored, every row kept (the normalizer skips CPUs).
    make, _ = _serve(status={"/v1/organizations/projects": 403})
    raw = asyncio.run(make().fetch())
    assert not raw[3].ok and raw[3].endpoint == TYPES and raw[1].ok and len(raw[1].payload["items"]) == 30


def test_fetch_without_keys():
    for kid, secret in ((None, None), ("kid-test", None), (None, DOC_SECRET)):
        make, seen = _serve()
        raw = asyncio.run(make(kid, secret).fetch())
        assert [r.endpoint for r in raw] == [shadeform.ENDPOINT] and all(r.url.host == "api.shadeform.ai" for r in seen)


def test_registry():
    assert PROVIDERS["crusoe"] is CrusoeProvider and mapping.check_mapping_coverage() == {}
    import config
    import fetch
    real = (config.settings.crusoe_access_key_id, config.settings.crusoe_secret_key)
    try:
        config.settings.crusoe_access_key_id = config.settings.crusoe_secret_key = None
        c = [p for p in fetch.build_fetchers() if p.name == "crusoe"]
        assert len(c) == 1 and c[0]._keys is None, "no keys: still polled, via Shadeform"
        config.settings.crusoe_access_key_id, config.settings.crusoe_secret_key = "kid-test", DOC_SECRET
        c = [p for p in fetch.build_fetchers() if p.name == "crusoe"]
        assert len(c) == 1 and c[0]._keys == ("kid-test", DOC_SECRET), "exactly one Crusoe fetcher"
        assert DOC_SECRET not in str(dict(c[0]._client.headers)), "the client itself never carries the secret"
    finally:
        config.settings.crusoe_access_key_id, config.settings.crusoe_secret_key = real


if __name__ == "__main__":
    for t in (test_signing_vectors, test_canonical_query, test_auth_headers, test_capacity_from_the_api,
              test_capacity_joins_to_listings, test_quantity_edge_cases, test_one_source_per_provider,
              test_fail_closed, test_fetch_with_keys, test_fetch_failures, test_fetch_without_keys, test_registry):
        t()
        print(t.__name__, "ok")
    print("all passed")
