"""Archived older designs: Hot Aisle, Jarvislabs, Google Cloud, Massed Compute (backfill/parsers/; fixtures are
trimmed Internet Archive captures, see each file's first line).

Run:  .venv/bin/python tests/test_backfill_pages_misc.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (isolates the process from real networks)

from backfill.parsers import gcp, hotaisle, jarvislabs, massedcompute  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2026, 3, 19, 14, 14, tzinfo=timezone.utc)


def _page(name):
    return (FIX / name).read_text()


def _src(provider):
    return next(s for s in sources() if s.provider == provider)


def test_hotaisle_designs():
    n = hotaisle.hotaisle_notion(_page("hotaisle_2025-12_notion.html"), AT)
    assert [l.listing_id for l in n] == ["mi300x:1x:vm", "mi300x:2x:vm", "mi300x:4x:vm", "mi300x:8x:vm"]
    assert all(l.price_per_gpu_hour == D("1.99") and l.canonical_gpu_name == "AMD Instinct MI300X 192GB" for l in n)
    assert n[2].price_per_instance_hour == D("7.96") and n[0].country is None, "no 'Michigan facility' line: no country"
    for name, fn in (("hotaisle_2026-04_headline.html", hotaisle.hotaisle_headline_price),
                     ("hotaisle_2026-05_minified.html", hotaisle.hotaisle_minified_cards)):
        ls = fn(_page(name), AT)
        # the 8x card is bare metal: skipped, as the live parser skips it
        assert [l.listing_id for l in ls] == ["mi300x:1x:vm", "mi300x:2x:vm", "mi300x:4x:vm"], name
        assert all(l.price_per_gpu_hour == D("1.99") and l.country == "US" and l.observed_at == AT for l in ls), name
    # through the Source: the live parser fails first, then the right older parser is named
    ls, used = _src("hotaisle").parse(_page("hotaisle_2026-05_minified.html").encode(), AT)
    assert used == "backfill.parsers.hotaisle.hotaisle_minified_cards" and len(ls) == 3


def test_jarvislabs_two_tables_and_per_gpu_cross_check():
    for name, h100 in (("jarvislabs_2025-12_two_tables.html", "2.99"), ("jarvislabs_2026-04_two_tables.html", "2.69")):
        ls = {l.listing_id: l for l in jarvislabs.jarvislabs_two_tables(_page(name), AT)}
        h = ls["nvidia-h100-sxm:1x"]
        assert h.price_per_gpu_hour == D(h100) and h.gpu_count == 1 and h.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
        assert h.raw_gpu_name == "NVIDIA H100 SXM 80GB" and h.vcpu == 16 and h.ram_gb == 200.0, name
        assert ls["nvidia-h200-sxm:1x"].canonical_gpu_name == "NVIDIA H200 141GB SXM5"
    # a workbench price that disagrees with the VM table's per-GPU price: the basis is unknown, so it raises
    page = _page("jarvislabs_2025-12_two_tables.html")
    bad = page.replace(">$2.99</td>", ">$3.09</td>", 1)
    try:
        jarvislabs.jarvislabs_two_tables(bad, AT)
        raise AssertionError("a workbench/VM price mismatch must raise")
    except ValueError as exc:
        assert "cannot tell" in str(exc)


def test_jarvislabs_live_layout_without_vendor_prefix():
    ls, used = _src("jarvislabs").parse(_page("jarvislabs_2026-05_no_vendor.html").encode(), AT)
    assert used.endswith("+ NVIDIA prefix")
    by = {l.listing_id: l for l in ls}
    assert by["nvidia-h100-sxm:1x"].canonical_gpu_name == "NVIDIA H100 80GB SXM5", "same id and GPU as live data"
    assert by["nvidia-h100-sxm:1x"].price_per_gpu_hour == D("2.69") and by["nvidia-l4:1x"].price_per_gpu_hour == D("0.44")


def test_gcp_on_demand_column_design():
    ls = {l.listing_id: l for l in gcp.gcp_on_demand_column(_page("gcp_2026-03_on_demand_column.html"), AT)}
    assert len(ls) == 28
    a3 = ls["a3-highgpu-8g:us-central1"]
    assert a3.region == "us-central1" and a3.country == "US" and a3.provider_tier == "hyperscaler"
    assert a3.gpu_count == 8 and a3.price_per_gpu_hour * 8 == a3.price_per_instance_hour
    assert ls["nvidia-tesla-t4:us-central1"].provider_tier == "gpu-attachment"
    assert "a4-highgpu-8g:us-central1" not in ls, "B200 on-demand is N/A on this capture: no listing"


def test_massedcompute_h3_tables():
    ls = {l.listing_id: l for l in massedcompute.massedcompute_h3_tables(_page("massedcompute_2026-03_h3_tables.html"), AT)}
    assert ls["H100 (80GB):4x"].price_per_instance_hour == D("9.60") and ls["H100 (80GB):4x"].price_per_gpu_hour == D("2.40")
    assert "H100 (80GB):8x" not in ls, "'Request' rows have no price"
    assert ls["RTX PRO 6000 Blackwell (96GB):1x"].vcpu == 16 and ls["RTX PRO 6000 Blackwell (96GB):1x"].ram_gb == 144.0


if __name__ == "__main__":
    for t in (test_hotaisle_designs, test_jarvislabs_two_tables_and_per_gpu_cross_check,
              test_jarvislabs_live_layout_without_vendor_prefix, test_gcp_on_demand_column_design,
              test_massedcompute_h3_tables):
        t()
        print(t.__name__, "ok")
