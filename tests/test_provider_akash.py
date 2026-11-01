"""Akash Network: the Console's bid-derived GPU market summary.

Parser runs on the REAL response (tests/fixtures_providers/akash_gpu_prices.json, fetched
2026-11-01 with an anonymous GET). The last test pushes it through raw storage, normalize +
quality screening, the hourly rollup and the index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_akash.py
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
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.akash import PRICES, AkashProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "akash_gpu_prices.json"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def body():
    return json.loads(FIX.read_text(encoding="utf-8"))


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def prov():
    def handler(request):
        if request.url.path != PRICES:
            return httpx.Response(404)
        return httpx.Response(200, text=FIX.read_text(encoding="utf-8"), headers={"content-type": "application/json"})
    return AkashProvider(httpx.AsyncClient(base_url="https://console-api.akash.network", transport=httpx.MockTransport(handler)))


def test_normalize():
    out = {l.listing_id: l for l in AkashProvider.normalize({PRICES: [snap(body())]})}
    assert len(out) == 22, "24 rows, two with price null (no bid in 31 days)"
    assert "nvidia-gtx1050ti-4Gi-PCIe" not in out and "nvidia-rtx3090ti-24Gi-PCIe" not in out
    h = out["nvidia-h100-80Gi-SXM5"]
    assert h.raw_gpu_name == "nvidia h100 80Gi SXM5" and h.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h.price_per_gpu_hour == D("2.73") == h.price_per_instance_hour, "weightedAverage, not min/med/max"
    assert (h.gpu_count, h.capacity, h.capacity_unit, h.available) == (1, 16, "gpu", True)
    assert h.provider_tier == "weighted" and h.market_type == "on_demand" and h.interruptible is False
    assert h.region is None and h.country is None
    a = out["nvidia-a100-80Gi-SXM4"]
    assert a.canonical_gpu_name == "NVIDIA A100 80GB SXM4" and a.price_per_gpu_hour == D("1.67") and a.capacity == 51
    b = out["nvidia-b300-288Gi-SXM6"]
    assert b.available is False and b.capacity == 0, "16 of 16 allocated"
    assert out["nvidia-pro6000se-96Gi-PCIe"].canonical_gpu_name == "NVIDIA RTX PRO 6000 96GB SE"
    assert out["nvidia-v100-16Gi-SXM2"].canonical_gpu_name == "NVIDIA Tesla V100 16GB SXM2"
    assert out["nvidia-v100-32Gi-PCIe"].canonical_gpu_name is None, "no 32 GB V100 canonical: never merged"
    assert out["nvidia-rtx5000-16Gi-PCIe"].canonical_gpu_name is None


def test_capacity():
    rows = {r.sku: r for r in AkashProvider.normalize_capacity({PRICES: [snap(body())]})}
    assert len(rows) == 24, "every row, priced or not"
    h = rows["nvidia-h100-80Gi-SXM5"]
    assert (h.available_count, h.total_count, h.count_unit, h.in_stock) == (16, 69, "gpu", True)
    assert h.region is None and h.source == PRICES and "providers=4" in h.raw_value
    assert rows["nvidia-gtx1050ti-4Gi-PCIe"].in_stock is True, "unpriced rows still report stock"
    assert rows["nvidia-b300-288Gi-SXM6"].in_stock is False
    listings = {l.sku: l for l in AkashProvider.normalize({PRICES: [snap(body())]})}
    assert all(rows[k].available_count == l.capacity for k, l in listings.items())


def test_failures():
    assert raises(AkashProvider.normalize, {PRICES: [snap({"gpus": []})]}), "reshaped"
    unpriced = body()
    for m in unpriced["models"]:
        m["price"] = None
    assert raises(AkashProvider.normalize, {PRICES: [snap(unpriced)]}), "no priced row is a failure"
    odd = body()
    odd["models"][0]["price"]["currency"] = "uakt"
    assert raises(AkashProvider.normalize, {PRICES: [snap(odd)]}), "a non-USD summary is never read as dollars"
    bad = body()
    bad["models"][0]["availability"] = {"total": 3, "available": 5}
    out = {l.sku: l for l in AkashProvider.normalize({PRICES: [snap(bad)]})}
    assert out["nvidia-a100-80Gi-SXM4"].capacity is None and out["nvidia-a100-80Gi-SXM4"].available is None
    assert AkashProvider.normalize({}) == [] and AkashProvider.normalize_capacity({}) == []

    raws = asyncio.run(prov().fetch())
    assert [r.endpoint for r in raws] == [PRICES] and raws[0].ok and raws[0].payload == body()
    failing = httpx.MockTransport(lambda r: httpx.Response(503, text="maintenance"))
    raws = asyncio.run(AkashProvider(httpx.AsyncClient(base_url="https://x", transport=failing)).fetch())
    assert not raws[0].ok and raws[0].payload is None
    assert raises(AkashProvider.normalize, {PRICES: [snap(raws[0].payload)]})


def test_registry():
    assert PROVIDERS["akash"] is AkashProvider
    assert "AkashProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "akash" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("akash")
    assert (m.provider_class, m.source_type, m.source) == ("decentralized", "public_api", "console-api.akash.network")


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_akash"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):
            responses = [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(prov().fetch())]
            fetch_mod.save(responses)
            normalize.refresh(["akash"])
        with Session() as s:
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'akash'")).scalar()
            b300 = s.execute(sql("SELECT price_per_gpu_hour FROM compute_listings "
                                 "WHERE listing_id = 'nvidia-b300-288Gi-SXM6'")).scalar()
            held = {r.listing_id: r.rule for r in s.execute(sql(
                "SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'"))}
        # The $0.23 B300 bid is below the datacenter floor: held by quality, never published as a price.
        assert held == {"nvidia-b300-288Gi-SXM6": "price_below_floor"}, held
        assert listed == 22 and b300 is None, (listed, b300)
        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        h100 = votes[indices.gpu_index_id("NVIDIA H100 80GB SXM5")]
        assert set(h100) == {"akash"} and abs(h100["akash"][0] - 2.73) < 1e-9
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_normalize, test_capacity, test_failures, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
