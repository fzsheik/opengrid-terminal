"""Archived designs for providers whose archived history was thin: Google Cloud's older GPU / VM price pages, Linode's
pricing page, Jarvislabs' 2021 - 23 pages, Baseten's 2025 page, Oracle's price list API and its 2018 static page
(backfill/parsers/; fixtures are trimmed Internet Archive captures, source URL in each file's first line).

Run:  .venv/bin/python tests/test_backfill_thin.py
"""

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (first: isolates the process from real networks)

from backfill.parsers import baseten, gcp, jarvislabs, linode, oracle  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _page(name):
    return (FIX / name).read_text()


def _src(url):
    return next(s for s in sources() if s.url == url)


def _raises(fn, *a, contains=""):
    try:
        fn(*a)
    except ValueError as exc:
        assert contains in str(exc), str(exc)
        return
    raise AssertionError("expected ValueError")


# ------------------------------------------------------------------------------------------------------- Google Cloud

def test_gcp_cloud_pricer_iowa_attachments_and_preemptible():
    ls, used = _src("cloud.google.com/compute/gpus-pricing").parse(
        (FIX / "gcp_gpus-pricing_2019-08_cloud_pricer.html").read_bytes(), AT)
    assert used.endswith("gcp_cloud_pricer")
    by = {l.listing_id: l for l in ls}
    t4 = by["nvidia-tesla-t4:us-central1"]       # io-hourly="$0.95 USD per GPU" (T4's launch price)
    assert t4.price_per_gpu_hour == D("0.95") and t4.gpu_count == 1 and t4.market_type == "on_demand"
    assert t4.raw_gpu_name == "nvidia-tesla-t4" and t4.canonical_gpu_name == "NVIDIA T4 16GB"
    assert t4.region == "us-central1" and t4.country == "US" and t4.provider_tier == "gpu-attachment"
    spot = by["nvidia-tesla-t4:us-central1:spot"]  # the Preemptible GPU price column
    assert spot.price_per_gpu_hour == D("0.29") and spot.market_type == "spot" and spot.interruptible
    assert by["nvidia-tesla-v100:us-central1"].price_per_gpu_hour == D("2.48")
    # K80 and the virtual-workstation rows are not live ATTACHABLE GPUs; commitment columns are never read
    assert set(by) == {f"nvidia-tesla-{g}:us-central1{s}" for g in ("t4", "p4", "v100", "p100") for s in ("", ":spot")}


def test_gcp_cloud_pricer_refuses_pages_without_the_table():
    _raises(gcp.gcp_cloud_pricer, "<h2>GPU pricing</h2><iframe src='/compute/gpus-pricing_x.frame'></iframe>", AT,
            contains="no GPU price table")
    page = _page("gcp_gpus-pricing_2019-08_cloud_pricer.html").replace('io-hourly="$0.95 USD per GPU"',
                                                                        'io-hourly="$0.95 per hour"', 1)
    _raises(gcp.gcp_cloud_pricer, page, AT, contains="not '$x USD per GPU'")
    page = _page("gcp_gpus-pricing_2019-08_cloud_pricer.html").replace("16 GB GDDR6", "15 GB GDDR6", 1)
    _raises(gcp.gcp_cloud_pricer, page, AT, contains="prints memory")


def test_gcp_gpus_pricing_per_gpu_design():
    ls = {l.listing_id: l for l in gcp.gcp_gpus_per_gpu(_page("gcp_gpus-pricing_2025-01_per_gpu.html"), AT)}
    assert set(ls) == {f"nvidia-tesla-{g}:us-central1" for g in ("t4", "p4", "v100", "p100")}, "no workstation rows"
    assert ls["nvidia-tesla-t4:us-central1"].price_per_gpu_hour == D("0.35")
    assert ls["nvidia-tesla-v100:us-central1"].price_per_gpu_hour == D("2.48")
    assert all(l.market_type == "on_demand" and l.provider_tier == "gpu-attachment" for l in ls.values())
    wrong_region = _page("gcp_gpus-pricing_2025-01_per_gpu.html").replace('>Iowa (us-central1)<', '>Tokyo (asia-northeast1)<')
    _raises(gcp.gcp_gpus_per_gpu, wrong_region, AT, contains="not Iowa")


