"""Market structure: regions, hardware, dispersion, provider value, capability, alternatives, heatmaps, endpoints.

Run:  .venv/Scripts/python tests/test_structure.py
"""

import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical  # noqa: E402
import hardware  # noqa: E402
import regions  # noqa: E402
from analytics import dispersion, providers  # noqa: E402

DB = "og_test_structure"

# Real (provider, region, country) strings from the dev database, and what they must map to.
REAL = [
    ("aws", "us-east-1", "US", "US"),
    ("crusoe", "US, Culpeper, VA", "US", "US"),
    ("crusoe", "US, Fairview, OH; US, Sparks, NV; US, Culpeper, VA", "US", "US"),
    ("denvr", "CA, Calgary", None, "Canada"),
    ("denvr", "US, Houston, TX", "US", "US"),
    ("hyperbolic", "us-east-1", None, "US"),
    ("hyperbolic", "us-east-2", None, "US"),
    ("latitude", "DE, Frankfurt; US, Dallas, TX", None, None),          # spans two groups
    ("latitude", "NL, Amsterdam; AU, Sydney; JP, Tokyo; US, Ashburn, VA; US, Chic…", None, None),
    ("latitude", "US, Dallas, TX; US, Ashburn, VA", "US", "US"),
    ("lium", "Almaty", "KZ", "APAC"),
    ("lium", "Carballo", "ES", "Europe"),
    ("lium", "Chiyoda City", "JP", "APAC"),
    ("lium", "Dnipro", "UA", "Europe"),
    ("lium", "Dublin", "IE", "Europe"),
    ("lium", "Guangzhou", "CN", "APAC"),
    ("lium", "Hong Kong", "HK", "APAC"),
    ("lium", "Minsk", "BY", "Europe"),
    ("lium", "Moscow", "RU", "Europe"),
    ("lium", "Singapore", "SG", "APAC"),
    ("lium", "Sydney", "AU", "APAC"),
    ("lium", "Toronto", "CA", "Canada"),
    ("lium", "Ploieşti", "RO", "Europe"),
    ("lium", "Loulé", "PT", "Europe"),
    ("massedcompute", None, None, None),
    ("nebius", None, None, None),
    ("vast", "Arizona, US", None, "US"),
    ("vast", "Australia, AU", None, "APAC"),
    ("vast", "Belgium, BE", None, "Europe"),
    ("vast", "Czechia, CZ", None, "Europe"),
    ("vast", "Japan, JP", None, "APAC"),
    ("vast", "United States, US", None, "US"),
    ("verda", None, None, None),
    ("voltagepark", None, None, None),
    # Shapes from the provider normalizers (not yet in the local DB).
    ("aws", "eu-west-2", None, "UK"),
    ("aws", "eu-central-1", None, "Europe"),
    ("lambda", "europe-central-1", None, "Europe"),
    ("lambda", "asia-northeast-1", None, "APAC"),
    ("lambda", "me-west-1", None, "Middle East"),
    ("digitalocean", "nyc2,sfo3", None, "US"),
    ("digitalocean", "lon1", None, "UK"),
    ("digitalocean", "nyc2,ams3", None, None),
    ("hyperstack", "CANADA-1", None, "Canada"),
    ("hyperstack", "NORWAY-1", None, "Europe"),
    ("syn_alpha", "eu-west-2", None, None),   # generic eu- code: UK is inside it, so not guessed
    ("syn_alpha", "somewhere", None, None),
]


def test_region_group_real_strings():
    for p, r, c, want in REAL:
        got = regions.region_group(p, r, c)
        assert got == want, (p, r, c, got, want)
        assert got is None or got in regions.REGION_GROUPS
    assert regions.region_groups("latitude", "DE, Frankfurt; US, Dallas, TX", None) == {"Europe", "US"}
    assert "UK" in regions.region_label("aws", "eu-west-2", None)


