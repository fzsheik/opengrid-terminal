"""Paperspace, read through Shadeform's public catalogue.

Parser runs on the REAL Shadeform response (tests/fixtures_providers/shadeform_paperspace.json,
fetched 2026-11-01): Paperspace's 22 types, plus one Excess Supply and one DigitalOcean row
that must not be read as Paperspace.

Run:  .venv/bin/python tests/test_provider_paperspace.py
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

import canonical  # noqa: E402
import fetch as fetch_mod  # noqa: E402
import mapping  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers.shadeform import ENDPOINT, PaperspaceProvider, ShadeformCloud  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers" / "shadeform_paperspace.json"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)
H100_LABEL = "H100 (Paperspace: HGX per its page, pcie per Shadeform)"


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
    out = {l.listing_id: l for l in PaperspaceProvider.normalize({ENDPOINT: [snap(body())]})}
    assert len(out) == 22 and all(l.provider == "paperspace" for l in out.values())
    a4 = out["A4000"]
    # Shadeform's 80 cents = Paperspace's own $0.76 list + $0.04 (documented, stored as Shadeform's resale price).
    assert a4.price_per_instance_hour == D("0.80") and a4.gpu_count == 1 and a4.canonical_gpu_name == "NVIDIA RTX A4000 16GB"
    assert a4.available is True and a4.region == "US, New York, NY" and a4.country == "US"
    assert a4.provider_tier == "vm/via-shadeform" and a4.market_type == "on_demand" and not a4.interruptible
    a6 = out["A6000x4"]
    assert a6.price_per_instance_hour == D("7.72") and a6.price_per_gpu_hour == D("1.93") and a6.gpu_count == 4
    assert a6.canonical_gpu_name == "NVIDIA RTX A6000 48GB" and out["A5000x2"].canonical_gpu_name == "NVIDIA RTX A5000 24GB"
    # H100: HGX on Paperspace's page, pcie in Shadeform -> relabelled, unmapped, never merged into a variant.
    h = out["H100"]
    assert h.raw_gpu_name == H100_LABEL and h.canonical_gpu_name is None and h.price_per_gpu_hour == D("5.99")
    assert canonical.is_known(H100_LABEL) and canonical.canonical_gpu_name("H100 pcie") == "NVIDIA H100 80GB PCIe"
    a100 = out["A100_sxm4_80Gx8"]
    assert a100.canonical_gpu_name == "NVIDIA A100 80GB SXM4" and a100.price_per_gpu_hour == D("3.22")
    assert a100.available is False and a100.country is None, "sold out everywhere; NL + US regions: no single country"
    assert out["A100"].canonical_gpu_name == "NVIDIA A100 40GB PCIe" and out["A100"].price_per_gpu_hour == D("3.19")
    for k in ("V100", "V100_32G", "V100_32Gx4"):
        assert out[k].canonical_gpu_name is None, k
    # Excess Supply and DigitalOcean rows in the same catalogue are not Paperspace.
    assert not any(l.sku.startswith(("gpu-", "excess")) for l in out.values())


def test_capacity():
    rows = PaperspaceProvider.normalize_capacity({ENDPOINT: [snap(body())]})
    assert rows and all(r.provider == "paperspace" for r in rows)
    keyed = {(r.sku, r.region): r for r in rows}
    assert keyed[("A4000", "newyork-usa-1")].in_stock is True and keyed[("A4000", "santaclara-usa-1")].in_stock is False
    assert keyed[("H100", "newyork-usa-1")].raw_gpu_name == H100_LABEL, "capacity uses the same relabelled name"
    assert len({r.sku for r in rows}) == 22


def test_failures():
    other = {"instance_types": [t for t in body()["instance_types"] if t["cloud"] != "paperspace"]}
    assert raises(PaperspaceProvider.normalize, {ENDPOINT: [snap(other)]}), "no Paperspace rows is a failure"
    assert raises(PaperspaceProvider.normalize, {ENDPOINT: [snap({"types": []})]}), "reshaped"
    assert PaperspaceProvider.normalize({}) == [], "no snapshot yet: nothing, not an error"

    def handler(request):
        return httpx.Response(200, text=FIX.read_text(encoding="utf-8"), headers={"content-type": "application/json"})
    prov = PaperspaceProvider(httpx.AsyncClient(base_url="https://api.shadeform.ai", transport=httpx.MockTransport(handler)))
    raws = asyncio.run(prov.fetch())
    assert len(raws) == 1 and raws[0].ok
    assert {t["cloud"] for t in raws[0].payload["instance_types"]} == {"paperspace"}, "only Paperspace rows are stored"


def test_registry():
    assert PROVIDERS["paperspace"] is PaperspaceProvider
    assert "PaperspaceProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "paperspace" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("paperspace")
    assert (m.provider_class, m.source_type, m.source) == ("neocloud", "aggregator", "api.shadeform.ai")
    via = {c.cloud for c in PROVIDERS.values() if issubclass(c, ShadeformCloud)}
    direct = {n for n, c in PROVIDERS.items() if not issubclass(c, ShadeformCloud)}
    assert "paperspace" in via and not (via & direct)
    assert "excesssupply" not in via and "imwt" not in via


if __name__ == "__main__":
    for t in (test_normalize, test_capacity, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
