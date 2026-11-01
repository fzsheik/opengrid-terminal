"""Archived-page parsers for designs older than 2025 (backfill/parsers/): the pages providers printed from 2016 to
2024, read so the archived history reaches back years, not months. Fixtures are trimmed Internet Archive captures
(tests/fixtures_backfill/, source URL in each file's first line).

Run:  .venv/bin/python tests/test_backfill_old_designs.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (first: isolates the process from real networks)

from backfill.__main__ import _day  # noqa: E402
from backfill.parsers import azure, coreweave  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2022, 1, 1, tzinfo=timezone.utc)


def _raises(fn, *a, contains=""):
    try:
        fn(*a)
    except ValueError as exc:
        assert contains in str(exc), str(exc)
        return
    raise AssertionError("expected ValueError")


def _src(url):
    return next(s for s in sources() if s.url == url)


def _fix(name):
    return (FIX / name).read_text()


def test_since_accepts_year_and_month():
    assert _day("2016-01") == "20160101" and _day("2016") == "20160101" and _day("2025-10-21") == "20251021"
    assert _day("2024-02", end=True) == "20240229" and _day("2025", end=True) == "20251231"
    for bad in ("2016-13", "16-01", "2016-01-32"):
        try:
            _day(bad)
        except (SystemExit, ValueError):
            continue
        raise AssertionError(bad)


# ---------------------------------------------------------------------------------------------------------- Azure

AZ = "azure.microsoft.com/en-us/pricing/details/virtual-machines/linux/"


def test_azure_vm_page_2018_names_without_s():
    ls, used = _src(AZ).parse((FIX / "azure_vm_2018-06.html").read_bytes(), AT)
    assert used.endswith("azure_vm_page")
    by = {l.listing_id: l for l in ls}
    nc6 = by["Standard_NC6s_v3:eastus"]          # printed "NC6 v3"
    assert nc6.sku == "Standard_NC6s_v3" and nc6.canonical_gpu_name == "NVIDIA Tesla V100 16GB"
    assert nc6.price_per_instance_hour == D("3.06") and nc6.gpu_count == 1 and nc6.region == "eastus"
    assert by["Standard_NC24rs_v3:eastus"].gpu_count == 4
    # reserved columns are never read; K80 / P100 / P40 sizes are not GPU_SIZES sizes, so they are skipped like live
    assert all(l.market_type == "on_demand" for l in ls)
    assert not any(s in l.sku for l in ls for s in ("NC6_v2", "NC6s_v2", "ND6", "Standard_NC6:"))
    assert {l.listing_id.split(":")[0] for l in ls} == {"Standard_NC6s_v3", "Standard_NC12s_v3", "Standard_NC24s_v3",
                                                         "Standard_NC24rs_v3"}


def test_azure_vm_page_2022_spot_column():
    ls = azure.azure_vm_page(_fix("azure_vm_2022-01_spot.html"), AT)
    by = {l.listing_id: l for l in ls}
    a100 = by["Standard_ND96asr_v4:eastus"]       # printed "ND96asr A100 v4"
    assert a100.canonical_gpu_name == "NVIDIA A100 40GB SXM4" and a100.price_per_instance_hour == D("27.197")
    assert a100.price_per_gpu_hour == D("27.197") / 8
    spot = by["Standard_ND96asr_v4:eastus:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.price_per_instance_hour == D("14.95835")
    t4 = by["Standard_NC4as_T4_v3:eastus2"]
    assert t4.canonical_gpu_name == "NVIDIA T4 16GB" and t4.price_per_instance_hour == D("0.526")
    assert not any(l.price_per_instance_hour == D("18.82831") for l in ls), "1 year reserved is never read"
    assert {l.region for l in ls} == {"eastus", "eastus2"}, "only the live poller's regions"


def test_azure_vm_page_old_and_new_designs_fail_loudly():
    _raises(azure.azure_vm_page, _fix("azure_vm_2017-01_k80_only.html"), AT, contains="no GPU_SIZES size")
    _raises(azure.azure_vm_page, _fix("azure_vm_2026-10_script_only.html"), AT, contains="loads prices by script")
    bad = _fix("azure_vm_2022-01_spot.html").replace('"us-east":27.197', '"us-east":"27.197"', 1)
    _raises(azure.azure_vm_page, bad, AT, contains="amount")
    bad = _fix("azure_vm_2022-01_spot.html").replace("<td>8x A100 (NVlink)</td>", "<td>4x A100 (NVlink)</td>", 1)
    _raises(azure.azure_vm_page, bad, AT, contains="disagrees")


# ------------------------------------------------------------------------------------------------------ CoreWeave

def test_coreweave_a_la_carte_component_table():
    ls, used = _src("www.coreweave.com/gpu-cloud-pricing").parse(
        (FIX / "coreweave_gpu-cloud-pricing_2023-06.html").read_bytes(), AT)
    assert used.endswith("coreweave_component_table")
    by = {l.listing_id: l for l in ls}
    h100 = by["nvidia-hgx-h100:gpu-component"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.price_per_gpu_hour == D("4.76")
    assert h100.gpu_count == 1 and h100.provider_tier == "a-la-carte"
    assert by["a100-80gb-nvlink:gpu-component"].canonical_gpu_name == "NVIDIA A100 80GB SXM4"
    # the desktop table says $2.06; the mobile card below it says $2.16: only the table is read
    assert by["a100-40gb-nvlink:gpu-component"].price_per_gpu_hour == D("2.06")
    assert by["tesla-v100-nvlink:gpu-component"].canonical_gpu_name == "NVIDIA Tesla V100 16GB SXM2"
    assert by["quadro-rtx-4000:gpu-component"].canonical_gpu_name is None, "variant-free Quadro names stay unmapped"
    assert len(ls) == 13
    page = _fix("coreweave_gpu-cloud-pricing_2023-06.html")
    _raises(coreweave.coreweave_component_table, page.replace("<strong>$4.25</strong>", "<strong>$4.25/mo</strong>"),
            AT, contains="price cell")
    _raises(coreweave.coreweave_component_table, page.replace("GPU Component", "GPU Instance"), AT, contains="header")


TESTS = [test_since_accepts_year_and_month,
         test_azure_vm_page_2018_names_without_s, test_azure_vm_page_2022_spot_column,
         test_azure_vm_page_old_and_new_designs_fail_loudly,
         test_coreweave_a_la_carte_component_table]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
