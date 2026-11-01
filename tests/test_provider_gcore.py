"""Gcore GPU Cloud: the public AI pricing page, EUR converted at the ECB rate.

Parser runs on the REAL page (tests/fixtures_providers/gcore_pricing_ai.html, gcore.com/pricing/ai fetched
2026-11-01 with an anonymous GET, trimmed to its GPU tables). No network, no database (fx.rate_at is stubbed).

Run:  .venv/bin/python tests/test_provider_gcore.py
"""

import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
import fx  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.gcore import ENDPOINT, GcoreProvider, extract  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "gcore_pricing_ai.html"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def page():
    return FIX.read_text(encoding="utf-8")


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


@contextmanager
def ecb_rate(rate="1.1500", day=None):
    real = fx.rate_at
    fixed = None if rate is None else fx.Rate("EUR", "USD", D(rate), day or (NOW - timedelta(days=1)).date())
    fx.rate_at = lambda at, base="EUR", quote="USD": fx.pick([fixed] if fixed else [], at)
    try:
        yield
    finally:
        fx.rate_at = real


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def test_extract():
    p = extract(page())
    bm = p["bare_metal"]
    assert bm["header"][0] == "H100 Get started" and bm["header"][4] == "NEW B300 Contact sales"
    assert bm["rows"]["GPU"][:4] == ["8×H100 SXM", "8×A100 SXM", "8×L40S PCIe", "8×H200 SXM"]
    assert bm["rows"]["Price"][0] == "€21.82 /hr €2.73 per GPU" and bm["rows"]["Price"][5] == "Contact sales"
    assert p["virtual"][0] == ["1×H100", "80 GB", "24 vCPU / 232 GB RAM", "1250 GB", "€2.82", "Get started"]
    assert len(p["virtual"]) == 4, "Everywhere Inference (no Storage column) is not taken for Virtual GPU"


def test_normalize():
    with ecb_rate("1.1500"):
        out = {l.listing_id: l for l in GcoreProvider.normalize({ENDPOINT: [snap(extract(page()))]})}
    assert sorted(out) == ["bare-metal:8x-a100-sxm", "bare-metal:8x-h100-sxm", "bare-metal:8x-h200-sxm",
                           "bare-metal:8x-l40s-pcie", "vm:1x-h100", "vm:2x-h100", "vm:4x-h100", "vm:8x-h100"], sorted(out)
    h = out["bare-metal:8x-h100-sxm"]
    assert (h.gpu_count, h.raw_gpu_name, h.canonical_gpu_name) == (8, "H100 SXM 80GB", "NVIDIA H100 80GB SXM5")
    assert h.original_currency == "EUR" and h.original_price_per_instance_hour == D("21.82")
    assert h.price_per_instance_hour == D("25.093000") and h.price_per_gpu_hour == D("3.136625"), h
    assert (h.currency, h.fx_rate, h.provider_tier, h.vcpu, h.ram_gb) == ("USD", D("1.1500"), "bare-metal", 224, 2048.0)
    assert out["bare-metal:8x-a100-sxm"].canonical_gpu_name == "NVIDIA A100 80GB SXM4"
    assert out["bare-metal:8x-l40s-pcie"].canonical_gpu_name == "NVIDIA L40S 48GB"
    assert out["bare-metal:8x-h200-sxm"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    v = out["vm:1x-h100"]
    assert (v.raw_gpu_name, v.canonical_gpu_name, v.provider_tier) == ("H100 80GB", None, "vm"), "SXM/PCIe not stated"
    assert (v.vcpu, v.ram_gb, v.storage_gb, v.original_price_per_instance_hour) == (24, 232.0, 1250.0, D("2.82"))
    assert out["vm:8x-h100"].original_price_per_gpu_hour == D("2.363750")
    assert all(l.region is None and l.available is None and l.market_type == "on_demand" for l in out.values())
    assert not any("b300" in k or "gb300" in k for k in out), "Contact-sales columns are not self-serve"


def test_failures():
    with ecb_rate(None):
        assert GcoreProvider.normalize({ENDPOINT: [snap(extract(page()))]}) == [], "no ECB rate: fail closed"
    with ecb_rate("1.15", (NOW - timedelta(days=fx.MAX_AGE_DAYS + 1)).date()):
        assert GcoreProvider.normalize({ENDPOINT: [snap(extract(page()))]}) == [], "stale rate: fail closed"
    assert raises(extract, "<html><table><tr><td>x</td></tr></table></html>"), "no GPU tables"
    assert raises(GcoreProvider.normalize, {ENDPOINT: [snap({"rows": []})]}), "reshaped payload"
    p = extract(page())
    p["bare_metal"]["rows"]["Price"][0] = "€21.82 /hr €2.99 per GPU"  # per-GPU disagrees with total / 8
    with ecb_rate():
        ids = {l.listing_id for l in GcoreProvider.normalize({ENDPOINT: [snap(p)]})}
    assert "bare-metal:8x-h100-sxm" not in ids and "bare-metal:8x-a100-sxm" in ids
    p = extract(page())
    p["bare_metal"]["header"] = [h.replace("Get started", "Contact sales") for h in p["bare_metal"]["header"]]
    p["virtual"] = [["1×H100", "80 GB", "?", "?", "price on request", ""]]
    with ecb_rate():
        assert raises(GcoreProvider.normalize, {ENDPOINT: [snap(p)]}), "nothing readable is a failure, not zero rows"
    assert GcoreProvider.normalize({}) == []

    def handler(request):
        assert request.url.path == ENDPOINT
        return httpx.Response(200, text=page(), headers={"content-type": "text/html"})
    raws = asyncio.run(GcoreProvider(httpx.AsyncClient(base_url="https://gcore.com",
                                                       transport=httpx.MockTransport(handler))).fetch())
    assert [r.endpoint for r in raws] == [ENDPOINT] and raws[0].ok and raws[0].payload == extract(page())
    bad = asyncio.run(GcoreProvider(httpx.AsyncClient(base_url="https://gcore.com", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>redesign</html>")))).fetch())
    assert not bad[0].ok and "not found" in bad[0].error


def test_registry():
    assert PROVIDERS["gcore"] is GcoreProvider and GcoreProvider.polling.interval_seconds == 3600
    assert "GcoreProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "gcore" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("gcore")
    assert (m.provider_class, m.source_type, m.source) == ("general_cloud", "public_page", "gcore.com")


if __name__ == "__main__":
    for t in (test_extract, test_normalize, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
