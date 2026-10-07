"""Market events: each detector fires on a crafted scenario and stays quiet on coverage artefacts.

Run:  .venv/Scripts/python tests/test_events.py
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
from sqlalchemy import text  # noqa: E402
from tables import ComputeListingRow, ListingObservation, RawSnapshot  # noqa: E402
from analytics import events, rollups  # noqa: E402

DB = "og_test_events"
H100, H200, A100, L40S, B200 = ("NVIDIA H100 80GB SXM5", "NVIDIA H200 141GB SXM5", "NVIDIA A100 80GB SXM4",
                                "NVIDIA L40S 48GB", "NVIDIA B200 180GB SXM")
rollups.AFTER_REFRESH.clear()  # tests call the detector explicitly


class World:
    """A hand-built market: listings with change-only observations, plus raw fetch rows."""

    def __init__(self, days):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.T = rollups.floor_hour(self.now)
        self.start = self.T - timedelta(days=days)
        self.listings, self.obs, self.raw = [], [], []

    def at(self, hours_ago, minutes_before=1):
        """A time that the rollup's hour (T - hours_ago) sees: just before the top of that hour."""
        return self.T - timedelta(hours=hours_ago, minutes=minutes_before)

    def fetch(self, prov, t, ok=True):
        self.raw.append(RawSnapshot(provider=prov, endpoint="/synthetic", fetched_at=t, ok=ok,
                                    status_code=200 if ok else 503, error=None if ok else "HTTP 503", payload={}))

    def listing(self, prov, gpu, steps, first=None, last_seen=None, region="us-east"):
        """steps: [(time, price, available)] ascending."""
        lid = f"{prov}:{gpu}:{region}"
        first = first or steps[0][0]
        for t, price, avail in steps:
            self.obs.append(ListingObservation(provider=prov, listing_id=lid, observed_at=t,
                                               price_per_gpu_hour=Decimal(str(price)),
                                               price_per_instance_hour=Decimal(str(price)), available=avail))
        _, price, avail = steps[-1]
        self.listings.append(ComputeListingRow(
            provider=prov, listing_id=lid, sku=lid, raw_gpu_name=gpu, canonical_gpu_name=gpu, gpu_count=1,
            region=region, country="US", price_per_gpu_hour=Decimal(str(price)),
            price_per_instance_hour=Decimal(str(price)), currency="USD", market_type="on_demand",
            provider_tier=None, interruptible=False, available=avail, observed_at=last_seen or self.now,
            first_seen_at=first))

    def save(self, Session):
        with Session.begin() as s:
            s.add_all(self.raw)
            s.add_all(self.listings)
            s.flush()
            s.add_all(self.obs)


def _setup(world):
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    world.save(Session)
    normalize.SessionLocal = Session
    rollups.refresh()
    return Session


def _scenario_a():
    """40 days of steady history, then one crafted move of each kind."""
    w = World(days=40)
    S = w.start
    for p in ("syn_a", "syn_b", "syn_c", "syn_d"):
        w.fetch(p, S)
        w.fetch(p, w.now - timedelta(minutes=2))
    # H100: syn_a cuts 2.00 -> 1.70 (-15%) at T-10h.
    w.listing("syn_a", H100, [(S, 2.00, True), (w.at(10), 1.70, True)])
    w.listing("syn_b", H100, [(S, 2.50, True)])
    w.listing("syn_c", H100, [(S, 3.00, True)])
    # A100: syn_b (the cheapest) sells out at T-20h and comes back at T-5h.
    w.listing("syn_b", A100, [(S, 1.30, True), (w.at(20), 1.30, False), (w.at(5), 1.30, True)])
    w.listing("syn_c", A100, [(S, 1.50, True)])
    # L40S: syn_c starts selling (new listing) at T-8h, below syn_a.
    w.listing("syn_a", L40S, [(S, 1.00, True)])
    w.listing("syn_c", L40S, [(w.at(8), 0.90, True)])
    # H200: syn_a's listing vanishes after T-30h; syn_c (alone then) sells out at T-12h.
    w.listing("syn_a", H200, [(S, 3.80, True)], last_seen=w.at(30, 0))
    w.listing("syn_c", H200, [(S, 3.50, True), (w.at(12), 3.50, False)])
    # B200: syn_eps is a provider we only began recording 3 days ago, and it is the cheapest.
    w.fetch("syn_eps", w.T - timedelta(days=3))
    w.fetch("syn_eps", w.now - timedelta(minutes=2))
    w.listing("syn_a", B200, [(S, 5.00, True)])
    w.listing("syn_b", B200, [(S, 5.50, True)])
    w.listing("syn_eps", B200, [(w.T - timedelta(days=3), 4.00, True)])
    # syn_d's feed fails 4 rounds from T-6h, then recovers at T-5h.
    for m in (0, 15, 30, 45):
        w.fetch("syn_d", w.T - timedelta(hours=6) + timedelta(minutes=m), ok=False)
    w.fetch("syn_d", w.T - timedelta(hours=5))
    return w


