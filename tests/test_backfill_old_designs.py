"""Archived-page parsers for designs older than 2025 (backfill/parsers/): the pages providers printed from 2016 to
2024, read so the archived history reaches back years, not months. Fixtures are trimmed Internet Archive captures
(tests/fixtures_backfill/, source URL in each file's first line).

Run:  .venv/bin/python tests/test_backfill_old_designs.py
"""

import re
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (first: isolates the process from real networks)

from backfill.__main__ import _day  # noqa: E402
from backfill.parsers import azure, coreweave, denvr, lambda_, runpod, verda  # noqa: E402
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


def test_azure_vm_page_2020_spot_savings_and_trailing_cell():
    page = _fix("azure_vm_2020-06_spot_savings.html")
    by = {l.listing_id: l for l in azure.azure_vm_page(page, AT)}
    assert by["Standard_NC6s_v3:eastus"].price_per_instance_hour == D("3.06")      # "Spot (% Savings) *" header
    assert by["Standard_NC6s_v3:eastus:spot"].price_per_instance_hour == D("0.601596")
    assert len(by) == 16
    # the unlabelled trailing cell is dropped only while it carries no price
    end = page.find("</tr>", page.find("NC6s v3 <"))
    i = page.find(">", page.rfind("<td", 0, end)) + 1
    priced = page[:i] + '<span data-amount=\'{"regional":{"us-east":1}}\'></span>' + page[i:]
    _raises(azure.azure_vm_page, priced, AT, contains="cells under")
    trunc = "<html><body>" + page[: page.find("NC6s v3") - 200]
    _raises(azure.azure_vm_page, trunc, AT, contains="truncated")


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


# --------------------------------------------------------------------------------------------------------- Lambda

GC, GCP_ = "lambdalabs.com/service/gpu-cloud", "lambdalabs.com/service/gpu-cloud/pricing"


def _lam(url, name, at):
    ls, used = _src(url).parse((FIX / name).read_bytes(), at)
    return {l.listing_id: l for l in ls}, used


def test_lambda_2020_cards():
    by, used = _lam(GC, "lambda_gpu-cloud_2020-05_cards.html", AT)
    assert used.endswith("gpu_cloud_cards_2020")
    v100 = by["nvidia-tesla-v100-16-gb:8x"]       # the id lambda.ai/pricing rows of the same product carry
    assert v100.canonical_gpu_name == "NVIDIA Tesla V100 16GB" and v100.price_per_instance_hour == D("12.00")
    assert v100.price_per_gpu_hour == D("1.5") and v100.vcpu == 92
    assert by["gtx-1080-ti-11-gb:4x"].canonical_gpu_name is None and len(by) == 2


def test_lambda_2021_pricing_page_vram_from_its_section():
    by, used = _lam(GCP_, "lambda_gpu-cloud-pricing_2021-08_vram_each.html", AT)
    assert used.endswith("pricing_tables_2021")
    a6 = by["nvidia-a6000-48-gb:2x"]              # "2x A6000" under "(48 GiB VRAM each)"
    assert a6.raw_gpu_name == "A6000 (48 GB)" and a6.canonical_gpu_name == "NVIDIA RTX A6000 48GB"
    assert by["nvidia-quadro-rtx-6000-24-gb:1x"].price_per_instance_hour == D("1.25")
    assert by["nvidia-tesla-v100-16-gb:8x"].price_per_instance_hour == D("12.00")   # "8x V100s"
    page = _fix("lambda_gpu-cloud-pricing_2021-08_vram_each.html")
    _raises(lambda_.pricing_tables_2021, re.sub(r"\(\d+ GiB VRAM each\)", "", page), AT, contains="no VRAM")


def test_lambda_2022_pricing_page_vram_in_cell_reserved_ignored():
    by, _ = _lam(GCP_, "lambda_gpu-cloud-pricing_2022-05_vram_in_cell.html", AT)
    assert by["nvidia-a6000-48-gb:1x"].price_per_instance_hour == D("1.45")
    assert by["nvidia-tesla-v100-16-gb:8x"].price_per_instance_hour == D("6.80")    # "Tesla V100s (16 GB)"
    assert not any(l.price_per_instance_hour in (D("14.21"), D("10.66"), D("5.44")) for l in by.values()), "reserved"
    assert len(by) == 7


