"""OpenGrid indices: the pure math, then the full pipeline on synthetic history, then the endpoints.

Run:  .venv/Scripts/python tests/test_indices.py
"""

import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import normalize  # noqa: E402
import provider_meta  # noqa: E402
import scratchdb  # noqa: E402
from sqlalchemy import text  # noqa: E402

from analytics import indices, rollups  # noqa: E402
from analytics.indices import IndexDef, robust_stat, screen, step_base, step_composite  # noqa: E402

DB = "og_test_indices"
DB_SHORT = "og_test_indices_short"
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
H = lambda n: T0 + timedelta(hours=n)  # noqa: E731
H100 = "NVIDIA H100 80GB SXM5"


def V(**prices):
    return {p: (v, None) for p, v in prices.items()}


def test_robust_stat():
    assert robust_stat([3, 1, 2]) == (2, "median")
    assert robust_stat([1, 2, 3, 4]) == (2.5, "median")
    v, m = robust_stat([1, 2, 3, 4, 100])
    assert m == "trimmed_mean_20" and v == 3.0, "n=5 trims one from each end: mean(2,3,4)"
    v, _ = robust_stat(list(range(1, 11)))
    assert v == sum(range(3, 9)) / 6, "n=10 trims two from each end"


def test_screen():
    inc, det = screen(V(a=2.0, b=2.2, c=2.4, d=10.0, e=0.5), down={"c"})
    st = {d["provider"]: d["status"] for d in det}
    assert st == {"a": "included", "b": "included", "c": "excluded_feed_down",
                  "d": "excluded_outlier", "e": "excluded_outlier"}, st
    assert inc == {"a": 2.0, "b": 2.2}
    # Below MIN_PROVIDERS nothing is screened as an outlier (no meaningful median to screen against)
    inc, det = screen(V(a=1.0, b=9.0))
    assert set(inc) == {"a", "b"}


def test_min_coverage():
    row, state = step_base(None, V(a=2.0, b=3.0), set(), H(0))
    assert not row["published"] and row["level"] is None and state is None
    assert "insufficient coverage" in row["reason"] and row["raw_level"] == 2.5
    row, _ = step_base(None, V(a=2.0, b=3.0, c=9.0), {"c"}, H(0))
    assert not row["published"], "a feed-down provider does not count toward coverage"


def test_chain_linking_late_joiner():
    row, st = step_base(None, V(a=2.0, b=3.0, c=4.0), set(), H(0))
    assert row["published"] and row["level"] == 3.0 and row["segment_no"] == 1
    # d joins much cheaper: the cross-section drops (3.0 -> 2.5) but no price changed
    row, st = step_base(st, V(a=2.0, b=3.0, c=4.0, d=1.0), set(), H(1))
    assert row["raw_level"] == 2.5 and row["level"] == 3.0 and row["link_constituents"] == 3
    # now a real move among the common set: a 2.0 -> 2.2 (median of 1, 2.2, 3, 4 = 2.6 vs 2.5)
    row, st = step_base(st, V(a=2.2, b=3.0, c=4.0, d=1.0), set(), H(2))
    assert abs(row["level"] - 3.0 * 2.6 / 2.5) < 1e-12
    # c leaves: no move either
    lvl = row["level"]
    row, st = step_base(st, V(a=2.2, b=3.0, d=1.0, e=1.5), set(), H(3))
    assert abs(row["level"] - lvl * robust_stat([2.2, 3.0, 1.0])[0] / robust_stat([2.2, 3.0, 1.0])[0]) < 1e-12
    # an unpublished hour keeps the chain; the next published hour links back across the gap
    row, st2 = step_base(st, V(a=9.0), set(), H(4))
    assert not row["published"] and st2 is st
    row, st = step_base(st, V(a=4.4, b=3.0, d=1.0, e=1.5), set(), H(5))
    assert row["published"] and row["link_constituents"] == 4
    # complete turnover: the chain is rebased and the segment number moves on
    row, st = step_base(st, V(x=5.0, y=6.0, z=7.0), set(), H(6))
    assert row["level"] == 6.0 and row["segment_no"] == 2 and row["reason"].startswith("rebased")


