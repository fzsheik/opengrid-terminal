"""Capacity: the three provider normalizers on saved real responses (one per shape: tier, boolean, count), Linode's
paging, change-only storage, fail-open behaviour, and the /v1/capacity endpoints.

Run:  .venv/Scripts/python tests/test_capacity.py
"""

import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from models import CapacityObservation  # noqa: E402
from providers.koyeb import KoyebProvider  # noqa: E402
from providers.linode import LinodeProvider  # noqa: E402
from providers.lium import LiumProvider  # noqa: E402
from providers import PROVIDERS  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_capacity"
SUPPORTED = ["boostrun", "clore", "crusoe", "deepinfra", "denvr", "digitalocean", "horizon", "hotaisle", "hyperbolic", "hyperstack", "jarvislabs",
             "koyeb", "lambda", "latitude", "linode", "lium", "massedcompute", "phyntec", "runpod", "salad", "scaleway",
             "thundercompute", "vast", "verda", "voltagepark"]
DB = "og_test_capacity"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _by_endpoint(name: str, edit=None) -> dict:
    raw = json.loads((FIX / f"{name}.json").read_text())
    if edit:
        edit(raw)
    return {ep: [SimpleNamespace(payload=p, fetched_at=T0, endpoint=ep) for p in payloads] for ep, payloads in raw.items()}


def _setup_db():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    return Session


# --------------------------------------------------------------------------- one provider per shape

def test_koyeb_tier():
    rows = KoyebProvider.normalize_capacity(_by_endpoint("koyeb"))
    assert len(rows) == 25 and all(r.provider == "koyeb" for r in rows)
    assert sum(1 for r in rows if r.region is None) == 7, "types with no per-region word are stored provider-wide"
    assert {r.tier for r in rows} <= {"low", "medium", "high"} and all(r.in_stock is True for r in rows)
    assert all(r.available_count is None and r.total_count is None for r in rows), "a tier never invents counts"
    a100 = next(r for r in rows if r.sku == "2-gpu-nvidia-a100-sxm")
    assert a100.gpu_count == 2 and a100.canonical_gpu_name == "NVIDIA A100 80GB SXM4" and a100.raw_value == "LOW"
    assert len(KoyebProvider.normalize(_by_endpoint("koyeb"))) == 15, "the listing path is unchanged"

    def unknown_word(raw):
        u = raw["/v1/catalog/usage"][0]["usage"]["instances"]["2-gpu-nvidia-a100-sxm"]
        u["regions"]["na"]["availability"] = "SOLD_OUT_SOMEHOW"
    r = next(r for r in KoyebProvider.normalize_capacity(_by_endpoint("koyeb", unknown_word))
             if r.sku == "2-gpu-nvidia-a100-sxm" and r.region == "na")
    assert r.in_stock is None and r.tier is None and r.raw_value == "SOLD_OUT_SOMEHOW", "an unknown word is never guessed"

    def reshaped(raw):
        raw["/v1/catalog/usage"][0] = {"usage": "nope"}
    try:
        KoyebProvider.normalize_capacity(_by_endpoint("koyeb", reshaped))
        raise AssertionError("a reshaped usage payload must raise")
    except ValueError:
        pass
    assert KoyebProvider.normalize_capacity({}) == [], "no raw yet: nothing, not an error"


def test_linode_boolean():
    rows = LinodeProvider.normalize_capacity(_by_endpoint("linode"))
    assert len(rows) == 429 and sum(r.in_stock for r in rows) == 43
    assert all(r.tier is None and r.available_count is None for r in rows), "a boolean is only in_stock"
    assert all(r.raw_value in ("true", "false") for r in rows) and all("gpu" in r.sku for r in rows)
    assert len({(r.sku, r.region) for r in rows}) == len(rows), "one row per (plan, region)"
    assert len(LinodeProvider.normalize(_by_endpoint("linode"))) == 13, "the listing path is unchanged"

    def reshaped(raw):
        key = next(k for k in raw if "regions/availability" in k)
        raw[key][0] = {"data": "nope"}
    try:
        LinodeProvider.normalize_capacity(_by_endpoint("linode", reshaped))
        raise AssertionError("a reshaped availability payload must raise")
    except ValueError:
        pass