def test_gcp_vm_instance_pricing_accelerator_section():
    ls = {l.listing_id: l for l in gcp.gcp_vm_instance_accel(_page("gcp_vm-instance-pricing_2025-06_accelerator.html"), AT)}
    a2 = ls["a2-highgpu-1g:us-central1"]
    assert a2.price_per_instance_hour == D("3.673385") and a2.canonical_gpu_name == "NVIDIA A100 40GB SXM4"
    assert a2.vcpu == 12 and a2.ram_gb == 85.0 and a2.provider_tier == "hyperscaler"
    h100 = ls["a3-highgpu-8g:us-central1"]
    assert h100.gpu_count == 8 and h100.price_per_gpu_hour == h100.price_per_instance_hour / 8
    assert ls["g2-standard-4:us-central1"].canonical_gpu_name == "NVIDIA L4 24GB"
    assert "a4-highgpu-8g:us-central1" not in ls, "A4 prints commitment prices only"
    assert not any(k.startswith("a3-highgpu-1g") for k in ls), "not in GPU_MACHINES (live: Spot only): skipped"
    assert all(l.market_type == "on_demand" for l in ls.values())


def test_gcp_cloud_pricer_dies_and_boards_header_2018():
    ls, used = _src("cloud.google.com/compute/pricing").parse((FIX / "gcp_pricing_2018-04_dies_boards.html").read_bytes(), AT)
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"nvidia-tesla-p100:us-central1", "nvidia-tesla-p100:us-central1:spot"}, "K80 is not ATTACHABLE"
    assert by["nvidia-tesla-p100:us-central1"].price_per_gpu_hour == D("1.46")
    assert by["nvidia-tesla-p100:us-central1:spot"].price_per_gpu_hour == D("0.73"), "Preemptible GPU (Beta) price"


def test_gcp_vm_instance_pricing_components_design():
    ls = {l.listing_id: l for l in gcp.gcp_vm_instance_accel(_page("gcp_vm-instance-pricing_2026-03_components.html"), AT)}
    assert ls["a2-highgpu-1g:us-central1"].price_per_instance_hour == D("3.673385")
    assert ls["g4-standard-48:us-central1"].gpu_count == 1 and ls["g4-standard-48:us-central1"].provider_tier == "hyperscaler"
    assert ls["g4-standard-192:us-central1"].gpu_count == 4, "a byte-order mark before the name is ignored"
    assert "a4-highgpu-8g:us-central1" not in ls, "on-demand N/A"
    assert not any(k.startswith("nvidia-") for k in ls), "the attachment table here prints wrong memory: not read"


def test_gcp_sources_registered():
    urls = {s.url: s for s in sources() if s.provider == "gcp"}
    assert {"cloud.google.com/compute/pricing", "cloud.google.com/compute/gpus-pricing",
            "cloud.google.com/compute/vm-instance-pricing"} <= set(urls)


# ------------------------------------------------------------------------------------------------------------- Oracle

def test_oracle_price_list_capture_through_live_parser():
    ls, used = _src("apexapps.oracle.com/pls/apex/cetools/api/v1/products/*").parse(
        (FIX / "oracle_price-list-api_2026-08_usd.json").read_bytes(), AT)
    assert "live parser" in used
    by = {l.listing_id: l for l in ls}
    h100 = by["BM.GPU.H100.8"]
    assert h100.price_per_gpu_hour == D("10") and h100.price_per_instance_hour == D("80") and h100.gpu_count == 8
    assert by["VM.GPU.A10.1"].provider_tier == "vm" and h100.provider_tier == "bare-metal"
    assert all(l.observed_at == AT for l in ls), "the capture time, not the list's lastUpdated"


def test_oracle_price_list_without_usd_raises():
    d = json.loads((FIX / "oracle_price-list-api_2026-08_usd.json").read_text())
    for item in d["items"]:
        for c in item["currencyCodeLocalizations"]:
            c["currencyCode"] = "BRL"
    _raises(oracle.oracle_price_list, json.dumps(d).encode(), AT, contains="USD")