def test_hardware_covers_every_canonical_name():
    assert hardware.missing() == []
    keys = {"vendor", "architecture", "generation", "vram_gb", "memory_type", "memory_bandwidth_tbps",
            "fp16_tflops_dense", "bf16_tflops_dense", "fp8_tflops_dense", "interconnect", "form_factor", "nvlink",
            "tdp_w", "workload_class", "released", "sources"}
    for n in canonical.all_canonical_names():
        s = hardware.spec(n)
        assert s and keys <= set(s), n
        assert s["sources"], n
        if s["fp8_supported"] is False:
            assert s["fp8_tflops_dense"] is None, n
        if s["fp8_tflops_dense"] and s["bf16_tflops_dense"]:
            assert abs(s["fp8_tflops_dense"] / s["bf16_tflops_dense"] - 2) < 0.05, n  # FP8 is 2x BF16 dense
    h = hardware.spec("NVIDIA H100 80GB SXM5")
    assert h["bf16_tflops_dense"] == 989.5 and h["memory_bandwidth_tbps"] == 3.35 and h["form_factor"] == "SXM"
    assert hardware.spec("NVIDIA A100 80GB SXM4")["fp8_tflops_dense"] is None
    assert hardware.spec("nope") is None
    hardware.spec("NVIDIA H100 80GB SXM5")["sources"].append("x")
    assert "x" not in hardware.spec("NVIDIA H100 80GB SXM5")["sources"], "spec returns a copy"


def test_dispersion_formulas():
    st = dispersion.stats([1.0, 2.0, 3.0, 4.0])
    assert st["low"] == 1 and st["high"] == 4 and st["median"] == 2.5 and st["spread_abs"] == 3
    assert st["spread_pct_of_low"] == 3.0 and abs(st["spread_pct_of_median"] - 1.2) < 1e-9
    assert st["q1"] == 1.75 and st["q3"] == 3.25 and st["iqr"] == 1.5 and abs(st["iqr_rel"] - 0.6) < 1e-9
    sd = statistics.stdev([1, 2, 3, 4])
    assert abs(st["stdev"] - sd) < 1e-6 and abs(st["cv"] - sd / 2.5) < 1e-6
    sc = dispersion.score(st)
    cv_adj = (sd / 2.5) * (1 + 1 / 16)
    expect = 100 * (0.4 * min(cv_adj / 0.6, 1) + 0.4 * min(0.6 / 0.6, 1) + 0.2 * min(3.0 / 3.0, 1))
    assert abs(sc["fragmentation"] - round(expect, 1)) < 1e-9 and sc["label"] == "highly fragmented"
    assert sc["efficiency"] == round(100 - expect, 1) and sc["confidence"] == "low"
    tight = dispersion.score(dispersion.stats([2.0, 2.02, 2.04, 2.06, 2.08, 2.1]))
    assert tight["label"] == "efficient" and tight["confidence"] == "normal", tight
    # Thin data: two providers -> spread yes, CV/IQR/score no, each with a reason.
    two = dispersion.stats([2.0, 3.0])
    assert two["spread_pct_of_low"] == 0.5 and two["cv"] is None and two["iqr"] is None and "cv" in two["reasons"]
    assert dispersion.score(two)["fragmentation"] is None and dispersion.score(two)["reason"]
    assert dispersion.stats([])["low"] is None
    assert abs(dispersion.premium(3.0, [2.0, 4.0, 2.5]) - 0.2) < 1e-9
    assert dispersion.premium(3.0, []) is None


H0 = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)


def _hr(gpu, p, h, price, avail=1):
    return {"gpu": gpu, "provider": p, "hour": H0 + timedelta(hours=h), "min_price": price,
            "available_listings": avail, "priced_listings": 1 if price else 0}


