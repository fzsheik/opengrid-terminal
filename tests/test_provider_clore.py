"""Clore.ai: a keyed marketplace of independent hosts.

The parser runs on a trimmed copy of the REAL response (tests/fixtures_providers/
clore_marketplace.json: 22 of 2,826 servers from GET https://api.clore.ai/v1/marketplace,
2026-10-21, `my_servers` emptied) and the real GET /v1/renter_fees body
(clore_renter_fees.json, the account's PoH balance removed). No key is stored: the
tests send a dummy one. The last test pushes both through raw storage, normalize +
quality screening, the hourly rollup and the index vote in a scratch database.

Prices are per whole server per day in USD (USD-Blockchain), plus the renter's 5%
on-demand fee: per GPU-hour = listed x 1.05 / 24 / GPUs.

Run:  .venv/bin/python tests/test_provider_clore.py
"""

import asyncio
import copy
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

import canonical  # noqa: E402
import fetch as fetch_mod  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from config import settings  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.clore import FEES, MARKET, CloreProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
KEY = "test-key-not-real"
NEW = ["clore"]


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def by_id(listings):
    return {l.listing_id: l for l in listings}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def _serve(market=None, fees=None, status=200, seen=None):
    market = json.dumps(load("clore_marketplace.json")) if market is None else market
    fees = json.dumps(load("clore_renter_fees.json")) if fees is None else fees

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.url.path, request.headers.get("auth")))
        body = {MARKET: market, FEES: fees}.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(status, text=body, headers={"content-type": "application/json"})
    return httpx.MockTransport(handler)


def prov(**kw):
    return CloreProvider(KEY, httpx.AsyncClient(base_url="https://api.clore.ai", headers={"auth": KEY},
                                                transport=_serve(**kw)))


def _fetch(p):
    import providers.clore as mod
    pause, mod.RATE_PAUSE_SECONDS = mod.RATE_PAUSE_SECONDS, 0
    try:
        return asyncio.run(p.fetch())
    finally:
        mod.RATE_PAUSE_SECONDS = pause


def _normalize(market, fees=None):
    fees = load("clore_renter_fees.json") if fees is None else fees
    return CloreProvider.normalize({MARKET: [snap(market)], FEES: [snap(fees)]})


def _fetched():
    raw = _fetch(prov())
    assert [r.endpoint for r in raw] == [MARKET, FEES] and all(r.ok for r in raw)
    return raw


# --------------------------------------------------------------------------
# Fetch: the key goes in `auth`, the body is cut down before storage
# --------------------------------------------------------------------------


def test_fetch_and_reshape():
    # The provider's own client carries the key in `auth` (not a Bearer header), as Clore documents.
    own = CloreProvider(KEY)._client
    assert own.headers["auth"] == KEY and "authorization" not in own.headers
    assert str(own.base_url).rstrip("/") == "https://api.clore.ai"
    seen = []
    raw = _fetch(prov(seen=seen))
    assert seen == [(MARKET, KEY), (FEES, KEY)], seen

    market, fees = raw
    assert set(market.payload) == {"code", "servers"}, "my_servers is dropped"
    assert len(market.payload["servers"]) == 22
    s = market.payload["servers"][0]
    assert set(s) == {"id", "rented", "price", "specs", "gpu_array"}
    assert s["price"] == {"on_demand": {"USD-Blockchain": 11.5}}, "only the USD on-demand price is kept"
    assert set(s["specs"]) == {"gpu", "gpuram", "cpus", "ram", "disk", "net"} and set(s["specs"]["net"]) == {"cc"}
    assert fees.payload == {"code": 0, "renter_fees": {"on_demand": load("clore_renter_fees.json")["renter_fees"]["on_demand"]}}
    text = json.dumps([r.model_dump(mode="json") for r in raw])
    assert KEY not in text, "the key is never stored"
    assert "owner" not in text and "poh_balance" not in text


# --------------------------------------------------------------------------
# Normalize
# --------------------------------------------------------------------------