def test_composite():
    d = IndexDef("c", "C", "composite", unit="points", components=(("x", 0.5), ("y", 0.5)))
    pub = lambda lvl, seg=1: {"published": True, "level": lvl, "segment_no": seg}  # noqa: E731
    row, st = step_composite(d, None, {"x": pub(2.0), "y": pub(10.0)}, H(0))
    assert row["level"] == 100.0 and row["published"]
    row, st = step_composite(d, st, {"x": pub(2.2), "y": pub(10.0)}, H(1))
    assert abs(row["level"] - 105.0) < 1e-9, "equal weights: (1.10 + 1.00) / 2"
    # y unpublished: one published component is not a composite -> unpublished, chain kept
    row, st2 = step_composite(d, st, {"x": pub(2.42), "y": {"published": False, "level": None, "segment_no": 1}}, H(2))
    assert not row["published"] and st2 is st
    d3 = IndexDef("c3", "C3", "composite", unit="points", components=(("x", 1), ("y", 1), ("z", 1)))
    row, st = step_composite(d3, None, {"x": pub(1.0), "y": pub(1.0), "z": pub(1.0)}, H(0))
    # z unpublished: 67% of weight, linked on x and y (re-weighted over the linkable components)
    row, st = step_composite(d3, st, {"x": pub(1.3), "y": pub(1.0)}, H(1))
    assert abs(row["level"] - 115.0) < 1e-9 and row["link_constituents"] == 2
    # z comes back at a different level: not in the previous hour, so re-entering is not a move
    row, st = step_composite(d3, st, {"x": pub(1.3), "y": pub(1.0), "z": pub(7.0)}, H(2))
    assert abs(row["level"] - 115.0) < 1e-9, "z re-entering is not a move"
    # a child rebased (segment changed): not linkable that hour
    row, st = step_composite(d3, st, {"x": pub(1.3), "y": pub(1.0), "z": pub(99.0, seg=2)}, H(3))
    assert abs(row["level"] - 115.0) < 1e-9
    row, _ = step_composite(d3, None, {"x": pub(1.0)}, H(0))
    assert not row["published"] and "33%" in row["reason"]
    assert step_composite(d3, None, {}, H(0)) == (None, None)


def _fake_regions():
    m = types.ModuleType("regions")
    m.REGION_GROUPS = indices.REGION_GROUPS
    m.region_group = lambda provider, region, country: (
        "US" if (region or "").startswith("us") else "Europe" if (region or "").startswith("eu") else None)
    return m


def _rows(Session, iid):
    with Session() as s:
        return s.execute(text("SELECT * FROM index_levels WHERE index_id = :i ORDER BY hour"), {"i": iid}).all()


