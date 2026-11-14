"""Internal monthly market report (reports/monthly.py, api/reports.py, web/pages/reports.js; methodology/monthly-report).

Hand-computed month statistics on seeded fixtures (live and archive bases), the within-cloud classes, missing data,
the idempotent monthly job, the Discord embed (no network), access control (anonymous, PUBLIC_PAGES, the Data API
host, the viewer), the public-safe summary (no provider names, rounded numbers) and migration 0039 up / down.

Run:  .venv/bin/python tests/test_monthly_report.py
"""

import base64
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402  (isolation: blanks credentials, blocks real network)
import fixtures  # noqa: E402
import normalize  # noqa: E402
from config import settings  # noqa: E402
from sqlalchemy import insert, text  # noqa: E402
from store.analytics import IndexLevel, MarketHourly  # noqa: E402
from store.archive import ArchivedPrice  # noqa: E402
from store.capacity import CapacityChange  # noqa: E402
from store.events import MarketEvent  # noqa: E402
from reports import monthly  # noqa: E402

DB = "og_test_monthly_report"
UTC = timezone.utc
H100, H200, A100, L40S = ("NVIDIA H100 80GB SXM5", "NVIDIA H200 141GB SXM5", "NVIDIA A100 80GB SXM4", "NVIDIA L40S 48GB")
OCT0, OCT1 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


def hrs(t0, n):
    return [t0 + timedelta(hours=i) for i in range(n)]


def mh(gpu, prov, hours, price, region="us-east"):
    return [{"segment": "on_demand", "gpu": gpu, "provider": prov, "region": region, "hour": h, "country": "US",
             "min_price": Decimal(str(price)), "max_price": Decimal(str(price)), "avg_price": Decimal(str(price)),
             "live_listings": 1, "priced_listings": 1, "available_listings": 1, "unknown_listings": 0,
             "sold_out_listings": 0, "capacity_gpus": None} for h in hours]


START = hrs(OCT0, 6)                                   # Oct 1 00:00-05:00
END = hrs(OCT1 - timedelta(hours=6), 6)                # Oct 31 18:00-23:00
MID = hrs(datetime(2026, 10, 15, tzinfo=UTC), 3)       # never an anchor


def live_pair(gpu, prov, p0, p1, mid=None):
    rows = mh(gpu, prov, START, p0) + mh(gpu, prov, END, p1)
    return rows + (mh(gpu, prov, MID, mid) if mid is not None else [])