def test_lium_count():
    rows = LiumProvider.normalize_capacity(_by_endpoint("lium"))
    raw = json.loads((FIX / "lium.json").read_text())["/api/executors"][0]
    usable = [e for e in raw if e.get("machine_name") and isinstance(e.get("gpu_count"), int) and e["gpu_count"] > 0
              and isinstance(e.get("available_gpu_count"), int)]
    assert len(rows) == 68 and all(r.count_unit == "gpu" for r in rows)
    assert sum(r.total_count for r in rows) == sum(e["gpu_count"] for e in usable), "counts are summed, none lost"
    assert sum(r.available_count for r in rows) == sum(e["available_gpu_count"] for e in usable)
    assert all(0 <= r.available_count <= r.total_count and r.in_stock == (r.available_count > 0) for r in rows)
    assert all(r.raw_value == f"{r.available_count}/{r.total_count}" for r in rows)


# --------------------------------------------------------------------------- Linode paging

def test_linode_paging():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        page = int(req.url.params.get("page", "1"))
        if page == 3:
            return httpx.Response(503, text="busy")
        rows = [{"region": f"r{page}", "plan": "g1-gpu-rtx6000-1", "available": True},
                {"region": f"r{page}", "plan": "g6-standard-2", "available": True}]
        return httpx.Response(200, json={"data": rows, "page": page, "pages": 2 if "two" in calls[0] else 3})

    async def go(tag):
        p = LinodeProvider(httpx.AsyncClient(base_url=f"https://api.linode.com/{tag}", transport=httpx.MockTransport(handler)))
        try:
            return await p._availability({"g1-gpu-rtx6000-1"})
        finally:
            await p.aclose()
    full = asyncio.run(go("two"))
    assert full.ok and full.payload["pages"] == 2 and len(full.payload["data"]) == 2, "both pages, GPU plans only"
    assert all(r["plan"] == "g1-gpu-rtx6000-1" for r in full.payload["data"])
    calls.clear()
    partial = asyncio.run(go("three"))
    assert not partial.ok and partial.status_code == 503, "a failed page is stored as a failure, never a partial set"


# --------------------------------------------------------------------------- storage

def _obs(sku="h100", region="na", in_stock=True, tier="low", avail=None, total=None, at=T0, provider="koyeb"):
    return CapacityObservation(provider=provider, sku=sku, region=region, raw_gpu_name="H100", canonical_gpu_name=None,
                               gpu_count=1, in_stock=in_stock, tier=tier, available_count=avail, total_count=total,
                               raw_value=str(tier), source="/x", observed_at=at)


def test_change_only_storage_and_fail_open():
    Session = _setup_db()
    try:
        import capacity
        from sqlalchemy import func, select
        from store.capacity import CapacityChange, CapacityCurrent

        count = lambda m: Session().scalar(select(func.count()).select_from(m))  # noqa: E731
        assert capacity.save([_obs(), _obs(region=None)]) == {"current": 2, "changes": 2}
        assert capacity.save([_obs(at=T0 + timedelta(minutes=15)), _obs(region=None, at=T0 + timedelta(minutes=15))]) \
            == {"current": 2, "changes": 0}, "nothing moved: no history row"
        with Session() as s:
            row = s.get(CapacityCurrent, ("koyeb", "h100", "na"))
            assert row.observed_at == T0 + timedelta(minutes=15) and row.changed_at == T0 and row.first_seen_at <= datetime.now(timezone.utc)
            assert s.get(CapacityCurrent, ("koyeb", "h100", "")).region is None, "provider-wide rows key on ''"
        assert capacity.save([_obs(tier="medium", at=T0 + timedelta(minutes=30))])["changes"] == 1
        with Session() as s:
            assert s.get(CapacityCurrent, ("koyeb", "h100", "na")).changed_at == T0 + timedelta(minutes=30)
        assert capacity.save([_obs(sku="x", tier=None, in_stock=False), _obs(sku="x", tier="low", in_stock=True)])["changes"] == 1, \
            "the last observation for a key wins within one batch"
        assert count(CapacityChange) == 4 and count(CapacityCurrent) == 3
        assert capacity.save([]) == {"current": 0, "changes": 0}

        # fail open: one provider's normalizer blowing up never stops the others
        real_latest, real = normalize._latest_raw, {n: c.normalize_capacity for n, c in capacity.PROVIDERS.items()}
        normalize._latest_raw = lambda s, p: {}
        try:
            capacity.PROVIDERS["koyeb"].normalize_capacity = staticmethod(lambda by: (_ for _ in ()).throw(RuntimeError("boom")))
            capacity.PROVIDERS["lium"].normalize_capacity = staticmethod(lambda by: [_obs(provider="lium", sku="a")])
            out = capacity.normalize_all()
            assert [o.provider for o in out] == ["lium"]
        finally:
            normalize._latest_raw = real_latest
            for n, f in real.items():
                capacity.PROVIDERS[n].normalize_capacity = staticmethod(f)
        assert capacity.supported() == SUPPORTED
    finally:
        scratchdb.drop(DB)


