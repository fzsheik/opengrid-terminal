"""RunPod: pods from REST v2's catalog (with a key) or GraphQL (without, or when REST fails); serverless pools from
GraphQL (gpuPools).

Fixtures (tests/fixtures_providers):
    runpod_gpu_pools.json                  real gpuPools answer (unauthenticated POST to api.runpod.io/graphql, 2026-11-12)
    runpod_graphql_pods_2026-11-13.json    real gpuTypes + lowestPrice answer (unauthenticated, 2026-11-13)
    runpod_rest_401_2026-11-13.json        real answer of an unauthenticated GET /v2/catalog/gpus (401)
    runpod_rest_catalog_2026-11-13.json    SPEC-SHAPED REST bodies, not a capture (REST needs a key; none was used):
                                           the v2 GpuType schema filled with the GraphQL capture's values (see _about)
The small pods body in pods() is synthetic in the real GraphQL shape. The pipeline tests push raw bodies through
storage, normalize + quality screening, the hourly rollup and the index vote in a scratch database: serverless rows
are listed, never voted; a switch between REST and GraphQL never mixes rounds.

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
from providers.runpod import CATALOG_KEYS, ENDPOINT, POOLS_ENDPOINT, RunPodProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def pools():
    return json.loads((FIX / "runpod_gpu_pools.json").read_text())


def gql_capture():
    return json.loads((FIX / "runpod_graphql_pods_2026-11-13.json").read_text())


def rest_bodies():
    return json.loads((FIX / "runpod_rest_catalog_2026-11-13.json").read_text())["bodies"]


def rest_401():
    return json.loads((FIX / "runpod_rest_401_2026-11-13.json").read_text())["body"]


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


def _provider(pool_status=200, key=None, rest=None):
    """A RunPodProvider on a mock transport. rest(url) -> httpx.Response answers the catalog GETs (default: the
    spec-shaped fixture bodies)."""
    seen = []
    bodies = rest_bodies()

    def handler(request):
        if request.method == "GET":
            url = str(request.url)
            seen.append(("GET", url))
            assert request.headers.get("authorization") == f"Bearer {key}", "REST carries the key"
            return rest(url) if rest else httpx.Response(200, json=bodies[url])
        q = json.loads(request.content)["query"]
        seen.append(("POST", "gpuPools" if "gpuPools" in q else "gpuTypes"))
        if "gpuPools" in q:
            return httpx.Response(pool_status, json=pools() if pool_status == 200 else {"error": "x"})
        return httpx.Response(200, json=pods())

    return RunPodProvider(api_key=key), handler, seen


def _run(prov_handler):
    prov, handler, seen = prov_handler
    prov._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=dict(prov._client.headers))
    return asyncio.run(prov.fetch()), seen


def test_fetch():
    # No key: GraphQL only (it needs none), and no Authorization header at all.
    prov = RunPodProvider()
    assert "authorization" not in prov._client.headers
    raw, seen = _run(_provider())
    assert [r.endpoint for r in raw] == [ENDPOINT, POOLS_ENDPOINT] and all(r.ok for r in raw)
    assert seen == [("POST", "gpuTypes"), ("POST", "gpuPools")]
    raw, _ = _run(_provider(pool_status=503))
    assert raw[0].ok and not raw[1].ok and raw[1].endpoint == POOLS_ENDPOINT, "a pools failure never hides pods"

    # A key: eight REST GETs (one per cloud x size, each its own stored endpoint), pools still GraphQL.
    raw, seen = _run(_provider(key="k"))
    assert [r.endpoint for r in raw] == [*CATALOG_KEYS, POOLS_ENDPOINT] and all(r.ok for r in raw)
    assert ("POST", "gpuTypes") not in seen and seen[-1] == ("POST", "gpuPools")
    assert all("include=AVAILABILITY&product=POD&count=" in u for u in list(CATALOG_KEYS))


def test_fetch_falls_back_to_graphql():
    # The real 401 (a key without catalog:read, or a revoked one): stop at the first GET, pods from GraphQL.
    raw, seen = _run(_provider(key="k", rest=lambda url: httpx.Response(401, json=rest_401())))
    assert [(r.endpoint, r.ok) for r in raw] == [(next(iter(CATALOG_KEYS)), False), (ENDPOINT, True),
                                                 (POOLS_ENDPOINT, True)], [(r.endpoint, r.ok) for r in raw]
    assert raw[0].status_code == 401 and "missing bearer token" in raw[0].error
    assert seen.count(("POST", "gpuTypes")) == 1 and len([s for s in seen if s[0] == "GET"]) == 1
    # A 401/403 will not fix itself in two minutes: the next polls go straight to GraphQL for an hour, then retry.
    prov, handler, seen = _provider(key="k", rest=lambda url: httpx.Response(403, json=rest_401()))
    _run((prov, handler, seen))
    assert prov._rest_paused_until > 0
    raw, _ = _run((prov, handler, seen))
    assert [r.endpoint for r in raw] == [ENDPOINT, POOLS_ENDPOINT] and len([s for s in seen if s[0] == "GET"]) == 1
    prov._rest_paused_until = 0.0
    _run((prov, handler, seen))
    assert len([s for s in seen if s[0] == "GET"]) == 2, "retried once the pause is over"
    # A transient failure (503) does not pause REST.
    prov, handler, seen = _provider(key="k", rest=lambda url: httpx.Response(503, text="busy"))
    _run((prov, handler, seen))
    assert prov._rest_paused_until == 0.0
    # A 200 without a gpus list is a reshape: stored as a failure, GraphQL covers the round.
    third = list(CATALOG_KEYS)[2]
    raw, _ = _run(_provider(key="k", rest=lambda url: httpx.Response(
        200, json={"data": []} if url == third else rest_bodies()[url])))
    assert [r.ok for r in raw[:3]] == [True, True, False] and raw[2].error == "runpod: catalog body has no gpus list"
    assert [r.endpoint for r in raw[3:]] == [ENDPOINT, POOLS_ENDPOINT]
    listings = RunPodProvider.normalize({r.endpoint: [snap(r.payload, r.fetched_at)] for r in raw if r.ok})
    assert "NVIDIA H200:secure:1" in by_id(listings), "the GraphQL round, not the two REST bodies, is read"


def _key(listing):
    return (listing.sku, listing.raw_gpu_name, listing.canonical_gpu_name, listing.gpu_count,
            listing.price_per_gpu_hour, listing.price_per_instance_hour, listing.provider_tier, listing.interruptible,
            listing.available, listing.vcpu, listing.ram_gb, listing.storage_gb, listing.market_type, listing.region)


def test_rest_parse_matches_graphql():
    """The REST mapping reproduces the GraphQL listings and capacity of the same moment, field for field."""
    gq = {ENDPOINT: [snap(gql_capture())]}
    rs = {url: [snap(body)] for url, body in rest_bodies().items()}
    g, r = by_id(RunPodProvider.normalize(gq)), by_id(RunPodProvider.normalize(rs))
    assert len(g) == len(r) == 72 and set(g) == set(r)
    assert all(_key(g[k]) == _key(r[k]) for k in g), [k for k in g if _key(g[k]) != _key(r[k])][:3]
    a100 = r["NVIDIA A100-SXM4-80GB:secure:4"]
    assert (a100.price_per_gpu_hour, a100.price_per_instance_hour, a100.vcpu, a100.ram_gb) == (
        D("2.19"), D("8.76"), 64, 471), "price per GPU x count; minVcpuCount / minRam are pod totals"
    assert r["NVIDIA GeForce RTX 3090:community:2"].interruptible and r["NVIDIA GeForce RTX 3090:community:2"].available
    gc = {c.sku: c for c in RunPodProvider.normalize_capacity(gq)}
    rc = {c.sku: c for c in RunPodProvider.normalize_capacity(rs)}
    assert set(gc) == set(rc) and all((gc[k].in_stock, gc[k].tier) == (rc[k].in_stock, rc[k].tier) for k in gc)
    one = rc["NVIDIA A100-SXM4-80GB:secure:4"]
    assert one.raw_value == one.raw_value.upper() and one.source == runpod.catalog_url("secure", 4)

    # Edges: NONE / a missing word / a zero or absent price is no listing; a non-list gpus raises.
    bodies = rest_bodies()
    url = runpod.catalog_url("secure", 1)
    gpus = bodies[url]["gpus"]
    h200 = next(x for x in gpus if x["id"] == "NVIDIA H200")
    assert h200["availability"] != "NONE"
    h200["availability"] = "NONE"
    b200 = next(x for x in gpus if x["id"] == "NVIDIA B200")
    b200["price"]["secure"] = 0
    out = by_id(RunPodProvider.normalize({u: [snap(b)] for u, b in bodies.items()}))
    assert "NVIDIA H200:secure:1" not in out and "NVIDIA B200:secure:1" not in out
    assert len(out) == 70, "only those two cells drop"
    bodies[url] = {"gpus": {"x": 1}}
    try:
        RunPodProvider.normalize({u: [snap(b)] for u, b in bodies.items()})
    except ValueError:
        pass
    else:
        raise AssertionError("a reshaped REST body raises")


def test_round_choice():
    """Each endpoint's newest ok snapshot of all time is handed over: the newest complete round wins, never a mix."""
    t = NOW
    gql = lambda at: {ENDPOINT: [snap(gql_capture(), at)]}  # noqa: E731
    rest = lambda at, skip=(): {u: [snap(b, at)] for u, b in rest_bodies().items() if u not in skip}  # noqa: E731
    first = next(iter(CATALOG_KEYS))
    kind = lambda by: runpod._pod_round(by)[0]  # noqa: E731
    assert kind({}) == "none" and RunPodProvider.normalize({}) == []
    assert kind(gql(t)) == "graphql" and kind(rest(t)) == "rest"
    assert kind({**gql(t - timedelta(days=3)), **rest(t)}) == "rest", "a key added: REST, the old GraphQL ignored"
    assert kind({**gql(t), **rest(t - timedelta(hours=1))}) == "graphql", "key removed: the later GraphQL round"
    # REST failed at its first GET this round: the other seven are an hour old; GraphQL ran this round.
    mixed = {**rest(t - timedelta(hours=1)), first: [snap(rest_bodies()[first], t)], **gql(t + timedelta(seconds=5))}
    assert kind(mixed) == "graphql"
    # Same, but GraphQL failed too and its last body is a day old: only this round's REST piece, never the stale ones.
    mixed[ENDPOINT] = [snap(gql_capture(), t - timedelta(days=1))]
    k, snaps = runpod._pod_round(mixed)
    assert k == "rest" and list(snaps) == [first]
    assert {c["at"] for c in runpod._pod_cells(mixed)} == {t}


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
            responses = [r.model_copy(update={"fetched_at": when}) for r in _run(_provider())[0]]
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


