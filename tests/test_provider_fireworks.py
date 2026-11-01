"""Fireworks AI: on-demand deployment prices from its public pricing page (internal-only source).

Parser runs on the REAL page (tests/fixtures_providers/fireworks_pricing.html, fireworks.ai/pricing fetched
2026-11-01 with an anonymous GET, trimmed to the On-Demand Pricing section). No network.

Run:  .venv/bin/python tests/test_provider_fireworks.py
"""

import asyncio
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
from providers.fireworks import ENDPOINT, FireworksProvider, extract  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "fireworks_pricing.html"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def page():
    return FIX.read_text(encoding="utf-8")


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def test_extract_and_normalize():
    p = extract(page())
    assert [r["gpu"] for r in p["rows"]] == ["H100 80 GB GPU", "H200 141 GB GPU", "B200 180 GB GPU",
                                             "B300 288 GB GPU", "GB300 288 GB GPU"]
    out = {l.listing_id: l for l in FireworksProvider.normalize({ENDPOINT: [snap(p)]})}
    assert len(out) == 5
    h = out["h100-80-gb-gpu"]
    assert (h.price_per_gpu_hour, h.price_per_instance_hour, h.gpu_count) == (D("8.00"), D("8.00"), 1)
    assert h.canonical_gpu_name is None, "H100: SXM or PCIe not stated"
    assert out["h200-141-gb-gpu"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    assert out["b200-180-gb-gpu"].canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert out["b200-180-gb-gpu"].price_per_gpu_hour == D("13.00")
    assert out["b300-288-gb-gpu"].canonical_gpu_name == "NVIDIA B300 288GB SXM"
    assert out["gb300-288-gb-gpu"].canonical_gpu_name == "NVIDIA GB300 288GB"
    assert all(l.provider_tier == "serverless" and l.currency == "USD" and l.region is None
               and l.available is None and l.market_type == "on_demand" for l in out.values())


def test_failures():
    p = extract(page())
    p["rows"][0]["per_minute"] = "0.200"  # x 60 = $12, not $8
    ids = {l.listing_id for l in FireworksProvider.normalize({ENDPOINT: [snap(p)]})}
    assert "h100-80-gb-gpu" not in ids and len(ids) == 4
    for r in p["rows"]:
        r["per_hour"] = "0.00"
    assert raises(FireworksProvider.normalize, {ENDPOINT: [snap(p)]}), "nothing readable is a failure"
    assert raises(FireworksProvider.normalize, {ENDPOINT: [snap({"x": 1})]})
    assert raises(extract, "<html><h2>Pricing</h2></html>")
    assert raises(extract, page().replace("Price ($) per minute", "Per min")), "header moved"
    assert FireworksProvider.normalize({}) == []

    def handler(request):
        assert request.url.path == ENDPOINT
        return httpx.Response(200, text=page(), headers={"content-type": "text/html"})
    raws = asyncio.run(FireworksProvider(httpx.AsyncClient(base_url="https://fireworks.ai",
                                                           transport=httpx.MockTransport(handler))).fetch())
    assert [r.endpoint for r in raws] == [ENDPOINT] and raws[0].ok and raws[0].payload == extract(page())


def test_registry():
    assert PROVIDERS["fireworks"] is FireworksProvider and FireworksProvider.polling.interval_seconds == 3600
    assert "FireworksProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "fireworks" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("fireworks")
    assert (m.provider_class, m.source_type, m.source) == ("general_cloud", "public_page", "fireworks.ai")
    assert "INTERNAL" in m.note and "INTERNAL ONLY" in (sys.modules["providers.fireworks"].__doc__ or "")


if __name__ == "__main__":
    for t in (test_extract_and_normalize, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