def test_oracle_static_page_2018():
    ls = oracle.oracle_static_page(_page("oracle_compute-pricing_2018-04_static_usd.html"), AT)
    assert len(ls) == 1, "P100 cells are script placeholders; V100 VMs are 'Coming Soon'"
    bm = ls[0]
    assert bm.listing_id == "BM.GPU3.8" and bm.gpu_count == 8 and bm.canonical_gpu_name == "NVIDIA Tesla V100 16GB"
    assert bm.price_per_gpu_hour == D("2.25") and bm.price_per_instance_hour == D("18.00")
    assert bm.market_type == "on_demand" and bm.provider_tier == "bare-metal" and bm.region is None


def test_oracle_placeholder_page_stores_nothing():
    _raises(oracle.oracle_static_page, _page("oracle_compute-pricing_2019-02_placeholders.html"), AT,
            contains="script placeholders")
    page = _page("oracle_compute-pricing_2018-04_static_usd.html").replace("GPU: 8x V100", "GPU: 4x V100")
    _raises(oracle.oracle_static_page, page, AT, contains="SHAPES says 8")


# ------------------------------------------------------------------------------------------------------------- Linode

LINODE = "www.linode.com/pricing/"


def test_linode_pricing_page_designs_join_the_live_ids():
    src = _src(LINODE)
    for name, parser, n in (("linode_pricing_2019-09_gpu_cards.html", "linode_gpu_cards", 4),
                            ("linode_pricing_2020-06_o_table.html", "linode_pricing_page", 4),
                            ("linode_pricing_2021-08_o_table_hourly.html", "linode_pricing_page", 4),
                            ("linode_pricing_2023-06_c_table.html", "linode_pricing_page", 4),
                            ("linode_pricing_2025-06_c_table_ada.html", "linode_pricing_page", 13),
                            ("linode_pricing_2025-10_plain_tables.html", "linode_pricing_page", 13)):
        ls, used = src.parse((FIX / name).read_bytes(), AT)
        assert used.endswith(parser) and len(ls) == n, (name, used, len(ls))
        by = {l.listing_id: l for l in ls}
        x1 = by["g1-gpu-rtx6000-1"]
        assert x1.price_per_instance_hour == D("1.5") and x1.gpu_count == 1 and x1.raw_gpu_name == "RTX6000", name
        assert x1.region is None and x1.provider_tier == "vm" and x1.vcpu == 8 and x1.ram_gb == 32.0
        x4 = by["g1-gpu-rtx6000-4"]
        assert x4.gpu_count == 4 and x4.price_per_gpu_hour == D("1.5"), "monthly figures are never read"
    ada = {l.listing_id: l for l in linode.linode_pricing_page(_page("linode_pricing_2025-06_c_table_ada.html"), AT)}
    assert ada["g2-gpu-rtx4000a2-hs"].price_per_instance_hour == D("1.49") and ada["g2-gpu-rtx4000a2-hs"].gpu_count == 2
    assert ada["g2-gpu-rtx4000a1-s"].canonical_gpu_name == "NVIDIA RTX 4000 Ada 20GB"


def test_linode_page_rows_must_agree_with_the_api_catalogue():
    page = _page("linode_pricing_2023-06_c_table.html").replace(
        'col--cpus">8</td>', 'col--cpus">6</td>', 1)
    _raises(linode.linode_pricing_page, page, AT, contains="the API said 1 / 8 / 32")
    page = _page("linode_pricing_2023-06_c_table.html").replace("RTX6000 GPU x1", "RTX8000 GPU x1")
    _raises(linode.linode_pricing_page, page, AT, contains="not one of PLANS")
    page = _page("linode_pricing_2019-09_gpu_cards.html").replace("<strong>32</strong>", "<strong>24</strong>", 1)
    _raises(linode.linode_gpu_cards, page, AT, contains="disagree with the API")


# --------------------------------------------------------------------------------------------------------- Jarvislabs