def test_refresh_drops_rows_from_a_source_the_provider_left():
    Session = _setup_db()
    try:
        import capacity
        from sqlalchemy import select
        from store.capacity import CapacityCurrent

        def obs(sku, region, source, provider="koyeb"):
            return _obs(sku=sku, region=region, provider=provider).model_copy(update={"source": source})

        real_latest, real = normalize._latest_raw, {n: c.normalize_capacity for n, c in capacity.PROVIDERS.items()}
        normalize._latest_raw = lambda s, p: {}
        try:
            for n in capacity.PROVIDERS:
                capacity.PROVIDERS[n].normalize_capacity = staticmethod(lambda by: [])
            capacity.PROVIDERS["koyeb"].normalize_capacity = staticmethod(
                lambda by: [obs("old", "r1", "via shadeform"), obs("old", "r2", "via shadeform")])
            capacity.PROVIDERS["lium"].normalize_capacity = staticmethod(lambda by: [obs("a", None, "lium api", "lium")])
            assert capacity.refresh()["pruned"] == 0
            # the provider moves to its own API: the Shadeform rows go, lium is untouched
            capacity.PROVIDERS["koyeb"].normalize_capacity = staticmethod(lambda by: [obs("new", None, "own api")])
            assert capacity.refresh()["pruned"] == 2
            with Session() as s:
                keys = {(r.provider, r.sku, r.region_key) for r in s.scalars(select(CapacityCurrent))}
            assert keys == {("koyeb", "new", ""), ("lium", "a", "")}, keys
            # same source, a SKU missing from this run: kept (it ages into stale), not dropped
            capacity.PROVIDERS["koyeb"].normalize_capacity = staticmethod(lambda by: [obs("other", None, "own api")])
            assert capacity.refresh()["pruned"] == 0
            with Session() as s:
                assert s.get(CapacityCurrent, ("koyeb", "new", "")) is not None
            # a normalizer returning nothing keeps everything
            capacity.PROVIDERS["koyeb"].normalize_capacity = staticmethod(lambda by: [])
            assert capacity.refresh()["pruned"] == 0
        finally:
            normalize._latest_raw = real_latest
            for n, f in real.items():
                capacity.PROVIDERS[n].normalize_capacity = staticmethod(f)
    finally:
        scratchdb.drop(DB)


def test_refresh_never_blocked_by_capacity():
    Session = _setup_db()
    try:
        import capacity
        real = capacity.refresh
        capacity.refresh = lambda only=None: (_ for _ in ()).throw(RuntimeError("capacity down"))
        try:
            out = normalize.refresh()
        finally:
            capacity.refresh = real
        assert out["capacity"] == {"error": "capacity refresh failed"} and "listings" in out
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------- API

def test_endpoints():
    Session = _setup_db()
    try:
        import config
        config.settings.app_password = None
        import capacity
        import main
        from fastapi.testclient import TestClient

        now = datetime.now(timezone.utc)
        day_old = now - timedelta(days=1)
        capacity.save([o.model_copy(update={"observed_at": day_old}) for o in LinodeProvider.normalize_capacity(_by_endpoint("linode"))]
                      + [o.model_copy(update={"observed_at": now}) for o in KoyebProvider.normalize_capacity(_by_endpoint("koyeb"))])
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        body = client.get("/v1/capacity").json()
        assert body["meta"]["methodology"] == "/methodology/capacity" and body["meta"]["kind"] == "observed"
        assert body["meta"]["count"] == 429 + 25 and body["meta"]["providers"] == SUPPORTED
        lin = client.get("/v1/capacity", params={"provider": "linode", "in_stock": "true"}).json()["data"]
        assert len(lin) == 43 and all(r["in_stock"] and r["stale"] for r in lin), "day-old rows are older than 3 polls: stale"
        koy = client.get("/v1/capacity", params={"provider": "koyeb"}).json()["data"]
        assert koy and not any(r["stale"] for r in koy) and all(r["tier"] for r in koy)
        a100 = client.get("/v1/capacity", params={"gpu": "NVIDIA A100 80GB SXM4"}).json()["data"]
        assert a100 and all(r["gpu"] == "NVIDIA A100 80GB SXM4" for r in a100)
        assert client.get("/v1/capacity", params={"provider": "aws"}).status_code == 404
        assert client.get("/v1/capacity", params={"gpu": "not-a-gpu"}).status_code == 404
        h = client.get("/v1/capacity/history", params={"provider": "koyeb", "days": 90}).json()
        assert h["meta"]["count"] == 25 and h["data"][0]["observed_at"] >= h["data"][-1]["observed_at"]
        assert client.get("/v1/capacity/history", params={"days": 91}).status_code == 422
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------- the other eleven, from data most already fetched

