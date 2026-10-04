"""Market history: the pure builders, then the SQL against a scratch database.

Run:  .venv/bin/python tests/test_market.py
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market
from market import aggregate, change_over, coherent_start, grid, price_at, provider_series

T0 = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
H = lambda n: T0 + timedelta(hours=n)  # noqa: E731
STALE = timedelta(minutes=40)


def test_price_at():
    ev = [(H(1), D("2.00"), True), (H(3), D("2.50"), True), (H(5), D("2.50"), False), (H(6), D("3.00"), True)]
    ts = [e[0] for e in ev]
    seen = H(10)
    f = lambda t: price_at(ts, ev, seen, STALE, t)  # noqa: E731
    assert f(H(0)) is None, "before the first observation we know nothing"
    assert f(H(1)) == 2.0 and f(H(2)) == 2.0, "a price holds until the next observation"
    assert f(H(3)) == 2.5 and f(H(4.9)) == 2.5
    assert f(H(5)) is None and f(H(5.5)) is None, "sold out is not a price"
    assert f(H(6)) == 3.0, "back in stock at the new price"
    assert f(H(10)) == 3.0 and f(H(10.5)) == 3.0
    assert f(H(11)) is None, "past last-seen plus a few polls the listing is gone"
    assert price_at([H(1)], [(H(1), None, True)], seen, STALE, H(2)) is None, "no price"
    assert price_at([H(1)], [(H(1), D("0"), True)], seen, STALE, H(2)) is None, "zero is not a price"
    assert price_at([H(1)], [(H(1), D("2"), None)], seen, STALE, H(2)) == 2.0, "unknown stock still counts"


def test_series_and_aggregate():
    # Provider a sells the GPU at two listings; b joins at hour 4.
    listings = [
        {"provider": "a", "listing_id": "a1", "gpu": "G", "last_seen": H(8)},
        {"provider": "a", "listing_id": "a2", "gpu": "G", "last_seen": H(8)},
        {"provider": "b", "listing_id": "b1", "gpu": "G", "last_seen": H(8)},
    ]
    events = {
        ("a", "a1"): [(H(0), D("4.00"), True)],
        ("a", "a2"): [(H(0), D("5.00"), True), (H(2), D("3.00"), True)],     # drops below a1 at hour 2
        ("b", "b1"): [(H(4), D("2.00"), True)],
    }
    times, series, first = provider_series(listings, events, H(0), H(8), 8)
    assert [t.hour for t in times] == list(range(9))
    assert series["G"]["a"][:4] == [4.0, 4.0, 3.0, 3.0], "a's price is the lowest of its live listings"
    assert series["G"]["b"][:4] == [None] * 4 and series["G"]["b"][4] == 2.0
    assert first[("G", "b")] == H(4) and first[("G", "a")] == H(0)

    by = series["G"]
    start = coherent_start(by, first, "G", times)
    assert start == 4, "market lines begin when b was first recorded, so b joining is not a price drop"
    lo, mid, hi = aggregate(by, start)
    assert lo[:4] == [None] * 4 and lo[4] == 2.0 and hi[4] == 3.0 and mid[4] == 2.5
    pct, since = change_over(lo, times, start)
    assert pct == 0.0 and since == times[4]
    # What a naive version would claim: lowest fell from 3.00 to 2.00 when b merely joined
    naive = aggregate(by, 0)[0]
    assert naive[3] == 3.0 and naive[4] == 2.0, "the artifact the coherent start avoids"

    # A provider that has stopped selling at the end does not hold the start back
    gone = {"provider": "c", "listing_id": "c1", "gpu": "G", "last_seen": H(5)}
    times2, s2, first2 = provider_series(listings + [gone], {**events, ("c", "c1"): [(H(6), D("1.00"), True)]}, H(0), H(8), 8)
    assert coherent_start(s2["G"], first2, "G", times2) == 4, "c is not live at the end, so it is ignored"


def test_change_over():
    times = grid(H(0), H(10), 10)
    assert change_over([None] * 11, times, 0) == (None, None)
    assert change_over([2.0] * 10 + [None], times, 0) == (None, None), "nothing selling now"
    lo = [4.0] + [None] * 9 + [3.0]
    pct, since = change_over(lo, times, 0)
    assert abs(pct - (-0.25)) < 1e-9 and since == times[0]
    # a span under 30 minutes is not a trend
    short = grid(H(0), H(0) + timedelta(minutes=20), 4)
    assert change_over([2.0, 2.0, 2.0, 2.0, 3.0], short, 0) == (None, None)
    # starting past a leading gap uses the first real price
    lo2 = [None, None, 5.0, 5.0, 4.0]
    assert change_over(lo2, grid(H(0), H(4), 4), 0)[0] == (4.0 - 5.0) / 5.0


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def test_sql():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import normalize
    from tables import Base, ComputeListingRow, ListingObservation

    run(["dropdb", "--if-exists", "opengrid_test"])
    run(["createdb", "opengrid_test"])
    engine = create_engine("postgresql+psycopg://localhost:5432/opengrid_test")
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        ago = lambda h: now - timedelta(hours=h)  # noqa: E731
        GPU = "NVIDIA TEST 80GB"

        def listing(s, prov, lid, price, gpu=GPU, market_type="on_demand", interruptible=False, tier=None, available=True):
            s.add(ComputeListingRow(
                provider=prov, listing_id=lid, sku=lid, raw_gpu_name="x", canonical_gpu_name=gpu, gpu_count=1,
                price_per_gpu_hour=D(price), currency="USD", market_type=market_type, interruptible=interruptible,
                provider_tier=tier, available=available, observed_at=now, first_seen_at=ago(50),
            ))
            s.flush()

        def obs(s, prov, lid, hours_ago, price, available=True):
            s.add(ListingObservation(provider=prov, listing_id=lid, observed_at=ago(hours_ago), price_per_gpu_hour=D(price), available=available))

        with Session.begin() as s:
            # a: recorded for 30h, price fell 4.00 -> 3.00 at 10h ago
            listing(s, "a", "a1", "3.00"); obs(s, "a", "a1", 30, "4.00"); obs(s, "a", "a1", 10, "3.00")
            # b: recorded for 30h too, steady 5.00
            listing(s, "b", "b1", "5.00"); obs(s, "b", "b1", 30, "5.00")
            # c: only started being recorded 2h ago, cheapest of all
            listing(s, "c", "c1", "1.50"); obs(s, "c", "c1", 2, "1.50")
            # d: sold out now: must not count
            listing(s, "d", "d1", "0.50", available=False); obs(s, "d", "d1", 30, "0.50"); obs(s, "d", "d1", 1, "0.50", available=False)
            # Excluded by the rules: spot, interruptible, vast-style cheapest, from-price floor, no canonical name
            listing(s, "e", "spot", "0.10", market_type="spot"); obs(s, "e", "spot", 30, "0.10")
            listing(s, "e", "int", "0.10", interruptible=True); obs(s, "e", "int", 30, "0.10")
            listing(s, "e", "cheap", "0.10", tier="cheapest"); obs(s, "e", "cheap", 30, "0.10")
            listing(s, "e", "from", "0.10", tier="from_price"); obs(s, "e", "from", 30, "0.10")
            listing(s, "e", "nocanon", "0.10", gpu=None); obs(s, "e", "nocanon", 30, "0.10")
            # A second GPU with a single steady provider
            listing(s, "a", "solo", "9.00", gpu="NVIDIA SOLO"); obs(s, "a", "solo", 30, "9.00")

        normalize.SessionLocal = Session
        ov = market.overview(24)
        by = {g["gpu"]: g for g in ov["gpus"]}
        assert set(by) == {GPU, "NVIDIA SOLO"}, set(by)
        g = by[GPU]
        assert g["providers"] == 3, "a, b, c sell it now; d is sold out and e's rows are excluded"
        assert g["lowest"] == 1.5 and g["lowest_provider"] == "c" and g["highest"] == 5.0 and g["median"] == 3.0
        assert len(g["spark"]) == len(ov["times"]) == 49
        # c joined 2h ago, so the sparkline and the change begin there, not 24h ago
        assert g["spark"][0] is None and g["spark"][-1] == 1.5
        first_value = next(i for i, v in enumerate(g["spark"]) if v is not None)
        assert 40 <= first_value <= 47, first_value
        # Two hours ago d was selling at 0.50; it sold out an hour ago. The market's lowest really did go 0.50 -> 1.50.
        assert g["change_pct"] is not None and abs(g["change_pct"] - 2.0) < 1e-9, g["change_pct"]
        assert g["spark"][first_value] == 0.5 and g["spark"][-1] == 1.5
        assert by["NVIDIA SOLO"]["providers"] == 1 and by["NVIDIA SOLO"]["change_pct"] == 0.0

        # Over 6h the change is measured over the same coherent span; over ALL history too
        assert market.overview(6)["gpus"][0]["gpu"] in (GPU, "NVIDIA SOLO")
        allh = market.overview(0)
        assert allh["gpus"] and allh["times"][0] <= ov["times"][0]

        d = market.detail(GPU, 24)
        provs = {p["provider"]: p for p in d["providers"]}
        assert set(provs) == {"a", "b", "c", "d"}, "d appears in history even though it is sold out now"
        assert [p["provider"] for p in d["providers"]][:3] == ["c", "a", "b"], "cheapest first; sold out last"
        assert provs["d"]["now"] is None and provs["c"]["now"] == 1.5
        assert provs["a"]["series"][0] == 4.0 and provs["a"]["series"][-1] == 3.0, "a's drop is in its own line"
        assert abs(provs["a"]["change_pct"] - (-0.25)) < 1e-9
        assert provs["c"]["first_seen"] is not None and provs["c"]["series"][0] is None
        assert d["lowest"][0] is None and d["lowest"][-1] == 1.5 and d["market_from"] > d["t0"]
        assert market.detail("NVIDIA NOPE", 24)["providers"] == []
    finally:
        engine.dispose()
        run(["dropdb", "--if-exists", "opengrid_test"])


if __name__ == "__main__":
    for t in (test_price_at, test_series_and_aggregate, test_change_over, test_sql):
        t(); print(t.__name__, "ok")