def test_daily_rows_definitions():
    g = "G"
    hourly = [_hr(g, "a", 0, 1.0), _hr(g, "b", 0, 2.0), _hr(g, "c", 0, 4.0),
              _hr(g, "a", 1, 3.0), _hr(g, "b", 1, 2.0),          # c absent in hour 1
              _hr(g, "b", 2, 2.0)]                               # only b in hour 2: no competition
    first = {(g, "a"): H0, (g, "b"): H0, (g, "c"): H0}
    hours = [H0 + timedelta(hours=i) for i in range(3)]
    prow, grow = providers.daily_rows(hourly, first, hours)
    P = {(r["gpu"], r["provider"]): r for r in prow}
    a, b, c = P[(g, "a")], P[(g, "b")], P[(g, "c")]
    # hour 0: a vs median(2,4)=3 -> -2/3 ; hour 1: a vs 2 -> +0.5
    assert a["hours_compared"] == 2 and abs(a["premium_sum"] - (-2 / 3 + 0.5)) < 1e-9
    assert a["hours_cheapest"] == 1 and a["hours_top3"] == 2 and a["rank_counts"] == {"1": 1, "2": 1}
    # c: tracked 3 hours, priced 1, market hours 3 (others priced in every hour), cheapest 0
    assert c["hours_tracked"] == 3 and c["hours_priced"] == 1 and c["hours_market"] == 3 and c["hours_cheapest"] == 0
    assert b["hours_market"] == 2 and b["hours_compared"] == 2 and b["hours_cheapest"] == 1
    assert a["price_close"] == 3.0 and a["price_low"] == 1.0 and a["price_avg"] == 2.0
    star = P[("*", "a")]
    assert star["hours_priced"] == 2 and star["hours_tracked"] == 3 and star["hours_compared"] == 2
    G = {r["gpu"]: r for r in grow}[g]
    assert G["hours_market"] == 3 and G["hours_cv"] == 1 and G["hours_spread"] == 2
    assert abs(G["spread_sum"] - (3.0 + 0.5)) < 1e-9
    assert G["close_low"] == 2.0 and G["close_providers"] == 1
    assert G["provider_hours_tracked"] == 9 and G["provider_hours_priced"] == 6
    # Late joiner: hours before its first record are not tracked against it.
    prow2, _ = providers.daily_rows(hourly, {**first, (g, "c"): H0 + timedelta(hours=2)}, hours)
    c2 = {(r["gpu"], r["provider"]): r for r in prow2}[(g, "c")]
    assert c2["hours_tracked"] == 1 and c2["hours_market"] == 1


def test_summarize_thin_and_full():
    def row(day, **kw):
        base = dict(hours_tracked=24, hours_priced=12, hours_available=12, hours_market=24, hours_compared=12,
                    hours_cheapest=6, hours_top3=12, premium_sum=1.2, rank_counts={"1": 6, "2": 6}, price_close=2.0,
                    day=H0.date() + timedelta(days=day))
        base.update(kw)
        return SimpleNamespace(**base)

    thin = providers.summarize([row(0, hours_tracked=5, hours_market=5, hours_compared=5)])
    assert thin["premium_avg"] is None and thin["cheapest_share"] is None and thin["availability"] is None
    assert thin["volatility_daily"] is None and len(thin["reasons"]) >= 4
    closes = [2.0, 2.2, 2.1, 2.3, 2.0, 2.4, 2.2, 2.5, 2.4]
    full = providers.summarize([row(i, price_close=p) for i, p in enumerate(closes)])
    assert abs(full["premium_avg"] - 0.1) < 1e-9 and abs(full["cheapest_share"] - 0.25) < 1e-9
    assert abs(full["availability"] - 0.5) < 1e-9 and full["rank_distribution"] == {"1": 0.5, "2": 0.5}
    import math
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    assert abs(full["volatility_daily"] - statistics.stdev(rets)) < 1e-12
    # A gap in days breaks the chain of daily changes.
    gap = providers.summarize([row(i * 2, price_close=p) for i, p in enumerate(closes)])
    assert gap["volatility_daily"] is None