def test_lambda_2022_reserved_table_with_the_same_header_is_skipped():
    by, used = _lam(GC, "lambda_gpu-cloud_2022-07_reserved_same_header.html", AT)
    assert used.endswith("gpu_cloud_tables")
    a100 = by["nvidia-a100-40-gb:1x"]
    assert a100.canonical_gpu_name is None, "the page does not say SXM or PCIe: unmapped, not guessed"
    assert a100.price_per_instance_hour == D("1.10")
    assert by["nvidia-tesla-v100-16-gb:8x"].price_per_instance_hour == D("4.40")
    assert not any(l.price_per_instance_hour in (D("10.00"), D("8.00"), D("3.20")) and l.gpu_count == 8
                   for l in by.values()), "reserved rows"
    assert len(by) == 10


def test_lambda_dec_2023_price_in_force_on_capture_day():
    name = "lambda_gpu-cloud_2023-12_two_price_columns.html"
    before, _ = _lam(GC, name, datetime(2023, 12, 28, tzinfo=timezone.utc))
    after, _ = _lam(GC, name, datetime(2024, 1, 1, 7, tzinfo=timezone.utc))
    assert before["nvidia-h100-sxm-80-gb:8x"].price_per_instance_hour == D("20.72")
    assert after["nvidia-h100-sxm-80-gb:8x"].price_per_instance_hour == D("27.92")
    assert after["nvidia-h100-sxm-80-gb:8x"].canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert after["nvidia-a100-pcie-40-gb:4x"].canonical_gpu_name == "NVIDIA A100 40GB PCIe"


def test_lambda_2024_per_gpu_prices_and_reserved_rows():
    for name in ("lambda_gpu-cloud_2024-08_market_column.html", "lambda_gpu-cloud_2024-11_market_prefix.html"):
        by, _ = _lam(GC, name, AT)
        h100 = by["nvidia-h100-sxm-80-gb:8x"]
        assert h100.price_per_gpu_hour == D("2.99") and h100.price_per_instance_hour == D("23.92"), name
        assert all(l.market_type == "on_demand" for l in by.values())
    by, _ = _lam(GC, "lambda_gpu-cloud_2024-11_market_prefix.html", AT)
    assert by["nvidia-gh200-96-gb:1x"].canonical_gpu_name == "NVIDIA GH200 96GB" and len(by) == 18
    page = _fix("lambda_gpu-cloud_2024-11_market_prefix.html")
    _raises(lambda_.gpu_cloud_tables, page.replace("$4.29 / GPU / hr", "$4.29 / hr", 1), AT, contains="under header")


# --------------------------------------------------------------------------------------------------------- RunPod

RP = "www.runpod.io/gpu-instance/pricing"


def test_runpod_next_data_list_reads_only_offered_clouds():
    ls, used = _src(RP).parse((FIX / "runpod_gpu-instance_2023-07_list.html").read_bytes(), AT)
    assert used.endswith("next_data_gpu_types")
    by = {l.listing_id: l for l in ls}
    sec = by["NVIDIA A100 80GB PCIe:secure:1"]      # the live normalizer's listing id
    assert sec.price_per_gpu_hour == D("1.79") and sec.provider_tier == "secure" and not sec.interruptible
    com = by["NVIDIA A100 80GB PCIe:community:1"]
    assert com.price_per_gpu_hour == D("1.69") and com.interruptible
    assert sec.canonical_gpu_name == "NVIDIA A100 80GB PCIe"
    # A100 SXM: communityCloud false, so its (filled-in) communityPrice is not a listing
    assert "NVIDIA A100-SXM4-80GB:secure:1" in by and "NVIDIA A100-SXM4-80GB:community:1" not in by
    assert not any(l.price_per_gpu_hour == D("0.89") for l in ls), "spot / bid prices are not read"


def test_runpod_next_data_map_and_lowest_price_designs():
    ls = runpod.next_data_gpu_types(_fix("runpod_gpu-instance_2024-06_map.html"), AT)
    by = {l.listing_id: l for l in ls}
    assert by["AMD Instinct MI300X OAM:secure:1"].price_per_gpu_hour == D("4.89")
    assert "AMD Instinct MI300X OAM:community:1" not in by
    ls = runpod.next_data_gpu_types(_fix("runpod_gpu-instance_2022-07_lowest_price.html"), AT)
    # 2022: only the cheaper cloud's price; read for types offered in exactly one cloud
    assert all(l.listing_id.endswith((":secure:1", ":community:1")) for l in ls) and len(ls) == 14
    page = _fix("runpod_gpu-instance_2023-07_list.html")
    _raises(runpod.next_data_gpu_types, page.replace('"securePrice":1.79', '"securePrice":"1.79"', 1), AT,
            contains="price")
    _raises(runpod.next_data_gpu_types, page.replace("__NEXT_DATA__", "x"), AT, contains="no __NEXT_DATA__")