def test_jarvislabs_docs_tables_reserved_is_on_demand():
    ls, used = _src("jarvislabs.ai/pricing").parse((FIX / "jarvislabs_pricing_2022-05_docs_tables.html").read_bytes(), AT)
    assert used.endswith("jarvislabs_docs_tables")
    by = {l.listing_id: l for l in ls}
    a6000 = by["nvidia-a6000:1x"]                  # "A6000 - 48GB ... $1.79/hr (Reserved) $0.79/hr (Spot)"
    assert a6000.price_per_gpu_hour == D("1.79") and a6000.market_type == "on_demand" and a6000.gpu_count == 1
    assert a6000.raw_gpu_name == "NVIDIA A6000 48GB" and a6000.canonical_gpu_name == "NVIDIA RTX A6000 48GB"
    assert a6000.vcpu == 7 and a6000.ram_gb == 32.0
    spot = by["nvidia-a6000:1x:spot"]
    assert spot.price_per_gpu_hour == D("0.79") and spot.market_type == "spot" and spot.interruptible
    assert by["nvidia-a100:1x"].price_per_gpu_hour == D("2.39") and by["nvidia-a100:1x"].raw_gpu_name == "NVIDIA A100 40GB"
    assert len(ls) == 10, "5 GPUs x (on-demand, spot); weekly / monthly never read"
    _raises(jarvislabs.jarvislabs_docs_tables, _page("jarvislabs_pricing_2022-05_docs_tables.html").replace(
        "$1.79/hr", "$1.79 per week"), AT, contains="not '$x/hr'")


def test_jarvislabs_cards_2021():
    ls, used = _src("jarvislabs.ai/pricing").parse((FIX / "jarvislabs_pricing_2021-09_cards.html").read_bytes(), AT)
    assert used.endswith("jarvislabs_cards")
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"nvidia-quadro-rtx-5000:1x", "nvidia-quadro-rtx-6000:1x", "nvidia-rtx-a6000:1x", "nvidia-rtx-a100:1x"}
    assert by["nvidia-quadro-rtx-5000:1x"].price_per_gpu_hour == D("0.49")
    assert by["nvidia-quadro-rtx-5000:1x"].raw_gpu_name == "NVIDIA QUADRO RTX 5000 16GB"
    assert all(l.market_type == "on_demand" and l.gpu_count == 1 for l in ls)


# ------------------------------------------------------------------------------------------------------------ Baseten

def test_baseten_specs_with_cpu_2025():
    ls, used = _src("www.baseten.co/pricing/").parse((FIX / "baseten_pricing_2025-05_specs_with_cpu.html").read_bytes(), AT)
    assert used.endswith("baseten_specs_with_cpu"), "the live parser rejects specs that also name CPUs and RAM"
    by = {l.listing_id: l for l in ls}
    h100 = by["h100"]                                  # "80 GiB VRAM, 26 vCPUs, 234 GiB RAM"
    assert h100.price_per_gpu_hour == D("6.5") and h100.raw_gpu_name == "NVIDIA H100 80GB" and h100.gpu_count == 1
    assert h100.vcpu == 26 and h100.ram_gb == 234.0 and h100.provider_tier == "serverless"
    assert by["a10g"].raw_gpu_name == "NVIDIA A10G 24GB", "'24 GiB VM' as live"
    assert not any("mig" in k for k in by)
    page = _page("baseten_pricing_2025-05_specs_with_cpu.html").replace("26 vCPUs", "26 cores")
    _raises(baseten.baseten_specs_with_cpu, page, AT, contains="are not")


TESTS = [test_gcp_cloud_pricer_iowa_attachments_and_preemptible, test_gcp_cloud_pricer_refuses_pages_without_the_table,
         test_gcp_gpus_pricing_per_gpu_design, test_gcp_vm_instance_pricing_accelerator_section,
         test_gcp_cloud_pricer_dies_and_boards_header_2018, test_gcp_vm_instance_pricing_components_design,
         test_gcp_sources_registered,
         test_oracle_price_list_capture_through_live_parser, test_oracle_price_list_without_usd_raises,
         test_oracle_static_page_2018, test_oracle_placeholder_page_stores_nothing,
         test_linode_pricing_page_designs_join_the_live_ids, test_linode_page_rows_must_agree_with_the_api_catalogue,
         test_jarvislabs_docs_tables_reserved_is_on_demand, test_jarvislabs_cards_2021, test_baseten_specs_with_cpu_2025]

if __name__ == "__main__":
    for t in TESTS:
        t()
        print(t.__name__, "ok")
