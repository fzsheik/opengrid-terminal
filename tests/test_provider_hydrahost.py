"""Hydra Host: the public Brokkr inventory, prices in cents, on-demand and interruptible.

Parser runs on the REAL response (tests/fixtures_providers/hydrahost_inventory.json, fetched
2026-11-01 with an anonymous GET, pageSize=100; OS lists and disk layouts removed, the IP
replaced with a documentation address).

Run:  .venv/bin/python tests/test_provider_hydrahost.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.hydrahost import INVENTORY, HydraHostProvider, country_of  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "hydrahost_inventory.json"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)
EP = f"{INVENTORY}?page=1&pageSize=100"


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


def by_id(listings):
    return {l.listing_id: l for l in listings}


def test_normalize():
    out = by_id(HydraHostProvider.normalize({EP: [snap(body())]}))
    devices = {d["id"]: d for d in body()["data"]}
    gpu_devices = [d for d in devices.values() if d["specs"]["gpu"]["count"]]
    assert {l.sku for l in out.values()} == {d["id"] for d in gpu_devices}, "every GPU device, no CPU server"
    # 8x L40S in Singapore: perGpu 59.375 cents, total 475 cents.
    sg = out["67df38a5-89a4-421c-853f-e6cec92c3ec0"]
    assert sg.price_per_gpu_hour == D("0.59375") and sg.price_per_instance_hour == D("4.75") and sg.gpu_count == 8
    assert sg.canonical_gpu_name == "NVIDIA L40S 48GB" and sg.region == "Singapore" and sg.country == "SG"
    assert sg.available is False and sg.market_type == "on_demand" and not sg.interruptible, "stockStatus reserve"
    assert sg.vcpu == 256 and sg.ram_gb == 2036 and sg.storage_gb == 29061 and sg.provider_tier == "baremetal"
    spot = out["67df38a5-89a4-421c-853f-e6cec92c3ec0:interruptible"]
    assert spot.price_per_gpu_hour == D("0.425") and spot.market_type == "spot" and spot.interruptible
    # Deployable now: "on demand".
    now = [l for l in out.values() if l.available]
    assert now and all(devices[l.sku]["stockStatus"] == "on demand" for l in now)
    a6000 = [l for l in now if l.canonical_gpu_name == "NVIDIA RTX A6000 48GB"]
    assert len(a6000) == 1 and a6000[0].price_per_gpu_hour == D("0.60") and a6000[0].country == "US"
    # Interruptible-only devices give no on-demand listing.
    only = [d["id"] for d in gpu_devices if d["listing"]["isInterruptibleOnly"]]
    assert only and all(i not in out and f"{i}:interruptible" in out for i in only)
    assert any(l.raw_gpu_name == "Quadro RTX 8000" and l.canonical_gpu_name == "NVIDIA Quadro RTX 8000 48GB" for l in out.values())
    h100 = [l for l in out.values() if l.raw_gpu_name == "NVIDIA H100 80GB HBM3" and l.market_type == "on_demand"]
    assert h100[0].canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100[0].price_per_gpu_hour == D("2.25")


def test_capacity():
    rows = HydraHostProvider.normalize_capacity({EP: [snap(body())]})
    listings = HydraHostProvider.normalize({EP: [snap(body())]})
    assert {r.sku for r in rows} == {l.sku for l in listings}, "same sku as the listings"
    for r in rows:
        on = [l for l in listings if l.sku == r.sku and l.market_type == "on_demand"]
        assert not on or on[0].available == r.in_stock
    assert {r.raw_value for r in rows} <= {"on demand", "reserve", "preorder"}
    assert sum(r.in_stock for r in rows) == 4


def test_failures():
    b = body()
    b["data"][0]["listing"]["onDemandPrice"]["perHour"]["total"] = 999  # disagrees with perGpu x 8
    out = by_id(HydraHostProvider.normalize({EP: [snap(b)]}))
    assert "67df38a5-89a4-421c-853f-e6cec92c3ec0" not in out, "inconsistent cents are skipped, never guessed"
    assert "67df38a5-89a4-421c-853f-e6cec92c3ec0:interruptible" in out
    b = body()
    b["data"][0]["stockStatus"] = "soon"
    assert by_id(HydraHostProvider.normalize({EP: [snap(b)]}))["67df38a5-89a4-421c-853f-e6cec92c3ec0"].available is None
    assert raises(HydraHostProvider.normalize, {EP: [snap({"items": []})]}), "reshaped"
    assert raises(HydraHostProvider.normalize, {EP: [snap({"data": []})]}), "empty inventory is a failure"
    assert HydraHostProvider.normalize({}) == []
    assert country_of("Texas") == "US" and country_of("Atlantis") is None

    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, text=FIX.read_text(encoding="utf-8"), headers={"content-type": "application/json"})
    prov = HydraHostProvider(httpx.AsyncClient(base_url="https://brokkr.hydrahost.com", transport=httpx.MockTransport(handler)))
    raws = asyncio.run(prov.fetch())
    assert len(raws) == 1 and raws[0].endpoint == EP and raws[0].ok, "meta says one page"
    assert seen == [{"page": "1", "pageSize": "100"}]
    stored = json.dumps(raws[0].payload)
    assert "203.0.113.10" not in stored and "networking" not in stored, "no IP addresses stored"
    assert by_id(HydraHostProvider.normalize({EP: [snap(raws[0].payload)]})).keys() == \
        by_id(HydraHostProvider.normalize({EP: [snap(body())]})).keys(), "trimming keeps every field read"
    failing = httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    raws = asyncio.run(HydraHostProvider(httpx.AsyncClient(base_url="https://x", transport=failing)).fetch())
    assert len(raws) == 1 and not raws[0].ok
    assert raises(HydraHostProvider.normalize, {raws[0].endpoint: [snap(raws[0].payload)]})


def test_registry():
    assert PROVIDERS["hydrahost"] is HydraHostProvider
    assert "HydraHostProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "hydrahost" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("hydrahost")
    assert (m.provider_class, m.source_type, m.source) == ("marketplace", "public_api", "brokkr.hydrahost.com")


if __name__ == "__main__":
    for t in (test_normalize, test_capacity, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
