"""Price-change comparison: the classifier, then the SQL against a scratch database.

Run:  .venv/bin/python tests/test_changes.py

The database half builds `og_test_changes` (never the real one), fills it with
synthetic prices at known times, and drops it again.
"""

import scratchdb
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalize
from normalize import classify_change

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
CUT = NOW - timedelta(hours=24)
BEFORE = CUT - timedelta(hours=1)
AFTER = CUT + timedelta(hours=1)


def cc(now, then, then_at=BEFORE, first=BEFORE - timedelta(days=1), cutoff=CUT):
    return classify_change(None if now is None else D(now), None if then is None else D(then), then_at, first, cutoff)


def test_classifier():
    assert cc("2.00", "2.50")[0] == "down" and cc("2.00", "2.50")[1] == D("-0.2")
    assert cc("3.00", "2.50")[0] == "up"
    assert cc("2.50", "2.50")[0] == "flat"
    # below 0.5%: Vast-style median wobble is not a change
    assert cc("2.5100", "2.5000")[0] == "flat"
    assert cc("2.5200", "2.5000")[0] == "up"            # 0.8%
    # below $0.001 even though large in percent terms: a 0.02 price moving by a tenth of a cent
    assert cc("0.0205", "0.0200")[0] == "flat"
    assert cc("0.0215", "0.0200")[0] == "up"            # 7.5% and $0.0015
    # no price in force at the cutoff but we were watching: the listing is new
    assert cc("2.00", None, then_at=None)[0] == "new"
    # we were not recording this provider yet: unknown, never "new" or "flat"
    assert cc("2.00", "2.50", first=AFTER)[0] == "nodata"
    assert cc("2.00", None, then_at=None, first=AFTER)[0] == "nodata"
    assert cc("2.00", "2.50", first=None)[0] == "nodata"
    assert cc(None, "2.50")[0] == "nodata"
    assert cc("2.00", "0")[0] == "nodata"               # no divide by zero
    # first fetch exactly at the cutoff counts as watching
    assert cc("2.00", "2.50", first=CUT)[0] == "down"




def test_sql():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tables import Base, ComputeListingRow, ListingObservation, RawSnapshot

    scratchdb.drop("og_test_changes")
    scratchdb.create("og_test_changes")
    engine = create_engine("postgresql+psycopg://localhost:5432/og_test_changes")
    try:
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        # The database's clock, not ours: the SQL compares against its now(), and a client
        # clock even milliseconds ahead would push the "exactly at the cutoff" row past it.
        from sqlalchemy import text
        with engine.connect() as conn:
            now = conn.execute(text("SELECT now()")).scalar()
        ago = lambda h: now - timedelta(hours=h)  # noqa: E731

        def listing(s, prov, lid, price):
            s.add(ComputeListingRow(
                provider=prov, listing_id=lid, sku=lid, raw_gpu_name="x", canonical_gpu_name="X", gpu_count=1,
                price_per_gpu_hour=None if price is None else D(price), currency="USD",
                observed_at=now, first_seen_at=ago(500),
            ))
            s.flush()

        def obs(s, prov, lid, hours_ago, price):
            s.add(ListingObservation(provider=prov, listing_id=lid, observed_at=ago(hours_ago), price_per_gpu_hour=D(price)))

        def seen(s, prov, hours_ago):
            s.add(RawSnapshot(provider=prov, endpoint="/e", fetched_at=ago(hours_ago), ok=True))

        with Session.begin() as s:
            # Provider "a" has been watched for 10 days.
            seen(s, "a", 240); seen(s, "a", 1)
            # Price history: 3.00 until 30h ago, 2.50 from 30h, 2.00 from 5h.  At the 24h cutoff the price in force was 2.50.
            listing(s, "a", "down", "2.00"); obs(s, "a", "down", 300, "3.00"); obs(s, "a", "down", 30, "2.50"); obs(s, "a", "down", 5, "2.00")
            # Changed twice inside the window and ended where it began: flat vs the cutoff.
            listing(s, "a", "roundtrip", "4.00"); obs(s, "a", "roundtrip", 300, "4.00"); obs(s, "a", "roundtrip", 10, "5.00"); obs(s, "a", "roundtrip", 2, "4.00")
            # Rose.
            listing(s, "a", "up", "6.00"); obs(s, "a", "up", 300, "5.00"); obs(s, "a", "up", 3, "6.00")
            # Never changed: its only observation is old.
            listing(s, "a", "flat", "1.00"); obs(s, "a", "flat", 300, "1.00")
            # First seen 3 hours ago, inside the window.
            listing(s, "a", "new", "7.00"); obs(s, "a", "new", 3, "7.00")
            # Observation exactly at the cutoff counts as in force at the cutoff.
            listing(s, "a", "edge", "1.50"); obs(s, "a", "edge", 24, "1.00"); obs(s, "a", "edge", 1, "1.50")
            # Provider "b" only started being recorded 2 hours ago: a 24h comparison is impossible.
            seen(s, "b", 2)
            listing(s, "b", "young", "9.00"); obs(s, "b", "young", 2, "9.00")
            # A failed fetch does not count as watching.
            s.add(RawSnapshot(provider="c", endpoint="/e", fetched_at=ago(500), ok=False))
            seen(s, "c", 1)
            listing(s, "c", "failed-early", "1.00"); obs(s, "c", "failed-early", 1, "1.00")

        normalize.SessionLocal = Session  # the code under test reads this
        out = normalize.price_changes(24)
        got = {(i["provider"], i["listing_id"]): i for i in out["items"]}
        expect = {
            ("a", "down"): ("down", 2.00, 2.50),
            ("a", "roundtrip"): ("flat", 4.00, 4.00),
            ("a", "up"): ("up", 6.00, 5.00),
            ("a", "flat"): ("flat", 1.00, 1.00),
            ("a", "new"): ("new", 7.00, None),
            ("a", "edge"): ("up", 1.50, 1.00),
            ("b", "young"): ("nodata", 9.00, None),
            ("c", "failed-early"): ("nodata", 1.00, None),
        }
        for key, (status, now_p, then_p) in expect.items():
            g = got[key]
            assert g["status"] == status, (key, g)
            assert g["price_now"] == now_p and g["price_then"] == then_p, (key, g)
        assert abs(got[("a", "down")]["pct"] - (-0.2)) < 1e-9
        assert out["hours"] == 24 and out["tracking_since"] is not None

        # A shorter window moves the cutoff: 4h ago, "down" had already reached 2.00 at 5h, so it is flat.
        short = {(i["provider"], i["listing_id"]): i["status"] for i in normalize.price_changes(4)["items"]}
        assert short[("a", "down")] == "flat", short
        assert short[("a", "roundtrip")] == "down", short         # 5.00 was in force 4h ago, now 4.00
        assert short[("a", "up")] == "up", short                  # 5.00 4h ago (it rose 3h ago), now 6.00
        assert short[("a", "new")] == "new", short                # first seen 3h ago, after the 4h cutoff
        assert short[("b", "young")] == "nodata", short           # recording began 2h ago, after the cutoff
        # A 1h window: provider b still has no data from before the cutoff? It does (2h ago), so it is known and unchanged.
        one = {(i["provider"], i["listing_id"]): i["status"] for i in normalize.price_changes(1.5)["items"]}
        assert one[("b", "young")] == "flat", one
    finally:
        engine.dispose()
        scratchdb.drop("og_test_changes")


if __name__ == "__main__":
    test_classifier(); print("classifier ok")
    test_sql(); print("sql ok")
