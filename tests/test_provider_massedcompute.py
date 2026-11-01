"""Massed Compute: the official gpu-inventory API (with MASSEDCOMPUTE_API_KEY), and the page + Shadeform without it.

Fixtures:
    fixtures_providers/massedcompute_gpu_inventory.json  GET vm.massedcompute.com/api/v1/gpu-inventory, fetched
                                                         2026-11-01, trimmed to 21 products (one CPU product kept)
    fixtures_capacity/massedcompute.json                 the /pricing page extract + Shadeform rows (the no-key path)

Every network call goes through httpx.MockTransport; scratchdb refuses real hosts.

Run:  .venv/bin/python tests/test_provider_massedcompute.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (blanks real keys, refuses real network)

import httpx  # noqa: E402

import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS, shadeform  # noqa: E402
from providers.massedcompute import API_ENDPOINT, ENDPOINT, BASE_URL, MassedComputeProvider  # noqa: E402

HERE = Path(__file__).resolve().parent
T0 = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
SHADE = shadeform.BASE_URL + shadeform.ENDPOINT


def inventory() -> dict:
    return json.loads((HERE / "fixtures_providers" / "massedcompute_gpu_inventory.json").read_text())


def page_and_shadeform() -> dict:
    return json.loads((HERE / "fixtures_capacity" / "massedcompute.json").read_text())


def snap(ep, payload, at=T0):
    return SimpleNamespace(endpoint=ep, payload=payload, fetched_at=at)


def api_only(payload=None, at=T0):
    return {API_ENDPOINT: [snap(API_ENDPOINT, inventory() if payload is None else payload, at)]}


def no_key(at=T0):
    raw = page_and_shadeform()
    return {ep: [snap(ep, p, at) for p in payloads] for ep, payloads in raw.items()}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def test_normalize_api():
    out = {l.listing_id: l for l in MassedComputeProvider.normalize(api_only())}
    assert len(out) == 20, "20 GPU products; the CPU product is skipped"
    a6000 = out["RTX A6000 (48GB):1x"]
    assert a6000.price_per_instance_hour == D("0.57") and a6000.price_per_gpu_hour == D("0.57")
    assert a6000.canonical_gpu_name == "NVIDIA RTX A6000 48GB" and a6000.gpu_count == 1 and a6000.region is None
    assert (a6000.vcpu, a6000.ram_gb, a6000.storage_gb) == (6, 48.0, 256.0)
    assert a6000.market_type == "on_demand" and not a6000.interruptible and a6000.available is None
    b300 = out["B300 SXM6:8x"]
    assert b300.price_per_instance_hour == D("52.80") and b300.price_per_gpu_hour == D("6.60")
    assert b300.canonical_gpu_name == "NVIDIA B300 288GB SXM"
    spot = out["L40 (48GB):1x:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.raw_gpu_name == "L40 (48GB)"
    assert spot.price_per_instance_hour == D("0.78") and spot.canonical_gpu_name == "NVIDIA L40 48GB"
    assert spot.sku == "L40 (48GB):1x:spot", "spot keeps its own sku, so capacity rows never collide with on-demand"
    assert not any(l.raw_gpu_name.lower().startswith(("mini", "small")) or "EPYC" in l.raw_gpu_name for l in out.values())


def test_continuity_with_the_page():
    """Every API product the page also prints keeps the page's listing id, GPU name, mapping, price and shape."""
    api = {l.listing_id: l for l in MassedComputeProvider.normalize(api_only())}
    page = {l.listing_id: l for l in MassedComputeProvider.normalize(
        {ENDPOINT: [snap(ENDPOINT, page_and_shadeform()[ENDPOINT][0])]})}
    shared = set(api) & set(page)
    assert len(shared) == 17 and set(api) - shared == {"L40 (48GB):1x:spot", "L40 (48GB):2x:spot", "RTX A6000 (48GB):1x:spot"}, set(api) - shared
    for k in shared:
        a, p = api[k], page[k]
        assert (a.sku, a.raw_gpu_name, a.canonical_gpu_name, a.gpu_count) == (p.sku, p.raw_gpu_name, p.canonical_gpu_name, p.gpu_count), k
        assert a.price_per_instance_hour == p.price_per_instance_hour, (k, a.price_per_instance_hour, p.price_per_instance_hour)