def seed_october(S):
    """Hand-built October 2026 (live). H100 moves: -10%, 0, 0, 0, +1%, +10% (+ a marketplace and a half-priced
    cloud left out); H200 +5% x3 (proportional); syn_b / syn_d / syn_e reprice two GPUs each."""
    rows = []
    rows += live_pair(H100, "syn_a", 2.00, 2.00, mid=1.00)
    # syn_b: the first 24 priced hours at 2.50, then 6 more at 9.99: the anchor is the median of the first 24 only
    rows += mh(H100, "syn_b", hrs(OCT0, 24), 2.50) + mh(H100, "syn_b", hrs(OCT0 + timedelta(hours=24), 6), 9.99)
    rows += mh(H100, "syn_b", END, 2.75)
    rows += live_pair(H100, "syn_c", 3.00, 3.00)
    rows += live_pair(H100, "syn_d", 2.20, 1.98)
    rows += live_pair(H100, "syn_e", 2.00, 2.02)       # exactly +1%: a move
    rows += live_pair(H100, "syn_f", 1.90, 1.90)
    rows += live_pair(H100, "vast", 1.50, 3.00)        # marketplace: never a cloud count
    rows += mh(H100, "syn_g", START, 2.10)             # priced at the start only: left out, with the reason
    # syn_a also in a second region: one vote per cloud (its lowest)
    rows += mh(H100, "syn_a", START + END, 2.60, region="eu-west")
    for prov, p0 in (("syn_a", 3.00), ("syn_b", 3.20), ("syn_c", 3.40)):
        rows += live_pair(H200, prov, p0, round(p0 * 1.05, 6))
    rows += live_pair(A100, "syn_d", 1.00, 1.03)
    rows += live_pair(L40S, "syn_e", 1.00, 1.02)
    ix = []
    for iid, pts in {"gpu-compute": [(OCT0, 100.0, 1), (datetime(2026, 10, 15, tzinfo=UTC), 101.0, 1), (END[-1], 103.0, 1)],
                     "h100-80gb-sxm5": [(OCT0, 2.40, 1), (END[-1], 2.46, 1)],
                     "h200-141gb-sxm5": [(OCT0, 3.20, 1), (END[-1], 3.36, 2)]}.items():
        for h, lv, seg in pts:
            ix.append({"index_id": iid, "hour": h, "published": True, "level": lv, "raw_level": lv, "constituents": 4,
                       "segment_no": seg, "methodology_version": "1.0", "computed_at": h})
    ev = [
        ("price_move", "major", H100, "syn_b", datetime(2026, 10, 20, tzinfo=UTC), 0.10, "syn_b raised H100 by 10%"),
        ("sold_out", "notable", H100, "syn_a", datetime(2026, 10, 5, tzinfo=UTC), None, "syn_a H100 sold out"),
        ("new_30d_low", "info", H200, None, datetime(2026, 10, 7, tzinfo=UTC), -0.02, "H200 new 30-day low"),
        ("provider_added_gpu", "info", H100, "syn_f", datetime(2026, 10, 3, tzinfo=UTC), None, "syn_f added H100"),
        ("coverage_started", "info", H100, "syn_h", datetime(2026, 10, 2, tzinfo=UTC), None, "tracking syn_h"),
        ("price_move", "major", H100, "syn_c", datetime(2026, 9, 30, 23, tzinfo=UTC), 0.2, "September: not this month"),
    ]
    events = [{"occurred_at": t, "detected_at": t, "type": ty, "segment": "on_demand", "gpu": g, "provider": p,
               "severity": sev, "title": title, "detail": {}, "pct": pct, "dedupe_key": f"t{i}"}
              for i, (ty, sev, g, p, t, pct, title) in enumerate(ev)]
    cap = []
    for prov, sku, gpu, t, stock, vis in (
            ("syn_a", "h100-1", H100, datetime(2026, 9, 20, tzinfo=UTC), True, "public"),
            ("syn_a", "h100-1", H100, datetime(2026, 10, 5, tzinfo=UTC), False, "public"),   # sold out
            ("syn_a", "h100-1", H100, datetime(2026, 10, 9, tzinfo=UTC), True, "public"),    # back in stock
            ("syn_b", "h100-1", H100, datetime(2026, 10, 6, tzinfo=UTC), False, "public"),   # first sighting: no transition
            ("syn_c", "h200-1", H200, datetime(2026, 9, 1, tzinfo=UTC), False, "public"),
            ("syn_c", "h200-1", H200, datetime(2026, 10, 10, tzinfo=UTC), True, "public"),   # back in stock
            ("syn_i", "h100-x", H100, datetime(2026, 9, 1, tzinfo=UTC), True, "internal"),
            ("syn_i", "h100-x", H100, datetime(2026, 10, 11, tzinfo=UTC), False, "internal")):  # internal: ignored
        cap.append({"provider": prov, "sku": sku, "region_key": "us", "canonical_gpu_name": gpu, "observed_at": t,
                    "in_stock": stock, "raw_value": str(stock), "visibility": vis})
    with S.begin() as s:
        s.execute(insert(MarketHourly), rows)
        s.execute(insert(IndexLevel), ix)
        s.execute(insert(MarketEvent), events)
        s.execute(insert(CapacityChange), cap)


def arch(prov, gpu, t, price, *, valid_to=None, basis="capture", vis="public", n=[0]):
    n[0] += 1
    return {"source": "wayback", "source_url": f"https://web.archive.org/x/{n[0]}", "source_sha256": "0" * 64,
            "parser": "test", "provider": prov, "listing_id": f"{prov}:{gpu}", "raw_gpu_name": gpu, "gpu": gpu,
            "gpu_count": 1, "price_per_gpu_hour": Decimal(str(price)), "market_type": "on_demand", "observed_at": t,
            "valid_to": valid_to, "imported_at": t, "visibility": vis, "basis": basis}