def test_capability_and_alternatives_pure():
    from analytics import alternatives, capability

    c = capability.capability("NVIDIA H100 80GB SXM5", 2.0, 2.5)
    m = c["metrics"]
    assert abs(m["per_bf16_tflop_hour"]["low"] - 2.0 / 989.5) < 1e-12 and abs(m["per_gb_vram_hour"]["median"] - 2.5 / 80) < 1e-12
    assert "not a workload benchmark" in c["label"]
    a100 = capability.capability("NVIDIA A100 80GB SXM4", 1.0, 1.0)["metrics"]["per_fp8_tflop_hour"]
    assert a100["low"] is None and a100["reason"]
    assert capability.capability("NVIDIA H100 80GB SXM5", None, None)["metrics"]["per_gb_vram_hour"]["reason"]
    h100, h200 = hardware.spec("NVIDIA H100 80GB SXM5"), hardware.spec("NVIDIA H200 141GB SXM5")
    dims = alternatives.shared(h100, h200, 0.0025, 0.003)
    assert "same_architecture" in dims and "training_oriented" in dims and "similar_price_performance" in dims
    assert "similar_vram" not in dims  # 141 vs 80 is outside +/-25%
    b300 = hardware.spec("NVIDIA B300 288GB SXM")
    assert "same_generation" in alternatives.shared(hardware.spec("NVIDIA B200 180GB SXM"), b300, None, None)


def _db():
    import fixtures
    import normalize
    import scratchdb

    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    meta = fixtures.seed(Session, days=40)
    normalize.SessionLocal = Session
    return meta


def test_end_to_end_on_synthetic_history():
    import scratchdb
    from analytics import alternatives, heatmaps, rollups

    try:
        _db()
        rollups.refresh()  # runs the AFTER_REFRESH hook -> refresh_daily
        providers.clear_caches()
        gpu = "NVIDIA H100 80GB SXM5"

        # Current market agrees with market.overview (same rules).
        import market
        ov = {g["gpu"]: g for g in market.overview(1)["gpus"]}
        m = dispersion.market_now(gpu)
        assert abs(m["low"] - ov[gpu]["lowest"]) < 1e-9 and abs(m["median"] - ov[gpu]["median"]) < 1e-9
        assert m["providers"] == ov[gpu]["providers"]
        for row in m["by_provider"]:
            others = [x["price"] for x in m["by_provider"] if x["provider"] != row["provider"]]
            if others:
                assert abs(row["premium_vs_others_median"] - (row["price"] / statistics.median(others) - 1)) < 1e-6

        # Provider premium over 30 days equals a direct recomputation from the rollup.
        now = datetime.now(timezone.utc)
        d0 = datetime.combine((now - timedelta(days=29)).date(), datetime.min.time(), tzinfo=timezone.utc)
        hourly = rollups.provider_hourly(gpu=gpu, t0=d0)
        by_h = {}
        for r in hourly:
            if r["min_price"]:
                by_h.setdefault(r["hour"], {})[r["provider"]] = r["min_price"]
        val = providers.provider_value_all(30)
        for p, by in val.items():
            prem = [px[p] / statistics.median([v for q, v in px.items() if q != p]) - 1
                    for px in by_h.values() if p in px and len(px) > 1]
            v = by.get(gpu)
            if v is None or len(prem) < providers.MIN_HOURS:
                continue
            assert abs(v["premium_avg"] - statistics.fmean(prem)) < 1e-9, (p, v["premium_avg"], statistics.fmean(prem))
            assert 0 <= v["cheapest_share"] <= 1 and 0 <= v["availability"] <= 1
        # Shares of cheapest across providers cover every competitive hour at least once (ties double count).
        daily = [r for r in providers._daily("on_demand", 30) if r.gpu == gpu]
        competitive = sum(1 for px in by_h.values() if len(px) > 1)
        assert sum(r.hours_cheapest for r in daily) >= competitive > 0
        assert any(providers.facts(p, by, 30) for p, by in val.items())

        # The late joiner is never tracked before it started.
        first = rollups.first_hours()
        late = [k for k in first if k[1] == "syn_eps"]
        for (g, p) in late:
            hs = providers.summarize([r for r in providers._daily("on_demand", 40, p) if r.gpu == g])
            assert hs["samples"]["hours_tracked"] <= (now - first[(g, p)]).total_seconds() / 3600 + 1

        hist = dispersion.history(gpu, 30)
        assert hist["points"] and all(p["cv_mean"] is None or p["cv_mean"] >= 0 for p in hist["points"])
        assert dispersion.history(gpu, 2, "1h")["points"]

        for kind in heatmaps.KINDS:
            hm = heatmaps.heatmap(kind)
            assert len(hm["cells"]) == len(hm["rows"]) and all(len(r) == len(hm["cols"]) for r in hm["cells"]), kind
        assert heatmaps.heatmap("gpu-provider-premium")["rows"]
        assert heatmaps.heatmap("gpu-time-change")["rows"]

        alt = alternatives.alternatives(gpu)
        assert all(a["gpu"] != gpu and a["shares"] for a in alt["alternatives"])
        assert any(a["gpu"] == "NVIDIA H200 141GB SXM5" for a in alt["alternatives"])

        life = providers.listing_lifetime()
        assert all(v["median_hours"] is None or v["median_hours"] >= 0 for v in life.values())
        fh = providers.feed_health()
        assert fh["syn_alpha"]["fetches_24h"] >= 1 and fh["syn_alpha"]["failure_rate_24h"] == 0

        _endpoints()
    finally:
        scratchdb.drop(DB)