def test_capacity_counts():
    rows = {r.sku: r for r in MassedComputeProvider.normalize_capacity(api_only())}
    listed = {l.sku for l in MassedComputeProvider.normalize(api_only())}
    assert set(rows) == listed, "capacity skus are the listing skus"
    assert all(r.provider == "massedcompute" and r.source == API_ENDPOINT and r.region is None for r in rows.values())
    a = rows["RTX A6000 (48GB):1x"]
    assert (a.in_stock, a.available_count, a.total_count, a.count_unit) == (True, 14, None, "instance")
    assert a.raw_value == "gpu_1x_a6000: capacity_available=14; regions=us-central-1,us-central-2,us-central-3"
    h100 = rows["H100 (80GB):1x"]
    assert (h100.in_stock, h100.available_count) == (False, 0) and h100.raw_value.endswith("regions=-")
    assert rows["L40 (48GB):2x:spot"].available_count == 19 and rows["B300 SXM6:8x"].gpu_count == 8
    assert sum(r.in_stock for r in rows.values()) == 11

    # A count the API leaves out: in_stock falls back to the region list, the count stays unknown.
    body = inventory()
    del body["gpu_inventory"]["gpu_1x_a6000"]["capacity_available"]
    a = next(r for r in MassedComputeProvider.normalize_capacity(api_only(body)) if r.sku == "RTX A6000 (48GB):1x")
    assert (a.in_stock, a.available_count, a.count_unit) == (True, None, None)


def test_one_source_per_poll():
    both = {**no_key(at=T0 - timedelta(hours=1)), **api_only(at=T0)}
    caps = MassedComputeProvider.normalize_capacity(both)
    assert caps and all(r.source == API_ENDPOINT for r in caps), "API newer: never also Shadeform's rows"
    assert any(l.market_type == "spot" for l in MassedComputeProvider.normalize(both)), "API newer: API prices"

    # The key was removed: the page and Shadeform are newer than the last API snapshot, so they win.
    older_api = {**no_key(at=T0), **api_only(at=T0 - timedelta(hours=1))}
    caps = MassedComputeProvider.normalize_capacity(older_api)
    assert caps and all(r.source == shadeform.ENDPOINT for r in caps)
    assert len(MassedComputeProvider.normalize(older_api)) == 74 and not any(
        l.market_type == "spot" for l in MassedComputeProvider.normalize(older_api))

    # The API failed this poll (only the page refetched): prices from the page, capacity keeps the last API state.
    api_failed = {**api_only(at=T0 - timedelta(minutes=15)), ENDPOINT: [snap(ENDPOINT, page_and_shadeform()[ENDPOINT][0], T0)]}
    assert len(MassedComputeProvider.normalize(api_failed)) == 74
    assert all(r.source == API_ENDPOINT for r in MassedComputeProvider.normalize_capacity(api_failed))


def test_fail_closed():
    assert MassedComputeProvider.normalize({}) == [] and MassedComputeProvider.normalize_capacity({}) == []
    assert raises(MassedComputeProvider.normalize, api_only({"data": []})), "reshaped body"
    assert raises(MassedComputeProvider.normalize_capacity, api_only({"data": []}))
    cpu_only = {"gpu_inventory": {k: v for k, v in inventory()["gpu_inventory"].items() if k.startswith("cpu_")}}
    assert raises(MassedComputeProvider.normalize, api_only(cpu_only)), "zero GPU products is a failure, not an empty market"


