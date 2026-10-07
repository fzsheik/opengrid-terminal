"""Historical context: percentiles on a known series, coverage-gated labels, the panel rule.

Run:  .venv/Scripts/python tests/test_stats.py
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402

from analytics import stats  # noqa: E402
from analytics.stats import label, percentile_rank, window_stats  # noqa: E402
from store.analytics import MarketHourly  # noqa: E402

DB = "og_test_indices_stats"
GPU = "NVIDIA H100 80GB SXM5"
END = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_percentile_rank():
    xs = list(range(1, 101))
    assert percentile_rank(xs, 18) == 17.5, "17 below, one equal: (17 + 0.5) / 100"
    assert percentile_rank(xs, 0.5) == 0.0 and percentile_rank(xs, 1000) == 100.0
    assert percentile_rank([2, 2, 2, 2], 2) == 50.0, "all equal: the middle"
    assert percentile_rank([1, 2, 2, 3], 2) == 50.0


def test_label_bands():
    assert [label(p) for p in (0, 10, 10.01, 30, 30.01, 69.99, 70, 89.99, 90, 100)] == [
        "very cheap", "very cheap", "cheap", "cheap", "normal", "normal", "expensive", "expensive",
        "very expensive", "very expensive"]


def test_window_stats_coverage():
    hours = [END - timedelta(hours=i) for i in range(720)][::-1]
    full = [(h, 2.0 + (i % 10) / 10) for i, h in enumerate(hours)]
    st = window_stats(full, 2.0, END, 30)
    assert st["coverage"] == 1.0 and st["label"] == "very cheap" and st["percentile"] == 5.0
    assert abs(st["distance_from_median_pct"] - (2.0 - 2.45) / 2.45) < 1e-12
    thin = full[-500:]  # 69% of the window
    st = window_stats(thin, 2.0, END, 30)
    assert st["label"] is None and st["percentile"] is None and "insufficient history" in st["reason"]
    st = window_stats(full[-504:], 2.0, END, 30)  # exactly 70%
    assert st["label"] is not None
    assert window_stats(full, None, END, 30)["reason"] == "no current price"


def _hourly(provider, hours_prices):
    return [{"segment": "on_demand", "gpu": GPU, "provider": provider, "region": "", "hour": h, "country": None,
             "min_price": Decimal(str(p)), "max_price": Decimal(str(p)), "avg_price": Decimal(str(p)),
             "live_listings": 1, "priced_listings": 1, "available_listings": 1, "unknown_listings": 0,
             "sold_out_listings": 0, "capacity_gpus": None} for h, p in hours_prices]


def test_known_series_db():
    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        # syn_k: 100 days of hourly prices cycling 2.00..2.49; the current hour is 2.09.
        hours = [END - timedelta(hours=i) for i in range(100 * 24)][::-1]
        k = [(h, round(2.0 + (i % 50) / 100, 2)) for i, h in enumerate(hours)]
        k[-1] = (END, 2.09)
        # syn_new: recorded only for the last 10 days, much cheaper
        new = [(h, 1.50) for h in hours[-240:]]
        with Session.begin() as s:
            s.execute(insert(MarketHourly).values(_hourly("syn_k", k) + _hourly("syn_new", new)))
        normalize.SessionLocal = Session
        stats.clear_caches()

        g = stats.gpu_context(GPU)
        assert g["current"]["lowest"] == 1.5 and g["current"]["providers"] == 2
        w30 = g["metrics"]["lowest"]["windows"]["30d"]
        assert w30["panel"] == ["syn_k"] and w30["excluded_recent_providers"] == ["syn_new"], w30
        assert w30["current"] == 2.09, "the panel's own current value, not the newcomer's"
        samples = [p for h, p in k if h > END - timedelta(days=30)]
        assert w30["samples"] == 720 and abs(w30["percentile"] - percentile_rank(samples, 2.09)) < 0.01
        below = sum(1 for x in samples if x < 2.09)
        equal = sum(1 for x in samples if x == 2.09)
        assert abs(w30["percentile"] - 100 * (below + equal / 2) / 720) < 0.01
        assert w30["label"] == "cheap", w30["percentile"]
        w90 = g["metrics"]["lowest"]["windows"]["90d"]
        assert w90["samples"] == 2160 and w90["label"] is not None
        assert g["label"] == g["metrics"]["median"]["windows"]["90d"]["label"] and g["label_window"] == "90d"
        assert any("percentile of its 90-day range" in x for x in g["summaries"]), g["summaries"]
        hist = g["metrics"]["lowest"]["historical"]
        assert hist["low"]["value"] == 1.5 and hist["from_low_pct"] == 0.0

        # One provider against its own history
        p = stats.provider_gpu_context("syn_k", GPU)
        assert p["current"] == 2.09 and p["windows"]["30d"]["percentile"] == w30["percentile"]
        assert p["windows"]["30d"]["distance_from_median_pct"] is not None
        assert any("30-day median" in x for x in p["summaries"]), p["summaries"]
        assert p["vs_market_median_pct"] is not None
        # Thin history: syn_new has 10 days, so no 30d or 90d label
        n = stats.provider_gpu_context("syn_new", GPU)
        assert n["label"] is None and "insufficient history" in n["windows"]["30d"]["reason"]
        assert n["windows"]["30d"]["coverage"] == round(240 / 720, 4)
        assert "Not enough" not in " ".join(n["summaries"]) or n["historical"]["low"] is None
        assert stats.provider_gpu_context("nobody", GPU)["reason"].startswith("this provider has no")

        # gpu_history: the cross-section, the join, the coherent start
        h = stats.gpu_history(GPU, t0=END - timedelta(days=12), t1=END)
        assert h["providers_joined"] == [{"provider": "syn_new", "first_hour": hours[-240].isoformat()}]
        assert h["coherent_from"] == hours[-240].isoformat()
        assert h["series"][-1]["lowest"] == 1.5 and h["series"][-1]["providers"] == 2
        d = stats.gpu_history(GPU, resolution="1d")
        assert len(d["series"]) in (100, 101) and all(x["hours"] <= 24 for x in d["series"])
    finally:
        scratchdb.drop(DB)


def test_thin_history_and_listing():
    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        fixtures.seed(Session, days=10)
        normalize.SessionLocal = Session
        from analytics import rollups
        rollups.refresh()
        stats.clear_caches()
        g = stats.gpu_context(GPU)
        assert g["label"] is None and g["label_reason"], g
        for m in ("lowest", "median"):
            for w in ("30d", "90d"):
                assert g["metrics"][m]["windows"][w]["percentile"] is None
                assert g["metrics"][m]["windows"][w]["reason"]
        lc = stats.listing_context("syn_alpha", f"syn_alpha:{GPU}")
        if lc is not None:
            assert lc["label"] is None and "insufficient history" in (lc["windows"]["30d"]["reason"] or "no current")
        assert stats.listing_context("syn_alpha", "nope") is None
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_percentile_rank, test_label_bands, test_window_stats_coverage, test_known_series_db,
              test_thin_history_and_listing):
        t(); print(t.__name__, "ok")