def _cap(name, edit=None):
    return PROVIDERS[name].normalize_capacity(_by_endpoint(name, edit))


def _common(name, rows):
    assert rows and all(r.provider == name and r.gpu_count >= 1 and r.raw_value for r in rows), name
    assert len({(r.sku, r.region) for r in rows}) == len(rows), f"{name}: one row per (sku, region)"
    assert all(r.tier is None or r.in_stock for r in rows), f"{name}: a tier always means in stock"


def test_lambda_where_not_how_much():
    rows = _cap("lambda")
    _common("lambda", rows)
    assert len(rows) == 23 and sum(r.in_stock for r in rows) == 2
    assert all((r.region is None) == (r.in_stock is False) for r in rows), "listed region = in stock; none listed = one False row"
    assert all(r.available_count is None for r in rows), "Lambda never says how much"


def test_hyperstack_vm_counts():
    rows = _cap("hyperstack")
    _common("hyperstack", rows)
    assert len(rows) == 80 and sum(1 for r in rows if r.in_stock) == 15
    counted = [r for r in rows if r.available_count is not None]
    assert len(counted) == 70 and all(r.count_unit == "instance" and r.total_count is None for r in counted)
    assert all(r.raw_value.startswith("stock_available=") for r in rows)
    sizes = {r.sku: r.gpu_count for r in rows}
    assert any(n == 8 for n in sizes.values()) and any(n == 1 for n in sizes.values())

    def reshaped(raw):
        raw[next(k for k in raw if "/core/stocks" in k)][0] = {"stocks": "nope"}
    try:
        _cap("hyperstack", reshaped)
        raise AssertionError("a reshaped stock payload must raise")
    except ValueError:
        pass


def test_digitalocean_regions():
    rows = _cap("digitalocean")
    _common("digitalocean", rows)
    assert len(rows) == 24 and sum(1 for r in rows if r.in_stock) == 14
    assert all(r.in_stock is not True for r in rows if r.region is None), "no region listed: never claimed in stock"


def test_runpod_tiers_per_pod_size():
    rows = _cap("runpod")
    _common("runpod", rows)
    assert len(rows) == 75 and {r.tier for r in rows} == {"high", "medium", "low"}
    assert all(r.sku.count(":") == 2 and r.sku.endswith(f":{r.gpu_count}") and r.region is None for r in rows)

    saved = json.loads((FIX / "runpod.json").read_text())
    offered = next(g["id"] for g in saved[next(iter(saved))][0]["data"]["gpuTypes"]
                   if all((g.get(k) or {}).get("uninterruptablePrice") is not None for k in ("secure_1", "secure_2")))

    def none_word(raw):
        gpu = next(g for g in raw[next(iter(raw))][0]["data"]["gpuTypes"] if g["id"] == offered)
        gpu["secure_1"]["stockStatus"] = "None"   # the word: no stock
        gpu["secure_2"]["stockStatus"] = None     # missing: not reported
    edited = {r.sku: r for r in _cap("runpod", none_word)}
    one, two = edited[f"{offered}:secure:1"], edited[f"{offered}:secure:2"]
    assert one.in_stock is False and one.tier is None and one.raw_value == "None"
    assert two.in_stock is None and two.tier is None, "the word None is out of stock; a missing value is unknown"


