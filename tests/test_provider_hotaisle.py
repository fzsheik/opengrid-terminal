"""Hot Aisle: page prices always; with an API key, VM stock counts from its own API.

Fixtures are REAL bodies: hotaisle_pricing.html (the public page) and hotaisle_vm_available.json, the
GET /teams/{handle}/virtual_machines/available/ body fetched 2026-11-01 with an account key (read-only; nothing
was provisioned). That day bare_metal/available answered `null`. The GET /teams/ body below is synthetic (the
spec's example handle), since the real one is account data. No network: every call goes through MockTransport.

Run:  .venv/bin/python tests/test_provider_hotaisle.py
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

import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
from providers import hotaisle  # noqa: E402
from providers.hotaisle import HotAisleProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
HANDLE = "acme-corporation"
TEAMS = [{"handle": HANDLE, "name": "Acme Corporation", "description": "", "roles": ["owner"],
          "effective_roles": ["user"]}]
KEY = "ha-test-key"


def text(name):
    return (FIX / name).read_text(encoding="utf-8")


def vm_body():
    return json.loads(text("hotaisle_vm_available.json"))


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def by_id(listings):
    return {l.listing_id: l for l in listings}


def page_payload():
    return hotaisle.extract(text("hotaisle_pricing.html"))


def api_server(vm=None, bm=None, teams=None, status=None):
    """A mock admin.hotaisle.app/api: routes by path, records (method, path, auth)."""
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        path = request.url.path.removeprefix("/api")
        if status is not None:
            return httpx.Response(status, json={"error": "nope"})
        if path == "/teams/":
            return httpx.Response(200, json=TEAMS if teams is None else teams)
        if path == f"/teams/{HANDLE}/virtual_machines/available/":
            return httpx.Response(200, json=vm_body() if vm is None else vm)
        if path == f"/teams/{HANDLE}/bare_metal/available/":
            return httpx.Response(200, content=b"null" if bm is None else json.dumps(bm).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404, json={"error": "not found"})
    return handle, seen


def keyed(handler, team="Acme Corporation", page_status=200):
    page = httpx.MockTransport(lambda r: httpx.Response(page_status, text=text("hotaisle_pricing.html"),
                                                        headers={"content-type": "text/html"}))
    return HotAisleProvider(
        httpx.AsyncClient(base_url=hotaisle.BASE_URL, transport=page), api_key=KEY, team=team,
        api_client=httpx.AsyncClient(base_url=hotaisle.API_BASE_URL, transport=httpx.MockTransport(handler),
                                     headers={"Authorization": f"Token {KEY}"}))


def run(p):
    return asyncio.run(p.fetch())


def by(raws):
    return {r.endpoint: [snap(r.payload, r.fetched_at)] for r in raws if r.ok}


def test_keyed_fetch_is_read_only_and_stores_no_secrets():
    handler, seen = api_server()
    raws = run(keyed(handler))
    assert [m for m, _, _ in seen] == ["GET"] * 3, "read-only: never a POST (provisioning is billed)"
    assert [p for _, p, _ in seen] == ["/api/teams/", f"/api/teams/{HANDLE}/virtual_machines/available/",
                                       f"/api/teams/{HANDLE}/bare_metal/available/"]
    assert all(a == f"Token {KEY}" for _, _, a in seen), "Authorization: Token <key>"
    assert [r.endpoint for r in raws] == [hotaisle.ENDPOINT, hotaisle.VM_ENDPOINT, hotaisle.BAREMETAL_ENDPOINT]
    assert all(r.ok for r in raws), [r.error for r in raws]
    assert raws[1].payload == vm_body(), "raw first: the VM body as received (OnDemandPrice included)"
    assert raws[2].payload is None, "bare metal null = nothing reservable now; a normal answer"
    dumped = json.dumps([r.model_dump(mode="json") for r in raws])
    assert KEY not in dumped and HANDLE not in dumped and "Acme" not in dumped, "no key, handle or team name stored"


def test_team_resolution():
    teams = TEAMS
    assert hotaisle.resolve_team(teams, HANDLE) == HANDLE
    assert hotaisle.resolve_team(teams, "ACME-corporation") == HANDLE
    assert hotaisle.resolve_team(teams, "acme corporation") == HANDLE, "the display name works too"
    assert hotaisle.resolve_team(teams, "Acme") == HANDLE, "no match but only one team: that team"
    assert hotaisle.resolve_team(teams, None) == HANDLE
    two = teams + [{"handle": "other-team", "name": "Other", "roles": ["user"], "effective_roles": ["user"]}]
    assert hotaisle.resolve_team(two, "Other") == "other-team"
    for bad_teams, want in ((two, "Acme"), (two, None), ([], HANDLE), ({"error": "x"}, HANDLE),
                            ([dict(TEAMS[0], invitation=True)], HANDLE)):
        try:
            hotaisle.resolve_team(bad_teams, want)
            raise AssertionError(f"must refuse {bad_teams!r} / {want!r}")
        except ValueError as exc:
            assert HANDLE not in str(exc) and "other-team" not in str(exc), "errors never name a team"
    # One lookup per process: the handle is cached across polls.
    handler, seen = api_server()
    p = keyed(handler, team=HANDLE)
    run(p), run(p)
    assert [path for _, path, _ in seen].count("/api/teams/") == 1


def test_prices_stay_the_page():
    handler, _ = api_server()
    raws = run(keyed(handler))
    keyed_out = by_id(HotAisleProvider.normalize(by(raws)))
    plain = by_id(HotAisleProvider.normalize({hotaisle.ENDPOINT: by(raws)[hotaisle.ENDPOINT]}))
    assert keyed_out == plain, "the key changes no listing: ids, skus, names and prices continuous with the scraper"
    assert set(plain) == {"mi300x:1x:vm", "mi300x:2x:vm", "mi300x:4x:vm"}
    assert plain["mi300x:4x:vm"].price_per_instance_hour == D("11.96")
    assert all(l.price_per_gpu_hour == D("2.99") and l.available is None for l in plain.values())
    assert hotaisle.check_prices(list(plain.values()), hotaisle.vm_rows(vm_body())) == [], "agreed on 2026-11-01"
    # An API price that differs is reported, never published.
    cheaper = vm_body()
    cheaper[0]["OnDemandPrice"] = 249
    diffs = hotaisle.check_prices(list(plain.values()), hotaisle.vm_rows(cheaper))
    assert diffs == ["MI300x:1x:vm: API 2.49/h, page 2.99/h"], diffs
    out = HotAisleProvider.normalize({hotaisle.ENDPOINT: by(raws)[hotaisle.ENDPOINT], hotaisle.VM_ENDPOINT: [snap(cheaper)]})
    assert by_id(out) == plain and not {str(l.price_per_gpu_hour) for l in out} & {"2.49"}
    # API only (no page stored): no listings rather than API prices.
    assert HotAisleProvider.normalize({hotaisle.VM_ENDPOINT: [snap(vm_body())]}) == []


def test_capacity_joins_page_skus():
    handler, _ = api_server()
    rows = {r.sku: r for r in HotAisleProvider.normalize_capacity(by(run(keyed(handler))))}
    page_skus = {l.sku for l in HotAisleProvider.normalize({hotaisle.ENDPOINT: [snap(page_payload())]})}
    assert set(rows) == page_skus == {"MI300x:1x:vm", "MI300x:2x:vm", "MI300x:4x:vm"}
    one = rows["MI300x:1x:vm"]
    assert (one.in_stock, one.available_count, one.count_unit, one.region, one.country) == (True, 1, "instance", None, "US")
    assert one.canonical_gpu_name == "AMD Instinct MI300X 192GB" and one.source == hotaisle.VM_ENDPOINT
    assert one.raw_value == "Quantity=1; MI300X x1" and "299" not in one.raw_value
    for sku in ("MI300x:2x:vm", "MI300x:4x:vm"):
        r = rows[sku]
        assert r.in_stock is False and r.available_count is None and r.count_unit is None, "not listed: out, count unknown"
    # Quantity 0 is out of stock with a count; a size the page does not price is kept, sku in the page's spelling.
    body = vm_body() + [{"Quantity": 2, "OnDemandPrice": 2392, "MinimumReservationMinutes": 1,
                         "Specs": {"cpu_cores": 104, "ram_capacity": 1, "disk_capacity": 1,
                                   "gpus": [{"count": 8, "manufacturer": "AMD", "model": "MI300X"}]}}]
    body[0] = dict(body[0], Quantity=0)
    rows = {r.sku: r for r in HotAisleProvider.normalize_capacity(
        {hotaisle.ENDPOINT: [snap(page_payload())], hotaisle.VM_ENDPOINT: [snap(body)]})}
    assert rows["MI300x:1x:vm"].in_stock is False and rows["MI300x:1x:vm"].available_count == 0
    assert rows["MI300x:8x:vm"].available_count == 2 and rows["MI300x:8x:vm"].canonical_gpu_name == "AMD Instinct MI300X 192GB"
    # No key (page only), or no page to join to: no capacity.
    assert HotAisleProvider.normalize_capacity({hotaisle.ENDPOINT: [snap(page_payload())]}) == []
    assert HotAisleProvider.normalize_capacity({hotaisle.VM_ENDPOINT: [snap(vm_body())]}) == []
    assert HotAisleProvider.normalize_capacity({}) == []


def test_empty_list_is_sold_out():
    """[] is a valid answer: the endpoint lists only what can be provisioned now, so every page size is out."""
    handler, _ = api_server(vm=[])
    page, vm, bm = run(keyed(handler))
    assert page.ok and vm.ok and vm.payload == [], "an empty list is a successful fetch"
    rows = {r.sku: r for r in HotAisleProvider.normalize_capacity(by([page, vm, bm]))}
    assert set(rows) == {"MI300x:1x:vm", "MI300x:2x:vm", "MI300x:4x:vm"}
    assert all(r.in_stock is False and r.available_count is None for r in rows.values())
    assert len(HotAisleProvider.normalize(by([page, vm, bm]))) == 3, "prices untouched"


def test_api_failures_never_touch_prices():
    """A VM body with no readable type is a failed fetch, never "nothing in stock"; prices carry on."""
    for body in ({"error": "x"}, [{"Quantity": 1}], [{"Quantity": "1", "Specs": {"gpus": []}}]):
        handler, _ = api_server(vm=body)
        page, vm, bm = run(keyed(handler))
        assert page.ok and len(HotAisleProvider.normalize(by([page, vm, bm]))) == 3, body
        assert not vm.ok and vm.error and vm.payload == body, body
        try:
            HotAisleProvider.normalize_capacity({hotaisle.ENDPOINT: [snap(page.payload)], hotaisle.VM_ENDPOINT: [snap(body)]})
            raise AssertionError(f"normalize_capacity must refuse {body!r}")
        except ValueError:
            pass
        assert len(HotAisleProvider.normalize({hotaisle.ENDPOINT: [snap(page.payload)],
                                               hotaisle.VM_ENDPOINT: [snap(body)]})) == 3
    # The live VM endpoint answers `null` when nothing is in stock? Still a failure, never a sell-out.
    handler, _ = api_server()
    p = keyed(handler)
    p._api = httpx.AsyncClient(base_url=hotaisle.API_BASE_URL, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=TEAMS) if r.url.path.endswith("/teams/")
        else httpx.Response(200, content=b"null", headers={"content-type": "application/json"})))
    page, vm, bm = run(p)
    assert not vm.ok and bm.ok, "VM null fails; bare-metal null is a normal answer"
    # Wrong team: the stock call fails, no POST, the page still prices.
    handler, seen = api_server(teams=TEAMS + [{"handle": "b", "name": "B", "roles": [], "effective_roles": []}])
    page, vm = run(keyed(handler, team="nobody"))
    assert page.ok and not vm.ok and "matches none" in vm.error and vm.endpoint == hotaisle.VM_ENDPOINT
    # Bad key: 401 everywhere.
    handler, seen = api_server(status=401)
    page, vm = run(keyed(handler))
    assert page.ok and not vm.ok and "401" in vm.error and all(m == "GET" for m, _, _ in seen)
    # A 404 on the VM call drops the cached handle so the next poll looks the team up again.
    handler, _ = api_server()
    p = keyed(handler, team=HANDLE)
    run(p)
    p._handle = "renamed-team"
    page, vm, bm = run(p)
    assert vm.status_code == 404 and not vm.ok and p._handle is None and "renamed-team" not in (vm.error or "")
    # Network down: a failed response, prices unaffected.
    p = keyed(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    page, vm = run(p)
    assert page.ok and not vm.ok and vm.payload is None
    # A broken page fails prices whatever the API says; the API never stands in.
    handler, _ = api_server()
    page, vm, bm = run(keyed(handler, page_status=503))
    assert not page.ok and vm.ok and HotAisleProvider.normalize(by([page, vm, bm])) == []


def test_no_key_is_the_scraper():
    p = HotAisleProvider(httpx.AsyncClient(base_url=hotaisle.BASE_URL, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=text("hotaisle_pricing.html")))))
    raws = run(p)
    assert p._api is None and [r.endpoint for r in raws] == [hotaisle.ENDPOINT] and raws[0].payload == page_payload()
    from config import settings
    ha = next(f for f in fetch_mod.build_fetchers() if f.name == "hotaisle")
    assert str(ha._client.base_url).startswith(hotaisle.BASE_URL), "the price page, key or no key"
    assert (ha._api is not None) == bool(settings.hotaisle_api_key), "a key adds the stock calls"
    assert [f.name for f in fetch_mod.build_fetchers()].count("hotaisle") == 1


def test_keyed_pipeline():
    """A keyed poll through storage: page prices listed, API stock in capacity, joined by sku."""
    import capacity
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql

    db = "opengrid_test_hotaisle_keyed"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        handler, _ = api_server()
        fetch_mod.save([r.model_copy(update={"fetched_at": now - timedelta(minutes=5)}) for r in run(keyed(handler))])
        normalize.refresh(["hotaisle"])
        stored = capacity.save(capacity.normalize_all(["hotaisle"]))
        with Session() as s:
            listed = dict(s.execute(sql("SELECT sku, price_per_gpu_hour FROM compute_listings "
                                        "WHERE provider = 'hotaisle'")).all())
            cap = dict(s.execute(sql("SELECT sku, available_count FROM capacity_current "
                                     "WHERE provider = 'hotaisle'")).all())
        assert listed == {f"MI300x:{n}x:vm": D("2.99") for n in (1, 2, 4)}, listed
        assert stored["current"] == 3 and set(cap) == set(listed) and cap["MI300x:1x:vm"] == 1, cap
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_keyed_fetch_is_read_only_and_stores_no_secrets, test_team_resolution, test_prices_stay_the_page,
              test_capacity_joins_page_skus, test_empty_list_is_sold_out, test_api_failures_never_touch_prices, test_no_key_is_the_scraper,
              test_keyed_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