def _serve(api_status=200, api_body=None):
    seen = []
    page = (HERE / "fixtures_providers" / "massedcompute_pricing_min.html")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.shadeform.ai":
            return httpx.Response(200, json={"instance_types": page_and_shadeform()[SHADE][0]["instance_types"]})
        if request.url.path == API_ENDPOINT:
            if request.headers.get("authorization") != "Bearer k-test":
                return httpx.Response(401, json={"error": "Unauthorized"})
            if api_status == 307:
                return httpx.Response(307, headers={"location": f"{BASE_URL}/login?callbackUrl=%2Fapi"})
            return httpx.Response(api_status, json=inventory() if api_body is None else api_body)
        if request.url.path == "/login":
            return httpx.Response(200, text="<html>login</html>")
        if request.url.path == ENDPOINT:
            return httpx.Response(200, text=page.read_text())
        return httpx.Response(404)

    def make(key):
        client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler), follow_redirects=True)
        return MassedComputeProvider(client, api_key=key)
    return make, seen


def test_fetch_with_key():
    make, seen = _serve()
    raw = asyncio.run(make("k-test").fetch())
    assert len(raw) == 1 and raw[0].ok and raw[0].endpoint == API_ENDPOINT
    assert raw[0].payload == inventory(), "stored verbatim"
    assert [r.url.path for r in seen] == [API_ENDPOINT], "no page, no Shadeform: one source"
    assert "k-test" not in str(seen[0].url), "the token travels in a header, never the URL"


def test_fetch_failures_fall_back_to_the_page():
    for status, body in ((307, None), (401, None), (200, {"gpu_inventory": {}})):
        make, seen = _serve(api_status=status, api_body=body)
        raw = asyncio.run(make("k-test").fetch())
        assert [r.endpoint for r in raw] == [API_ENDPOINT, ENDPOINT], status
        assert not raw[0].ok, f"{status}: recorded as a failure"
        assert raw[1].ok and len(raw[1].payload["shapes"]) == 2, "the page keeps prices flowing"
        assert "/login" not in [r.url.path for r in seen], "a redirect to /login is never followed"
        assert all("authorization" not in r.headers for r in seen if r.url.path != API_ENDPOINT), \
            "the token goes only to the API"
        assert "k-test" not in (raw[0].error or "")


def test_fetch_without_key():
    make, seen = _serve()
    raw = asyncio.run(make(None).fetch())
    assert [r.endpoint for r in raw] == [ENDPOINT, SHADE] and all(r.ok for r in raw)
    assert API_ENDPOINT not in [r.url.path for r in seen]
    assert all(t["cloud"] == "massedcompute" for t in raw[1].payload["instance_types"])


def test_registry():
    assert PROVIDERS["massedcompute"] is MassedComputeProvider
    assert mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("massedcompute")
    assert (m.source_type, m.source) == ("authenticated_api", "vm.massedcompute.com")
    import config
    import fetch
    real = config.settings.massedcompute_api_key
    try:
        config.settings.massedcompute_api_key = None
        mc = [p for p in fetch.build_fetchers() if isinstance(p, MassedComputeProvider)]
        assert len(mc) == 1 and mc[0]._api_key is None, "no key: the page scraper, still polled"
        config.settings.massedcompute_api_key = "k-test"
        mc = [p for p in fetch.build_fetchers() if isinstance(p, MassedComputeProvider)]
        assert len(mc) == 1 and mc[0]._api_key == "k-test", "exactly one Massed Compute fetcher"
        assert "k-test" not in str(dict(mc[0]._client.headers)), "the client itself never carries the token"
    finally:
        config.settings.massedcompute_api_key = real


if __name__ == "__main__":
    for t in (test_normalize_api, test_continuity_with_the_page, test_capacity_counts, test_one_source_per_poll,
              test_fail_closed, test_fetch_with_key, test_fetch_failures_fall_back_to_the_page, test_fetch_without_key,
              test_registry):
        t()
        print(t.__name__, "ok")
    print("all passed")