def seed_march_2024(S):
    """An archive month: syn_x +10%, syn_z -10% (an AWS-style price-list version still valid at the start), syn_w
    flat; syn_y's last document is too old to say what held on March 1; syn_v is internal; syn_m started live
    recording mid-month (its archive is not used, and it has no live start anchor)."""
    d = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
    rows = [arch("syn_x", H100, d(2024, 2, 10), 2.00), arch("syn_x", H100, d(2024, 3, 20), 2.20),
            arch("syn_y", H100, d(2023, 12, 1), 3.00), arch("syn_y", H100, d(2024, 3, 15), 3.30),
            arch("syn_z", H100, d(2023, 11, 1), 4.00, valid_to=d(2024, 3, 1), basis="price_list_version"),
            arch("syn_z", H100, d(2024, 3, 1), 3.60, valid_to=d(2024, 4, 1), basis="price_list_version"),
            arch("syn_w", H100, d(2024, 2, 20), 1.50), arch("syn_w", H100, d(2024, 3, 10), 1.50),
            arch("syn_v", H100, d(2024, 2, 20), 1.00, vis="internal"), arch("syn_v", H100, d(2024, 3, 10), 9.00, vis="internal"),
            arch("syn_m", H100, d(2024, 2, 15), 2.00), arch("syn_m", H100, d(2024, 3, 5), 2.50)]
    with S.begin() as s:
        s.execute(insert(ArchivedPrice), rows)
        s.execute(insert(MarketHourly), mh(H100, "syn_m", hrs(d(2024, 3, 30), 48), 2.50))


S = None


def setup():
    global S
    if S is None:
        S = fixtures.session(scratchdb.create(DB))
        seed_october(S)
        seed_march_2024(S)
    normalize.SessionLocal = S
    return S


def feat(rep, gpu):
    return next(f for f in rep["featured"] if f["gpu"] == gpu)


# ------------------------------------------------------------------------------------------------ pure statistics

def test_cloud_stats_hand_computed():
    mv = lambda p, c: {"provider": p, "from": 1.0, "to": 1 + c, "change": c, "basis": "live"}  # noqa: E731
    st = monthly.cloud_stats([mv("a", 0.0), mv("b", 0.10), mv("c", 0.0), mv("d", -0.10), mv("e", 0.01), mv("f", 0.0)])
    assert (st["clouds"], st["up"], st["flat"], st["down"], st["moved"]) == (6, 2, 3, 1, 3), st
    assert st["median_move"] == 0.0 and st["middle_half"] == [0.0, 0.0075] and st["range"] == [-0.1, 0.1], st
    # |dev| from the median 0: [0, .1, 0, .1, .01, 0] -> median (0 + .01) / 2 = 0.5 pp; iqr 0.75 pp; movers -10..+10
    assert st["dispersion"] == {"mad_pp": 0.5, "iqr_pp": 0.75, "movers_spread_pp": 20.0}, st["dispersion"]
    assert st["pattern"] == "broad_uneven" and st["proportional"] is False, "3 of 6 moved: half -> broad"
    assert [m["provider"] for m in st["biggest_movers"]] == ["b", "d", "e"], "by |change|, ties by name"
    prop = monthly.cloud_stats([mv("a", 0.05), mv("b", 0.06), mv("c", 0.04)])
    assert prop["pattern"] == "proportional" and prop["proportional"] is True and prop["median_move"] == 0.05
    few = monthly.cloud_stats([mv(x, 0.0) for x in "abc"] + [mv("d", 0.3)])
    assert few["pattern"] == "few_movers" and few["median_move"] == 0.0
    none = monthly.cloud_stats([mv(x, 0.004) for x in "abc"])
    assert none["pattern"] == "no_moves" and none["proportional"] is None and none["flat"] == 3
    two = monthly.cloud_stats([mv("a", 0.1), mv("b", -0.2)])
    assert two["median_move"] is None and "at least 3" in two["reasons"]["median_move"] and two["up"] == 1
    assert two["dispersion"]["movers_spread_pp"] == 30.0
    assert monthly.direction(2.02 / 2.00 - 1) == "up" and monthly.direction(-0.0099) == "flat"


