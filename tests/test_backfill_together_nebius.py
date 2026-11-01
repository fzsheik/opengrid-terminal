"""Archived designs of together.ai/pricing and nebius.com/prices (backfill/parsers/together.py, nebius.py).

Run:  .venv/bin/python tests/test_backfill_together_nebius.py
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (isolates the process from real networks)

from backfill.parsers import nebius as NB  # noqa: E402
from backfill.parsers import together as TG  # noqa: E402
from backfill.sources import sources  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_backfill"
PROV_FIX = Path(__file__).resolve().parent / "fixtures_providers"
AT = datetime(2026, 5, 15, 14, 20, tzinfo=timezone.utc)


def _src(provider):
    return next(s for s in sources() if s.provider == provider)


def _prices(listings, market="on_demand"):
    return {l.listing_id: l.price_per_gpu_hour for l in listings if l.market_type == market}


def test_together_designs_pick_the_right_parser_and_prices():
    src = _src("together")
    cases = [
        ("together_2025-12.html", "instant_clusters",
         {"nvidia-hgx-h100-sxm": D("2.99"), "nvidia-hgx-h200": D("3.79"), "nvidia-hgx-b200": D("5.50")}),
        ("together_2026-03-02.html", "on_demand_hourly",
         {"nvidia-hgx-h100-sxm": D("2.99"), "nvidia-hgx-h200": D("3.79"), "nvidia-hgx-b200": D("5.50")}),
        # H200 is "Contact us" here: no listing, not a zero
        ("together_2026-05.html", "on_demand_hourly", {"nvidia-hgx-h100": D("5.49"), "nvidia-hgx-b200": D("9.95")}),
        ("together_2026-08.html", "per_gpu_table",
         {"nvidia-hgx-h100": D("3.99"), "nvidia-hgx-h200": D("5.99"), "nvidia-hgx-b200": D("8.19")}),
    ]
    for name, parser, want in cases:
        listings, used = src.parse((FIX / name).read_bytes(), AT)
        assert used.endswith("." + parser), (name, used)
        assert _prices(listings) == want, (name, _prices(listings))
        assert not _prices(listings, "spot"), "no preemptible column in these designs"
        for l in listings:
            assert l.gpu_count == 1 and l.price_per_instance_hour == l.price_per_gpu_hour and l.observed_at == AT
            assert l.provider_tier == "gpu-cluster" and l.interruptible is False
    h100 = next(l for l in TG.per_gpu_table((FIX / "together_2026-08.html").read_text(), AT) if "h100" in l.listing_id)
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"


def test_together_per_gpu_table_reads_preemptible_like_live():
    page = next(PROV_FIX.glob("together*"), None)
    if page is None:
        print("  (no live together fixture; skipped)")
        return
    from providers.together import TogetherProvider, extract
    from types import SimpleNamespace
    live = TogetherProvider.normalize({"/pricing": [SimpleNamespace(endpoint="/pricing", payload=extract(page.read_text()),
                                                                    fetched_at=AT)]})
    mine = TG.per_gpu_table(page.read_text(), AT)
    key = lambda ls: sorted((l.listing_id, l.market_type, l.price_per_gpu_hour) for l in ls)  # noqa: E731
    assert key(mine) == key(live), "the archive parser agrees with the live parser on the current design"


def test_together_rejects_unknown_header():
    bad = ('<p>All prices are per GPU per hour.</p><table><tr><th>Hardware</th><th>Monthly</th></tr>'
           '<tr><td>NVIDIA HGX H100</td><td>$3.99</td></tr></table>')
    try:
        TG.per_gpu_table(bad, AT)
        raise AssertionError("an unknown column layout must raise")
    except ValueError as exc:
        assert "header not recognised" in str(exc)
    odd = ('<h2>GPU Clusters</h2><p>Pay as you go GPU capacity on an hourly basis.</p><table><tr><th>Hardware</th>'
           '<th>Hourly</th></tr><tr><td>NVIDIA HGX H100</td><td>3.99 USD</td></tr></table>')
    try:
        TG.on_demand_hourly(odd, AT)
        raise AssertionError("a price in an unexpected shape must raise")
    except ValueError as exc:
        assert "price cell" in str(exc)


def test_nebius_designs():
    src = _src("nebius")
    listings, used = src.parse((FIX / "nebius_2025-11.html").read_bytes(), AT)
    assert used.startswith("providers.nebius"), used
    assert _prices(listings) == {"NVIDIA HGX B200": D("5.50"), "NVIDIA HGX H200": D("3.50"), "NVIDIA HGX H100": D("2.95"),
                                 "NVIDIA L40S GPU with AMD": D("1.82"), "NVIDIA L40S GPU with Intel": D("1.55")}
    listings, used = src.parse((FIX / "nebius_2026-07.html").read_bytes(), AT)
    # The live parser reads this design again since Nebius brought it back (Oct 2026); it must agree exactly
    # with the archive parser written for it.
    assert used.startswith("providers.nebius"), used
    archive = NB.with_preemptible((FIX / "nebius_2026-07.html").read_text(), AT)
    assert [l.model_dump() for l in listings] == [l.model_dump() for l in archive], "live and archive parsers agree"
    p = _prices(listings)
    assert p["NVIDIA HGX H100"] == D("3.85") and p["NVIDIA HGX B300"] == D("7.85") and p["NVIDIA RTX PRO 6000"] == D("1.80")
    assert "NVIDIA GB300 NVL72" not in p, "'Contact us' is not a price"
    rtx = next(l for l in listings if l.raw_gpu_name == "NVIDIA RTX PRO 6000")
    assert rtx.canonical_gpu_name == "NVIDIA RTX PRO 6000 96GB SE", "archived rows: same Nebius lookup as live"
    assert all(l.market_type == "on_demand" and l.gpu_count == 1 for l in listings), "preemptible is never read"
    h100 = next(l for l in listings if l.listing_id == "NVIDIA HGX H100")
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"


def test_nebius_with_preemptible_needs_that_design():
    try:
        NB.with_preemptible((FIX / "nebius_2025-11.html").read_text(), AT)
        raise AssertionError("the older design has no preemptible column: not this parser's")
    except ValueError as exc:
        assert "no preemptible column" in str(exc)


if __name__ == "__main__":
    for t in (test_together_designs_pick_the_right_parser_and_prices, test_together_per_gpu_table_reads_preemptible_like_live,
              test_together_rejects_unknown_header, test_nebius_designs, test_nebius_with_preemptible_needs_that_design):
        t()
        print(t.__name__, "ok")
