"""Hugging Face Inference Endpoints: resold AWS/GCP capacity, stored but never in market statistics.

Parser runs on the REAL response (tests/fixtures_providers/huggingface_provider.json, fetched
2026-11-01 with an anonymous GET). The pipeline test runs it beside a direct provider's listing
for the same GPU and proves only the direct one votes.

Run:  .venv/bin/python tests/test_provider_huggingface.py
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
import market  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.huggingface import PROVIDERS_EP, HuggingFaceProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "huggingface_provider.json"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def body():
    return json.loads(FIX.read_text(encoding="utf-8"))


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def prov():
    def handler(request):
        return httpx.Response(200, text=FIX.read_text(encoding="utf-8"), headers={"content-type": "application/json"})
    return HuggingFaceProvider(httpx.AsyncClient(base_url="https://api.endpoints.huggingface.cloud",
                                                 transport=httpx.MockTransport(handler)))


def test_normalize():
    out = {l.listing_id: l for l in HuggingFaceProvider.normalize({PROVIDERS_EP: [snap(body())]})}
    assert len(out) == 38, len(out)  # GPU computes minus 8 deprecated us-west-2 ones; no CPU or Inferentia
    assert not any("us-west-2" in k or "inf2" in k or "intel" in k for k in out)
    a = out["aws-us-east-1-nvidia-a100-x2"]
    assert a.price_per_instance_hour == D("5.0") and a.price_per_gpu_hour == D("2.5") and a.gpu_count == 2
    assert a.raw_gpu_name == "aws Nvidia A100 80GB" and a.canonical_gpu_name == "NVIDIA A100 80GB SXM4"
    assert a.provider_tier == "resold:aws" and a.provider_tier.startswith(market.RESOLD_PREFIX)
    assert a.region == "us-east-1" and a.country == "US" and a.available is True
    g = out["gcp-us-east4-nvidia-h100-x4"]
    assert g.provider_tier == "resold:gcp" and g.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and g.price_per_gpu_hour == D("10")
    ie = out["aws-eu-west-1-nvidia-a100-x1"]
    assert ie.available is False and ie.country == "IE", "status not_available"
    assert out["aws-us-east-2-nvidia-rtx-pro-6000-x8"].price_per_gpu_hour == D("2.75")
    assert all(l.provider_tier.startswith("resold:") for l in out.values())


def test_capacity():
    rows = {r.sku: r for r in HuggingFaceProvider.normalize_capacity({PROVIDERS_EP: [snap(body())]})}
    assert set(rows) == {l.sku for l in HuggingFaceProvider.normalize({PROVIDERS_EP: [snap(body())]})}
    assert rows["aws-eu-west-1-nvidia-t4-x4"].in_stock is False and rows["aws-eu-west-1-nvidia-t4-x4"].raw_value == "not_available"
    assert rows["gcp-us-south1-nvidia-h200-x8"].in_stock is True and rows["gcp-us-south1-nvidia-h200-x8"].gpu_count == 8


def test_failures():
    assert raises(HuggingFaceProvider.normalize, {PROVIDERS_EP: [snap({"items": []})]})
    assert raises(HuggingFaceProvider.normalize, {PROVIDERS_EP: [snap({"vendors": []})]}), "empty = failure"
    b = body()
    b["vendors"][0]["regions"][1]["status"] = "not_available"
    us = {l.listing_id: l for l in HuggingFaceProvider.normalize({PROVIDERS_EP: [snap(b)]})}
    assert us["aws-us-east-1-nvidia-a100-x1"].available is False, "a closed region is not in stock"
    assert HuggingFaceProvider.normalize({}) == []
    raws = asyncio.run(prov().fetch())
    assert [r.endpoint for r in raws] == [PROVIDERS_EP] and raws[0].ok


def test_registry():
    assert PROVIDERS["huggingface"] is HuggingFaceProvider
    assert "HuggingFaceProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "huggingface" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("huggingface")
    assert (m.provider_class, m.source_type) == ("general_cloud", "public_api") and "resold" in m.note


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups
    from providers.akash import AkashProvider

    akash_fix = FIX.parent / "akash_gpu_prices.json"

    def akash():
        return AkashProvider(httpx.AsyncClient(base_url="https://console-api.akash.network", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=akash_fix.read_text(encoding="utf-8"), headers={"content-type": "application/json"}))))

    db = "opengrid_test_huggingface"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):
            responses = [r.model_copy(update={"fetched_at": when})
                         for r in asyncio.run(prov().fetch()) + asyncio.run(akash().fetch())]
            fetch_mod.save(responses)
            normalize.refresh(["huggingface", "akash"])
        rollups.rebuild()
        with Session() as s:
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'huggingface'")).scalar()
            hourly = s.execute(sql("SELECT count(*) FROM market_hourly WHERE provider = 'huggingface'")).scalar()
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        assert listed == 38 and hourly == 0, (listed, hourly)
        # H100 SXM5: Hugging Face lists GCP's at $10/GPU-h; only Akash (a direct source) votes.
        votes = indices.votes_by_index(rows)[indices.gpu_index_id("NVIDIA H100 80GB SXM5")]
        assert set(votes) == {"akash"}, votes
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_normalize, test_capacity, test_failures, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