def test_provider_classes():
    pc = monthly.provider_class_of
    assert pc([0.05, 0.06, 0.0]) == "uniform"              # 1 pp apart; the flat GPU is not a repricing
    assert pc([0.05, 0.10]) == "same_direction"
    assert pc([-0.05, 0.03]) == "mixed"
    assert pc([0.05, 0.0, 0.005]) is None, "one repriced GPU is not a multi-GPU repricing"
    assert pc([0.10, 0.12]) == "uniform" and pc([0.10, 0.1201]) == "same_direction", "2 pp band, inclusive"


def test_months():
    assert monthly.parse_month("2026-10") == (OCT0, OCT1)
    assert monthly.parse_month("2026-12")[1] == datetime(2027, 1, 1, tzinfo=UTC)
    for bad in ("2026-13", "2026-1", "26-10", "2026-10-01", "", "x"):
        try:
            monthly.parse_month(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert monthly.previous_month(datetime(2026, 1, 3, tzinfo=UTC)) == "2025-12"
    assert monthly.months_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]


# ------------------------------------------------------------------------------------------------ generate (DB)

def test_live_month_hand_computed():
    setup()
    rep = monthly.generate("2026-10", now=NOW)
    h = feat(rep, H100)
    assert (h["clouds"], h["up"], h["flat"], h["down"]) == (6, 2, 3, 1), h
    assert h["basis"] == {"live": 6, "archive": 0}
    assert h["median_move"] == 0.0 and h["middle_half"] == [0.0, 0.0075] and h["range"] == [-0.1, 0.1]
    assert h["dispersion"]["mad_pp"] == 0.5 and h["pattern"] == "broad_uneven"
    big = h["biggest_movers"]
    assert [(b["provider"], b["from"], b["to"], b["change"]) for b in big[:2]] == [
        ("syn_b", 2.5, 2.75, 0.1), ("syn_d", 2.2, 1.98, -0.1)], big
    assert "partial" in h["reasons"] and "1 cloud" in h["reasons"]["partial"], "syn_g: start only, said so"
    # month end: syn_f 1.90 is cheapest (vast's 3.00 is a marketplace, syn_a's eu-west 2.60 loses to its 2.00);
    # end prices 2.00 2.75 3.00 1.98 2.02 1.90 -> median (2.00 + 2.02) / 2 = 2.01
    assert h["cheapest_end"]["provider"] == "syn_f" and h["cheapest_end"]["price"] == 1.9
    assert h["median_end"] == 2.01 and h["cheapest_below_median"] == round(1 - 1.9 / 2.01, 6)
    assert h["index"]["start"] == 2.4 and h["index"]["end"] == 2.46 and h["index"]["change"] == round(2.46 / 2.4 - 1, 6)
    h2 = feat(rep, H200)
    assert h2["pattern"] == "proportional" and h2["median_move"] == 0.05 and h2["up"] == 3
    assert h2["index"]["change"] is None and "rebased" in h2["index"]["reason"]
    b2 = feat(rep, "NVIDIA B200 180GB SXM")
    assert b2["clouds"] == 0 and b2["index"]["start"] is None and "no published hour" in b2["index"]["reason"]
    assert b2["median_move"] is None and b2["reasons"]["clouds"]
    a1 = feat(rep, A100)
    assert a1["clouds"] == 1 and a1["median_move"] is None and a1["up"] == 1 and "at least 3" in a1["reasons"]["median_move"]
    ci = rep["headline"]["compute_index"]
    assert (ci["start"], ci["end"], ci["change"]) == (100.0, 103.0, 0.03), ci
    # c. within one cloud: syn_b (+10, +5) same direction, syn_d (-10, +3) mixed, syn_e (+1, +2) uniform
    wp = rep["within_provider"]
    assert wp["providers_repricing_2plus"] == 3 and wp["counts"] == {"uniform": 1, "same_direction": 1, "mixed": 1}
    assert {p["provider"]: p["class"] for p in wp["providers"]} == {"syn_b": "same_direction", "syn_d": "mixed", "syn_e": "uniform"}
    assert wp["shares"]["uniform"] == round(1 / 3, 4)
    # d. generations: only Hopper has a GPU with >= 3 clouds (H100 median 0, H200 median 5%)
    assert [g["generation"] for g in rep["generations"]] == ["Hopper"]
    hop = rep["generations"][0]
    assert hop["median_gpu_move"] == 0.025 and hop["pooled_median_move"] == 0.01 and hop["clouds"] == 9
    # e. supply: one sold out + one back for H100 (syn_b's first sighting and the internal row do not count)
    sup = {g["gpu"]: g for g in rep["supply"]["gpus"]}
    assert (sup[H100]["sold_out"], sup[H100]["back_in_stock"], sup[H200]["back_in_stock"]) == (1, 1, 1), sup[H100]
    assert sup[H100]["providers_listed"] == ["syn_f"] and rep["supply"]["reason"] is None
    # f. events: September and coverage_started out; notable = major + notable, major first
    ev = rep["events"]
    assert ev["total"] == 4 and ev["by_severity"] == {"major": 1, "notable": 1, "info": 2}, ev
    assert [e["type"] for e in ev["notable"]] == ["price_move", "sold_out"]
    # g. coverage
    cov = rep["coverage"]
    assert cov["basis"] == "live" and cov["pairs_live"] == 11 and cov["pairs_archive"] == 0
    assert cov["providers_excluded_class"] == ["vast"] and "vast" not in cov["providers"]
    assert cov["data_kinds"] == ["derived", "observed"] and cov["thresholds"]["move"] == 0.01
    # deterministic: same stored data, same report
    assert monthly.generate("2026-10", now=NOW) == rep
    assert monthly.generate("2026-10", now=NOW + timedelta(days=40)) == rep, "no clock inside the report"