def _by_type(evs):
    out = {}
    for e in evs:
        out.setdefault(e["type"], []).append(e)
    return out


def _hours_ago(w, e):
    return round((w.T - datetime.fromisoformat(e["occurred_at"])).total_seconds() / 3600, 2)


def test_crafted_detectors():
    w = _scenario_a()
    try:
        _setup(w)
        r = events.run()
        assert r["events_written"] > 0, r
        evs = events.recent(limit=1000)
        t = _by_type(evs)

        # price_move: syn_a cut H100 15% at T-10h; the 24h drift afterwards is in cooldown, not repeated.
        pm = [e for e in t.get("price_move", []) if e["provider"] == "syn_a" and e["gpu"] == H100]
        assert len(pm) == 1, pm
        assert _hours_ago(w, pm[0]) == 10 and abs(pm[0]["pct"] + 0.15) < 1e-9 and pm[0]["severity"] == "notable"
        assert pm[0]["title"] == "syn_a cut NVIDIA H100 80GB SXM5 by 15.0% to $1.70/hr", pm[0]["title"]
        assert pm[0]["detail"]["cause"] == "price_change" and pm[0]["kind"] == "observed"

        # Records: 40 days of history -> an all-time low (which supersedes the 30-day low on the lowest price).
        atl = [e for e in t.get("new_all_time_low", []) if e["gpu"] == H100]
        assert len(atl) == 1 and _hours_ago(w, atl[0]) == 10, atl
        assert "since tracking began" in atl[0]["title"]
        assert not [e for e in t.get("new_30d_low", []) if e["gpu"] == H100 and e["detail"]["metric"] == "lowest"]

        # Spread: 50% for 40 days, 76% after the cut -> above p95 x 1.25.
        sp = [e for e in t.get("spread_anomaly", []) if e["gpu"] == H100]
        assert len(sp) == 1 and sp[0]["detail"]["basis"] == "30d_p95" and _hours_ago(w, sp[0]) == 10, sp

        # sold_out / capacity_returned for syn_b A100, and the cheapest-provider handover both ways.
        so = [e for e in t.get("sold_out", []) if e["provider"] == "syn_b" and e["gpu"] == A100]
        assert len(so) == 1 and _hours_ago(w, so[0]) == 20 and so[0]["severity"] == "notable", so
        cr = [e for e in t.get("capacity_returned", []) if e["provider"] == "syn_b" and e["gpu"] == A100]
        assert len(cr) == 1 and _hours_ago(w, cr[0]) == 5, cr
        nc = sorted((_hours_ago(w, e), e["provider"]) for e in t.get("new_cheapest_provider", []) if e["gpu"] == A100)
        assert nc == [(5, "syn_b"), (20, "syn_c")], nc

        # provider_added_gpu: syn_c's new L40S listing; it undercuts, so notable, and becomes cheapest.
        add = t.get("provider_added_gpu", [])
        assert [(e["provider"], e["gpu"], _hours_ago(w, e)) for e in add] == [("syn_c", L40S, 8)], add
        assert add[0]["severity"] == "notable" and add[0]["kind"] == "inferred"
        assert any(e["gpu"] == L40S and e["provider"] == "syn_c" for e in t.get("new_cheapest_provider", []))

        # provider_removed_gpu: syn_a's H200, confirmed after two hours gone.
        rm = t.get("provider_removed_gpu", [])
        assert [(e["provider"], e["gpu"], _hours_ago(w, e)) for e in rm] == [("syn_a", H200, 29)], rm
        # H200 then sells out everywhere when syn_c sells out.
        mso = [e for e in t.get("sold_out", []) if e["gpu"] == H200 and e["provider"] is None]
        assert len(mso) == 1 and mso[0]["severity"] == "major" and _hours_ago(w, mso[0]) == 12, mso

        # Feed outage and recovery.
        fd, fr = t.get("provider_feed_down", []), t.get("provider_feed_recovered", [])
        assert [(e["provider"], _hours_ago(w, e)) for e in fd] == [("syn_d", 6)], fd
        assert [(e["provider"], _hours_ago(w, e)) for e in fr] == [("syn_d", 5)], fr
        assert fr[0]["detail"]["outage_seconds"] == 3600

        # The late joiner: coverage_started, and nothing else about it or about B200.
        cs = [e for e in t.get("coverage_started", []) if e["provider"] == "syn_eps"]
        assert len(cs) == 1 and cs[0]["detail"]["gpus"] == [B200], cs
        b200 = [e for e in evs if e["gpu"] == B200]
        assert b200 == [], [(e["type"], e["title"]) for e in b200]
        assert not [e for e in evs if e["provider"] == "syn_eps" and e["type"] != "coverage_started"]

        # Filters.
        assert all(e["gpu"] == A100 for e in events.recent(gpu=A100))
        assert all(e["type"] == "sold_out" for e in events.recent(types=["sold_out"]))
        items, total = events.query(min_severity="major")
        assert total == len(items) and all(e["severity"] == "major" for e in items) and total >= 2
        assert all(e["provider"] == "syn_d" for e in events.recent(provider="syn_d"))
    finally:
        scratchdb.drop(DB)


