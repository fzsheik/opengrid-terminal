"""RunPod: pods (gpuTypes + lowestPrice) and serverless pools (gpuPools), one GraphQL endpoint, two stored bodies.

Fixture: runpod_gpu_pools.json, the real gpuPools answer (unauthenticated POST to api.runpod.io/graphql,
2026-11-12). The pods body here is a small synthetic one in the real shape. The last test pushes both through raw
storage, normalize + quality screening, the hourly rollup and the index vote in a scratch database: serverless rows
are listed, never voted.

Run:  .venv/bin/python tests/test_provider_runpod.py
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

from providers import runpod  # noqa: E402
from providers.runpod import ENDPOINT, POOLS_ENDPOINT, RunPodProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def pools():
    return json.loads((FIX / "runpod_gpu_pools.json").read_text())


def pods():
    block = {"uninterruptablePrice": 3.59, "stockStatus": "High", "minVcpu": 16, "minMemory": 125, "minDisk": None}
    gpu = {"id": "NVIDIA H200", "displayName": "H200 SXM", "manufacturer": "Nvidia", "memoryInGb": 141,
           "securePrice": 3.59, "communityPrice": None, "maxGpuCount": 8, "minPodGpuCount": 1,
           **{runpod._alias(c, n): None for c, _ in runpod.CLOUDS for n in runpod.GPU_COUNTS}}
    gpu["secure_1"] = block
    return {"data": {"gpuTypes": [gpu]}}


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def by_id(listings):
    return {l.listing_id: l for l in listings}


def test_serverless():
    out = by_id(runpod.serverless_listings({POOLS_ENDPOINT: [snap(pools())]}))
    assert len(out) == 26, "13 pools x (flex, active)"
    h200 = out["serverless:HOPPER_141:flex"]
    assert h200.price_per_gpu_hour == D("6.99") and h200.provider_tier == "serverless_flex" and h200.gpu_count == 1
    assert h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5" and h200.market_type == "on_demand"
    assert not h200.interruptible and h200.region is None and h200.available is None
    assert out["serverless:HOPPER_141:active"].price_per_gpu_hour == D("4.74")
    assert out["serverless:BLACKWELL_288:flex"].canonical_gpu_name == "NVIDIA B300 288GB SXM"
    # A pool of several models is its label, never a guessed canonical GPU; MIG slices do not count as a model.
    h100 = out["serverless:ADA_80_PRO:flex"]
    assert (h100.raw_gpu_name, h100.canonical_gpu_name, h100.price_per_gpu_hour) == ("H100", None, D("5.89"))
    assert out["serverless:ADA_24:flex"].canonical_gpu_name == "NVIDIA RTX 4090 24GB"


def test_serverless_edges():
    body = pools()
    body["data"]["gpuPools"][0]["isActive"] = False
    body["data"]["gpuPools"][1]["activeHourlyCost"] = None
    out = by_id(runpod.serverless_listings({POOLS_ENDPOINT: [snap(body)]}))
    assert not any(k.startswith("serverless:AMPERE_16:") for k in out), "an inactive pool is not listed"
    assert "serverless:AMPERE_24:flex" in out and "serverless:AMPERE_24:active" not in out
    assert runpod.serverless_listings({}) == []
    try:
        runpod.serverless_listings({POOLS_ENDPOINT: [snap({"data": {"gpuPools": {"x": 1}}})]})
    except ValueError:
        pass
    else:
        raise AssertionError("a reshaped gpuPools body raises")


def test_pods_and_pools_are_read_apart():
    both = {ENDPOINT: [snap(pods())], POOLS_ENDPOINT: [snap(pools())]}
    out = by_id(RunPodProvider.normalize(both))
    assert out["NVIDIA H200:secure:1"].price_per_gpu_hour == D("3.59")
    assert len(out) == 27, len(out)
    caps = RunPodProvider.normalize_capacity(both)
    assert [c.sku for c in caps] == ["NVIDIA H200:secure:1"], "pools carry no capacity"
    assert len(RunPodProvider.normalize({ENDPOINT: [snap(pods())]})) == 1, "no pools body: pods only"


def _provider(pool_status=200):
    seen = []

    def handler(request):
        q = json.loads(request.content)["query"]
        seen.append(q)
        if "gpuPools" in q:
            return httpx.Response(pool_status, json=pools() if pool_status == 200 else {"error": "x"})
        return httpx.Response(200, json=pods())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer k"})
    return RunPodProvider("k", client=client), seen


def test_fetch():
    prov, seen = _provider()
    raw = asyncio.run(prov.fetch())
    assert [r.endpoint for r in raw] == [ENDPOINT, POOLS_ENDPOINT] and all(r.ok for r in raw)
    assert len(seen) == 2 and "gpuPools" in seen[1]
    failing, _ = _provider(pool_status=503)
    raw = asyncio.run(failing.fetch())
    assert raw[0].ok and not raw[1].ok and raw[1].endpoint == POOLS_ENDPOINT, "a pools failure never hides pods"


def test_pipeline():
    import fetch as fetch_mod
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_runpod"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):
            prov, _ = _provider()
            responses = [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(prov.fetch())]
            fetch_mod.save(responses)
            normalize.refresh(["runpod"])
        with Session() as s:
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'runpod'")).scalar()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert listed == 27 and not held, (listed, held)
        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        h200 = votes[indices.gpu_index_id("NVIDIA H200 141GB SXM5") + ".neocloud"]
        assert abs(h200["runpod"][0] - 3.59) < 1e-9, "the pod price votes, never the serverless one"
        assert not any("runpod" in v for k, v in votes.items() if "b300" in k or "b200" in k), \
            "a GPU RunPod sells only as serverless here gets no RunPod vote"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_serverless, test_serverless_edges, test_pods_and_pools_are_read_apart, test_fetch, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