def test_pipeline_switches_sources():
    """GraphQL (no key) -> REST (key) -> REST 401, GraphQL fallback -> REST, through real storage and _latest_raw:
    every round lists the same 72 pods + 26 pool rows, nothing held, and each round is read from its own source."""
    import fetch as fetch_mod
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql

    db = "opengrid_test_runpod_switch"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)

        def gql_handler(request):
            q = json.loads(request.content)["query"]
            return httpx.Response(200, json=pools() if "gpuPools" in q else gql_capture())

        rounds = [(None, None, "graphql"), ("k", None, "rest"),
                  ("k", lambda u: httpx.Response(401, json=rest_401()), "graphql"), ("k", None, "rest")]
        for i, (key, rest, expect) in enumerate(rounds):
            prov, handler, _ = _provider(key=key, rest=rest)

            def both(request, handler=handler):
                return handler(request) if request.method == "GET" else gql_handler(request)

            raw, _ = _run((prov, both, []))
            when = now - timedelta(hours=len(rounds) - i)
            fetch_mod.save([r.model_copy(update={"fetched_at": when}) for r in raw])
            with Session() as s:
                latest = normalize._latest_raw(s, "runpod")
            assert runpod._pod_round(latest)[0] == expect, (i, runpod._pod_round(latest)[0])
            normalize.refresh(["runpod"])
            with Session() as s:
                listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'runpod'")).scalar()
                fresh = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'runpod' "
                                      "AND observed_at = :w"), {"w": when}).scalar()
                held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
            assert listed == fresh == 98 and not held, (i, listed, fresh, held)
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_serverless, test_serverless_edges, test_pods_and_pools_are_read_apart, test_fetch,
              test_fetch_falls_back_to_graphql, test_rest_parse_matches_graphql, test_round_choice, test_pipeline,
              test_pipeline_switches_sources):
        t()
        print(t.__name__, "ok")
    print("all passed")