def test_threshold_is_configurable():
    setup()
    rep = monthly.generate("2026-10", now=NOW, threshold=0.05)
    h = feat(rep, H100)
    assert (h["up"], h["flat"], h["down"]) == (1, 4, 1), "syn_e's +1% is flat at a 5% threshold"
    assert rep["coverage"]["thresholds"]["move"] == 0.05


def test_archive_month():
    setup()
    rep = monthly.generate("2024-03", now=NOW)
    h = feat(rep, H100)
    assert h["basis"] == {"live": 0, "archive": 3}, h["basis"]
    assert (h["up"], h["flat"], h["down"]) == (1, 1, 1) and h["median_move"] == 0.0
    assert sorted((b["provider"], b["change"]) for b in h["biggest_movers"]) == [("syn_x", 0.1), ("syn_z", -0.1)]
    assert all(b["basis"] == "archive" for b in h["biggest_movers"])
    assert rep["coverage"]["basis"] == "archive" and "archived" in rep["coverage"]["data_kinds"]
    assert "syn_v" not in rep["coverage"]["providers"], "internal archived rows never count"
    assert "syn_m" not in rep["coverage"]["providers"] and "syn_y" not in rep["coverage"]["providers"]
    assert "partial" in h["reasons"], "syn_m began live recording mid-month: left out, said so"
    assert rep["headline"]["compute_index"]["change"] is None and rep["headline"]["compute_index"]["reason"]


def test_missing_data_stays_missing():
    setup()
    rep = monthly.generate("2025-06", now=NOW)
    assert rep["coverage"]["basis"] == "none" and rep["coverage"]["reason"]
    for f in rep["featured"]:
        assert f["clouds"] == 0 and f["median_move"] is None and f["cheapest_end"] is None and f["reasons"]["clouds"]
    assert rep["within_provider"]["reason"] and rep["generations"] == []
    assert rep["events"]["total"] == 0 and rep["events"]["reason"]
    assert "not enough for a price comparison" in rep["public_summary"]
    try:
        monthly.generate("2026-11", now=NOW)
        raise AssertionError("an unfinished month must be refused")
    except ValueError as e:
        assert "not ended" in str(e)