def test_pipeline():
    sys.modules["regions"] = _fake_regions()
    classes = {"syn_alpha": "neocloud", "syn_beta": "neocloud", "syn_gamma": "marketplace",
               "syn_delta": "neocloud", "syn_eps": "neocloud"}
    for p, c in classes.items():
        provider_meta.PROVIDER_META[p] = provider_meta.ProviderMeta(p, p, c, "public_api", "", "")
    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        meta = fixtures.seed(Session, days=120)
        _in_stock(Session)
        normalize.SessionLocal = Session
        rollups.refresh()  # the AFTER_REFRESH hook computes the indices
        with Session() as s:
            n = s.execute(text("SELECT count(*) FROM index_levels")).scalar()
            ver = s.execute(text("SELECT DISTINCT methodology_version FROM index_levels")).scalars().all()
        assert n > 0 and ver == [indices.METHODOLOGY_VERSION + "+regions"], (n, ver)

        # The late joiner: at the hour syn_eps first counts, the index moves only by the common set's move.
        first = rollups.first_hours()
        def eps_join(rows):
            hs = [r.hour for r in rows if any(d["provider"] == "syn_eps" and d["status"] == "included" for d in r.detail)]
            return min(hs) if hs else None

        for gpu in sorted(g for (g, p) in first if p == "syn_eps"):
            iid = indices.gpu_index_id(gpu)
            rows = _rows(Session, iid)
            join = eps_join(rows)
            if join and any(r.published and r.hour < join for r in rows) and next(
                    r for r in rows if r.hour == join).published:
                break
        else:
            raise AssertionError("fixture has no GPU where syn_eps joins a published index")
        by_h = {r.hour: r for r in rows}
        assert join >= meta["start"] + timedelta(days=70), join
        cur = by_h[join]
        prev = max((r for r in rows if r.hour < join and r.published), key=lambda r: r.hour)
        assert cur.published, cur.reason
        inc = lambda r: {d["provider"]: d["price"] for d in r.detail if d["status"] == "included"}  # noqa: E731
        common = sorted(set(inc(cur)) & set(inc(prev)))
        assert "syn_eps" not in common and cur.link_constituents == len(common)
        expect = prev.level * robust_stat([inc(cur)[p] for p in common])[0] / robust_stat([inc(prev)[p] for p in common])[0]
        assert abs(cur.level - expect) < 1e-9, (cur.level, expect)
        # What a naive cross-section would have done at that hour vs what the index did:
        naive = cur.raw_level / prev.raw_level - 1
        moved = cur.level / prev.level - 1
        print(f"  {gpu}: syn_eps joined {join:%Y-%m-%d %H}h; cross-section moved {naive:+.2%}, index {moved:+.2%}")

        # Every published hour is chain-consistent with its predecessor
        last = None
        for r in rows:
            if not r.published:
                continue
            if last is not None and r.segment_no == last.segment_no and r.link_constituents:
                c = sorted(set(inc(r)) & set(inc(last)))
                e = last.level * robust_stat([inc(r)[p] for p in c])[0] / robust_stat([inc(last)[p] for p in c])[0]
                assert abs(r.level - e) < 1e-9 * max(1, e)
            last = r

        # Coverage: a GPU's index is unpublished in hours with fewer than 3 included providers
        for r in rows:
            assert r.published == (r.constituents >= indices.MIN_PROVIDERS), r

        # Regional and class indices exist; regional constituents carry their region
        reg = _rows(Session, gpu_slug_id(H100, "us"))
        assert reg and all(d.get("region", "").startswith("us") for r in reg for d in r.detail), "US only"
        cls = _rows(Session, gpu_slug_id(H100, "neocloud"))
        assert cls and all(classes[d["provider"]] == "neocloud" for r in cls for d in r.detail)
        assert not _rows(Session, gpu_slug_id(H100, "hyperscaler"))

        # Composite: base 100, published now
        comp = _rows(Session, "gpu-compute")
        firstpub = next(r for r in comp if r.published)
        assert firstpub.level == 100.0 and comp[-1].published, (comp[-1].hour, comp[-1].reason, comp[-1].detail, firstpub.level)

        # Readers
        indices.clear_caches()
        lv = indices.index_level(iid)
        assert lv["published"] and lv["level"] is not None
        assert lv["low"]["level"] <= lv["level"] <= lv["high"]["level"]
        ch = lv["changes"]
        assert ch["ytd"]["pct"] is None and "history does not cover" in ch["ytd"]["reason"], ch["ytd"]
        assert ch["all"]["pct"] is not None
        for w in ("24h", "7d", "30d", "90d"):
            assert (ch[w]["pct"] is None) == (ch[w]["reason"] is not None), (w, ch[w])
        if ch["24h"]["pct"] is not None:
            ref = next(r for r in rows if r.hour.isoformat() == ch["24h"]["from_hour"])
            assert abs(ch["24h"]["pct"] - (lv["level"] / ref.level - 1)) < 1e-12
        g = indices.index_level("gpu-compute")
        assert g["published"] and g["unit"] == "points"
        assert g["volatility"]["7d"]["annualized"] is not None, g["volatility"]
        assert g["changes"]["90d"]["pct"] is not None, g["changes"]["90d"]
        lst = indices.index_list()
        assert lst[0]["kind"] == "composite" and any(x["id"] == iid for x in lst)
        assert indices.index_level("nope") is None
        cn = indices.constituents_now(iid)
        assert cn["constituents"] and all("recorded_since" in c for c in cn["constituents"])
        hd = indices.index_history(iid, None, None, "1d")
        assert 115 <= len(hd) <= 122 and all(h["published_hours"] <= 24 for h in hd)
        assert hd[-1]["level"] == lv["level"] or not hd[-1]["published"]

        # Incremental refresh == full rebuild
        snap = {(r.index_id, r.hour): (r.published, r.level) for r in _all(Session)}
        with Session.begin() as s:
            s.execute(text("DELETE FROM index_levels WHERE hour >= :t"), {"t": max(h for _, h in snap) - timedelta(hours=10)})
        indices.refresh()
        again = {(r.index_id, r.hour): (r.published, r.level) for r in _all(Session)}
        assert again.keys() == snap.keys()
        assert all(again[k][0] == snap[k][0] and _close(again[k][1], snap[k][1]) for k in snap)
        r = indices.refresh(full=True)
        assert r["full"]

        # A failing feed: every snapshot in the hour before H failed -> excluded at H
        h = rows[-30].hour
        prov = next(d["provider"] for d in by_h[h].detail if d["status"] == "included")
        from tables import RawSnapshot
        with Session.begin() as s:
            s.add(RawSnapshot(provider=prov, endpoint="/synthetic", fetched_at=h - timedelta(minutes=20),
                              ok=False, status_code=503, payload=None))
        indices.refresh(full=True)
        after = {r.hour: r for r in _rows(Session, iid)}
        st = {d["provider"]: d["status"] for d in after[h].detail}
        assert st[prov] == "excluded_feed_down", st
        nxt = after.get(h + timedelta(hours=1))
        assert nxt is None or {d["provider"]: d["status"] for d in nxt.detail}.get(prov) != "excluded_feed_down"
    finally:
        sys.modules.pop("regions", None)
        scratchdb.drop(DB)