def test_scaleway_own_stock_levels():
    rows = _cap("scaleway")
    _common("scaleway", rows)
    assert len(rows) == 29 and {r.tier for r in rows if r.tier} == {"high", "low"}
    assert all(r.in_stock is False for r in rows if r.raw_value == "shortage")
    assert all(r.region and r.region[:2] in ("fr", "nl", "pl") and r.country == r.region[:2].upper() for r in rows)
    assert all(r.source.endswith("/products/servers/availability") for r in rows)

    def unknown(raw):
        ep = next(k for k, v in raw.items() if k.endswith("/availability") and v[0]["servers"])
        sku = next(iter(raw[ep][0]["servers"]))
        raw[ep][0]["servers"][sku]["availability"] = "maybe"
    assert any(r.raw_value == "maybe" and r.in_stock is None and r.tier is None for r in _cap("scaleway", unknown))
    assert not PROVIDERS["scaleway"].normalize_capacity(_by_endpoint("crusoe")), "Scaleway never reads Shadeform's yes/no"


def test_shadeform_clouds_booleans():
    for name, n in (("crusoe", 31), ("latitude", 17), ("denvr", 6), ("boostrun", 9), ("horizon", 2), ("phyntec", 2)):
        rows = _cap(name)
        _common(name, rows)
        assert len(rows) == n and all(isinstance(r.in_stock, bool) and r.raw_value in ("true", "false") for r in rows), name
        listed = {l.sku for l in PROVIDERS[name].normalize(_by_endpoint(name))}
        assert {r.sku for r in rows} & listed, f"{name}: capacity skus join to listing skus"


def test_massedcompute_via_shadeform():
    rows = _cap("massedcompute")
    _common("massedcompute", rows)
    assert len(rows) == 166 and sum(r.in_stock for r in rows) == 19
    assert all(isinstance(r.in_stock, bool) and r.source == "/v1/instances/types" for r in rows), "Shadeform's yes/no"
    assert len(PROVIDERS["massedcompute"].normalize(_by_endpoint("massedcompute"))) == 74, "prices still from the page only"


def test_hyperbolic_missing_total_is_unknown():
    rows = _cap("hyperbolic")
    _common("hyperbolic", rows)
    assert len(rows) == 4 and all(r.in_stock is None and r.available_count is None for r in rows), \
        "totalAvailable missing: unknown, never zero"

    def counted(raw):
        opts = raw[next(iter(raw))][0]
        opts[0]["totalAvailable"], opts[1]["totalAvailable"], opts[1]["enabled"] = 16, 8, False
    a, b = _cap("hyperbolic", counted)[:2]
    assert a.in_stock is True and a.available_count == 16 and a.count_unit == "gpu"
    assert b.in_stock is False and b.available_count == 8, "a disabled option is not in stock whatever its count"


def test_voltagepark_counts_and_locations():
    rows = _cap("voltagepark")
    _common("voltagepark", rows)
    bare = [r for r in rows if ":bare-metal:" in r.sku]
    assert bare and all(r.available_count is not None and r.count_unit == "gpu" and r.region.startswith("location:") for r in bare)
    assert all(r.in_stock == (r.available_count > 0) for r in bare)
    vms = [r for r in rows if ":vm:" in r.sku]
    assert all((r.region is None) == (r.in_stock is False) for r in vms), "listed location = in stock; none = one False row"


def _vast(edit=None):
    calls = json.loads((FIX / "vast_census.json").read_text())
    if edit:
        edit(calls)
    from providers.vast import ENDPOINT
    return {ENDPOINT: [SimpleNamespace(payload=c["payload"], request=c["request"], fetched_at=T0) for c in calls]}