# ------------------------------------------------------------------------------------------------ public summary

def _provider_names():
    import provider_meta
    from providers import PROVIDERS

    names = set(PROVIDERS) | set(provider_meta.PROVIDER_META)
    out = {n.lower() for n in names} | {provider_meta.meta(n).display_name.lower() for n in names}
    return {n for n in out if len(n) >= 3} | {f"syn_{c}" for c in "abcdefghimvwxyz"}


def check_public_summary(summary: str, has_data: bool = True):
    low = summary.lower()
    for name in _provider_names():
        assert not re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", low), f"provider name {name!r} in summary"
    # numbers: whole numbers (counts, %, index points, years), or dollars rounded to 10 cents ($2.40)
    for tok in re.findall(r"\$?\d[\d.,]*\d|\$?\d", summary):
        tok = tok.rstrip(".,")
        if tok.startswith("$"):
            assert re.fullmatch(r"\$\d+\.\d0", tok), f"unrounded dollar figure {tok!r}"
        else:
            assert re.fullmatch(r"\d+", tok), f"unrounded number {tok!r} in summary"
    bullets = [line for line in summary.splitlines() if line.startswith("- ")]
    # 4-8 headline bullets; a month without data says so in one line instead of padding it
    assert (4 <= len(bullets) <= 8) if has_data else len(bullets) == 1, bullets
    assert summary.splitlines()[-1].startswith("Methodology: ")


def test_public_summary_safe():
    setup()
    for month in ("2026-10", "2024-03", "2025-06"):
        rep = monthly.generate(month, now=NOW)
        check_public_summary(rep["public_summary"], has_data=rep["coverage"]["pairs"] > 0)
    rep = monthly.generate("2026-10", now=NOW)
    s = rep["public_summary"]
    assert "GPU Compute Index ended October 2026 at about 103, up 3% on the month" in s, s
    assert "H100 80GB SXM5 index ended the month at about $2.50 per GPU-hour, up 2%" in s, s
    assert "2 of 6 clouds raised prices, 1 cut them and 3 left them unchanged" in s, s
    assert "between -10% and +10%" in s and "Of the 3 clouds that repriced two or more GPUs, 1 moved" in s, s
    assert "sold out 1 time at individual clouds and came back in stock 2 times" in s, s


# ------------------------------------------------------------------------------------------------ storage + job

def _wipe():
    with S.begin() as s:
        s.execute(text("DELETE FROM monthly_reports"))
        s.execute(text("DELETE FROM ops_alert_state"))


def test_store_once_and_force():
    setup()
    _wipe()
    t1, t2 = NOW, NOW + timedelta(days=1)
    rep = monthly.generate("2026-10", now=NOW)
    assert monthly.store("2026-10", rep, now=t1) == "created"
    other = {**rep, "label": "changed"}
    assert monthly.store("2026-10", other, now=t2) == "exists"
    row = monthly.get("2026-10")
    assert row["report"]["label"] == "October 2026" and row["regenerated_at"] is None
    assert monthly.store("2026-10", other, force=True, now=t2) == "regenerated"
    row = monthly.get("2026-10")
    assert row["report"]["label"] == "changed" and row["generated_at"] == t1.isoformat(), "generated_at kept"
    assert row["regenerated_at"] == t2.isoformat()
    assert [r["month"] for r in monthly.list_reports()] == ["2026-10"]


class FakeSend:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, channel, payload):
        self.calls.append((channel, payload))
        return self.ok