def _endpoints():
    from fastapi.testclient import TestClient

    import main

    c = TestClient(main.app)  # not entered: no lifespan, so nothing touches the dev database
    for path in ("/v1/gpus", "/v1/gpus/h100-80gb-sxm5", "/v1/markets/h100-80gb-sxm5", "/v1/markets/h100-80gb-sxm5/dispersion",
                 "/v1/markets/h100-80gb-sxm5/dispersion?resolution=1h&days=2",
                 "/v1/spreads", "/v1/providers", "/v1/providers/syn_alpha", "/v1/hardware", "/v1/regions",
                 "/v1/heatmaps/gpu-region-cheapest", "/v1/heatmaps/gpu-time-volatility",
                 "/v1/compare?a=h100-80gb-sxm5&b=h200-141gb-sxm5", "/v1/compare?a=syn_alpha&b=syn_beta"):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:500])
        body = r.json()
        assert "data" in body and "as_of" in body["meta"], path
    assert c.get("/v1/gpus/not-a-gpu").status_code == 404
    assert c.get("/v1/providers/nobody").status_code == 404
    assert c.get("/v1/heatmaps/nonsense").status_code == 404
    assert c.get("/v1/compare?a=h100-80gb-sxm5&b=syn_alpha").status_code == 400
    g = c.get("/v1/gpus").json()
    assert g["meta"]["total"] == len(canonical.all_canonical_names())
    m = c.get("/v1/markets/h100-80gb-sxm5").json()
    assert m["meta"]["methodology"] == "/methodology/dispersion" and m["data"]["score"]
    p = c.get("/v1/providers/syn_alpha").json()["data"]
    assert p["catalog"] and "beats_the_market" in p and p["rank_history"]


if __name__ == "__main__":
    for t in (test_region_group_real_strings, test_hardware_covers_every_canonical_name, test_dispersion_formulas,
              test_daily_rows_definitions, test_summarize_thin_and_full, test_capability_and_alternatives_pure,
              test_end_to_end_on_synthetic_history):
        t(); print(t.__name__, "ok")
