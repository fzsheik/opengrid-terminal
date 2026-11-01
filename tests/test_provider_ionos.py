"""IONOS Cloud: Cloud GPU VM templates from IONOS CLOUD Inc.'s public USD price list.

Parser runs on the REAL price list (tests/fixtures_providers/ionos_cloud_inc.md, the Markdown copy of
docs.ionos.com/support/general-information/price-list/ionos-cloud-inc fetched 2026-11-01 with an anonymous GET,
trimmed to its title and the Cloud GPU VM table). No network.

Run:  .venv/bin/python tests/test_provider_ionos.py
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
from providers.ionos import ENDPOINT, IonosProvider, extract  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "ionos_cloud_inc.md"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def md():
    return FIX.read_text(encoding="utf-8")


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def test_normalize():
    out = {l.listing_id: l for l in IonosProvider.normalize({ENDPOINT: [snap(extract(md()))]})}
    assert sorted(out) == ["H200-L", "H200-M", "H200-S", "H200-XL"]
    s = out["H200-S"]
    assert (s.gpu_count, s.price_per_instance_hour, s.price_per_gpu_hour) == (1, D("3.261"), D("3.261"))
    assert (s.raw_gpu_name, s.canonical_gpu_name) == ("NVIDIA H200 PCIe", "NVIDIA H200 143GB NVL")
    assert (s.vcpu, s.ram_gb, s.storage_gb, s.region, s.country) == (15, 267.0, 1024.0, "de/fra/2; gb/bhx", None)
    xl = out["H200-XL"]
    assert xl.gpu_count == 8 and xl.price_per_instance_hour == D("26.086") and xl.price_per_gpu_hour == D("3.26075")
    assert all(l.currency == "USD" and l.original_currency is None and l.provider_tier == "vm"
               and l.available is None and l.market_type == "on_demand" for l in out.values())


def test_failures():
    assert raises(extract, md().replace("### Cloud GPU VM", "### GPU servers")), "section renamed"
    assert raises(extract, md().replace("| Price per hour |", "| Price per month |")), "columns changed"
    assert raises(extract, md().replace("# IONOS CLOUD Inc.", "# IONOS CLOUD (EUR)")), "not the USD list"
    p = extract(md())
    p["rows"][0]["Price per hour"] = "3,00€"
    assert "H200-S" not in {l.listing_id for l in IonosProvider.normalize({ENDPOINT: [snap(p)]})}
    for r in p["rows"]:
        r["Number of GPUs"] = "?"
    assert raises(IonosProvider.normalize, {ENDPOINT: [snap(p)]}), "nothing readable is a failure"
    assert IonosProvider.normalize({}) == []

    def handler(request):
        assert request.url.path == ENDPOINT
        return httpx.Response(200, text=md(), headers={"content-type": "text/markdown"})
    raws = asyncio.run(IonosProvider(httpx.AsyncClient(base_url="https://docs.ionos.com",
                                                       transport=httpx.MockTransport(handler))).fetch())
    assert [r.endpoint for r in raws] == [ENDPOINT] and raws[0].ok and raws[0].payload == extract(md())


def test_registry():
    assert PROVIDERS["ionos"] is IonosProvider and IonosProvider.polling.interval_seconds == 3600
    assert "IonosProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "ionos" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("ionos")
    assert (m.provider_class, m.source_type, m.source) == ("general_cloud", "public_page", "docs.ionos.com")


if __name__ == "__main__":
    for t in (test_normalize, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
