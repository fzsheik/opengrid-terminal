"""Hourly rollup: it must agree with market.py's own sampling.

Run:  .venv/bin/python tests/test_rollups.py
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import market  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from analytics import rollups  # noqa: E402

DB = "og_test_rollups"


def test_rollup_matches_market():
    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        meta = fixtures.seed(Session, days=10)
        normalize.SessionLocal = Session
        r = rollups.refresh()
        assert r["rows"] > 0 and r["hours"] >= 239, r
        # Incremental refresh recomputes the tail and leaves the row count stable.
        before = rollups.coverage()["rows"]
        rollups.refresh()
        assert rollups.coverage()["rows"] == before

        # Every hour's provider minimum equals market.provider_series at the same instant.
        gpu = "NVIDIA H100 80GB SXM5"
        rows = rollups.provider_hourly(gpu=gpu)
        hours = sorted({x["hour"] for x in rows})[-48:]
        _, t0, listings, events = market._load(gpu, 0)
        for h in hours[::7]:
            _, series, _ = market.provider_series(listings, events, h, h + timedelta(seconds=1), 1)
            expect = {p: s[0] for p, s in series.get(gpu, {}).items() if s[0] is not None}
            got = {x["provider"]: x["min_price"] for x in rows if x["hour"] == h and x["min_price"] is not None}
            assert set(expect) == set(got), (h, expect, got)
            for p in expect:
                assert abs(expect[p] - got[p]) < 1e-6, (h, p, expect[p], got[p])
        first = rollups.first_hours()
        late = [k for k in first if k[1] == "syn_eps"]
        assert all(first[k] >= meta["start"] + timedelta(days=5) for k in late), "late joiner starts late"
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_rollup_matches_market,):
        t(); print(t.__name__, "ok")