# ------------------------------------------------------------------------------------------- DataCrunch (Verda)

DC = "datacrunch.io/products"


def test_datacrunch_2020_type_codes():
    ls, used = _src(DC).parse((FIX / "datacrunch_products_2020-09_type_codes.html").read_bytes(), AT)
    assert used.endswith("datacrunch_products")
    by = {l.listing_id: l for l in ls}
    v = by["1V100.6V"]                              # the instance type is the live API's listing id
    assert v.provider == "verda" and v.raw_gpu_name == "Tesla V100 16GB" and v.canonical_gpu_name == "NVIDIA Tesla V100 16GB"
    assert v.price_per_instance_hour == D("0.65") and v.gpu_count == 1
    assert by["8V100.48M"].gpu_count == 8, "bare metal type"


def test_datacrunch_2021_and_2024_tables():
    by = {l.listing_id: l for l in verda.datacrunch_products(_fix("datacrunch_products_2021-09_gpu_model.html"), AT)}
    assert by["1A100.22V"].canonical_gpu_name is None, "'A100 80GB' does not say SXM or PCIe"
    assert by["1A100.22V"].price_per_instance_hour == D("2.20"), "on-demand, not weekly / monthly"
    assert by["8A6000.80V"].canonical_gpu_name == "NVIDIA RTX A6000 48GB"
    by = {l.listing_id: l for l in verda.datacrunch_products(_fix("datacrunch_products_2024-04_cpu_rows.html"), AT)}
    h = by["8H100.80S.176V"]
    assert h.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h.price_per_instance_hour == D("30.50")
    assert by["1A100.40S.22V"].canonical_gpu_name == "NVIDIA A100 40GB SXM4"
    assert not any(l.listing_id.startswith("CPU.") for l in by.values())
    _raises(verda.datacrunch_products, _fix("datacrunch_products_2022-05_script_filled.html"), AT, contains="by script")
    bad = _fix("datacrunch_products_2021-09_gpu_model.html").replace("$2.20/h", "€2.20/h", 1)
    _raises(verda.datacrunch_products, bad, AT, contains="price")


# ---------------------------------------------------------------------------------------------------------- Denvr

def test_denvr_denvrdata_2025_list_before_header():
    ls, used = _src("www.denvrdata.com/pricing").parse(
        (FIX / "denvr_denvrdata_2025-07_list_before_header.html").read_bytes(), AT)
    by = {l.listing_id: l for l in ls}
    h = by["nvidia-h100-sxm:8x:80gb"]             # the id www.denvr.com/pricing rows of the same shape carry
    assert h.price_per_gpu_hour == D("2.10") and h.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h.gpu_count == 8
    assert len(ls) == 7 and not any(l.price_per_gpu_hour in (D("0.31"), D("1.59")) for l in ls), "CPU rows"
    page = _fix("denvr_denvrdata_2025-07_list_before_header.html")
    _raises(denvr.denvr_table, page.replace("$2.10 / GPU", "$2.10 / GPU / mo"), AT, contains="unreadable")


TESTS = [test_since_accepts_year_and_month,
         test_azure_vm_page_2018_names_without_s, test_azure_vm_page_2022_spot_column,
         test_azure_vm_page_2020_spot_savings_and_trailing_cell, test_azure_vm_page_old_and_new_designs_fail_loudly,
         test_coreweave_a_la_carte_component_table,
         test_lambda_2020_cards, test_lambda_2021_pricing_page_vram_from_its_section,
         test_lambda_2022_pricing_page_vram_in_cell_reserved_ignored,
         test_lambda_2022_reserved_table_with_the_same_header_is_skipped, test_lambda_dec_2023_price_in_force_on_capture_day,
         test_lambda_2024_per_gpu_prices_and_reserved_rows,
         test_runpod_next_data_list_reads_only_offered_clouds, test_runpod_next_data_map_and_lowest_price_designs,
         test_datacrunch_2020_type_codes, test_datacrunch_2021_and_2024_tables,
         test_denvr_denvrdata_2025_list_before_header]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