def test_listings():
    market, fees = _fetched()
    out = by_id(CloreProvider.normalize({MARKET: [snap(market.payload)], FEES: [snap(fees.payload)]}))
    skus = {l.sku for l in out.values()}
    assert skus == {"RTX 4090 24GB", "RTX 3070 8GB", "RTX 3070 Ti 8GB", "RTX 5090 32GB", "A100SXM440GB 40GB",
                    "RTX 4090 23GB", "CMP 90HX 10GB", "RTX 3090 24GB"}, skus
    assert len(out) == 16

    # RTX 4090 24GB: five free servers, 11.5 / 11.8 / 24 (2 GPUs) / 50 (4 GPUs) / 12.5 USD a day.
    med, low = out["RTX 4090 24GB:median"], out["RTX 4090 24GB:cheapest"]
    assert low.price_per_gpu_hour == D("11.5") * D("1.05") / 24 == D("0.503125")
    assert med.price_per_gpu_hour == D("24") * D("1.05") / 24 / 2 == D("0.525"), "median of five asks"
    assert med.price_per_instance_hour == med.price_per_gpu_hour and med.gpu_count == 1
    assert med.capacity == 9 and med.capacity_unit == "gpu", "1 + 1 + 2 + 4 + 1 GPUs; rented servers not counted"
    assert med.canonical_gpu_name == low.canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    assert (med.country, med.vcpu, med.ram_gb, med.storage_gb) == (None, None, None, None)
    assert (low.country, low.vcpu, low.ram_gb, low.storage_gb) == ("BY", 16, 32.0, 63.0)
    for l in (med, low):
        assert l.provider == "clore" and l.currency == "USD" and l.market_type == "on_demand"
        assert not l.interruptible and l.available is True and l.region is None and l.observed_at == NOW
    assert (med.provider_tier, low.provider_tier) == ("median", "cheapest")

    # RTX 3070: 18/8 GPUs, 27/10, 2.7/1, 2.76/1 - the median of an even book is the mean of the middle two.
    assert out["RTX 3070 8GB:cheapest"].price_per_gpu_hour == D("0.0984375")
    assert out["RTX 3070 8GB:median"].price_per_gpu_hour == D("0.118125")
    assert out["RTX 3070 8GB:median"].capacity == 20
    # Exact Decimal even when /24 does not terminate.
    assert out["RTX 3070 Ti 8GB:median"].price_per_gpu_hour == D("14") * D("1.05") / 24 / 3 == D("14.7") / 72
    assert out["RTX 5090 32GB:median"].price_per_gpu_hour == D("1.225")
    assert out["RTX 5090 32GB:cheapest"].price_per_gpu_hour == D("0.7875")
    assert out["RTX 3090 24GB:median"].price_per_gpu_hour == D("0.4375")

    # Names that fix model and VRAM map; the rest stay unmapped.
    assert out["A100SXM440GB 40GB:median"].canonical_gpu_name == "NVIDIA A100 40GB SXM4"
    assert out["A100SXM440GB 40GB:median"].price_per_gpu_hour == D("0.74375")
    for sku in ("RTX 4090 23GB", "CMP 90HX 10GB"):
        assert out[f"{sku}:median"].canonical_gpu_name is None, sku

    # Skipped: rented servers (incl. the only H100), a mixed-GPU rig, a server not priced in USD.
    assert not any(l.sku.startswith(("H100", "RTX 3080")) for l in out.values())


def test_disagreeing_count_skipped():
    body = load("clore_marketplace.json")
    row = next(s for s in body["servers"] if s["id"] == 6954)
    assert row["specs"]["gpu"].startswith("3x") and len(row["gpu_array"]) == 3
    row["gpu_array"] = row["gpu_array"][:2]   # says 3x, lists 2: never guess which is right
    out = by_id(_normalize(body))
    assert "RTX 3070 Ti 8GB:median" not in out
    assert "RTX 4090 24GB:median" in out, "the other rows still count"
    row["specs"]["gpu"] = "NVIDIA GeForce RTX 3070 Ti"   # no count prefix at all
    assert "RTX 3070 Ti 8GB:median" not in by_id(_normalize(body))


def test_fee_is_read():
    fees = copy.deepcopy(load("clore_renter_fees.json"))
    fees["renter_fees"]["on_demand"]["base"]["USD-Blockchain"] = 2.5
    out = by_id(_normalize(load("clore_marketplace.json"), fees))
    assert out["RTX 4090 24GB:cheapest"].price_per_gpu_hour == D("11.5") * D("1.025") / 24


# --------------------------------------------------------------------------
# Capacity: free (not rented) GPUs out of all GPUs, per GPU name
# --------------------------------------------------------------------------


def test_capacity():
    market = load("clore_marketplace.json")
    rows = {r.sku: r for r in CloreProvider.normalize_capacity({MARKET: [snap(market)]})}
    listings = by_id(_normalize(market))
    # Every priced GPU name has a row, plus the H100 whose only server is rented (no price, out of stock).
    assert set(rows) == {l.sku for l in listings.values()} | {"H100 80GB"}, sorted(rows)
    r = rows["RTX 4090 24GB"]
    assert (r.available_count, r.total_count, r.count_unit, r.in_stock) == (9, 14, "gpu", True), \
        "free: 1 + 1 + 2 + 4 + 1; rented: 4 + 1"
    assert r.raw_value == "free_gpus=9; total_gpus=14; free_servers=5; servers=7"
    assert r.region is None and r.gpu_count == 1 and r.canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    assert r.provider == "clore" and r.source == MARKET and r.observed_at == NOW and r.tier is None
    h = rows["H100 80GB"]
    assert (h.in_stock, h.available_count, h.total_count) == (False, 0, 1)
    # Free counts are exactly the listings' book; never a row for skipped servers (mixed rig, non-USD).
    for sku, row in rows.items():
        if sku != "H100 80GB":
            assert row.available_count == listings[f"{sku}:median"].capacity, sku
    assert not any(s.startswith("RTX 3080") for s in rows)
    # Fail closed / open like the listings.
    assert raises(CloreProvider.normalize_capacity, {MARKET: [snap({"data": []})]})
    assert CloreProvider.normalize_capacity({}) == []
    assert CloreProvider.normalize_capacity({MARKET: [snap({"servers": [], "code": 0})]}) == []
    # The fee call does not matter to stock.
    assert len(CloreProvider.normalize_capacity({MARKET: [snap(market)], FEES: [snap({"code": 3})]})) == len(rows)


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


