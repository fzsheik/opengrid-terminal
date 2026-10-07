"""Homepage overview (movers), opportunity monitor and the /v1 endpoints, on crafted markets.

Run:  .venv/Scripts/python tests/test_opportunities.py
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from test_events import A100, B200, H100, H200, L40S, World  # noqa: E402
from analytics import events, movers, opportunities, rollups  # noqa: E402

DB = "og_test_events_opps"
RTX, MI300 = "NVIDIA RTX 4090 24GB", "AMD Instinct MI300X 192GB"
rollups.AFTER_REFRESH.clear()


def _clear():
    movers.overview.cache_clear()
    opportunities.monitor.cache_clear()


def _world():
    w = World(days=40)
    S = w.start
    for p in ("syn_a", "syn_b", "syn_c"):
        w.fetch(p, S)
        w.fetch(p, w.now - timedelta(minutes=2))
    # H100: b and c both cut 20% at T-6h -> market median 2.50 -> 2.00.
    w.listing("syn_a", H100, [(S, 2.00, True)])
    w.listing("syn_b", H100, [(S, 2.50, True), (w.at(6), 2.00, True)])
    w.listing("syn_c", H100, [(S, 3.00, True), (w.at(6), 2.40, True)])
    # A100: syn_a has 6 regional listings; 5 sell out at T-3h -> availability halves vs its norm.
    for i, reg in enumerate(("us-east", "us-west", "eu-west", "eu-north", "ap-south", "ap-east")):
        steps = [(S, 1.00 + i * 0.01, True)] + ([(w.at(3), 1.00 + i * 0.01, False)] if i else [])
        w.listing("syn_a", A100, steps, region=reg)
    w.listing("syn_b", A100, [(S, 1.20, True)])
    w.listing("syn_c", A100, [(S, 1.40, True)])
    # L40S: new cheap listing from syn_c two hours ago.
    w.listing("syn_a", L40S, [(S, 1.00, True)])
    w.listing("syn_b", L40S, [(S, 1.20, True)])
    w.listing("syn_c", L40S, [(w.at(2), 0.80, True)])
    # B200: syn_a (usually 20% under the median) halves its price at T-1h -> spread 200%.
    w.listing("syn_a", B200, [(S, 4.00, True), (w.at(1), 2.00, True)])
    w.listing("syn_b", B200, [(S, 5.00, True)])
    w.listing("syn_c", B200, [(S, 6.00, True)])
    # RTX 4090: genuinely new on the market 2 days ago (syn_c already tracked).
    w.listing("syn_c", RTX, [(w.T - timedelta(days=2), 0.40, True)])
    # MI300X: only syn_b, sold out since T-3h.
    w.listing("syn_b", MI300, [(S, 2.20, True), (w.at(3), 2.20, False)])
    # H200: only syn_eps, a provider we began recording 12h ago. Not new on the market, not an opportunity.
    w.fetch("syn_eps", w.T - timedelta(hours=12))
    w.fetch("syn_eps", w.now - timedelta(minutes=2))
    w.listing("syn_eps", H200, [(w.T - timedelta(hours=12), 2.00, True)])
    w.listing("syn_eps", L40S, [(w.T - timedelta(hours=12), 0.50, True)])
    return w


def _setup(w=None):
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    if w is not None:
        w.save(Session)
    normalize.SessionLocal = Session
    rollups.refresh()
    events.run()
    _clear()
    return Session


def test_thin_data():
    """No history at all: every section says why it is unavailable instead of inventing numbers."""
    try:
        _setup(None)
        o = movers.overview()
        assert o["as_of_hour"] is None
        for k in ("gainers", "losers", "volatile", "unusual", "sold_out", "newly_available"):
            assert o[k]["available"] is False and o[k]["reason"], (k, o[k])
        m = opportunities.monitor()
        assert m["items"] == [] and {u["type"] for u in m["unavailable"]} == set(opportunities.TYPES)
        assert movers.tape() == []
    finally:
        scratchdb.drop(DB)


def test_short_history():
    """6 hours of data: board and liquidity work, 24h/7d/30d statistics are unavailable with reasons."""
    w = World(days=0.25)
    for p in ("syn_a", "syn_b"):
        w.fetch(p, w.start)
    w.listing("syn_a", H100, [(w.start, 2.0, True)])
    w.listing("syn_b", H100, [(w.start, 2.5, True)])
    try:
        _setup(w)
        o = movers.overview()
        assert o["board"]["available"] and o["board"]["items"][0]["gpu"] == H100
        assert o["board"]["items"][0]["change_24h_median"] is None
        assert not o["gainers"]["available"] and "24h" in o["gainers"]["reason"]
        assert not o["volatile"]["available"] and not o["unusual"]["available"]
        # GPUs on sale when coverage started are not "newly available".
        assert o["newly_available"]["items"] == [] and o["newly_available"]["note"]
        m = opportunities.monitor()
        assert {"price_cut", "supply_change", "scarcity", "below_usual_premium"} <= {u["type"] for u in m["unavailable"]}
    finally:
        scratchdb.drop(DB)


def test_overview_and_opportunities():
    w = _world()
    try:
        _setup(w)
        o = movers.overview()
        assert o["history_hours"] >= 24 * 39

        losers = o["losers"]["items"]
        assert losers and losers[0]["gpu"] == H100 and abs(losers[0]["median_pct"] + 0.2) < 1e-9, losers
        assert losers[0]["providers_matched"] == 3 and losers[0]["gpu_slug"] == "h100-80gb-sxm5"
        assert all(x["gpu"] != H100 for x in o["gainers"]["items"])

        assert [x["gpu"] for x in o["sold_out"]["items"]] == [MI300], o["sold_out"]
        assert o["sold_out"]["items"][0]["last_lowest"] == 2.2

        new = [x["gpu"] for x in o["newly_available"]["items"]]
        assert new == [RTX], new  # H200 (syn_eps, coverage start) is not new on the market

        assert o["volatile"]["available"] and o["unusual"]["available"]
        lose = {x["gpu"]: x for x in o["availability_losing"]["items"]}
        assert lose[A100]["delta_listings"] == -5, lose
        assert o["capacity"]["gpus_sold_out_everywhere"] == 1
        changes = o["price_changes"]["items"]
        assert {(x["provider"], x["gpu"]) for x in changes} >= {("syn_b", H100), ("syn_c", H100), ("syn_a", B200)}
        assert all(x["status"] in ("up", "down") for x in changes)
        tape = o["tape"]["items"]
        assert tape and all(x["type"] != "coverage_started" for x in tape)

        m = opportunities.monitor()
        by = {}
        for x in m["items"]:
            by.setdefault(x["type"], []).append(x)
            assert set(x) >= {"type", "score", "explanation", "numbers", "gpu_slug", "kind", "links"}
            assert x["kind"] in ("observed", "inferred") and 0 <= x["score"] <= 100
        cuts = {(x["provider"], x["gpu"]) for x in by["price_cut"]}
        assert cuts == {("syn_b", H100), ("syn_c", H100), ("syn_a", B200)}, cuts
        assert [(x["provider"], x["gpu"]) for x in by["new_cheap_inventory"]] == [("syn_c", L40S)], by.get("new_cheap_inventory")
        assert any(x["gpu"] == B200 and x["provider"] == "syn_a" for x in by["wide_spread"]), by.get("wide_spread")
        bu = [x for x in by["below_usual_premium"] if x["gpu"] == B200]
        assert bu and bu[0]["provider"] == "syn_a" and abs(bu[0]["numbers"]["premium_30d"] + 0.2) < 0.01, bu
        assert [x["gpu"] for x in by["scarcity"]] == [A100], by.get("scarcity")
        assert [(x["provider"], x["gpu"]) for x in by.get("new_cheapest_provider", [])] == [("syn_c", L40S)], by.get("new_cheapest_provider")
        assert not [x for x in m["items"] if x["provider"] == "syn_eps"], "late joiner is not an opportunity"
        assert m["items"] == sorted(m["items"], key=lambda x: (-x["score"], x["type"], x["gpu"] or "", x["provider"] or ""))

        f = opportunities.opportunities(types=["price_cut"], gpu=H100)
        assert f["total"] == 2 and all(x["type"] == "price_cut" and x["gpu"] == H100 for x in f["items"])

        _endpoints()
    finally:
        scratchdb.drop(DB)


def _endpoints():
    from fastapi.testclient import TestClient
    import main

    c = TestClient(main.app)  # no `with`: the lifespan (migrations, poller) does not run
    r = c.get("/v1/events", params={"gpu": "h100-80gb-sxm5", "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meta"]["methodology"] == "/methodology/events" and body["meta"]["pagination"]["limit"] == 5
    assert all(e["gpu"] == H100 for e in body["data"])
    r = c.get("/v1/events", params={"type": "price_move,sold_out", "min_severity": "notable"})
    assert r.status_code == 200 and all(e["type"] in ("price_move", "sold_out") and e["severity"] != "info"
                                        for e in r.json()["data"])
    assert c.get("/v1/events", params={"type": "nope"}).status_code == 422
    assert c.get("/v1/events", params={"gpu": "not-a-gpu"}).status_code == 404
    t = c.get("/v1/events/types").json()["data"]
    assert {x["type"] for x in t["events"]} == set(events.TYPES) and all(x["severity"] for x in t["events"])
    o = c.get("/v1/overview").json()
    assert o["data"]["losers"]["items"][0]["gpu"] == H100 and o["meta"]["cache_seconds"] == 60
    op = c.get("/v1/opportunities", params={"type": "price_cut", "gpu": "h100-80gb-sxm5"}).json()
    assert op["data"]["total"] == 2 and op["meta"]["methodology"] == "/methodology/opportunities"
    tp = c.get("/v1/tape", params={"limit": 3}).json()
    assert len(tp["data"]) <= 3


if __name__ == "__main__":
    for t in (test_thin_data, test_short_history, test_overview_and_opportunities):
        t(); print(t.__name__, "ok")
