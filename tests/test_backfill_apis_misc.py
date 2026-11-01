"""Archived-page parsers for providers OpenGrid reads live through APIs or Shadeform (backfill/parsers/).

Run:  .venv/bin/python tests/test_backfill_apis_misc.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (first: isolates the process from real networks)

import json  # noqa: E402

from backfill.parsers import azure, crusoe, denvr, digitalocean, hyperstack, koyeb, latitude, linode, lium, ovhcloud, salad, shadeform, verda, voltagepark, vultr  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _raises(fn, *a, contains=""):
    try:
        fn(*a)
    except ValueError as exc:
        assert contains in str(exc), str(exc)
        return
    raise AssertionError("expected ValueError")


def test_latitude_plans_json():
    ls = latitude.latitude_plans_json((FIX / "latitude_2025-11.html").read_text(), AT)
    by = {l.listing_id: l for l in ls}
    h100 = by["g3-h100-small:united-states"]
    assert h100.gpu_count == 1 and h100.price_per_instance_hour == D("2.41") and h100.region == "United States"
    rtx = by["g4-rtx6kpro-large:united-states"]
    assert rtx.gpu_count == 8 and rtx.price_per_gpu_hour == D("19.5") / 8
    assert not any("united-kingdom" in k for k in by), "a region priced null produces no listing"
    assert all(l.market_type == "on_demand" and l.observed_at == AT for l in ls)
    _raises(latitude.latitude_plans_json, "<html>stub page</html>", AT, contains="no embedded plans")
    # Not registered (see the module): the page's hourly figures disagree with the live Shadeform listing.
    assert not latitude.SOURCES and latitude.PAGE_SOURCE.provider == "latitude"


def test_denvr_table_desktop_rows_only():
    ls = denvr.denvr_table((FIX / "denvr_2026-07.html").read_text(), AT)
    by = {l.listing_id: l for l in ls}
    assert len(ls) == 8, [l.listing_id for l in ls]          # the mobile copy of the rows is not read twice
    h100 = by["nvidia-h100-sxm:8x:80gb"]
    assert h100.raw_gpu_name == "NVIDIA H100 SXM 80GB" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.gpu_count == 8 and h100.price_per_gpu_hour == D("2.30") and h100.price_per_instance_hour == D("18.40")
    assert by["nvidia-gh200:1x:96gb"].canonical_gpu_name == "NVIDIA GH200 96GB"
    assert not any(k.startswith("nvidia-h200") for k in by), "'Reserved only' is not a price"
    bad = (FIX / "denvr_2026-07.html").read_text().replace("$2.30 / GPU", "$2.30 per GPU-month", 1)
    _raises(denvr.denvr_table, bad, AT, contains="unreadable row")
    _raises(denvr.denvr_table, "<p>nothing</p>", AT, contains="no Platform")


def test_crusoe_gpu_list():
    ls = crusoe.crusoe_gpu_list((FIX / "crusoe_2025-12.html").read_text(), AT)
    by = {l.listing_id: l for l in ls}
    h100 = by["nvidia-hgx-h100-80gb"]
    assert h100.raw_gpu_name == "NVIDIA HGX H100 80GB" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.gpu_count == 1 and h100.price_per_gpu_hour == D("3.90") and h100.market_type == "on_demand"
    spot = by["nvidia-hgx-h100-80gb:spot"]
    assert spot.market_type == "spot" and spot.interruptible and spot.price_per_gpu_hour == D("1.60")
    assert by["nvidia-a100-80gb-pcie"].canonical_gpu_name == "NVIDIA A100 80GB PCIe"
    assert "nvidia-hgx-h200-141gb:spot" not in by, "Contact sales is not a price"
    assert not any("vcpu" in l.raw_gpu_name.lower() for l in ls), "the CPU table is not read"
    assert crusoe.raw_name("NVIDIA A100", ["80GB", "PCIe"]) == "NVIDIA A100 80GB PCIe"
    assert crusoe.raw_name("NVIDIA B200", ["180GB", "HGX"]) == "NVIDIA HGX B200 180GB"
    bad = (FIX / "crusoe_2025-12.html").read_text().replace("$3.90/GPU-hr", "$3.90/hr", 1)
    _raises(crusoe.crusoe_gpu_list, bad, AT, contains="not '$x/GPU-hr'")


def test_lium_next_data_through_the_live_parser():
    old = lium.lium_next_data((FIX / "lium_2025-11.html").read_text(), AT)
    a100 = next(l for l in old if l.raw_gpu_name == "NVIDIA A100-SXM4-80GB")
    # no top-level gpu_count in 2025: taken from specs.gpu.count; price is the executor's own per-GPU price
    assert a100.gpu_count == 8 and a100.price_per_gpu_hour == D("0.43") and a100.canonical_gpu_name == "NVIDIA A100 80GB SXM4"
    assert a100.price_per_instance_hour == D("3.44") and a100.observed_at == AT and a100.provider == "lium"
    new = lium.lium_next_data((FIX / "lium_2026-10.html").read_text(), AT)   # the /nodes query key
    assert {l.raw_gpu_name for l in new} == {"NVIDIA B300 SXM6 AC", "NVIDIA B200"}
    assert next(l for l in new if l.raw_gpu_name == "NVIDIA B200").canonical_gpu_name == "NVIDIA B200 180GB SXM"
    _raises(lium.lium_next_data, "<html></html>", AT, contains="no __NEXT_DATA__")
    _raises(lium.lium_next_data, '<script id="__NEXT_DATA__">{"props":{}}</script>', AT, contains="no executors query")


def test_azure_first_page_through_the_live_normalizer():
    body = (FIX / "azure_retail_2026-10.json").read_bytes()
    ls = azure.azure_retail_first_page(body, AT)
    # only the eastus/eastus2 Linux Consumption row survives the live rules: no Low Priority, Windows,
    # Reservation or other-region rows
    assert [(l.listing_id, l.price_per_instance_hour) for l in ls] == [("Standard_NC48ads_A100_v4:eastus2", D("7.346"))]
    assert ls[0].gpu_count == 2 and ls[0].canonical_gpu_name == "NVIDIA A100 80GB PCIe" and ls[0].observed_at == AT
    eur = json.loads(body)
    eur["BillingCurrency"] = "EUR"
    _raises(azure.azure_retail_first_page, json.dumps(eur).encode(), AT, contains="billing currency")


def test_linode_types_through_the_live_normalizer():
    body = (FIX / "linode_types_2026-09.json").read_bytes()
    ls = linode.linode_types(body, AT)
    assert ls and all(l.provider == "linode" and l.gpu_count >= 1 and l.price_per_gpu_hour > 0 for l in ls)
    assert any(l.canonical_gpu_name == "NVIDIA RTX 4000 Ada 20GB" for l in ls)
    partial = json.loads(body)
    partial["pages"] = 2
    _raises(linode.linode_types, json.dumps(partial).encode(), AT, contains="partial catalogue")


def test_shadeform_catalogue_one_source_per_cloud():
    body = (FIX / "shadeform_types_2026-01.json").read_bytes()
    by_cloud = {s.provider: s for s in shadeform.SOURCES}
    assert {"crusoe", "latitude", "boostrun", "horizon", "phyntec", "scaleway"} <= set(by_cloud)
    assert "denvr" not in by_cloud, "Denvr is read from its own page only (two sources ~5% apart would alternate)"
    assert {s.url for s in shadeform.SOURCES} == {shadeform.URL}
    ls, used = by_cloud["latitude"].parse(body, AT)
    assert used.startswith("backfill.parsers.shadeform (LatitudeProvider") and {l.provider for l in ls} == {"latitude"}
    h100 = next(l for l in ls if l.listing_id == "H100")
    assert h100.price_per_instance_hour == D("1.99") and h100.canonical_gpu_name == "NVIDIA H100 80GB PCIe"
    for cloud in ("crusoe", "latitude"):
        assert {l.provider for l in by_cloud[cloud].parse(body, AT)[0]} == {cloud}, "another cloud's rows are never read"
    _raises(lambda: by_cloud["horizon"].parse(body, AT), contains="no listings")   # a cloud with no rows stores nothing


def test_ovhcloud_price_tables():
    page = (FIX / "ovhcloud_2025-10.html").read_text()
    by = {l.sku: l for l in ovhcloud.ovh_price_tables(page, AT)}
    assert set(by) == {"l40s-90", "a100-360", "h100-380", "h100-760", "rtx5000-28", "t1-le-45"}, "CPU rows are not read"
    a100 = by["a100-360"]
    assert a100.raw_gpu_name == "A100 80GB" and a100.gpu_count == 2 and a100.price_per_instance_hour == D("6.15")
    assert by["l40s-90"].canonical_gpu_name == "NVIDIA L40S 48GB" and by["l40s-90"].listing_id == "page:l40s-90"
    assert by["t1-le-45"].raw_gpu_name == "Tesla V100 16GB", "same raw name shape as the live catalogue"
    bad = page.replace("$2.99", "€2.99", 1)
    _raises(ovhcloud.ovh_price_tables, bad, AT, contains="is not '$x /hour'")
    # the Aug 2026+ design: embedded plan objects
    new = (FIX / "ovhcloud_2026-10.html").read_text()
    by = {l.sku: l for l in ovhcloud.ovh_plan_json(new, AT)}
    assert set(by) == {"t2-le-45", "h100-380", "rtx5000-56", "a100-360"}, "ai-training products and CPU flavors are not read"
    assert by["a100-360"].raw_gpu_name == "A100 80GB" and by["a100-360"].gpu_count == 2
    assert by["a100-360"].price_per_instance_hour == D("6.15") and by["h100-380"].price_per_gpu_hour == D("2.99")
    _raises(ovhcloud.ovh_plan_json, page, AT, contains="no embedded plan objects")
    src = next(x for x in sources() if x.provider == "ovhcloud")
    assert src.parse(new.encode(), AT)[1].endswith("ovh_plan_json") and src.parse(page.encode(), AT)[1].endswith("ovh_price_tables")


def test_hyperstack_cards():
    ls = hyperstack.hyperstack_cards((FIX / "hyperstack_2025-10.html").read_text(), AT)
    by = {l.listing_id: l for l in ls}
    sxm = by["nvidia-h100-sxm"]
    assert sxm.raw_gpu_name == "NVIDIA H100 SXM 80GB" and sxm.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert sxm.price_per_gpu_hour == D("2.40") and sxm.gpu_count == 1 and sxm.market_type == "on_demand"
    assert by["nvidia-h100-pcie:spot"].market_type == "spot" and by["nvidia-h100-pcie:spot"].price_per_gpu_hour == D("1.52")
    assert by["nvidia-a100:spot"].raw_gpu_name == "NVIDIA A100 80GB", "a spot row takes the on-demand card's VRAM"
    assert len(ls) == 16, "the reservation card ('Starting from') is not read"
    _raises(hyperstack.hyperstack_cards, "<div></div>", AT, contains="no on-demand")


def test_koyeb_cards_unmapped_and_unregistered():
    ls = koyeb.koyeb_cards((FIX / "koyeb_2025-11.html").read_text(), AT)
    by = {l.sku: l for l in ls}
    assert len(ls) == 16 and by["H100"].price_per_gpu_hour == D("3.30") and by["4x H100"].gpu_count == 4
    assert by["8x A100 SXM"].price_per_instance_hour == D("17.2") and by["8x A100 SXM"].vcpu == 120
    # the page prints no VRAM; bare names would map to the wrong variant ("A100 SXM" -> 40GB), so none is mapped
    assert all(l.canonical_gpu_name is None for l in ls)
    assert not koyeb.SOURCES and koyeb.PAGE_SOURCE.provider == "koyeb"


def test_verda_instance_tables():
    ls = verda.verda_instance_tables((FIX / "verda_2026-10.html").read_text(), AT)
    by = {l.listing_id: l for l in ls}
    h100 = by["1x-h100-sxm5-80gb-30v"]
    assert h100.raw_gpu_name == "H100 SXM5 80GB" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.price_per_gpu_hour == D("4.211") and by["1x-h100-sxm5-80gb-30v:spot"].price_per_gpu_hour == D("2.106")
    assert "1x-b300-cc-sxm6-268gb-30v" in by, "the Confidential computing table in the GPU section is read"
    assert not any("amd" in k for k in by), "the serverless-containers section is not read"
    assert len(ls) == 32


def test_voltagepark_cards():
    ls = voltagepark.voltagepark_cards((FIX / "voltagepark_2026-01.html").read_text(), AT)
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"on-demand-ethernet", "on-demand-3200-gbps-infiniband"}, "the reserve card is not read"
    eth, ib = by["on-demand-ethernet"], by["on-demand-3200-gbps-infiniband"]
    assert eth.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and eth.price_per_gpu_hour == D("1.99") and eth.gpu_count == 1
    assert ib.gpu_count == 8 and ib.price_per_gpu_hour == D("2.49") and ib.price_per_instance_hour == D("19.92")
    _raises(voltagepark.voltagepark_cards, (FIX / "voltagepark_2026-10.html").read_text(), AT, contains="no on-demand card has a price")


def test_digitalocean_cards_skip_reserved():
    old = {l.listing_id: l for l in digitalocean.digitalocean_cards((FIX / "digitalocean_2025-11.html").read_text(), AT)}
    # the page's first H100x8 price ($1.99) is a 12-month reservation: never read; on-demand is $2.99
    assert old["nvidia-h100-8x"].price_per_gpu_hour == D("2.99") and old["nvidia-h100-8x"].gpu_count == 8
    assert old["nvidia-h100-8x"].raw_gpu_name == "NVIDIA H100 80GB", "per-GPU memory: 640 GB / 8"
    assert old["nvidia-h100-8x"].canonical_gpu_name is None, "'NVIDIA H100' alone does not say SXM or PCIe"
    assert old["nvidia-h200-1x"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    new = {l.listing_id: l for l in digitalocean.digitalocean_cards((FIX / "digitalocean_2026-10.html").read_text(), AT)}
    assert new["nvidia-hgx-h100-1x"].canonical_gpu_name == "NVIDIA H100 80GB SXM5" and new["nvidia-hgx-h100-1x"].price_per_gpu_hour == D("4.41")
    assert new["nvidia-hgx-b300-1x:spot"].market_type == "spot" and new["nvidia-hgx-b300-1x:spot"].price_per_gpu_hour == D("6.17")
    assert not any(l.price_per_gpu_hour == D("3.26") for l in new.values()), "the reserved H100 price is not read"


def test_salad_designs():
    old = {l.raw_gpu_name: l for l in salad.salad_class_prices((FIX / "salad_2025-10.html").read_text(), AT)}
    assert len(old) == 38 and "RTX 4090 (24 GB)" in old, "group headings do not leak into names"
    r4090 = old["RTX 4090 (24 GB)"]
    assert r4090.price_per_gpu_hour == D("0.16") and r4090.provider_tier == "unstated" and r4090.interruptible
    assert r4090.canonical_gpu_name == "NVIDIA RTX 4090 24GB"
    new = salad.salad_tier_table((FIX / "salad_2026-10.html").read_text(), AT)
    tiers = {l.provider_tier: l.price_per_gpu_hour for l in new if l.raw_gpu_name == "RTX 4090 (24 GB)"}
    assert tiers == {"high": D("0.330"), "medium": D("0.273"), "low": D("0.217"), "batch": D("0.160")}, "Lowest = batch"
    _raises(salad.salad_tier_table, (FIX / "salad_2025-10.html").read_text(), AT, contains="no 'High")
    # the Dec 2025 - Sep 2026 bundle design (GPU + 8GB RAM + 4 vCPUs) is read by neither
    bundle = "<p>GPU VRAM Memory 1-16GB vCPUs 1-16 $/hr (Batch) $/Mo (Batch) RTX 4090 24GB 8GB 4 vCPUs $0.204 $148.90</p>"
    _raises(salad.salad_tier_table, bundle, AT, contains="no 'High")
    _raises(salad.salad_class_prices, bundle, AT, contains="not the 2025")


def test_vultr_cloud_gpu_only_whole_gpus():
    page = (FIX / "vultr_2026-10.html").read_text()
    ls = vultr.vultr_cloud_gpu(page, AT)
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"nvidia-hgx-h100-8x-216-vcpus", "nvidia-a40-1x-24-vcpus", "nvidia-a40-4x-96-vcpus"}, \
        "Contact Sales rows, fractional vGPU sizes and the mobile copies are not read"
    h100 = by["nvidia-hgx-h100-8x-216-vcpus"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.price_per_gpu_hour == D("2.300") and h100.gpu_count == 8
    a40 = by["nvidia-a40-4x-96-vcpus"]
    assert a40.raw_gpu_name == "NVIDIA A40 48GB" and a40.price_per_instance_hour == D("6.849"), "'$x/hr' is per instance"
    i = page.find("NVIDIA A40")
    sliced = page[:i] + page[i:].replace("<strong>48</strong>", "<strong>12</strong>", 1)   # the 1x row: a 12 GB slice
    assert "nvidia-a40-1x-24-vcpus" not in {l.listing_id for l in vultr.vultr_cloud_gpu(sliced, AT)}
    _raises(vultr.vultr_cloud_gpu, page.replace("<h2>Cloud GPU</h2>", "<h2>Other</h2>"), AT, contains="no 'Cloud GPU'")


def test_registered():
    provs = {s.provider for s in sources()}
    assert {"denvr", "crusoe", "lium", "azure", "linode", "latitude", "ovhcloud", "hyperstack", "verda", "voltagepark", "digitalocean", "salad", "vultr"} <= provs
    assert not any(s.url in ("www.latitude.sh/pricing", "www.koyeb.com/pricing") for s in sources()), "kept, not registered"


if __name__ == "__main__":
    for t in (test_latitude_plans_json, test_denvr_table_desktop_rows_only, test_crusoe_gpu_list,
              test_lium_next_data_through_the_live_parser,
              test_azure_first_page_through_the_live_normalizer, test_linode_types_through_the_live_normalizer,
              test_shadeform_catalogue_one_source_per_cloud,
              test_ovhcloud_price_tables, test_hyperstack_cards,
              test_koyeb_cards_unmapped_and_unregistered, test_verda_instance_tables,
              test_voltagepark_cards, test_digitalocean_cards_skip_reserved,
              test_salad_designs, test_vultr_cloud_gpu_only_whole_gpus, test_registered):
        t()
        print(t.__name__, "ok")