def test_job_idempotent_previous_month_after_the_first():
    from alerts import channels

    setup()
    _wipe()
    saved = (settings.market_webhook_url, settings.ops_alert_webhook_secret, settings.monthly_report_enabled, channels.send)
    try:
        settings.market_webhook_url, settings.ops_alert_webhook_secret = "https://discord.com/api/webhooks/1/x", "sec"
        fake = FakeSend(ok=False)
        channels.send = fake
        early = monthly.run_job(datetime(2026, 11, 1, 3, tzinfo=UTC))
        assert "waiting" in early["skipped"] and monthly.get("2026-10") is None, early
        settings.monthly_report_enabled = False
        assert "monthly_report_enabled" in monthly.run_job(datetime(2026, 11, 2, tzinfo=UTC))["skipped"]
        settings.monthly_report_enabled = True
        t = datetime(2026, 11, 1, 7, tzinfo=UTC)
        first = monthly.run_job(t)
        assert first == {"month": "2026-10", "result": "created", "post": "failed"}, first
        row = monthly.get("2026-10")
        assert row["generated_at"] == t.isoformat() and row["posted_at"] is None
        fake.ok = True
        second = monthly.run_job(datetime(2026, 11, 2, 7, tzinfo=UTC))
        assert second == {"month": "2026-10", "result": "exists", "post": "posted"}, second
        third = monthly.run_job(datetime(2026, 11, 20, tzinfo=UTC))
        assert third == {"month": "2026-10", "result": "exists", "post": "already"}, third
        assert len(fake.calls) == 2 and all(c == "market" for c, _ in fake.calls)
        assert monthly.get("2026-10")["generated_at"] == t.isoformat(), "generated once"
        # the next month's run generates November, never October again
        dec = monthly.run_job(datetime(2026, 12, 3, tzinfo=UTC))
        assert dec["month"] == "2026-11" and dec["result"] == "created"
        # without the channel configured nothing is posted
        settings.market_webhook_url = None
        assert monthly.post("2026-11") == "not configured"
        import jobs
        assert "monthly_report" in jobs.JOBS and jobs.JOBS["monthly_report"].every_seconds == 86400
    finally:
        settings.market_webhook_url, settings.ops_alert_webhook_secret, settings.monthly_report_enabled, channels.send = saved
        _wipe()


def test_discord_embed():
    from alerts import channels

    setup()
    saved = settings.public_base_url
    settings.public_base_url = "https://terminal.example"
    try:
        msg = monthly.discord_message(monthly.generate("2026-10", now=NOW))
    finally:
        settings.public_base_url = saved
    assert msg["allowed_mentions"] == {"parse": []} and msg["username"] == "OpenGrid"
    e = msg["embeds"][0]
    assert e["title"] == "📊 Monthly market report: October 2026" and e["color"] == channels.GREEN
    assert e["url"] == "https://terminal.example/reports/2026-10" and e["description"] == "GPU Compute Index 103.0 (+3%)"
    f = {x["name"]: x["value"] for x in e["fields"]}
    assert f["H100 80GB SXM5"] == "2↑ 3= 1↓ of 6 · median 0% · biggest syn_b +10%", f
    assert f["Within-cloud repricing"] == "3 clouds: 1 uniform · 1 same direction · 1 mixed"
    assert len(e["fields"]) <= 25 and all(len(x["value"]) <= 1024 for x in e["fields"])
    assert "Monthly market report" in channels.plain_text(msg), "Slack fallback text"


# ------------------------------------------------------------------------------------------------ access control