def test_idempotent():
    w = _scenario_a()
    try:
        Session = _setup(w)
        events.run()
        def q(sql):
            with Session() as s:
                return list(s.execute(text(sql)).scalars())
        count = lambda: q("SELECT count(*) FROM market_events")[0]  # noqa: E731
        keys = lambda: sorted(q("SELECT dedupe_key FROM market_events"))  # noqa: E731
        n, k = count(), keys()
        r2 = events.run()
        assert count() == n and r2["events_written"] == 0, r2
        # Incremental path: rewind the watermark 12h and drop the last 11h of events; a run restores them.
        with Session.begin() as s:
            s.execute(text("DELETE FROM market_events WHERE occurred_at >= :t AND type NOT LIKE 'provider_feed%'"),
                      {"t": w.T - timedelta(hours=11)})
            s.execute(text("UPDATE event_detector_state SET watermark = :t WHERE name = 'market:on_demand'"),
                      {"t": w.T - timedelta(hours=12)})
        assert count() < n
        r3 = events.run()
        assert r3["hours"] == 16 and count() == n and keys() == k, r3
        # Forget every watermark and recompute from scratch: still the same rows.
        rollups.refresh()
        events.rebuild()
        with Session.begin() as s:
            s.execute(text("DELETE FROM event_detector_state"))
        events.run()
        assert count() == n and keys() == k
    finally:
        scratchdb.drop(DB)


def test_thin_history():
    """10 days: moves and spread (absolute basis) fire; 30-day and all-time records do not."""
    w = World(days=10)
    S = w.start
    for p in ("syn_a", "syn_b", "syn_c"):
        w.fetch(p, S)
        w.fetch(p, w.now - timedelta(minutes=2))
    w.listing("syn_a", H100, [(S, 2.00, True), (w.at(10), 1.70, True)])
    w.listing("syn_b", H100, [(S, 2.50, True)])
    w.listing("syn_a", A100, [(S, 1.00, True)])
    w.listing("syn_b", A100, [(S, 1.10, True)])
    w.listing("syn_c", A100, [(S, 1.20, True), (w.at(4), 5.00, True)])
    # A 4% move is below the 10% threshold.
    w.listing("syn_c", L40S, [(S, 1.00, True), (w.at(6), 0.96, True)])
    try:
        _setup(w)
        events.run()
        t = _by_type(events.recent(limit=1000))
        assert [(e["provider"], e["gpu"]) for e in t.get("price_move", []) if e["provider"]] == \
            [("syn_c", A100), ("syn_a", H100)], t.get("price_move")
        big = [e for e in t["price_move"] if e["provider"] == "syn_c"][0]
        assert big["severity"] == "major"
        for typ in ("new_30d_low", "new_30d_high", "new_all_time_low", "new_all_time_high"):
            assert typ not in t, (typ, t.get(typ))
        sp = t.get("spread_anomaly", [])
        assert len(sp) == 1 and sp[0]["gpu"] == A100 and sp[0]["detail"]["basis"] == "absolute", sp
        # Only one provider sells H100 at 2 points -> spread needs 3: nothing for H100.
        assert not [e for e in sp if e["gpu"] == H100]
    finally:
        scratchdb.drop(DB)