def test_vast_census_free_and_total():
    from providers.vast import CENSUS_CAP, VastProvider
    rows = {r.sku: r for r in VastProvider.normalize_capacity(_vast())}
    assert set(rows) == {"H100 SXM", "B200"}
    for r in rows.values():
        assert r.region is None and r.count_unit == "gpu" and r.in_stock == (r.available_count > 0)
        assert 0 < r.available_count < r.total_count, r.raw_value
    h = rows["H100 SXM"]
    assert h.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h.available_count == 9 and h.total_count == 101, h.raw_value
    assert VastProvider.normalize(_vast()) == [], "census rows never become price listings"

    # Free = distinct GPU ids in rentable offers; a busy 2x slice holding one of them changes neither count.
    def overlap(calls):
        free = next(c for c in calls if c["request"]["gpu_name"] == "B200" and c["request"]["rentable"])
        busy = next(c for c in calls if c["request"]["gpu_name"] == "B200" and not c["request"]["rentable"])
        o = free["payload"][0]
        busy["payload"].append({**o, "rentable": False, "num_gpus": o["num_gpus"] * 2, "gpu_frac": o["gpu_frac"] * 2,
                                "gpu_ids": o["gpu_ids"] * 2})
    b = next(r for r in VastProvider.normalize_capacity(_vast(overlap)) if r.sku == "B200")
    assert (b.available_count, b.total_count) == (rows["B200"].available_count, rows["B200"].total_count)

    def half(calls):  # one half missing: no row, never all-free or all-busy
        calls[:] = [c for c in calls if not (c["request"]["gpu_name"] == "B200" and not c["request"]["rentable"])]
    assert [r.sku for r in VastProvider.normalize_capacity(_vast(half))] == ["H100 SXM"]

    def capped(calls):
        busy = next(c for c in calls if c["request"]["gpu_name"] == "H100 SXM" and not c["request"]["rentable"])
        busy["payload"] = (busy["payload"] * CENSUS_CAP)[:CENSUS_CAP]
    h = next(r for r in VastProvider.normalize_capacity(_vast(capped)) if r.sku == "H100 SXM")
    assert h.available_count is None and h.total_count is None and h.in_stock is True, "a capped census withholds counts"

    def reshaped(calls):
        calls[0]["payload"] = {"offers": "nope"}
    try:
        VastProvider.normalize_capacity(_vast(reshaped))
        raise AssertionError("a reshaped census must raise")
    except ValueError:
        pass
    assert VastProvider.normalize_capacity({}) == []


def test_vast_census_only_with_key():
    from providers.vast import ENDPOINT, GPU_NAMES, VastProvider
    seen = []

    def handler(request):
        seen.append((request.method, request.headers.get("authorization")))
        return httpx.Response(200, json={"offers": []})
    import providers.vast as vast_mod
    pause = vast_mod.RATE_PAUSE_SECONDS
    vast_mod.RATE_PAUSE_SECONDS = 0
    try:
        for key, posts in ((None, 0), ("k-test", 2 * len(GPU_NAMES))):
            seen.clear()
            prov = VastProvider(httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)), api_key=key)
            raw = asyncio.run(prov.fetch())
            assert sum(m == "POST" for m, _ in seen) == posts and all(r.endpoint == ENDPOINT for r in raw)
            assert all((a == "Bearer k-test") == (m == "POST") for m, a in seen), "the key goes on census calls only"
            assert all("k-test" not in json.dumps(r.model_dump(mode="json")) for r in raw), "never stored"
    finally:
        vast_mod.RATE_PAUSE_SECONDS = pause


def test_vast_census_row_budget_and_quota():
    """The census spends at most CENSUS_DAILY_ROW_BUDGET rows a day and stops at Vast's quota refusal."""
    from datetime import datetime, timedelta, timezone
    import providers.vast as vast_mod
    from providers.vast import GPU_NAMES, VastProvider
    posts, mode = [], {"quota": False}

    def handler(request):
        if request.method == "POST":
            posts.append(1)
            if mode["quota"]:
                return httpx.Response(429, json={"error": "search_quota_exceeded", "msg": "Daily search row quota exhausted",
                                                 "retry_after": 6400, "limit": 20000, "remaining": 0})
            return httpx.Response(200, json={"offers": [{"machine_id": i, "num_gpus": 1, "gpu_frac": 1, "gpu_ids": [i],
                                                         "rentable": True} for i in range(100)]})
        return httpx.Response(200, json={"offers": []})
    pause = vast_mod.RATE_PAUSE_SECONDS
    vast_mod.RATE_PAUSE_SECONDS = 0
    try:
        prov = VastProvider(httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)), api_key="k")
        before = datetime.now(timezone.utc)
        asyncio.run(prov.fetch())
        assert len(posts) == 2 * len(GPU_NAMES), "first poll: a full census"
        rows = 100 * 2 * len(GPU_NAMES)
        expect = timedelta(days=1) * rows / vast_mod.CENSUS_DAILY_ROW_BUDGET
        gap = prov._census_next - before
        assert min(max(expect, vast_mod.CENSUS_MIN_GAP), vast_mod.CENSUS_MAX_GAP) - timedelta(seconds=5) <= gap
        posts.clear()
        asyncio.run(prov.fetch())
        assert posts == [], "inside the gap: prices only, no census"
        # Quota refused: one census call, then wait out Vast's retry_after; no hammering every poll.
        prov._census_next = None
        mode["quota"] = True
        posts.clear()
        raw = asyncio.run(prov.fetch())
        assert len(posts) == 1, "stops at the first quota refusal (no same-poll retry)"
        assert timedelta(seconds=6300) < prov._census_next - datetime.now(timezone.utc) <= timedelta(seconds=6400)
        posts.clear()
        asyncio.run(prov.fetch())
        assert posts == []
        assert VastProvider.capacity_interval_seconds == int(vast_mod.CENSUS_MAX_GAP.total_seconds())
    finally:
        vast_mod.RATE_PAUSE_SECONDS = pause


