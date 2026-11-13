"""Lium: the executors list (free machines), the public nodes feed (the whole fleet, rented machines too) and Lium's
public market statistics.

Fixtures (real answers, unauthenticated GETs to lium.io, 2026-11-13, trimmed to eight machines):
    lium_executors_with_nodes.json      /api/executors: the three of the eight with a free GPU
    lium_nodes_include_rented.json      /api/public/v1/nodes?include_rented=true: all eight (one free H200 x8, a
                                        half-rented RTX 3090 x4, rented H200 x8 and x4, A100 SXM4, RTX 6000 Ada, a
                                        spot B200 x8; one free RTX 6000 Ada)
    lium_public_stats.json              /api/public/v1/stats

Run:  .venv/bin/python tests/test_provider_lium.py
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

from providers import lium  # noqa: E402
from providers.lium import ENDPOINT, NODES, STATS, LiumProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 13, 4, tzinfo=timezone.utc)


def fx(name):
    return json.loads((FIX / name).read_text())


def serve(nodes_status=200, nodes_body=None, stats_body=None):
    seen = []

    def handler(request):
        seen.append(request.url)
        if request.url.path == ENDPOINT:
            return httpx.Response(200, json=fx("lium_executors_with_nodes.json"))
        if request.url.path == NODES:
            return httpx.Response(nodes_status, json=nodes_body if nodes_body is not None else fx("lium_nodes_include_rented.json"))
        if request.url.path == STATS:
            return httpx.Response(200, json=stats_body if stats_body is not None else fx("lium_public_stats.json"))
        return httpx.Response(404)

    return LiumProvider(httpx.AsyncClient(base_url=lium.BASE_URL, transport=httpx.MockTransport(handler))), seen


def by_endpoint(responses, at=NOW):
    return {r.endpoint: [SimpleNamespace(payload=r.payload, request=r.request, fetched_at=at, endpoint=r.endpoint)]
            for r in responses if r.ok}


def poll(**kw):
    prov, seen = serve(**kw)
    out = asyncio.run(prov.fetch())
    return out, seen


def test_fetch_stores_three_feeds():
    out, seen = poll()
    assert [r.endpoint for r in out] == [ENDPOINT, NODES, STATS] and all(r.ok for r in out)
    assert any(u.path == NODES and u.params.get("include_rented") == "true" for u in seen)
    nodes = out[1].payload
    assert len(nodes) == 8 and set(nodes[0]) == set(lium._NODE_FIELDS), "only the fields read; no live counters"
    assert "generated_at" not in out[2].payload and out[2].payload["fleet"]["gpus_listed"] == 2226


def test_rented_machines_stay_listed():
    out, _ = poll()
    listings = {l.listing_id: l for l in LiumProvider.normalize(by_endpoint(out))}
    assert len(listings) == 8, "three free machines from the executors list + five fully rented ones"
    rented = [l for l in listings.values() if l.available is False]
    assert len(rented) == 5 and all(l.capacity == 0 and l.capacity_unit == "gpu" for l in rented)
    h200 = next(l for l in rented if l.gpu_count == 8 and l.raw_gpu_name == "NVIDIA H200")
    assert h200.price_per_gpu_hour == D("4.04") and h200.price_per_instance_hour == D("32.32")
    assert h200.canonical_gpu_name == "NVIDIA H200 141GB SXM5" and h200.country and h200.region
    # A model no free machine carries this round still gets its nvidia-smi name (machine_name()).
    a100 = next(l for l in rented if l.gpu_count == 1 and "A100" in l.raw_gpu_name)
    assert a100.raw_gpu_name == "NVIDIA A100-SXM4-80GB" and a100.canonical_gpu_name == "NVIDIA A100 80GB SXM4"
    ada = next(l for l in rented if "6000 Ada" in l.raw_gpu_name)
    assert ada.raw_gpu_name == "NVIDIA RTX 6000 Ada Generation", "taken from the free machine of the same model"
    b200 = next(l for l in rented if "B200" in l.raw_gpu_name)
    assert b200.market_type == "spot" and b200.interruptible and b200.provider_tier == "spot"
    # The free machines are exactly what the executors list says.
    free = [l for l in listings.values() if l.available]
    assert len(free) == 3 and all(l.capacity and l.capacity > 0 for l in free)


def test_machine_name_rule():
    for model, name in (("RTX 5090", "NVIDIA GeForce RTX 5090"), ("RTX 4070 Ti SUPER", "NVIDIA GeForce RTX 4070 Ti SUPER"),
                        ("RTX 6000 Ada Generation", "NVIDIA RTX 6000 Ada Generation"), ("H200", "NVIDIA H200"),
                        ("RTX A6000", "NVIDIA RTX A6000"), ("RTX PRO 6000 Blackwell Server Edition",
                                                            "NVIDIA RTX PRO 6000 Blackwell Server Edition"),
                        ("T4 Tensor Core GPU", "NVIDIA T4 Tensor Core GPU"), ("A100-SXM4-80GB", "NVIDIA A100-SXM4-80GB")):
        assert lium.machine_name(model) == name, model


def test_capacity_counts_the_whole_fleet():
    out, _ = poll()
    be = by_endpoint(out)
    caps = LiumProvider.normalize_capacity(be)
    free, total = sum(c.available_count for c in caps), sum(c.total_count for c in caps)
    assert (free, total) == (8 + 2 + 1, 8 + 4 + 8 + 4 + 1 + 1 + 1 + 8), (free, total)
    # Without the nodes feed: the free machines only (the old reading).
    alone = LiumProvider.normalize_capacity({ENDPOINT: be[ENDPOINT]})
    assert sum(c.total_count for c in alone) == 8 + 4 + 1


def test_nodes_feed_failing_or_reshaped_changes_nothing_else():
    for kw in ({"nodes_status": 503}, {"nodes_body": {"nodes": {"x": 1}}}, {"nodes_body": ["not", "a", "dict"]}):
        out, _ = poll(**kw)
        assert out[0].ok and not out[1].ok, kw
        listings = LiumProvider.normalize(by_endpoint(out))
        assert len(listings) == 3 and all(l.available for l in listings), kw
    out, _ = poll(stats_body={"models": "nope"})
    assert not out[2].ok and out[1].ok


def test_free_in_nodes_but_gone_from_executors_is_not_guessed():
    """The executors list is fresher: a machine the (older) nodes snapshot shows free but the executors list has
    dropped was rented or went offline since. It is not re-listed from the nodes snapshot."""
    out, _ = poll()
    be = by_endpoint(out)
    be[ENDPOINT][0].payload = [e for e in be[ENDPOINT][0].payload if not e["id"].startswith("6c5f66c8")]
    ids = {l.listing_id for l in LiumProvider.normalize(be)}
    assert not any(i.startswith("6c5f66c8") for i in ids) and len(ids) == 7


def test_market_stats():
    out, _ = poll()
    st = lium.market_stats(by_endpoint(out))
    h200 = next(m for m in st["models"] if m["gpu_model"] == "H200")
    assert h200["median_price_30d"] == 3.85 and h200["utilisation_30d"] == 0.763 and st["observed_at"] == NOW
    assert st["throughput"]["days"] == 30 and len(st["throughput"]["daily"]) == 30
    assert lium.market_stats({}) is None


def test_pipeline():
    """Two polls through storage, normalize + screening and the rollup: rented machines are listings but never in
    market math (not in stock), and nothing is held."""
    import fetch as fetch_mod
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import rollups

    db = "opengrid_test_lium_nodes"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):
            out, _ = poll()
            fetch_mod.save([r.model_copy(update={"fetched_at": when}) for r in out])
            normalize.refresh(["lium"])
        with Session() as s:
            listed = dict(s.execute(sql("SELECT available, count(*) FROM compute_listings WHERE provider = 'lium' "
                                        "GROUP BY 1")).all())
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert listed == {True: 3, False: 5} and not held, (listed, held)
        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            gpus = {r[0] for r in s.execute(sql("SELECT gpu FROM market_hourly WHERE provider = 'lium' AND hour = :h "
                                                "AND min_price IS NOT NULL"), {"h": last})}
        assert "NVIDIA A100 80GB SXM4" not in gpus, "a fully rented machine is not a price you can pay now"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_fetch_stores_three_feeds, test_rented_machines_stay_listed, test_machine_name_rule,
              test_capacity_counts_the_whole_fleet, test_nodes_feed_failing_or_reshaped_changes_nothing_else,
              test_free_in_nodes_but_gone_from_executors_is_not_guessed, test_market_stats, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