def test_fail_closed():
    market = load("clore_marketplace.json")
    assert raises(_normalize, {"data": []}), "servers renamed"
    assert raises(_normalize, []), "body no longer an object"
    assert raises(_normalize, {"servers": [], "code": 3}), "error code (bad key)"
    bad = copy.deepcopy(market)
    del bad["servers"][0]["rented"]
    assert raises(_normalize, bad), "rented flag gone"
    assert raises(_normalize, market, {"renter_fees": {}}), "fee shape changed"
    assert raises(_normalize, market, {"code": 3}), "fee call failed"
    assert raises(CloreProvider.normalize, {MARKET: [snap(market)]}), "no fee snapshot at all"
    # An empty but well-formed book is a real "nothing listed".
    assert _normalize({"servers": [], "code": 0}) == []
    assert CloreProvider.normalize({}) == []
    # A reshaped body is stored untouched by fetch(), so normalize sees it and fails.
    raw = _fetch(prov(market=json.dumps({"data": []})))
    assert raw[0].payload == {"data": []} and raises(_normalize, raw[0].payload)


def test_fetch_failures():
    raw = _fetch(prov(market='{"code": 3}', fees='{"code": 3}', status=401))
    assert len(raw) == 2 and not any(r.ok for r in raw) and raw[0].status_code == 401
    assert raw[0].endpoint == MARKET and raw[1].endpoint == FEES

    def boom(request):
        raise httpx.ConnectError("no route")
    p = CloreProvider(KEY, httpx.AsyncClient(base_url="https://api.clore.ai", transport=httpx.MockTransport(boom)))
    raw = _fetch(p)
    assert len(raw) == 2 and not any(r.ok for r in raw) and "ConnectError" in raw[0].error


# --------------------------------------------------------------------------
# Registry, mapping, provenance, aliases
# --------------------------------------------------------------------------


def test_registry():
    assert PROVIDERS["clore"] is CloreProvider
    saved = settings.clore_api_key
    try:
        settings.clore_api_key = None
        assert "clore" not in {p.name for p in fetch_mod.build_fetchers()}, "skipped without a key"
        settings.clore_api_key = KEY
        assert "clore" in {p.name for p in fetch_mod.build_fetchers()}
    finally:
        settings.clore_api_key = saved
    coverage = mapping.check_mapping_coverage()
    assert "clore" not in coverage, coverage.get("clore")
    assert "clore" in mapping.PROVIDER_MAPPINGS
    m = provider_meta.meta("clore")
    assert m.provider_class == "marketplace" and m.source_type == "authenticated_api" and m.source == "api.clore.ai"
    assert m.website == "https://clore.ai"


def test_aliases():
    assert canonical.canonical_gpu_name("RTX 4090 24GB") == "NVIDIA RTX 4090 24GB"
    assert canonical.canonical_gpu_name("RTX 3060 12GB") == "NVIDIA RTX 3060 12GB"
    assert canonical.canonical_gpu_name("RTX 3060 8GB") == "NVIDIA RTX 3060 8GB"
    assert canonical.canonical_gpu_name("Tesla V100SXM216GB 16GB") == "NVIDIA Tesla V100 16GB SXM2"
    assert canonical.canonical_gpu_name("H100 NVL 94GB") == "NVIDIA H100 94GB NVL"
    for name in ("H100 80GB", "Tesla T4 15GB", "L40S 45GB", "RTX 4090 23GB", "RTX 5060 Ti 8GB",
                 "RTX 3080 12GB", "Tesla V100SXM232GB 32GB", "RTX 4090 D 48GB"):
        assert canonical.canonical_gpu_name(name) is None, name


# --------------------------------------------------------------------------
# Pipeline: raw storage -> normalize + quality -> rollup -> index votes
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_clore"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = [r.model_copy(update={"fetched_at": when}) for r in _fetch(prov())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(NEW)

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == {"clore": 4}, stored
        assert bodies == 2, "identical bodies from the second poll are stored once"
        assert listed == {"clore": 16}, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        rtx4090 = indices.gpu_index_id("NVIDIA RTX 4090 24GB")
        # The median row votes; the cheapest row (0.503125) is kept out of market math.
        assert abs(votes[rtx4090]["clore"][0] - 0.525) < 1e-9, votes.get(rtx4090)
        assert not any("clore" in v for k, v in votes.items() if k.endswith(".spot")), "never a spot price"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_fetch_and_reshape, test_listings, test_disagreeing_count_skipped, test_fee_is_read,
              test_capacity, test_fail_closed, test_fetch_failures, test_registry, test_aliases, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
