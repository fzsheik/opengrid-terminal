"""Exoscale: the public opencompute price feed, GPU table from its pricing page.

Parser runs on the REAL response (tests/fixtures_providers/exoscale_opencompute.json, fetched
2026-11-01 with an anonymous GET).

Run:  .venv/bin/python tests/test_provider_exoscale.py
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
from providers.exoscale import PRICING, ExoscaleProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "exoscale_opencompute.json"
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


def test_normalize():
    out = {l.listing_id: l for l in ExoscaleProvider.normalize({PRICING: [snap(body())]})}
    assert len(out) == 22, sorted(out)  # 26 GPU keys minus the 4 legacy "gpu_*" ones
    assert not any(k.startswith("gpu.") for k in out), "legacy family not on the page: skipped"
    a40 = out["gpu3.huge"]
    assert a40.gpu_count == 8 and a40.price_per_instance_hour == D("8.36270000")
    assert a40.price_per_gpu_hour == D("8.3627") / 8 and a40.canonical_gpu_name == "NVIDIA A40 48GB"
    assert (a40.vcpu, a40.ram_gb, a40.region, a40.country) == (96, 448.0, "DE-FRA-1", "DE")
    v = out["gpu2.large"]
    assert v.gpu_count == 3 and v.canonical_gpu_name == "NVIDIA Tesla V100 16GB" and v.country == "AT"
    assert out["gpua5000.small"].canonical_gpu_name == "NVIDIA RTX A5000 24GB"
    assert out["gpu3080ti.medium"].canonical_gpu_name == "NVIDIA RTX 3080 Ti 12GB"
    assert out["gpua30.small"].canonical_gpu_name == "NVIDIA A30 24GB" and out["gpua30.small"].country == "CH"
    pro = out["gpurtx6000pro.small"]
    assert pro.canonical_gpu_name is None and pro.country is None and pro.price_per_gpu_hour == D("2.15277778")
    assert all(l.available is None and l.capacity is None and l.currency == "USD" for l in out.values())


def test_failures():
    assert raises(ExoscaleProvider.normalize, {PRICING: [snap({"chf": {}})]}), "no usd map"
    assert raises(ExoscaleProvider.normalize, {PRICING: [snap({"usd": {"running_small": "0.02"}})]}), "no GPU key"
    b = body()
    b["usd"]["running_gpu9_small"] = "1.0"
    assert "gpu9.small" not in {l.sku for l in ExoscaleProvider.normalize({PRICING: [snap(b)]})}, "unknown family"
    assert ExoscaleProvider.normalize({}) == []

    def handler(request):
        return httpx.Response(200, text=FIX.read_text(encoding="utf-8"), headers={"content-type": "application/json"})
    raws = asyncio.run(ExoscaleProvider(httpx.AsyncClient(base_url="https://portal.exoscale.com",
                                                          transport=httpx.MockTransport(handler))).fetch())
    assert [r.endpoint for r in raws] == [PRICING] and raws[0].ok and raws[0].payload == body()


def test_registry():
    assert PROVIDERS["exoscale"] is ExoscaleProvider
    assert "ExoscaleProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "exoscale" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("exoscale")
    assert (m.provider_class, m.source_type, m.source) == ("general_cloud", "public_api", "portal.exoscale.com")


if __name__ == "__main__":
    for t in (test_normalize, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
