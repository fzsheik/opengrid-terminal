"""GPU.ai: public pricing catalogue of a reseller; stored, never counted in market statistics.

Parser runs on the REAL response (tests/fixtures_providers/gpuai_pricing.json, fetched
2026-11-01 with an anonymous GET, limit=200). The pipeline test proves the resold rows reach
compute_listings but never the hourly rollup, the index votes or the market view.

Run:  .venv/bin/python tests/test_provider_gpuai.py
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
from providers.gpuai import PRICING, GpuAiProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "gpuai_pricing.json"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)
EP = f"{PRICING}?limit=200&page=1"


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


def prov(pages=None):
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        if pages:
            return httpx.Response(200, json=pages[len(seen) - 1])
        return httpx.Response(200, text=FIX.read_text(encoding="utf-8"), headers={"content-type": "application/json"})
    return GpuAiProvider(httpx.AsyncClient(base_url="https://api.gpu.ai", transport=httpx.MockTransport(handler))), seen


def test_normalize():
    rows = body()["data"]
    out = {l.listing_id: l for l in GpuAiProvider.normalize({EP: [snap(body())]})}
    assert len(out) == len(rows) == 66
    assert all(l.provider_tier == "resold:undisclosed" and l.provider_tier.startswith(market.RESOLD_PREFIX)
               for l in out.values()), "every row is resold"
    pair = [r for r in rows if r["gpu_type"] == "a100_80gb" and r["gpu_count"] == 2][0]
    l = out[pair["offering_id"]]
    assert l.price_per_instance_hour == D(str(pair["price_per_hour"])) and l.price_per_gpu_hour == D(str(pair["price_per_hour"])) / 2
    assert l.canonical_gpu_name is None, "a100_80gb: form factor not stated"
    h = [x for x in out.values() if x.raw_gpu_name == "h100_sxm"][0]
    assert h.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h.capacity_unit == "instance"
    assert all(x.country == "US" for x in out.values() if (x.region or "").startswith("us-"))
    assert all(x.country is None for x in out.values() if (x.region or "").startswith("eu-"))
    assert not any(x.canonical_gpu_name for x in out.values() if x.raw_gpu_name.startswith("rtx_pro"))
    # Offset paging can return an offering on two pages (2026-11-11): one listing, not two.
    page2 = {**body(), "data": body()["data"][:4]}
    both = GpuAiProvider.normalize({EP: [snap(body())], f"{PRICING}?limit=200&page=2": [snap(page2)]})
    assert len(both) == 66 and len({l.listing_id for l in both}) == 66, len(both)


def test_capacity():
    rows = GpuAiProvider.normalize_capacity({EP: [snap(body())]})
    listings = GpuAiProvider.normalize({EP: [snap(body())]})
    assert len(rows) == len(listings) and {r.sku for r in rows} == {l.sku for l in listings}
    assert all(r.count_unit == "instance" and r.available_count is not None for r in rows)


def test_failures_and_paging():
    assert raises(GpuAiProvider.normalize, {EP: [snap({"items": []})]})
    assert raises(GpuAiProvider.normalize, {EP: [snap({"data": [], "next_cursor": None})]}), "empty = failure"
    assert GpuAiProvider.normalize({}) == []
    p, seen = prov()
    raws = asyncio.run(p.fetch())
    assert len(raws) == 1 and raws[0].endpoint == EP and seen == [{"limit": "200"}]
    first, second = body(), body()
    first["data"], first["next_cursor"] = first["data"][:30], "abc"
    second["data"], second["next_cursor"] = second["data"][30:], None
    p, seen = prov([first, second])
    raws = asyncio.run(p.fetch())
    assert seen == [{"limit": "200"}, {"limit": "200", "cursor": "abc"}] and len(raws) == 2
    by_ep = {r.endpoint: [snap(r.payload)] for r in raws}
    assert len(GpuAiProvider.normalize(by_ep)) == 66, "both pages read"


def test_registry():
    assert PROVIDERS["gpuai"] is GpuAiProvider
    assert "GpuAiProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "gpuai" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("gpuai")
    assert (m.provider_class, m.source_type) == ("marketplace", "public_api") and "resold" in m.note


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import rollups

    db = "opengrid_test_gpuai"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):
            responses = [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(prov()[0].fetch())]
            fetch_mod.save(responses)
            normalize.refresh(["gpuai"])
        rollups.rebuild()
        with Session() as s:
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'gpuai'")).scalar()
            hourly = s.execute(sql("SELECT count(*) FROM market_hourly WHERE provider = 'gpuai'")).scalar()
            eligible = s.execute(sql(f"SELECT count(*) FROM compute_listings c WHERE {market._ELIGIBLE}")).scalar()
        assert listed == 66, listed
        assert hourly == 0 and eligible == 0, "resold rows never reach the rollup or the market view"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_normalize, test_capacity, test_failures_and_paging, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