def test_latitude_own_stock():
    from providers.shadeform import LATITUDE_PLANS, LatitudeProvider
    plans = json.loads((FIX / "latitude_plans.json").read_text())
    shade = _by_endpoint("latitude")

    def by(p):
        return {**shade, LATITUDE_PLANS: [SimpleNamespace(payload=p, fetched_at=T0)]}
    rows = LatitudeProvider.normalize_capacity(by(plans))
    assert rows and all(r.source == LATITUDE_PLANS and r.count_unit == "instance" for r in rows), "own API beats Shadeform"
    assert all(r.in_stock is False and r.available_count == 0 and r.tier is None for r in rows), "all sold out when saved"
    assert {r.sku for r in rows} == {"g3-h100-small", "g4-rtx6kpro-large", "g4-b300-large"}
    assert next(r for r in rows if r.sku == "g3-h100-small").canonical_gpu_name == "NVIDIA H100 80GB PCIe", "Shadeform label for the same plan"

    stocked = copy.deepcopy(plans)
    region = stocked["data"][0]["attributes"]["regions"][0]
    loc = region["locations"]["available"][0]
    region["locations"].update(in_stock=[loc], in_stock_count={loc: 3})
    region["stock_level"] = "medium"
    r = next(r for r in LatitudeProvider.normalize_capacity(by(stocked)) if r.region == loc and r.sku == stocked["data"][0]["attributes"]["slug"])
    assert (r.in_stock, r.available_count, r.tier, r.country) == (True, 3, "medium", "US")

    region["locations"]["in_stock_count"] = {}
    r = next(r for r in LatitudeProvider.normalize_capacity(by(stocked)) if r.region == loc and r.sku == stocked["data"][0]["attributes"]["slug"])
    assert r.in_stock is True and r.available_count is None, "in stock with no count: unknown, never guessed"

    assert all(r.source == "/v1/instances/types" for r in LatitudeProvider.normalize_capacity(shade)), "no key: Shadeform"
    try:
        LatitudeProvider.normalize_capacity(by({"data": "nope"}))
        raise AssertionError("a reshaped plans payload must raise")
    except ValueError:
        pass


def test_unmapped_queue_includes_capacity_names():
    Session = _setup_db()
    try:
        import capacity
        capacity.save([_obs(sku="only-in-capacity", provider="koyeb").model_copy(update={"raw_gpu_name": "Mystery GPU 99GB"})])
        names = {(r["provider"], r["raw_gpu_name"]) for r in normalize.unmapped_gpus()}
        assert ("koyeb", "Mystery GPU 99GB") in names, "a capacity-only unmapped name joins the same to-do list as prices"
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_koyeb_tier, test_linode_boolean, test_lium_count, test_linode_paging,
              test_change_only_storage_and_fail_open, test_refresh_drops_rows_from_a_source_the_provider_left, test_refresh_never_blocked_by_capacity, test_endpoints,
              test_lambda_where_not_how_much, test_hyperstack_vm_counts, test_digitalocean_regions,
              test_runpod_tiers_per_pod_size, test_scaleway_own_stock_levels, test_shadeform_clouds_booleans,
              test_massedcompute_via_shadeform, test_hyperbolic_missing_total_is_unknown,
              test_voltagepark_counts_and_locations, test_vast_census_free_and_total, test_vast_census_only_with_key, test_vast_census_row_budget_and_quota,
              test_latitude_own_stock, test_unmapped_queue_includes_capacity_names):
        t(); print(t.__name__, "ok")
