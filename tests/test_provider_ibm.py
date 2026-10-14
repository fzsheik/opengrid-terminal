"""IBM Cloud VPC GPU prices from the anonymous Global Catalog pricing API.

The parser runs on trimmed copies of the REAL responses
(tests/fixtures_providers/ibm_gpu_pricing.json, fetched 2026-10-13 with
unauthenticated GETs of /api/v1/{plan}/pricing/deployment for the ten GPU plans;
kept: four regions per plan, the instance-hour / spot / dedicated-host metrics,
the USA amounts). The last test runs the pipeline in a scratch database.

Run:  .venv/bin/python tests/test_provider_ibm.py
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
from providers import PROVIDERS, IbmProvider  # noqa: E402
from providers import ibm  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
BODIES: dict = json.loads((FIX / "ibm_gpu_pricing.json").read_text(encoding="utf-8"))
H100 = "99548819-fcbc-486c-8eeb-0355a56b035e"
A100 = "fb875111-9688-4b8e-8ae6-e5e74497662f"


def snap(payload, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def _serve(bodies: dict, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        for plan_id, body in bodies.items():
            if path == ibm.endpoint(plan_id):
                return httpx.Response(status, text=json.dumps(body), headers={"content-type": "application/json"})
        return httpx.Response(404, text='{"message": "not found"}')
    return httpx.MockTransport(handler)


def _provider(bodies=BODIES, status=200):
    return IbmProvider(httpx.AsyncClient(base_url="https://globalcatalog.cloud.ibm.com", transport=_serve(bodies, status)))


def _fetch_and_normalize(bodies=BODIES):
    raws = asyncio.run(_provider(bodies).fetch())
    return raws, {l.listing_id: l for l in IbmProvider.normalize({r.endpoint: [snap(r.payload)] for r in raws if r.ok})}


def test_fetch():
    raws = asyncio.run(_provider().fetch())
    assert len(raws) == 10 and all(r.ok for r in raws), [r.error for r in raws if not r.ok]
    assert sorted(r.endpoint for r in raws) == sorted(ibm.endpoint(p) for p in ibm.PROFILES)
    h100 = next(r for r in raws if r.endpoint == ibm.endpoint(H100)).payload
    metrics = {m["charge_unit_name"] for res in h100["resources"] for m in res["metrics"]}
    assert metrics == {ibm.ON_DEMAND, ibm.SPOT}, "dedicated host is dropped"
    assert all(a["country"] == "USA" for res in h100["resources"] for m in res["metrics"] for a in m["amounts"])


def test_normalize():
    _, out = _fetch_and_normalize()
    h100 = out["gx3d-160x1792x8h100:us-south"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.raw_gpu_name == "NVIDIA H100 SXM5 80GB"
    assert h100.gpu_count == 8 and h100.price_per_instance_hour == D("85.85")
    assert h100.price_per_gpu_hour == D("85.85") / 8 == D("10.73125") and isinstance(h100.price_per_gpu_hour, D)
    assert h100.market_type == "on_demand" and not h100.interruptible and h100.available is None
    assert h100.region == "us-south" and h100.country is None and h100.provider_tier == "vm"
    assert h100.vcpu == 160 and h100.ram_gb == 1792.0 and h100.observed_at == NOW
    spot = out["gx3d-160x1792x8h100:us-south:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.price_per_instance_hour == D("21.4625")
    assert out["gx3d-160x1792x8h100:jp-tok"].price_per_instance_hour == D("104.73"), "regions priced separately"

    assert out["gx3d-160x1792x8h200:eu-de"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    l4 = out["gx3-64x320x4l4:us-east"]
    assert l4.canonical_gpu_name == "NVIDIA L4 24GB" and l4.gpu_count == 4 and l4.price_per_gpu_hour == D("1.555")
    l40s = out["gx3-48x240x2l40s:us-south"]
    assert l40s.canonical_gpu_name == "NVIDIA L40S 48GB" and l40s.price_per_gpu_hour == D("3.25")
    a100 = out["gx3d-24x120x1a100p:us-east"]
    assert a100.canonical_gpu_name == "NVIDIA A100 80GB PCIe" and a100.price_per_gpu_hour == D("4.633")
    assert "gx3d-24x120x1a100p:us-east:spot" not in out, "the A100 profiles have no spot price"
    gaudi = out["gx3d-160x1792x8gaudi3:us-south"]
    assert gaudi.canonical_gpu_name is None and gaudi.price_per_gpu_hour == D("60.6") / 8

    # 10 plans x regions: 8 plans with 4 regions and spot, H200/Gaudi 3 with 3 regions and spot,
    # two A100 plans with 4 regions (us-east listed twice, collapsed) and no spot.
    assert len(out) == 6 * 4 * 2 + 2 * 3 * 2 + 2 * 4, len(out)
    assert len([l for l in out.values() if l.market_type == "on_demand"]) == 6 * 4 + 2 * 3 + 2 * 4


def test_zone_disagreement_and_fail_closed():
    # Two us-east deployments that disagree: the region is skipped, never averaged.
    bodies = copy.deepcopy(BODIES)
    east = [r for r in bodies[A100]["resources"] if r["deployment_location"] == "us-east"]
    m = next(m for m in east[1]["metrics"] if m["charge_unit_name"] == ibm.ON_DEMAND)
    m["amounts"][0]["prices"][0]["price"] = 9.99
    _, out = _fetch_and_normalize(bodies)
    assert "gx3d-24x120x1a100p:us-east" not in out and "gx3d-24x120x1a100p:us-south" in out

    def broken(edit):
        b = copy.deepcopy(BODIES[H100])
        edit(b)
        return b

    def od(b):
        return next(m for m in b["resources"][0]["metrics"] if m["charge_unit_name"] == ibm.ON_DEMAND)

    cases = [
        broken(lambda b: b.pop("resources")),
        broken(lambda b: b["resources"][0].update(metrics=[])),
        broken(lambda b: od(b).update(charge_unit="Month")),
        broken(lambda b: od(b).update(metric_id="part-is.instance-hours-gx3d-160x1792x8h200")),
        broken(lambda b: od(b)["amounts"][0].update(currency="EUR")),
        broken(lambda b: od(b)["amounts"][0]["prices"].append({"quantity_tier": 100, "price": 1})),
        broken(lambda b: od(b)["amounts"][0]["prices"][0].update(price="lots")),
        broken(lambda b: b["resources"][0].update(deployment_id="someone-else:us-south")),
        broken(lambda b: b.update(resources=[])),
    ]
    for bad in cases:
        assert raises(ibm.extract, H100, bad)
        assert raises(IbmProvider.normalize, {ibm.endpoint(H100): [snap(bad)]}), "normalize fails closed too"
    assert raises(ibm.extract, H100, broken(lambda b: b.update(count=50))), "paginated: not all returned"

    # Through fetch(): a reshaped plan is a failed response; the other plans still parse.
    bodies = copy.deepcopy(BODIES)
    bodies[H100] = cases[2]
    raws = asyncio.run(_provider(bodies).fetch())
    bad = next(r for r in raws if r.endpoint == ibm.endpoint(H100))
    assert not bad.ok and bad.payload is None and "ValueError" in bad.error
    assert sum(r.ok for r in raws) == 9
    raws = asyncio.run(_provider(status=503).fetch())
    assert not any(r.ok for r in raws) and all(r.status_code == 503 for r in raws)
    assert IbmProvider.normalize({}) == []


def test_registry():
    assert PROVIDERS["ibm"] is IbmProvider
    assert any(isinstance(p, IbmProvider) for p in fetch_mod.build_fetchers())
    assert mapping.check_mapping_coverage() == {} and "ibm" in mapping.PROVIDER_MAPPINGS
    m = provider_meta.meta("ibm")
    assert m.provider_class == "general_cloud" and m.source_type == "public_api"
    unmapped = {n for _, _, n in ibm.PROFILES.values() if canonical.canonical_gpu_name(n) is None}
    assert unmapped == {"Intel Gaudi 3 128GB"}, unmapped
    # The profile name and the table agree on GPU count.
    for profile, count, _ in ibm.PROFILES.values():
        assert profile.split("x")[-1].startswith(str(count)), profile


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_ibm"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):
            responses = [r.model_copy(update={"fetched_at": when}) for r in asyncio.run(_provider().fetch())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(["ibm"])

        with Session() as s:
            stored = s.execute(sql("SELECT count(*) FROM raw_snapshots WHERE provider = 'ibm'")).scalar()
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'ibm'")).scalar()
            held = s.execute(sql("SELECT listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
        assert stored == 20 and bodies == 10, (stored, bodies)
        assert listed == 68, listed
        assert not held, f"nothing held on clean data: {held}"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        h100 = votes[indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".general-cloud"]
        assert set(h100) == {"ibm"} and abs(h100["ibm"][0] - 10.73125) < 1e-9, "cheapest region, per GPU"
        assert "ibm" not in votes.get(indices.gpu_index_id("NVIDIA H100 80GB SXM5") + ".neocloud", {})
        spot = votes.get(indices.gpu_index_id("NVIDIA H100 80GB SXM5", "spot"), {})
        assert "ibm" in spot and abs(spot["ibm"][0] - 21.4625 / 8) < 1e-6, "spot stays in the spot segment"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_fetch, test_normalize, test_zone_disagreement_and_fail_closed, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
    print("all passed")