def _basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_access_control():
    from fastapi.testclient import TestClient

    import main
    from accounts import security

    setup()
    _wipe()
    monthly.store("2026-10", monthly.generate("2026-10", now=NOW), now=NOW)
    client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
    saved = (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
             settings.data_api_host, settings.environment)
    try:
        security.reset()
        settings.app_password, settings.environment = "pw", "development"
        settings.viewer_user, settings.viewer_password = "vc", "look-only"
        op, viewer = _basic("opengrid", "pw"), _basic("vc", "look-only")
        for public in (True, False):
            settings.public_pages = public
            for path in ("/v1/reports/monthly", "/v1/reports/monthly/2026-10"):
                assert client.get(path).status_code == 401, (public, path)
            assert client.post("/v1/reports/monthly/2026-10/regenerate").status_code == 401
            page = client.get("/reports/2026-10", headers={"Accept": "text/html"}, follow_redirects=False)
            if public:   # the generic shell only: no report data, never indexed
                assert page.status_code == 200 and 'name="robots" content="noindex"' in page.text
                assert "syn_" not in page.text and "Copy public summary" not in page.text
            else:
                assert page.status_code == 303 and page.headers["location"].startswith("/login"), page.status_code
        settings.public_pages = True
        assert "/reports" not in client.get("/sitemap.xml").text
        from api import pages
        assert "reports" in pages.PRIVATE_V1 and "reports" not in pages.PUBLIC_V1
        # signed in: the operator and the read-only viewer read it; only an admin regenerates
        r = client.get("/v1/reports/monthly", headers=op)
        assert r.status_code == 200 and [x["month"] for x in r.json()["data"]] == ["2026-10"], r.text
        r = client.get("/v1/reports/monthly/2026-10", headers=viewer)
        assert r.status_code == 200 and r.json()["data"]["report"]["featured"][0]["clouds"] == 6
        assert client.get("/v1/reports/monthly/2026-09", headers=op).status_code == 404
        assert client.get("/v1/reports/monthly/2026-9", headers=op).status_code == 422
        assert client.post("/v1/reports/monthly/2026-10/regenerate", headers=viewer).status_code == 403
        r = client.post("/v1/reports/monthly/2026-10/regenerate", headers=op)
        assert r.status_code == 200 and r.json()["data"]["result"] == "regenerated", r.text
        assert r.json()["data"]["generated_at"] == NOW.isoformat()
        assert client.post("/v1/reports/monthly/2099-01/regenerate", headers=op).status_code == 409, "unfinished month"
        # the Data API host never serves it, key or not
        settings.data_api_host = "api.opengrid.test"
        r = client.get("/v1/reports/monthly", headers={"Host": "api.opengrid.test", "Authorization": "Bearer opg_live_x"})
        assert r.status_code == 404, r.status_code
        from api import contract
        assert not contract.is_data_path("/v1/reports/monthly")
    finally:
        (settings.app_password, settings.public_pages, settings.viewer_user, settings.viewer_password,
         settings.data_api_host, settings.environment) = saved
        security.reset()
        _wipe()


def test_page_registered():
    from api import pages

    assert ("/reports", "Monthly market report") == pages.PAGES[[p[0] for p in pages.PAGES].index("/reports")][:2]
    assert all(not p[3] for p in pages.PAGES if p[0].startswith("/reports"))
    root = Path(__file__).resolve().parent.parent
    js = (root / "web" / "pages" / "reports.js").read_text(encoding="utf-8")
    assert 'OG.page("/reports"' in js and 'OG.page("/reports/:month"' in js and "Copy public summary" in js
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<a href="/reports" data-nav="reports" data-access="account"', html)
    assert (root / "methodology" / "monthly-report.md").exists()


# ------------------------------------------------------------------------------------------------ migration

def test_migration_0039():
    from alembic import command
    from sqlalchemy import create_engine

    import db as dbmod
    name = DB + "_mig"
    url = scratchdb.create(name)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        command.upgrade(cfg, "0039_monthly_reports")
        eng = create_engine(url)
        q = "SELECT column_name FROM information_schema.columns WHERE table_name = 'monthly_reports'"
        with eng.connect() as c:
            assert {r[0] for r in c.execute(text(q))} == {"month", "generated_at", "regenerated_at", "version",
                                                          "report", "public_summary", "posted_at"}
        assert dbmod.head_revision() == "0039_monthly_reports"
        command.downgrade(cfg, "0038_measured_performance")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('public.monthly_reports')")).scalar() is None
            assert c.execute(text("SELECT to_regclass('public.performance_observations')")).scalar() is not None
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(name)


TESTS = [test_cloud_stats_hand_computed, test_provider_classes, test_months, test_live_month_hand_computed,
         test_threshold_is_configurable, test_archive_month, test_missing_data_stays_missing, test_public_summary_safe,
         test_store_once_and_force, test_job_idempotent_previous_month_after_the_first, test_discord_embed,
         test_access_control, test_page_registered, test_migration_0039]


def main():
    failed = 0
    try:
        for fn in TESTS:
            try:
                fn()
                print(f"ok   {fn.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                import traceback
                print(f"FAIL {fn.__name__}")
                traceback.print_exc()
    finally:
        scratchdb.drop(DB)
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
