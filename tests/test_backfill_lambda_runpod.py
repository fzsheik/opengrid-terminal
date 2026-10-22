"""Archived-page parsers for Lambda (lambda.ai/pricing) and RunPod (www.runpod.io/pricing): backfill/parsers/lambda_.py,
backfill/parsers/runpod.py. Fixtures are trimmed Internet Archive captures (tests/fixtures_backfill/, source in each).

Run:  .venv/bin/python tests/test_backfill_lambda_runpod.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (isolates the process from real networks)
from backfill.parsers import lambda_, runpod  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
AT = datetime(2026, 1, 9, 10, 39, tzinfo=timezone.utc)


def _by(listings, key):
    out = {}
    for l in listings:
        out[key(l)] = l
    return out


def _lambda(name):
    src = lambda_.SOURCES[0]
    return src.parse((FIX / name).read_bytes(), AT)


def test_lambda_current_tabs_match_live_api_names_and_prices():
    ls, used = _lambda("lambda_2026-10.html")
    assert used.endswith("instance_tabs")
    by = _by(ls, lambda l: (l.canonical_gpu_name, l.gpu_count))
    h100 = by[("NVIDIA H100 80GB SXM5", 8)]
    assert h100.raw_gpu_name == "H100 (80 GB SXM5)", "page name translated to the live API's gpu_description"
    assert h100.price_per_gpu_hour == D("3.99") and h100.price_per_instance_hour == D("31.92")
    assert by[("NVIDIA H100 80GB SXM5", 1)].price_per_gpu_hour == D("4.29")
    assert by[("NVIDIA A100 80GB SXM4", 8)].price_per_gpu_hour == D("2.79")
    assert by[("NVIDIA A100 40GB SXM4", 8)].price_per_gpu_hour == D("1.99")
    assert by[("NVIDIA Tesla V100 16GB", 8)].price_per_gpu_hour == D("0.79"), "V100 is only offered as 8x"
    assert by[("NVIDIA B200 180GB SXM", 4)].price_per_gpu_hour == D("6.79")
    assert all(l.canonical_gpu_name for l in ls), [l.raw_gpu_name for l in ls if not l.canonical_gpu_name]
    assert all(l.market_type == "on_demand" and l.interruptible is False and l.observed_at == AT for l in ls)
    # cluster tabs (2 weeks - 1 year commitments, $9.86/GPU-hr B200 etc.) are never read as instances
    assert not any(l.price_per_gpu_hour in (D("9.86"), D("6.16")) for l in ls)
    assert len(ls) == 22


def test_lambda_jan_2026_tabs_with_reused_panel_ids():
    ls, used = _lambda("lambda_2026-01.html")
    assert used.endswith("instance_tabs")
    by = _by(ls, lambda l: (l.canonical_gpu_name, l.gpu_count))
    assert by[("NVIDIA H100 80GB SXM5", 8)].price_per_gpu_hour == D("2.99")
    assert by[("NVIDIA H100 80GB SXM5", 4)].price_per_gpu_hour == D("3.09")
    assert by[("NVIDIA H100 80GB SXM5", 1)].price_per_gpu_hour == D("3.29")
    assert by[("NVIDIA GH200 96GB", 1)].price_per_gpu_hour == D("1.49")
    # the cluster tab set's "On-Demand $3.79" row (same panel ids) is not read as an 8x instance
    assert not any(l.price_per_gpu_hour == D("3.79") for l in ls)


def test_lambda_nov_2025_header_tables():
    ls, used = _lambda("lambda_2025-11.html")
    assert used.endswith("header_tables")
    by = _by(ls, lambda l: (l.canonical_gpu_name, l.gpu_count))
    assert by[("NVIDIA B200 180GB SXM", 8)].price_per_gpu_hour == D("4.99")
    assert by[("NVIDIA H100 80GB SXM5", 8)].price_per_gpu_hour == D("2.99")
    assert by[("NVIDIA H100 80GB PCIe", 1)].price_per_gpu_hour == D("2.49")
    assert by[("NVIDIA Quadro RTX 6000 24GB", 1)].price_per_gpu_hour == D("0.50")
    assert len(ls) == 19 and all(l.canonical_gpu_name for l in ls)


def test_lambda_rejects_a_recognised_row_it_cannot_read():
    bad = (FIX / "lambda_2026-10.html").read_text().replace("$3.99", "$3.99/mo", 1)
    try:
        lambda_.instance_tabs(bad, AT)
        raise AssertionError("an unreadable price in a recognised row must raise")
    except ValueError as exc:
        assert "unreadable price" in str(exc)


def _runpod(name):
    return runpod.SOURCES[0].parse((FIX / name).read_bytes(), AT)


def test_runpod_current_cards():
    ls, used = _runpod("runpod_2026-10.html")
    assert used.endswith("gpu_cards")
    by = _by(ls, lambda l: (l.raw_gpu_name, l.provider_tier))
    h100 = by[("NVIDIA H100 80GB HBM3", "secure")]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.price_per_gpu_hour == D("3.99")
    assert h100.listing_id == "NVIDIA H100 80GB HBM3:secure:1", "same id as the live 1-GPU listing"
    assert by[("NVIDIA H100 80GB HBM3", "community")].price_per_gpu_hour == D("2.69")
    assert by[("NVIDIA H100 80GB HBM3", "community")].interruptible is True and h100.interruptible is False
    assert by[("NVIDIA B300 SXM6 AC", "secure")].price_per_gpu_hour == D("8.99"), "VRAM read from '288 GB HBM3e'"
    assert by[("NVIDIA GeForce RTX 4090", "community")].price_per_gpu_hour == D("0.34")
    # ambiguous on the page (Server or Workstation Edition): kept under the page's name, unmapped
    assert by[("RTX Pro 6000 96GB", "secure")].canonical_gpu_name is None
    assert all(l.gpu_count == 1 and l.market_type == "on_demand" for l in ls)


def test_runpod_older_gpu_rows():
    ls, used = _runpod("runpod_2025-12.html")
    assert used.endswith("gpu_rows")
    by = _by(ls, lambda l: (l.raw_gpu_name, l.provider_tier))
    assert by[("NVIDIA H200", "secure")].price_per_gpu_hour == D("3.59")
    assert by[("NVIDIA B200", "secure")].price_per_gpu_hour == D("5.19")
    assert by[("NVIDIA A100 80GB PCIe", "community")].price_per_gpu_hour == D("1.19")
    assert by[("NVIDIA H100 PCIe", "community")].canonical_gpu_name == "NVIDIA H100 80GB PCIe"


def test_runpod_refuses_per_second_values():
    page = (FIX / "runpod_2025-12.html").read_text().replace('data-secure-cloud-price="3.59"',
                                                                 'data-secure-cloud-price="0.000997"', 1)
    try:
        runpod.gpu_rows(page, AT)
        raise AssertionError("a per-second-sized value must not be read as hourly")
    except ValueError as exc:
        assert "per-second" in str(exc)


def test_registered():
    got = {(s.provider, s.url) for s in sources()}
    assert ("lambda", "lambda.ai/pricing") in got and ("runpod", "www.runpod.io/pricing") in got


if __name__ == "__main__":
    for t in (test_lambda_current_tabs_match_live_api_names_and_prices, test_lambda_jan_2026_tabs_with_reused_panel_ids,
              test_lambda_nov_2025_header_tables, test_lambda_rejects_a_recognised_row_it_cannot_read,
              test_runpod_current_cards, test_runpod_older_gpu_rows, test_runpod_refuses_per_second_values,
              test_registered):
        t()
        print(t.__name__, "ok")
