"""CUDO Compute: VM and bare-metal machine types with free capacity, no key.

Parsers run on the REAL responses (tests/fixtures_providers/cudo_*.json, fetched 2026-11-01
with anonymous GETs).

Run:  .venv/bin/python tests/test_provider_cudo.py
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
from providers.cudo import METAL, VMS, CudoProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def snap(payload):
    return SimpleNamespace(payload=payload, request=None, fetched_at=NOW)


def eps(vm=None, metal=None):
    return {VMS: [snap(vm if vm is not None else load("cudo_vm_machine_types.json"))],
            METAL: [snap(metal if metal is not None else load("cudo_machines_types.json"))]}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def test_normalize():
    out = {l.listing_id: l for l in CudoProvider.normalize(eps())}
    assert set(out) == {"vm:epyc-genoa-l40s-graphics@no-kristiansand-1", "vm:sapphire-rapids-h100@no-kristiansand-1",
                        "baremetal:epyc-genoa-a100-pcie@no-kristiansand-1",
                        "baremetal:epyc-genoa-h100-nvl-pcie@no-kristiansand-1",
                        "baremetal:epyc-genoa-l40s@no-kristiansand-1", "baremetal:sapphire-rapids-h100@no-kristiansand-1",
                        "baremetal:epyc-genoa-l40s@se-skarholmen-1"}, set(out)
    h = out["vm:sapphire-rapids-h100@no-kristiansand-1"]
    # 1.79 GPU + 1 vCPU x 0.0059 + ceil(1 / 0.2063492) = 5 GiB x 0.0049
    assert h.price_per_gpu_hour == D("1.79") + D("0.0059") + 5 * D("0.0049") and h.vcpu == 1 and h.ram_gb == 5
    assert h.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h.gpu_count == 1 and h.provider_tier == "vm"
    assert h.capacity == 0 and h.available is False and h.country == "NO" and h.region == "no-kristiansand-1"
    g = out["vm:epyc-genoa-l40s-graphics@no-kristiansand-1"]
    assert g.canonical_gpu_name is None and g.capacity == 2 and g.capacity_unit == "gpu" and g.available
    m = out["baremetal:sapphire-rapids-h100@no-kristiansand-1"]
    assert m.price_per_instance_hour == D("14.32") and m.price_per_gpu_hour == D("1.79") and m.gpu_count == 8
    assert m.capacity == 0 and m.capacity_unit == "instance" and m.vcpu == 112 and m.ram_gb == 2048
    nvl = out["baremetal:epyc-genoa-h100-nvl-pcie@no-kristiansand-1"]
    assert nvl.canonical_gpu_name == "NVIDIA H100 94GB NVL" and nvl.available and nvl.price_per_gpu_hour == D("1.79")
    assert out["baremetal:epyc-genoa-l40s@se-skarholmen-1"].country == "SE"
    assert out["baremetal:epyc-genoa-a100-pcie@no-kristiansand-1"].canonical_gpu_name is None
    assert not any("epyc-9654" in k or k.startswith("vm:epyc-genoa@") for k in out), "CPU-only types skipped"


def test_capacity():
    rows = {(r.sku, r.region): r for r in CudoProvider.normalize_capacity(eps())}
    listings = {(l.sku, l.region): l for l in CudoProvider.normalize(eps())}
    assert set(rows) == set(listings)
    assert all(rows[k].available_count == l.capacity for k, l in listings.items())
    v = rows[("vm:epyc-genoa-l40s-graphics", "no-kristiansand-1")]
    assert (v.available_count, v.count_unit, v.in_stock, v.source) == (2, "gpu", True, VMS)
    b = rows[("baremetal:epyc-genoa-a100-pcie", "no-kristiansand-1")]
    assert (b.available_count, b.count_unit, b.gpu_count) == (2, "instance", 8)


def test_failures():
    assert raises(CudoProvider.normalize, eps(vm={"types": []}))
    assert raises(CudoProvider.normalize, eps(vm={"machineTypes": []}, metal={"machineTypes": []})), "empty = failure"
    only_vm = {VMS: eps()[VMS]}
    assert len(CudoProvider.normalize(only_vm)) == 2, "one catalogue failing leaves the other readable"
    vm = load("cudo_vm_machine_types.json")
    vm["machineTypes"][1]["gpuPriceHr"] = {"value": "0"}
    assert "vm:sapphire-rapids-h100@no-kristiansand-1" not in {l.listing_id for l in CudoProvider.normalize(eps(vm=vm))}
    assert CudoProvider.normalize({}) == []

    def handler(request):
        name = {VMS: "cudo_vm_machine_types.json", METAL: "cudo_machines_types.json"}.get(request.url.path)
        if not name:
            return httpx.Response(404)
        return httpx.Response(200, text=(FIX / name).read_text(encoding="utf-8"), headers={"content-type": "application/json"})
    raws = asyncio.run(CudoProvider(httpx.AsyncClient(base_url="https://rest.compute.cudo.org",
                                                      transport=httpx.MockTransport(handler))).fetch())
    assert [r.endpoint for r in raws] == [VMS, METAL] and all(r.ok for r in raws)


def test_registry():
    assert PROVIDERS["cudo"] is CudoProvider
    assert "CudoProvider" in {type(f).__name__ for f in fetch_mod.build_fetchers()}
    assert "cudo" in mapping.PROVIDER_MAPPINGS and mapping.check_mapping_coverage() == {}
    m = provider_meta.meta("cudo")
    assert (m.provider_class, m.source_type, m.source) == ("neocloud", "public_api", "rest.compute.cudo.org")


if __name__ == "__main__":
    for t in (test_normalize, test_capacity, test_failures, test_registry):
        t()
        print(t.__name__, "ok")