def test_random_fixture_late_joiner():
    """The shared random fixture: syn_eps joins late and must not look like market news when it does."""
    try:
        url = scratchdb.create(DB)
        Session = fixtures.session(url)
        meta = fixtures.seed(Session, days=60)
        normalize.SessionLocal = Session
        rollups.refresh()
        events.run()
        evs = events.recent(limit=100000)
        joined = meta["start"] + timedelta(days=60 * 0.6)
        eps = [e for e in evs if e["provider"] == "syn_eps"]
        assert [e for e in eps if e["type"] == "coverage_started"], "coverage start is recorded"
        assert not [e for e in eps if e["type"] == "provider_added_gpu"], "joining is not adding a GPU"
        def within(e, hours):
            return joined - timedelta(hours=1) <= datetime.fromisoformat(e["occurred_at"]) < joined + timedelta(hours=hours)
        # At the join itself nothing about syn_eps; for the warm-up day it cannot move market figures.
        # (Its own later price steps are real observations and may fire price_move.)
        bad = [e for e in eps if within(e, 2) and e["type"] != "coverage_started"]
        bad += [e for e in eps if within(e, 24) and e["type"] in (
            "new_cheapest_provider", "new_30d_low", "new_all_time_low", "spread_anomaly")]
        assert not bad, [(e["type"], e["title"]) for e in bad]
        # Synthetic random walks do produce real events elsewhere.
        assert any(e["type"] == "price_move" for e in evs) and any(e["type"] == "sold_out" for e in evs)
        # The derived table exists for every rollup hour of each GPU.
        with Session() as s:
            n = s.execute(text("SELECT count(DISTINCT hour) FROM market_gpu_hourly")).scalar()
        assert n >= 24 * 59, n
    finally:
        scratchdb.drop(DB)


def test_marketplace_persistence():
    """Vast (provider_meta class marketplace): a one-hour swing never fires; a held move fires once,
    labelled "held 2h"; severity is capped at notable unless the move exceeds 40%. Idempotent."""
    w = World(days=10)
    S = w.start
    for p in ("vast", "syn_a"):
        w.fetch(p, S)
        w.fetch(p, w.now - timedelta(minutes=2))
    # H100: a +50% one-hour swing at T-20 (reverts at T-19), then a held -15% cut from T-10.
    w.listing("vast", H100, [(S, 2.00, True), (w.at(20), 3.00, True), (w.at(19), 2.00, True), (w.at(10), 1.70, True)])
    w.listing("vast", A100, [(S, 1.00, True), (w.at(6), 1.30, True)])      # +30%, held: notable (capped)
    w.listing("vast", L40S, [(S, 1.00, True), (w.at(4), 1.50, True)])      # +50%, held: major
    # Control: the same one-hour swing at a non-marketplace provider does fire.
    w.listing("syn_a", B200, [(S, 5.00, True), (w.at(20), 7.50, True), (w.at(19), 5.00, True)])
    try:
        Session = _setup(w)
        events.run()
        pm = [e for e in events.recent(limit=1000, types=["price_move"]) if e["provider"]]
        vh = [e for e in pm if e["provider"] == "vast" and e["gpu"] == H100]
        assert len(vh) == 1, [(e["title"], e["occurred_at"]) for e in vh]
        e = vh[0]
        assert _hours_ago(w, e) == 9 and e["severity"] == "notable" and "held 2h" in e["title"], e
        assert e["detail"]["persistence"]["held_hours"] == 2 and e["detail"]["provider_class"] == "marketplace"
        assert e["value_before"] == 2.0 and e["value_after"] == 1.7
        va = [e for e in pm if e["provider"] == "vast" and e["gpu"] == A100]
        assert len(va) == 1 and va[0]["severity"] == "notable", va
        vl = [e for e in pm if e["provider"] == "vast" and e["gpu"] == L40S]
        assert len(vl) == 1 and vl[0]["severity"] == "major", vl
        ctl = [e for e in pm if e["provider"] == "syn_a" and e["gpu"] == B200]
        assert ctl and all(e["severity"] == "major" and "held" not in e["title"] for e in ctl), ctl
        with Session() as s:
            n = s.execute(text("SELECT count(*) FROM market_events")).scalar()
        assert events.run()["events_written"] == 0
        with Session() as s:
            assert s.execute(text("SELECT count(*) FROM market_events")).scalar() == n
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_crafted_detectors, test_idempotent, test_thin_history, test_random_fixture_late_joiner,
              test_marketplace_persistence):
        t(); print(t.__name__, "ok")
