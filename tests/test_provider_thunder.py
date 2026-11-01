"""Thunder Compute: price map, spec catalogue and live stock, read with an account key.

Parsers run on copies of the REAL responses (tests/fixtures_providers/thunder_v2_*.json,
fetched 2026-10-21). The key used here is a fake: the mock checks it is sent as a
Bearer token, because Thunder's terms allow automated access only that way. The
last test pushes the provider through raw storage, normalize + quality screening,
the hourly rollup and the index vote in a scratch database.

Run:  .venv/bin/python tests/test_provider_thunder.py
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

import canonical  # noqa: E402
import fetch as fetch_mod  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from config import settings  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.thundercompute import PRICING, SPECS, STATUS, ThunderComputeProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
KEY = "test-thunder-key"


def text(name):
    return (FIX / name).read_text(encoding="utf-8")


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


def _serve(routes: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, text="unauthorized")
        path = request.url.raw_path.decode()
        for prefix, body in routes.items():
            if path.startswith(prefix):
                return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def thunder_prov(key=KEY):
    return ThunderComputeProvider(key, httpx.AsyncClient(
        base_url="https://api.thundercompute.com:8443",
        headers={"Authorization": f"Bearer {key}"},
        transport=_serve({PRICING: text("thunder_v2_pricing.json"), SPECS: text("thunder_v2_specs.json"),
                          STATUS: text("thunder_v2_status.json")})))


def _fetch(prov):
    return asyncio.run(prov.fetch())


def test_thundercompute():
    raws = _fetch(thunder_prov())
    assert [r.endpoint for r in raws] == [PRICING, SPECS, STATUS] and all(r.ok for r in raws)
    by_ep = {r.endpoint: [snap(r.payload)] for r in raws}
    out = by_id(ThunderComputeProvider.normalize(by_ep))
    assert sorted(out) == ["a100xl_x1", "a100xl_x2", "a100xl_x4", "a100xl_x8", "a6000_x1",
                           "h100_x1", "h100_x2", "h100_x4", "h100_x8", "l40_x1", "l40_x2", "l40_x4", "l40_x8"]
    h = out["h100_x2"]
    assert h.raw_gpu_name == "NVIDIA H100 80GB" and h.canonical_gpu_name is None, "SXM/PCIe not in the specs"
    assert h.gpu_count == 2 and h.price_per_instance_hour == D("6.4") and h.price_per_gpu_hour == D("3.2")
    assert h.vcpu == 8 and h.ram_gb == 64 and h.storage_gb == 200
    assert h.region is None and h.market_type == "on_demand" and not h.interruptible
    # Live stock from /v2/status.
    assert h.available is False and out["a100xl_x1"].available is True and out["l40_x8"].available is False
    a4 = out["a100xl_x4"]
    assert a4.raw_gpu_name == "NVIDIA A100 80GB" and a4.canonical_gpu_name is None
    assert a4.price_per_gpu_hour == D("5.96") / 4 == D("1.49"), "not linear in GPU count: read, never extrapolated"
    assert a4.vcpu == 36 and a4.ram_gb == 420 - (60 - 36) * 8, "ramCapGiB rule"
    assert out["a6000_x1"].canonical_gpu_name == "NVIDIA RTX A6000 48GB" and out["a6000_x1"].price_per_gpu_hour == D("0.35")
    assert out["l40_x8"].canonical_gpu_name == "NVIDIA L40 48GB" and out["l40_x8"].price_per_gpu_hour == D("0.99")
    # Legacy mode keys, sandboxes and add-on rates give no listing.
    for gone in ("h100", "a100xl", "a100xl_native", "h100_native", "sandbox_h100", "additional_vcpus", "disk_gb"):
        assert gone not in out, gone

    # Stock is optional: without the status call, or with an unknown word, `available` is unknown.
    no_status = {PRICING: by_ep[PRICING], SPECS: by_ep[SPECS]}
    assert all(l.available is None for l in ThunderComputeProvider.normalize(no_status))
    odd = json.loads(json.dumps(raws[2].payload))
    odd["specs"]["a100xl_x1"] = "limited"
    assert by_id(ThunderComputeProvider.normalize({**no_status, STATUS: [snap(odd)]}))["a100xl_x1"].available is None

    # A spec that disagrees with its key, or with its own name's VRAM, is skipped, never guessed.
    specs = json.loads(json.dumps(raws[1].payload))
    specs["specs"]["h100_x2"]["gpuCount"] = 1
    specs["specs"]["a100xl_x1"]["vramGB"] = 40
    out = by_id(ThunderComputeProvider.normalize({PRICING: by_ep[PRICING], SPECS: [snap(specs)]}))
    assert "h100_x2" not in out and "a100xl_x1" not in out and "h100_x1" in out
    # A priced size without a spec is skipped.
    del specs["specs"]["l40_x1"]
    assert "l40_x1" not in by_id(ThunderComputeProvider.normalize({PRICING: by_ep[PRICING], SPECS: [snap(specs)]}))
    # Fail closed.
    assert raises(ThunderComputeProvider.normalize, {PRICING: [snap({"prices": {}})], SPECS: by_ep[SPECS]})
    assert raises(ThunderComputeProvider.normalize, {PRICING: by_ep[PRICING], SPECS: [snap({"gpus": []})]})
    assert raises(ThunderComputeProvider.normalize, {PRICING: [snap({"pricing": {"h100": 3.2}})], SPECS: by_ep[SPECS]}), \
        "no sized keys left: reshaped, not 'nothing for sale'"
    lost = json.loads(json.dumps(raws[1].payload))
    del lost["specs"]["h100_x1"]["vcpuOptions"]
    assert raises(ThunderComputeProvider.normalize, {PRICING: by_ep[PRICING], SPECS: [snap(lost)]})


def test_capacity():
    raws = _fetch(thunder_prov())
    by_ep = {r.endpoint: [snap(r.payload)] for r in raws}
    rows = {r.sku: r for r in ThunderComputeProvider.normalize_capacity(by_ep)}
    listings = by_id(ThunderComputeProvider.normalize(by_ep))
    assert set(rows) == set(listings), "one row per priced size, same sku as the listing"
    assert {k for k, r in rows.items() if r.in_stock} == {"a100xl_x1", "a100xl_x2", "a100xl_x4", "a100xl_x8",
                                                         "a6000_x1", "l40_x1", "l40_x2", "l40_x4"}
    h = rows["h100_x8"]
    assert (h.in_stock, h.raw_value, h.gpu_count, h.raw_gpu_name) == (False, "unavailable", 8, "NVIDIA H100 80GB")
    assert h.region is None and h.tier is None and h.available_count is None and h.source == STATUS
    assert rows["l40_x2"].canonical_gpu_name == "NVIDIA L40 48GB"
    assert all(r.in_stock == listings[k].available for k, r in rows.items()), "agrees with the listing flag"
    # An unknown word is kept, never guessed; a size the status map omits gets no row.
    odd = json.loads(json.dumps(raws[2].payload))
    odd["specs"]["a100xl_x1"] = "limited"
    del odd["specs"]["l40_x1"]
    rows = {r.sku: r for r in ThunderComputeProvider.normalize_capacity({**by_ep, STATUS: [snap(odd)]})}
    assert rows["a100xl_x1"].in_stock is None and rows["a100xl_x1"].raw_value == "limited" and "l40_x1" not in rows
    # No status call: no capacity (prices are unaffected). A reshaped status raises.
    assert ThunderComputeProvider.normalize_capacity({PRICING: by_ep[PRICING], SPECS: by_ep[SPECS]}) == []
    assert ThunderComputeProvider.normalize_capacity({}) == []
    assert raises(ThunderComputeProvider.normalize_capacity, {**by_ep, STATUS: [snap({"sizes": []})]})


def test_fetch_failures():
    """An error response is stored as a failed response, never as prices; normalize refuses it."""
    raws = _fetch(thunder_prov(key="wrong-key"))
    assert raws and all(not r.ok and r.status_code == 401 and r.payload is None for r in raws)
    assert raises(ThunderComputeProvider.normalize, {r.endpoint: [snap(r.payload)] for r in raws})
    failing = httpx.MockTransport(lambda r: httpx.Response(503, text="maintenance"))
    raws = _fetch(ThunderComputeProvider(KEY, httpx.AsyncClient(base_url="https://x", transport=failing)))
    assert raws and all(not r.ok and r.payload is None for r in raws)
    assert raises(ThunderComputeProvider.normalize, {r.endpoint: [snap(r.payload)] for r in raws})


def test_registry():
    assert "thundercompute" in PROVIDERS
    assert "thundercompute" in mapping.PROVIDER_MAPPINGS and "thundercompute" not in mapping.check_mapping_coverage()
    m = provider_meta.meta("thundercompute")
    assert (m.provider_class, m.source_type) == ("neocloud", "authenticated_api")
    # Polled only with the account key.
    saved = settings.thunder_api_key
    try:
        settings.thunder_api_key = None
        assert "thundercompute" not in {p.name for p in fetch_mod.build_fetchers()}
        settings.thunder_api_key = KEY
        assert "thundercompute" in {p.name for p in fetch_mod.build_fetchers()}
    finally:
        settings.thunder_api_key = saved
    for bare in ("NVIDIA H100 80GB", "NVIDIA A100 80GB"):
        assert canonical.canonical_gpu_name(bare) is None, bare


def test_pipeline():
    import fixtures
    import normalize
    import scratchdb
    from sqlalchemy import text as sql
    from analytics import indices, rollups

    db = "opengrid_test_thunder"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session

        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=3), now - timedelta(minutes=5)):  # two polls, three hours apart
            responses = [r.model_copy(update={"fetched_at": when}) for r in _fetch(thunder_prov())]
            assert all(r.ok for r in responses), [r.error for r in responses if not r.ok]
            fetch_mod.save(responses)
            normalize.refresh(["thundercompute"])

        with Session() as s:
            stored = dict(s.execute(sql("SELECT provider, count(*) FROM raw_snapshots GROUP BY 1")).all())
            bodies = s.execute(sql("SELECT count(*) FROM raw_payloads")).scalar()
            listed = dict(s.execute(sql("SELECT provider, count(*) FROM compute_listings GROUP BY 1")).all())
            held = s.execute(sql("SELECT provider, listing_id, rule FROM quality_quarantine WHERE status = 'pending'")).all()
            secret = s.execute(sql("SELECT count(*) FROM raw_snapshots WHERE request::text LIKE :k"),
                               {"k": f"%{KEY}%"}).scalar()
        assert stored == {"thundercompute": 6}, stored
        assert bodies == 3, "identical bodies from the second poll are stored once"
        assert listed == {"thundercompute": 13}, listed
        assert not held, f"nothing held on clean data: {held}"
        assert secret == 0, "the key is never stored"

        rollups.rebuild()
        with Session() as s:
            last = s.execute(sql("SELECT max(hour) FROM market_hourly")).scalar()
            rows = [dict(r._mapping) for r in s.execute(sql(
                "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly "
                "WHERE hour = :h AND min_price IS NOT NULL"), {"h": last})]
        votes = indices.votes_by_index(rows)
        a6000 = votes[indices.gpu_index_id("NVIDIA RTX A6000 48GB")]
        assert set(a6000) == {"thundercompute"} and abs(a6000["thundercompute"][0] - 0.35) < 1e-9
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_thundercompute, test_capacity, test_fetch_failures, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
