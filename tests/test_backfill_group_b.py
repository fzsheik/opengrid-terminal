"""Archived price history for Mithril (Foundry), Hyperbolic, DeepInfra, Replicate and IONOS (backfill/parsers/; fixtures
are trimmed Internet Archive captures, source URL in each file's first line).

Run:  .venv/bin/python tests/test_backfill_group_b.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (first: isolates the process from real networks)

from backfill.load import _rows  # noqa: E402
from backfill.parsers import deepinfra, hyperbolic, ionos, mithril, replicate  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _fix(name):
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


def test_sources_registered():
    urls = {s.url: s.provider for s in sources()}
    for url, p in (("mithril.ai/pricing", "mithril"), ("mlfoundry.com/pricing", "mithril"),
                   ("hyperbolic.xyz/pricing", "hyperbolic"), ("www.hyperbolic.ai/marketplace", "hyperbolic"),
                   ("deepinfra.com/gpu-instances", "deepinfra"), ("deepinfra.com/pricing", "deepinfra"),
                   ("replicate.com/pricing", "replicate"),
                   ("docs.ionos.com/support/general-information/price-list/ionos-cloud-inc", "ionos"),
                   ("docs.ionos.com/support/general-information/price-list/ionos-cloud-inc.md", "ionos")):
        assert urls.get(url) == p, url


# ---------------------------------------------------------------------------------------------------------- Mithril

def test_mithril_foundry_two_columns_reads_spot_only():
    ls = {l.listing_id: l for l in mithril.two_columns(_fix("mithril_mlfoundry-pricing_2025-01_two_columns.html"), AT)}
    assert len(ls) == 15
    h = ls["nvidia-h100-sxm5:8x:192c-1814g-15202d"]
    # spot column $8.02 instance/hr, never the reserved $23.92
    assert h.price_per_instance_hour == D("8.02") and h.price_per_gpu_hour == D("1.0025") and h.gpu_count == 8
    assert h.market_type == "spot" and h.interruptible and h.provider_tier == "vm"
    assert h.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h.vcpu == 192 and h.ram_gb == 1814.0
    a40 = ls["nvidia-a40-pcie:1x:12c-58g-862.5d"]
    assert a40.price_per_gpu_hour == D("0.27") and a40.storage_gb == 862.5
    assert ls["nvidia-a100-sxm4:1x:24c-225g-1750d"].canonical_gpu_name is None, "VRAM not stated: unmapped, as live"
    assert all(l.price_per_instance_hour not in (D("23.92"), D("1.49")) for l in ls.values()), "reserved never read"


def test_mithril_2026_two_columns_through_source():
    ls, used = _src("mithril.ai/pricing").parse(_fix("mithril_pricing_2026-02_two_columns.html").encode(), AT)
    assert used == "backfill.parsers.mithril.two_columns", "the live parser rejects the Reserved column"
    by = {l.listing_id: l for l in ls}
    assert "nvidia-a100-pcie:1x:16c-128g-2.5d" not in by, "a $0.00 row is skipped, as live"
    assert by["h200-sxm5:8x:168c-1600g-1120d"].price_per_instance_hour == D("8.00")
    assert by["h200-sxm5:8x:168c-1600g-1120d"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    # the live listing id shape ("h200-sxm5:8x:<vcpu>c-<ram>g-<disk>d"), so the series joins live data
    assert all(l.listing_id.count(":") == 2 and l.observed_at == AT for l in ls)


def test_mithril_rejects_garbage():
    page = _fix("mithril_pricing_2026-02_two_columns.html")
    _raises(mithril.two_columns, page.replace("average of the last 7 days", "average"), AT, contains="7-day")
    _raises(mithril.two_columns, page.replace("instance/hr", "GPU/hr", 1), AT, contains="does not read")
    _raises(mithril.two_columns, "<html><p>Pricing</p></html>", AT, contains="7-day")


# ------------------------------------------------------------------------------------------------------- Hyperbolic

def test_hyperbolic_xyz_table_reads_rent_table_not_supply_caps():
    ls = hyperbolic.xyz_pricing_table(_fix("hyperbolic_xyz-pricing_2025-06_table_rows.html"), AT)
    assert [(l.listing_id, l.price_per_gpu_hour, l.gpu_count) for l in ls] == [("H200:from", D("2.4"), 1)]
    assert ls[0].canonical_gpu_name == "NVIDIA H200 141GB SXM5" and ls[0].provider_tier == "starting-from"
    assert ls[0].market_type == "on_demand"
    # supplier caps ("Max Rental Payment" $3.20/hour) are never read
    assert all(l.price_per_gpu_hour != D("3.20") for l in ls)
    # Jan 2025: every price is filled in the browser: nothing printed, nothing stored, reported
    _raises(hyperbolic.xyz_pricing_table, _fix("hyperbolic_xyz-pricing_2025-01_table_rows_unfilled.html"), AT,
            contains="no price printed")


def test_hyperbolic_marketplace_list_and_table():
    ls = {l.listing_id: l for l in hyperbolic.marketplace_list(_fix("hyperbolic_marketplace_2025-06_list.html"), AT)}
    assert ls["H100 SXM:from"].price_per_gpu_hour == D("1.50")
    assert ls["H100 SXM:from"].canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert ls["B200:from"].canonical_gpu_name == "NVIDIA B200 180GB SXM" and ls["RTX 4090:from"].price_per_gpu_hour == D("0.30")
    assert ls["RTX 3070:from"].canonical_gpu_name is None
    ls2, used = _src("www.hyperbolic.ai/marketplace").parse(_fix("hyperbolic_marketplace_2026-11_table.html").encode(), AT)
    assert used.endswith("marketplace_table")
    assert {l.listing_id: l.price_per_gpu_hour for l in ls2} == {"H100 SXM:from": D("3.19"), "H200:from": D("3.99"),
                                                                 "B200:from": D("5.99")}
    _raises(hyperbolic.marketplace_table, _fix("hyperbolic_marketplace_2026-11_table.html").replace("$3.19 / HR", "$3.19 / mo"),
            AT, contains="is not '$x / hr'")


# -------------------------------------------------------------------------------------------------------- DeepInfra

def test_deepinfra_gpu_instances_live_parser():
    ls, used = _src("deepinfra.com/gpu-instances").parse(_fix("deepinfra_gpu-instances_2025-08_live.html").encode(), AT)
    assert used == "providers.deepinfra (live parser)"
    by = {l.listing_id: l for l in ls}
    assert by["nvidia-b200:8x"].price_per_instance_hour == D("19.92") and by["nvidia-b200:8x"].price_per_gpu_hour == D("2.49")
    assert all(l.visibility == "public" and not l.listing_id.endswith(":api") for l in ls), "page prices only, public"
    rows, _ = _rows(ls, source="wayback", source_url="x", sha256="0", parser=used)
    assert all(r["visibility"] == "public" for r in rows)


def test_deepinfra_custom_llm_tables():
    ls = {l.listing_id: l for l in deepinfra.custom_llm_table(_fix("deepinfra_pricing_2024-04_custom_llm_two_columns.html"), AT)}
    assert set(ls) == {"a100-80gb:custom-llm", "h100-80gb:custom-llm"}
    assert ls["a100-80gb:custom-llm"].price_per_gpu_hour == D("2.00")
    assert ls["a100-80gb:custom-llm"].canonical_gpu_name == "NVIDIA A100 80GB SXM4", "VRAM from the section's sentence"
    assert ls["h100-80gb:custom-llm"].canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert all(l.provider_tier == "custom-llm" and l.gpu_count == 1 and l.visibility == "public" for l in ls.values())
    new = {l.listing_id: l for l in deepinfra.custom_llm_table(
        _fix("deepinfra_pricing_2026-11_custom_llm_memory_column.html"), AT)}
    assert new["b200-180gb:custom-llm"].price_per_gpu_hour == D("4.89")
    assert new["b200-180gb:custom-llm"].canonical_gpu_name == "NVIDIA B200 180GB SXM"
    assert new["b300-270gb:custom-llm"].canonical_gpu_name is None, "270 GB is not canonical's 288 GB B300"
    # without "SXM-connected" the form factor is not stated: the A100 stays unmapped
    plain = _fix("deepinfra_pricing_2024-04_custom_llm_two_columns.html").replace("SXM-connected", "connected")
    a100 = {l.listing_id: l for l in deepinfra.custom_llm_table(plain, AT)}["a100-80gb:custom-llm"]
    assert a100.raw_gpu_name == "NVIDIA A100 80GB" and a100.canonical_gpu_name is None
    _raises(deepinfra.custom_llm_table, plain.replace("/GPU-hour", "/hour", 1), AT, contains="$x/GPU-hour")


# -------------------------------------------------------------------------------------------------------- Replicate

def test_replicate_cards_2022():
    ls, used = _src("replicate.com/pricing").parse(_fix("replicate_pricing_2022-07_cards.html").encode(), AT)
    assert used.endswith("cards_2022")
    by = {l.listing_id: l for l in ls}
    assert set(by) == {"gpu-t4", "nvidia-a100-gpu"}, "the CPU card gives no listing"
    assert by["gpu-t4"].price_per_instance_hour == D("0.00055") * 3600 == D("1.98")
    assert by["gpu-t4"].canonical_gpu_name == "NVIDIA T4 16GB" and by["gpu-t4"].provider_tier == "serverless"
    assert by["nvidia-a100-gpu"].raw_gpu_name == "Nvidia A100 (40GB)" and by["nvidia-a100-gpu"].canonical_gpu_name is None
    bad = _fix("replicate_pricing_2022-07_cards.html").replace("$0.033 per minute", "$0.034 per minute")
    _raises(replicate.cards_2022, bad, AT, contains="x 60")


def test_replicate_2023_tables():
    jul = {l.listing_id: l for l in replicate.hardware_table(_fix("replicate_pricing_2023-07_table_per_minute.html"), AT)}
    assert jul["gpu-t4"].price_per_instance_hour == D("1.98") and "cpu" not in jul
    a40x8 = jul["8x-nvidia-a40-gpu"]   # printed "Nvidia 8x A40 GPU": the count is read, never the A40 (Large) id
    assert a40x8.gpu_count == 8 and a40x8.price_per_instance_hour == D("16.956") and a40x8.raw_gpu_name == "Nvidia A40"
    sep = {l.listing_id: l for l in replicate.hardware_table(_fix("replicate_pricing_2023-09_table_public_private.html"), AT)}
    # the public-model column ($0.000225/sec), not the private-model one ($0.000550/sec)
    assert sep["gpu-t4"].price_per_instance_hour == D("0.81") and sep["gpu-a100-large"].price_per_instance_hour == D("5.04")
    bad = _fix("replicate_pricing_2023-07_table_per_minute.html").replace("$0.033 per minute", "$0.043 per minute")
    _raises(replicate.hardware_table, bad, AT, contains="x 60")


def test_replicate_tables():
    ls ={l.listing_id: l for l in replicate.hardware_table(_fix("replicate_pricing_2023-11_table_no_ids.html"), AT)}
    assert ls["gpu-a40-large-8x"].gpu_count == 8 and ls["gpu-a40-large-8x"].price_per_instance_hour == D("20.88")
    assert ls["gpu-a40-large-8x"].price_per_gpu_hour == D("2.61")
    assert "nvidia-a100-40gb-gpu" in ls and ls["gpu-a40-small"].canonical_gpu_name == "NVIDIA A40 48GB"
    ids = {l.listing_id: l for l in replicate.hardware_table(_fix("replicate_pricing_2024-12_table_ids.html"), AT)}
    assert ids["gpu-8x-l40s"].price_per_gpu_hour == D("3.51") and ids["gpu-8x-l40s"].canonical_gpu_name == "NVIDIA L40S 48GB"
    assert not any("h100" in k for k in ids), "'Additional hardware' (committed spend) is never read"
    spaced = {l.listing_id: l for l in replicate.hardware_table(_fix("replicate_pricing_2025-05_table_spaced.html"), AT)}
    assert spaced["gpu-a100-large"].price_per_instance_hour == D("5.04") and spaced["gpu-a100-large"].vcpu == 10
    # a per-second price that disagrees with the printed hourly one raises
    bad = _fix("replicate_pricing_2024-12_table_ids.html").replace("$0.81/hr", "$0.91/hr")
    _raises(replicate.hardware_table, bad, AT, contains="x 3600")


# ------------------------------------------------------------------------------------------------------------ IONOS

def test_ionos_markdown_and_gitbook_use_live_normalize():
    md, used = _src("docs.ionos.com/support/general-information/price-list/ionos-cloud-inc.md").parse(
        _fix("ionos_price-list-md_2026-01_markdown_bold.md").encode(), AT)
    assert used.endswith("markdown_bold"), "the live section pattern misses the bold heading"
    for name in ("ionos_price-list_2026-01_gitbook.html", "ionos_price-list_2026-02_gitbook_icon_heading.html",
                 "ionos_price-list_2026-06_gitbook.html"):
        g = ionos.gitbook_table(_fix(name), AT)
        assert [(l.listing_id, l.price_per_instance_hour, l.gpu_count) for l in g] == \
               [(l.listing_id, l.price_per_instance_hour, l.gpu_count) for l in md], name
    by = {l.listing_id: l for l in md}
    assert by["H200-XL"].price_per_instance_hour == D("26.086") and by["H200-XL"].gpu_count == 8
    assert by["H200-S"].canonical_gpu_name == "NVIDIA H200 143GB NVL" and by["H200-S"].currency == "USD"
    assert by["H200-S"].region == "de/fra/2; gb/bhx" and by["H200-S"].vcpu == 15
    # savings-plan (commitment) rows never appear
    assert set(by) == {"H200-S", "H200-M", "H200-L", "H200-XL"}


def test_ionos_rejects_other_lists_and_reshapes():
    page = _fix("ionos_price-list_2026-06_gitbook.html")
    _raises(ionos.gitbook_table, page.replace("Inc.", "GmbH"), AT, contains="USD")
    _raises(ionos.gitbook_table, page.replace(">Number of GPUs<", ">GPUs<"), AT, contains="columns changed")
    _raises(ionos.gitbook_table, "<title>IONOS CLOUD Inc.</title><h3>Compute Engine</h3>", AT, contains="no 'Cloud GPU VM'")
    _raises(ionos.markdown_bold, _fix("ionos_price-list-md_2026-01_markdown_bold.md").replace("IONOS Inc.", "IONOS SE"),
            AT, contains="USD")


if __name__ == "__main__":
    for t in (test_sources_registered, test_mithril_foundry_two_columns_reads_spot_only,
              test_mithril_2026_two_columns_through_source, test_mithril_rejects_garbage,
              test_hyperbolic_xyz_table_reads_rent_table_not_supply_caps, test_hyperbolic_marketplace_list_and_table,
              test_deepinfra_gpu_instances_live_parser, test_deepinfra_custom_llm_tables, test_replicate_cards_2022,
              test_replicate_2023_tables,
              test_replicate_tables, test_ionos_markdown_and_gitbook_use_live_normalize,
              test_ionos_rejects_other_lists_and_reshapes):
        t()
        print(t.__name__, "ok")