def _in_stock(Session):
    """The fixture flips stock at random, so about half the providers are sold out at any hour. Keep
    everything in stock so the readers have a published index to check; sell-outs are the rollup's
    concern and are tested there."""
    with Session.begin() as s:
        s.execute(text("UPDATE listing_observations SET available = true"))
        s.execute(text("UPDATE compute_listings SET available = true"))


def gpu_slug_id(gpu, suffix):
    return f"{indices.gpu_index_id(gpu)}.{suffix}"


def _all(Session):
    with Session() as s:
        return s.execute(text("SELECT index_id, hour, published, level FROM index_levels")).all()


def _close(a, b):
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 1e-9 * max(1, abs(b)))


def test_short_history():
    """Ten days of history: 30d / 90d / ytd changes and 30d volatility are null, with reasons."""
    sys.modules["regions"] = None  # as if regions.py did not exist: `import regions` raises ImportError
    url = scratchdb.create(DB_SHORT)
    try:
        Session = fixtures.session(url)
        fixtures.seed(Session, days=10)
        _in_stock(Session)
        normalize.SessionLocal = Session
        rollups.refresh()
        indices.clear_caches()
        with Session() as s:
            assert s.execute(text("SELECT DISTINCT methodology_version FROM index_levels")).scalars().all() == [
                indices.METHODOLOGY_VERSION]
        lv = indices.index_level("gpu-compute")
        assert lv["published"]
        for w in ("30d", "90d", "ytd"):
            assert lv["changes"][w]["pct"] is None and "history does not cover" in lv["changes"][w]["reason"], w
        assert lv["changes"]["24h"]["pct"] is not None
        assert lv["volatility"]["30d"]["annualized"] is None and "insufficient history" in lv["volatility"]["30d"]["reason"]
        reg = indices.index_level(gpu_slug_id(H100, "us"))
        assert not reg["published"] and "regions.py" in reg["reason"]
    finally:
        sys.modules.pop("regions", None)
        scratchdb.drop(DB_SHORT)


def test_endpoints():
    from fastapi.testclient import TestClient

    import main
    from analytics import stats

    url = scratchdb.create(DB_SHORT)
    try:
        Session = fixtures.session(url)
        fixtures.seed(Session, days=40)
        _in_stock(Session)
        normalize.SessionLocal = Session
        rollups.refresh()
        indices.clear_caches()
        stats.clear_caches()
        c = TestClient(main.app)  # no `with`: the lifespan (poller, migrations) does not run
        r = c.get("/v1/indices")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meta"]["methodology"] == "/methodology/indices" and body["data"]
        r = c.get("/v1/indices/h100-80gb-sxm5")
        assert r.status_code == 200 and r.json()["data"]["definition"]["unit"] == "usd_per_gpu_hour"
        assert c.get("/v1/indices/nope").status_code == 404
        r = c.get("/v1/indices/gpu-compute/history", params={"window": "7d"})
        assert r.status_code == 200 and r.json()["data"]["resolution"] == "1h" and r.json()["data"]["points"]
        r = c.get("/v1/indices/gpu-compute/history", params={"window": "all", "resolution": "1d"})
        assert r.status_code == 200 and 39 <= len(r.json()["data"]["points"]) <= 42
        assert c.get("/v1/indices/gpu-compute/history", params={"window": "5y"}).status_code == 422
        r = c.get("/v1/history/h100-80gb-sxm5", params={"window": "7d"})
        d = r.json()["data"]
        assert r.status_code == 200 and d["series"] and d["index"]["points"] and d["gpu"] == H100
        assert c.get("/v1/history/not-a-gpu").status_code == 404
        r = c.get("/v1/markets/h100-80gb-sxm5/context")
        assert r.status_code == 200 and r.json()["meta"]["methodology"] == "/methodology/historical-context"
        r = c.get("/v1/providers/syn_alpha/gpus/h100-80gb-sxm5/context")
        assert r.status_code == 200 and r.json()["data"]["provider"] == "syn_alpha", r.text
        assert c.get("/v1/providers/nobody/gpus/h100-80gb-sxm5/context").status_code == 404
        r = c.get("/v1/providers/syn_alpha/listings/context", params={"listing_id": f"syn_alpha:{H100}"})
        assert r.status_code in (200, 404)
    finally:
        scratchdb.drop(DB_SHORT)


if __name__ == "__main__":
    for t in (test_robust_stat, test_screen, test_min_coverage, test_chain_linking_late_joiner, test_composite,
              test_pipeline, test_short_history, test_endpoints):
        t(); print(t.__name__, "ok")
