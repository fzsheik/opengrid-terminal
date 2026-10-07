"""Routing benchmark: deterministic, crafted market states -> the EXACT routing decision, exclusion codes and
explanation. Table-driven: every row of SCENARIOS builds its own market (compute_listings for the bench GPUs),
execution controls, account limits and fake-provider behaviour, runs one engine entry point (rank / preview /
route / preview->launch / route->approve / HTTP) and checks the outcome.

Tie-break (documented in methodology/best-execution.md): CHEAPEST orders by (price, data age, provider,
listing_id); every other mode by (-score, price, data age, provider, listing_id). Two listings exactly tied on
price and freshness therefore resolve to the alphabetically first provider, then listing id: deterministic.

Scratch DB og_test_bench; fake providers only (tests/bench_fixtures.Sim). No network.
Run:  .venv/Scripts/python tests/test_routing_benchmark.py
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_fixtures as bf  # noqa: E402  (sets env + sys.path)
from bench_fixtures import Sim, listing  # noqa: E402

import normalize  # noqa: E402
from accounts.auth import OPERATOR  # noqa: E402
from analytics import rollups  # noqa: E402
from config import settings  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routing import adapters, control, deployments, engine, guards, quotes, scoring  # noqa: E402
from sqlalchemy import text  # noqa: E402

DB = "og_test_bench"
G = "NVIDIA H100 80GB SXM5"
G2 = "NVIDIA H100 80GB PCIe"
GM = "NVIDIA A100 80GB SXM4"          # the routing-modes market (with 40 days of history)
PROVIDERS = ("syn_a", "syn_b", "syn_c", "syn_d", "syn_e")
MODE_PROVIDERS = ("m_cheap", "m_volatile", "m_steady", "m_pricey")     # m_nolevel has no adapter (level 0)
T = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)


def L(provider, price, **kw):
    kw.setdefault("at", T)
    return listing(provider, price, gpu=kw.pop("gpu", G), **kw)


def default_market():
    return [L("syn_a", 1.00), L("syn_b", 1.10), L("syn_c", 1.20)]


def spec(**kw):
    base = {"gpu": G, "count": 1, "region_group": None, "max_price_per_gpu_hour": None, "duration_hours": 2,
            "deadline_hours": None, "mode": "CHEAPEST", "weights": None, "preferences": {}, "launch": None,
            "strict_region": False}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# result helpers
# --------------------------------------------------------------------------

def excl(out) -> dict:
    """{provider: code} of the exclusions (first per provider)."""
    d = {}
    for e in out["exclusions"]:
        d.setdefault(e["provider"], e["code"])
    return d


def cons(out) -> dict:
    d = {}
    for c in out.get("considered") or []:
        d.setdefault(c["provider"], c)
    return d


def sel(out):
    s = out.get("selected")
    return s["provider"] if s else None


def provisioned():
    return [c[1] for c in Sim.calls("provision")]


def refused_with(fn, code):
    try:
        fn()
    except HTTPException as e:
        assert isinstance(e.detail, dict) and e.detail.get("code") == code, (code, e.status_code, e.detail)
        return e
    raise AssertionError(f"expected a refusal {code}")


# --------------------------------------------------------------------------
# scenario runner
# --------------------------------------------------------------------------

class Ctx:
    def __init__(self, sc):
        self.sc = sc
        self.acct = bf.fresh_account()
        self.who = bf.key(self.acct)

    def route(self, **kw):
        return engine.route(spec(**{**self.sc.get("spec", {}), **kw}), self.who)

    def preview(self, **kw):
        return engine.preview(spec(**{**self.sc.get("spec", {}), **kw}), self.who)


def prepare(sc) -> Ctx:
    Sim.reset()
    settings.routing_max_attempts = 1
    settings.route_live_check_candidates = 3
    settings.quote_price_tolerance = 0.02
    for p in PROVIDERS:
        adapters.register(p, Sim)
    bf.market([G, G2], sc.get("market") or default_market())
    bf.mode(sc.get("mode", "LIVE"), env=sc.get("env", True))
    for p in PROVIDERS:
        bf.flags(p, **(sc.get("flags") or {}).get(p, {}))
    ctx = Ctx(sc)
    if sc.get("limits"):
        guards.set_limits(ctx.acct, sc["limits"], by="test", reason="bench")
    for k, v in (sc.get("sim") or {}).items():
        getattr(Sim, k).update(v)
    return ctx


def S(name, check, **setup):
    return {"name": name, "check": check, **setup}


# --------------------------------------------------------------------------
# checks (one per scenario)
# --------------------------------------------------------------------------

def c_cheapest_unavailable_live(ctx):
    code, out = ctx.route()
    assert cons(out)["syn_a"]["outcome"] == "unavailable" and "sold out" in cons(out)["syn_a"]["reason"]
    assert out["status"] == "provisioned" and provisioned() == ["syn_b"], (out["status"], provisioned())
    assert out["deployment"]["provider"] == "syn_b" and out["quote"]["quote_price_per_gpu_hour"] == 1.10


def c_cheapest_sold_out_in_market(ctx):
    out = ctx.preview()
    assert excl(out) == {"syn_a": "sold_out"} and sel(out) == "syn_b", (excl(out), sel(out))
    assert out["quote_record"]["provider"] == "syn_b" and not Sim.CALLS, "preview never calls a provider"


def c_cheapest_stale(ctx):
    out = ctx.preview()
    e = next(x for x in out["exclusions"] if x["provider"] == "syn_a")
    assert e["code"] == "stale" and "3.0 h ago" in e["reason"] and "past 38 min" in e["reason"], e
    assert sel(out) == "syn_b" and out["exclusions_by_code"] == {"stale": 1}


def c_stale_boundary_inside(ctx):
    out = ctx.preview()
    assert sel(out) == "syn_a", "2200 s old is inside stale_after (2250 s): still a candidate"
    f = out["selected"]["factors"]["freshness"]
    assert 0 < f["value"] < 0.05 and f["note"].startswith("aging"), f
    assert out["alternatives"][0]["provider"] == "syn_b"


def c_stale_boundary_outside(ctx):
    out = ctx.preview()
    assert excl(out).get("syn_a") == "stale" and sel(out) == "syn_b"


def c_market_over_ceiling(ctx):
    code, out = ctx.route()
    assert out["status"] == "no_candidates" and out["selected"] is None
    assert out["exclusions_by_code"] == {"over_max_price": 3}
    out = ctx.preview()
    assert set(excl(out).values()) == {"over_max_price"} and len(out["exclusions"]) == 3
    assert "$1.00/GPU-h is over the $0.95 maximum" in next(e["reason"] for e in out["exclusions"] if e["provider"] == "syn_a")
    assert not Sim.CALLS


def c_live_quote_over_ceiling(ctx):
    code, out = ctx.route()
    c = cons(out)["syn_a"]
    assert c["outcome"] == "over_max_price" and "$1.30/GPU-h is over $1.15" in c["reason"], c
    assert provisioned() == ["syn_b"] and out["status"] == "provisioned"


def c_region_strict_unavailable(ctx):
    code, out = ctx.route()
    assert out["status"] == "no_candidates"
    assert out["exclusions_by_code"] == {"wrong_region": 1, "region_unconfirmed": 1}, out["exclusions_by_code"]
    assert excl(ctx.preview()) == {"syn_a": "wrong_region", "syn_b": "region_unconfirmed"}
    assert not Sim.CALLS


def c_region_nonstrict_unavailable(ctx):
    out = ctx.preview()
    assert excl(out) == {"syn_a": "wrong_region"}, "a listing confirmed OUTSIDE the region is never used"
    assert sel(out) == "syn_b", "non-strict: a listing of unknown location is kept"
    assert "location unknown: not confirmed to be in Europe" in out["selected"]["explanation"]
    assert out["selected"]["factors"]["region_match"]["value"] == 0.5


def c_region_confirmed_preferred_balanced(ctx):
    out = ctx.preview()
    assert sel(out) == "syn_c" and "region matched (Europe)" in out["selected"]["explanation"], sel(out)
    alt = out["alternatives"][0]
    assert alt["provider"] == "syn_b" and alt["factors"]["region_match"]["value"] == 0.5
    w = out["weights"]
    assert w["region_match"] > 0 and abs(sum(w.values()) - 1) < 1e-4


def c_exact_tie(ctx):
    winners = set()
    for _ in range(5):
        r = scoring.rank_listings(G, mode="CHEAPEST")
        winners.add(tuple(c["provider"] for c in r["candidates"]))
    assert winners == {("syn_a", "syn_b", "syn_c")}, winners
    out = ctx.preview()
    assert sel(out) == "syn_a" and out["alternatives"][0]["vs_selected"].startswith("same price")
    bal = scoring.rank_listings(G, mode="BALANCED")
    assert [c["provider"] for c in bal["candidates"]][:2] == ["syn_a", "syn_b"]
    assert bal["candidates"][0]["score"] == bal["candidates"][1]["score"], "a true tie in score"


def c_tie_broken_by_freshness(ctx):
    r = scoring.rank_listings(G, mode="CHEAPEST")
    assert [c["provider"] for c in r["candidates"]][:2] == ["syn_b", "syn_a"], "same price: fresher data first"


def c_provider_api_down(ctx):
    code, out = ctx.route()
    c = cons(out)["syn_a"]
    assert c["step"] == "availability_check" and c["outcome"] == "error" and c["error_kind"] == "server", c
    assert provisioned() == ["syn_b"] and out["status"] == "provisioned"


def c_provider_adapter_crash(ctx):
    code, out = ctx.route()
    c = cons(out)["syn_a"]
    assert c["outcome"] == "error" and c["error_kind"] == "unknown_state", c
    assert provisioned() == ["syn_b"]


def c_everyone_stale(ctx):
    pv = ctx.preview()
    assert pv["selected"] is None and pv["quote_record"] is None
    assert pv["reason"] == "no eligible listing satisfies the request; see exclusions"
    assert pv["exclusions_by_code"] == {"stale": 3}
    code, out = ctx.route()
    assert out["status"] == "no_candidates" and out["reason"] == "no eligible listing satisfies the request"
    assert out["deployment"] is None and not Sim.CALLS


def c_no_provider_meets_requirements(ctx):
    code, out = ctx.route()
    assert out["status"] == "no_candidates"
    assert out["exclusions_by_code"] == {"availability_unknown": 2, "over_max_price": 1}, out["exclusions_by_code"]
    assert not Sim.CALLS


def c_only_interruptible(ctx):
    code, out = ctx.route()
    assert out["status"] == "no_candidates" and out["exclusions_by_code"] == {"not_on_demand": 2}
    assert all("not on-demand" in e["reason"] for e in ctx.preview()["exclusions"])
    assert not Sim.CALLS, "spot capacity is never silently substituted for on-demand"


def c_ineligible_tiers(ctx):
    out = ctx.preview()
    assert excl(out) == {"syn_a": "single_host_ask", "syn_b": "floor_price"}, excl(out)
    assert sel(out) == "syn_c"


def c_shape_8_vs_4(ctx):
    code, out = ctx.route()
    assert out["status"] == "no_candidates" and out["selected"] is None and not Sim.CALLS
    pv = ctx.preview()
    assert excl(pv) == {"syn_c": "wrong_count"}
    multi = pv["multi_instance_alternatives"]
    assert [m["provider"] for m in multi] == ["syn_a", "syn_b"] and all(m["instances"] == 2 for m in multi)
    assert "not provisioned automatically" in multi[0]["note"] and pv["quote_record"] is None
    assert not Sim.CALLS, "multi-instance alternatives are listed, never provisioned"


def c_modes(ctx):
    expect = {"CHEAPEST": "m_nolevel", "FASTEST_AVAILABLE": "m_volatile", "BALANCED": "m_nolevel",
              "MOST_STABLE": "m_nolevel"}
    for m, winner in expect.items():
        r = scoring.rank_listings(GM, mode=m)
        _documented_order(r)
        assert r["candidates"][0]["provider"] == winner, (m, [(c["provider"], c["score"]) for c in r["candidates"]])
    r = scoring.rank_listings(GM, mode="USER_DEFINED", weights={"availability_persistence": 1, "integration_level": 1})
    _documented_order(r)
    assert r["candidates"][0]["provider"] == "m_volatile", [(c["provider"], c["score"]) for c in r["candidates"]]
    assert r["weights"]["availability_persistence"] == 0.5 and r["weights"]["integration_level"] == 0.5
    # the route launches the best PROVISIONABLE candidate; the level-0 winner is skipped with the reason
    for p in MODE_PROVIDERS:
        bf.flags(p)
    for m, launched in (("CHEAPEST", "m_cheap"), ("MOST_STABLE", "m_steady"), ("FASTEST_AVAILABLE", "m_volatile")):
        Sim.CALLS.clear()
        code, out = engine.route(spec(gpu=GM, mode=m), bf.key(bf.fresh_account()))
        assert provisioned() == [launched], (m, provisioned(), out["considered"])
        if out["selected"]["provider"] == "m_nolevel":
            assert cons(out)["m_nolevel"]["step"] == "capability" and "integration level 0" in cons(out)["m_nolevel"]["reason"]


def _documented_order(r):
    """Every score is sum(weight x value) (imputed 0.5 for a weighted factor without data) and the order is the
    documented one."""
    for c in r["candidates"]:
        total = 0.0
        for name, f in c["factors"].items():
            w = r["weights"].get(name, 0.0)
            if w > 0:
                total += w * (0.5 if f["value"] is None else f["value"])
        assert abs(total - c["score"]) < 2e-4, (c["provider"], total, c["score"])
    cs = r["candidates"]
    if r["mode"] == "CHEAPEST":
        keys = [(c["price_per_gpu_hour"], c["age_seconds"], c["provider"], c["listing_id"]) for c in cs]
    else:
        keys = [(-c["score"], c["price_per_gpu_hour"], c["age_seconds"], c["provider"], c["listing_id"]) for c in cs]
    assert keys == sorted(keys), keys


def c_family_needs_variant(ctx):
    main, c = _client()
    try:
        _as(main, ctx.who)
        for path in ("/v1/route/preview", "/v1/route"):
            r = c.post(path, json={"gpu": "h100", "count": 1, "mode": "cheapest"}, headers={"Idempotency-Key": "fam"})
            assert r.status_code == 422, (path, r.status_code, r.text)
            d = r.json()["detail"]
            assert d["code"] == "family_needs_variant" and d["family"] == "H100"
            assert "h100-80gb-sxm5" in {v["slug"] for v in d["variants"]}
        r = c.post("/v1/route/preview", json={"gpu": "h100", "allow_variants": True, "count": 1, "mode": "cheapest"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["family"] == "H100" and data["by_variant"], data.keys()
        assert data["market"] == next(v["market"] for v in data["by_variant"] if v["gpu"] == G2),             "the market shown is the selected variant's own, never a family-wide price"
        assert data["selected"]["variant"] == G2 and data["selected"]["provider"] == "syn_b"
        assert {a["variant"] for a in data["alternatives"]} == {G}
        assert not Sim.CALLS
    finally:
        main.app.dependency_overrides.clear()


def c_unvalidated_cheapest_route(ctx):
    pv = ctx.preview()
    assert sel(pv) == "syn_a" and pv["quote_record"]["provider"] == "syn_a", "preview may show it"
    code, out = ctx.route()
    c = cons(out)["syn_a"]
    assert c["step"] == "execution_control" and "not validated" in c["reason"], c
    assert provisioned() == ["syn_b"], "an unvalidated adapter never provisions customer compute"


def c_unvalidated_quote_launch(ctx):
    pv = ctx.preview()
    code, out = ctx.route(quote_id=pv["quote_record"]["quote_id"])
    assert out["status"] == "not_provisioned" and "not validated" in out["reason"] and out["deployment"] is None
    assert not Sim.calls("provision") and not Sim.calls("check")


def c_unvalidated_approval_refused(ctx):
    code, out = ctx.route()
    assert out["status"] == "pending_approval" and out["deployment"]["provider"] == "syn_a"
    control.set_provider_flags("syn_a", reason="bench: demote", by="test", adapter_status="simulated")
    refused_with(lambda: engine.approve(out["route_request_id"], OPERATOR, quote_id=out["quote"]["quote_id"]),
                 "launch_not_permitted")
    assert not Sim.calls("provision")


def c_killed_provider(ctx):
    code, out = ctx.route()
    c = cons(out)["syn_a"]
    assert c["step"] == "execution_control" and "killed" in c["reason"] and "bench: provider incident" in c["reason"]
    assert provisioned() == ["syn_b"]


def c_kill_all(ctx):
    control.kill_all("bench drill", "test")
    code, out = ctx.route()
    assert out["status"] == "not_provisioned" and out["execution_mode"] == "DISABLED" and not Sim.calls("provision")


def _limit_check(field):
    def check(ctx):
        pv = ctx.preview()
        codes = [v["code"] for v in pv["limit_violations"]]
        assert field in codes, (field, pv["limit_violations"])
        s = pv["selected"]
        assert field in [v["code"] for v in s["limit_violations"]], s.get("limit_violations")
        assert any(e["provider"] == s["provider"] and field in e["limits"] for e in pv["limit_exclusions"])
        code, out = ctx.route()
        assert out["status"] == "pending_approval" and not Sim.calls("provision"), "the route agrees: no launch"
        assert field in [v["code"] for v in out["deployment"]["limit_violations"]]
    return check


def c_allowlist_preview(ctx):
    _limit_check("provider_allowlist")(ctx)
    pv = ctx.preview()
    b = next(a for a in pv["alternatives"] if a["provider"] == "syn_b")
    assert b["limit_violations"] == [], "the allowlisted provider is clean"


def _quote_flow(between, expect):
    def check(ctx):
        pv = ctx.preview()
        qid = pv["quote_record"]["quote_id"]
        assert pv["quote_record"]["price_source"] == "observed" and pv["quote_record"]["provider"] == "syn_a"
        between(ctx, qid)
        expect(ctx, qid)
    return check


def _price(p):
    def between(ctx, qid):
        Sim.PRICE["syn_a"] = p
    return between


def e_launch_ok(ctx, qid):
    code, out = ctx.route(quote_id=qid)
    assert out["status"] == "provisioned" and provisioned() == ["syn_a"], out
    assert quotes.get(qid)["status"] == "consumed"


def e_requote(new_price):
    def expect(ctx, qid):
        e = refused_with(lambda: ctx.route(quote_id=qid), "quote_invalid")
        nq = e.detail["new_quote"]
        assert nq and nq["quote_price_per_gpu_hour"] == new_price and nq["price_source"] == "live_check"
        assert "over the 2% tolerance" in e.detail["message"] and not Sim.calls("provision")
        assert quotes.get(qid)["status"] == "superseded"
        code, out = ctx.route(quote_id=nq["quote_id"])          # re-approval of the new quote launches
        assert out["status"] == "provisioned" and len(Sim.calls("provision")) == 1
    return expect


def b_expire(ctx, qid):
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE quotes SET expires_at = now() - interval '1 second' WHERE id = :q"), {"q": qid})


def e_expired(ctx, qid):
    e = refused_with(lambda: ctx.route(quote_id=qid), "quote_expired")
    assert e.detail["new_quote"]["quote_id"] != qid and not Sim.calls("provision"), "never launch on an expired quote"


def b_remap(ctx, qid):
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE compute_listings SET canonical_gpu_name = :g WHERE provider = 'syn_a'"), {"g": G2})


def e_mapping_refused(ctx, qid):
    e = refused_with(lambda: ctx.route(quote_id=qid), "quote_invalid")
    assert "GPU mapping changed" in e.detail["message"] and G2 in e.detail["message"], e.detail
    assert e.detail["new_quote"] is None and not Sim.calls("provision") and not Sim.calls("check")
    assert quotes.get(qid)["status"] == "expired"


def b_check(mode_):
    def between(ctx, qid):
        Sim.CHECK["syn_a"] = mode_
    return between


def e_gone(reason):
    def expect(ctx, qid):
        e = refused_with(lambda: ctx.route(quote_id=qid), "quote_invalid")
        assert reason in e.detail["message"] and e.detail["new_quote"] is None, e.detail
        assert not Sim.calls("provision")
    return expect


def c_provider_unregistered(ctx):
    pv = ctx.preview()
    qid = pv["quote_record"]["quote_id"]
    adapters.unregister("syn_a")
    try:
        refused_with(lambda: ctx.route(quote_id=qid), "no_credentials")
    finally:
        adapters.register("syn_a", Sim)
    assert not Sim.calls("provision")


def c_quote_single_use_and_owner(ctx):
    pv = ctx.preview()
    qid = pv["quote_record"]["quote_id"]
    code, out = ctx.route(quote_id=qid)
    assert out["status"] == "provisioned"
    refused_with(lambda: ctx.route(quote_id=qid), "quote_invalid")
    refused_with(lambda: engine.route(spec(quote_id=qid), bf.key(bf.fresh_account())), "quote_not_found")
    assert len(Sim.calls("provision")) == 1


def c_no_data_factor(ctx):
    try:
        scoring.rank_listings(G, mode="USER_DEFINED", weights={"reliability": 1})
        raise AssertionError("weighting a factor with no data must be refused")
    except scoring.ScoringError as e:
        assert "no data" in str(e)


def c_selected_not_provisionable(ctx):
    pv = ctx.preview()
    assert sel(pv) == "syn_z" and pv["can_provision_selected"] is False
    assert pv["best_provisionable"]["provider"] == "syn_a" and pv["quote_record"]["provider"] == "syn_a"
    assert "market data only: OpenGrid cannot provision here" in pv["selected"]["explanation"]
    code, out = ctx.route()
    assert cons(out)["syn_z"]["step"] == "capability" and provisioned() == ["syn_a"]


def c_require_level(ctx):
    out = ctx.preview()
    assert excl(out) == {"syn_z": "below_required_level"} and sel(out) == "syn_a"


def c_exclude_preference(ctx):
    out = ctx.preview()
    assert excl(out) == {"syn_a": "excluded_by_preference"} and sel(out) == "syn_b"


def c_failover_definitive(ctx):
    code, out = ctx.route()
    assert out["status"] == "provisioned" and provisioned() == ["syn_a", "syn_b"] and len(out["deployments"]) == 2
    first = deployments.load_row(out["deployments"][0])
    assert first.status == "provision_failed" and first.provider == "syn_a"


def c_no_failover_ambiguous(ctx):
    code, out = ctx.route()
    assert out["status"] == "provider_timeout" and provisioned() == ["syn_a"] and code == 202
    assert "NOT failing over" in out["reason"]


def c_latency_limit(ctx):
    code, out = ctx.route()
    assert len(Sim.calls("check")) == 2 and any(c["outcome"] == "not_checked" for c in out["considered"])
    assert out["status"] == "not_provisioned"


def c_preview_only(ctx):
    code, out = ctx.route()
    assert out["status"] == "not_provisioned" and out["reason"] == "execution mode is PREVIEW_ONLY"
    assert out["deployment"] is None and out["quote"]["provider"] == "syn_a" and not Sim.calls("provision")


def c_env_ceiling(ctx):
    code, out = ctx.route()
    assert out["execution_mode"] == "PREVIEW_ONLY" and out["reason"] == engine.NOT_LIVE and not Sim.calls("provision")


def c_supervised(ctx):
    code, out = ctx.route()
    assert code == 202 and out["status"] == "pending_approval" and not Sim.calls("provision")
    ap = out["approval"]
    assert ap["provider"] == "syn_a" and ap["quote_price_per_gpu_hour"] == 1.0 and ap["est_total_cost"] == 2.0
    assert ap["body"]["quote_id"] == out["quote"]["quote_id"] and ap["limit_violations"] == []


def c_explanation(ctx):
    out = ctx.preview()
    ex = out["selected"]["explanation"]
    for part in ("9% below current market median ($1.10)", "available now", "fresh", "availability history insufficient",
                 "OpenGrid can provision via API (level 3)"):
        assert part in ex, (part, ex)
    assert out["alternatives"][0]["vs_selected"].startswith("+10% more expensive"), out["alternatives"][0]["vs_selected"]
    assert out["market"]["median"] == 1.10 and out["market"]["low_provider"] == "syn_a"
    q = out["quote"]
    assert q["kind"] == "quote" and q["basis"] == "observed_listing" and q["expected_cost_usd"] == 2.0
    assert q["savings_vs_median"]["per_gpu_hour"] == 0.1


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------

AGE = {"stale3h": timedelta(hours=3), "in": timedelta(seconds=2200), "out": timedelta(seconds=2300)}

SCENARIOS = [
    S("cheapest unavailable on live check -> next provider", c_cheapest_unavailable_live,
      sim={"CHECK": {"syn_a": "unavailable"}}),
    S("cheapest sold out in market data -> excluded sold_out", c_cheapest_sold_out_in_market,
      market=[L("syn_a", 1.00, avail=False), L("syn_b", 1.10), L("syn_c", 1.20)]),
    S("cheapest stale (past stale_after) -> excluded stale", c_cheapest_stale,
      market=[L("syn_a", 0.50, age=AGE["stale3h"]), L("syn_b", 1.10), L("syn_c", 1.20)]),
    S("stale boundary: 2200 s old is still a candidate (aging)", c_stale_boundary_inside,
      market=[L("syn_a", 1.00, age=AGE["in"]), L("syn_b", 1.10)]),
    S("stale boundary: 2300 s old is excluded", c_stale_boundary_outside,
      market=[L("syn_a", 1.00, age=AGE["out"]), L("syn_b", 1.10)]),
    S("market prices over the request's ceiling -> no route", c_market_over_ceiling,
      spec={"max_price_per_gpu_hour": 0.95}),
    S("cheapest's LIVE quote over the ceiling -> next provider", c_live_quote_over_ceiling,
      spec={"max_price_per_gpu_hour": 1.15}, sim={"PRICE": {"syn_a": 1.30}}),
    S("region unavailable, strict -> no route (wrong_region, region_unconfirmed)", c_region_strict_unavailable,
      market=[L("syn_a", 1.00), L("syn_b", 1.10, region=None, country=None)],
      spec={"region_group": "Europe", "strict_region": True}),
    S("region unavailable, non-strict -> unknown-location listing, never a confirmed-wrong one",
      c_region_nonstrict_unavailable,
      market=[L("syn_a", 1.00), L("syn_b", 1.10, region=None, country=None)], spec={"region_group": "Europe"}),
    S("region available: BALANCED prefers the confirmed match", c_region_confirmed_preferred_balanced,
      market=[L("syn_a", 1.00), L("syn_b", 1.10, region=None, country=None), L("syn_c", 1.20, region="fra", country="DE")],
      spec={"region_group": "Europe", "mode": "BALANCED"}),
    S("two providers exactly tied -> alphabetical provider (deterministic)", c_exact_tie,
      market=[L("syn_c", 1.20), L("syn_b", 1.00), L("syn_a", 1.00)]),
    S("same price, fresher data wins", c_tie_broken_by_freshness,
      market=[L("syn_a", 1.00, age=timedelta(seconds=120)), L("syn_b", 1.00, age=timedelta(seconds=10))]),
    S("provider API down (check errors) -> flagged, others chosen", c_provider_api_down,
      sim={"CHECK": {"syn_a": "error"}}),
    S("adapter crashes in the check -> flagged unknown_state, others chosen", c_provider_adapter_crash,
      sim={"CHECK": {"syn_a": "crash"}}),
    S("market data stale for everyone -> no route, clear reason", c_everyone_stale,
      market=[L(p, pr, age=timedelta(hours=2)) for p, pr in (("syn_a", 1.0), ("syn_b", 1.1), ("syn_c", 1.2))]),
    S("no provider meets requirements (explicit availability + ceiling)", c_no_provider_meets_requirements,
      market=[L("syn_a", 1.00, avail=None), L("syn_b", 1.10, avail=None), L("syn_c", 3.00)],
      spec={"preferences": {"require_available": True}, "max_price_per_gpu_hour": 2.0}),
    S("only interruptible capacity -> on-demand request gets no route", c_only_interruptible,
      market=[L("syn_a", 0.40, market_type="spot"), L("syn_b", 0.50, interruptible=True)]),
    S("Vast single-host ask and 'from' floor prices are never routed", c_ineligible_tiers,
      market=[L("syn_a", 0.30, tier="cheapest"), L("syn_b", 0.40, tier="from_price"), L("syn_c", 1.20)]),
    S("8 GPUs requested, only 4-GPU shapes -> multi-instance listed, never provisioned", c_shape_8_vs_4,
      market=[L("syn_a", 1.00, count=4), L("syn_b", 1.10, count=4), L("syn_c", 0.90, count=16)], spec={"count": 8}),
    S("each routing mode picks its documented winner on the same market", c_modes),
    S("family request without allow_variants -> 422; with it, variant-tagged", c_family_needs_variant,
      market=[L("syn_a", 1.00), L("syn_b", 0.80, gpu=G2)]),
    S("unvalidated (simulated) cheapest provider: preview shows it, route skips it", c_unvalidated_cheapest_route,
      flags={"syn_a": {"validated": False}}),
    S("unvalidated provider's preview quote -> launch refused", c_unvalidated_quote_launch,
      flags={"syn_a": {"validated": False}}),
    S("provider demoted to simulated after quote -> approval refused", c_unvalidated_approval_refused,
      mode="SUPERVISED"),
    S("killed provider excluded", c_killed_provider, flags={"syn_a": {"killed": True}}),
    S("global kill switch -> nothing provisions", c_kill_all),
    S("account provider_allowlist reflected in preview", c_allowlist_preview, limits={"provider_allowlist": ["syn_b"]}),
    S("account region_allowlist reflected in preview", _limit_check("region_allowlist"),
      limits={"region_allowlist": ["Europe"]}),
    S("account max_price_per_gpu_hour reflected in preview", _limit_check("max_price_per_gpu_hour"),
      limits={"max_price_per_gpu_hour": 0.5}),
    S("account max_gpus reflected in preview", _limit_check("max_gpus"), limits={"max_gpus": 0}),
    S("price moves +1% between preview and launch -> proceeds", _quote_flow(_price(1.01), e_launch_ok)),
    S("price moves +10% -> refused, new quote for re-approval", _quote_flow(_price(1.10), e_requote(1.10))),
    S("price moves -10% -> refused too (either direction)", _quote_flow(_price(0.90), e_requote(0.90))),
    S("quote expired before launch -> refused, fresh quote", _quote_flow(lambda c, q: b_expire(c, q), e_expired)),
    S("GPU mapping changed between preview and launch -> refused", _quote_flow(b_remap, e_mapping_refused)),
    S("listing disappears (live check: not available) -> refused", _quote_flow(b_check("unavailable"),
                                                                               e_gone("no longer available"))),
    S("listing disappears (provider 404 on re-check) -> refused", _quote_flow(b_check("not_found"),
                                                                              e_gone("live re-check failed: not_found"))),
    S("provider adapter disappears between quote and launch -> refused", c_provider_unregistered),
    S("a quote is single-use and owned by its account", c_quote_single_use_and_owner),
    S("USER_DEFINED weight on a no-data factor -> refused", c_no_data_factor),
    S("selected listing not provisionable (level 0) -> best provisionable launched", c_selected_not_provisionable,
      market=[L("syn_z", 0.90), L("syn_a", 1.00), L("syn_b", 1.10)]),
    S("require_level excludes market-only providers", c_require_level,
      market=[L("syn_z", 0.90), L("syn_a", 1.00)], spec={"preferences": {"require_level": 2}}),
    S("exclude_providers preference", c_exclude_preference, spec={"preferences": {"exclude_providers": ["syn_a"]}}),
    S("definitive rejection fails over as a NEW deployment", c_failover_definitive,
      sim={"PROVISION": {"syn_a": "capacity"}}, attempts=2),
    S("ambiguous outcome never fails over", c_no_failover_ambiguous, sim={"PROVISION": {"syn_a": "timeout"}}, attempts=3),
    S("route latency limit: at most N live checks", c_latency_limit,
      market=[L(p, 1.0 + i / 10) for i, p in enumerate(("syn_a", "syn_b", "syn_c", "syn_d"))],
      sim={"CHECK": {p: "unavailable" for p in ("syn_a", "syn_b", "syn_c", "syn_d")}}, checks=2),
    S("PREVIEW_ONLY mode quotes but never launches", c_preview_only, mode="PREVIEW_ONLY"),
    S("env ceiling caps LIVE at PREVIEW_ONLY", c_env_ceiling, env=False),
    S("SUPERVISED: pending approval with the full approval block", c_supervised, mode="SUPERVISED"),
    S("explanation and alternatives' comparison text", c_explanation),
]


def seed_modes_market():
    """GM: 40 days of change-only history so persistence and stability have data."""
    start = T - timedelta(days=40)
    rows = [listing("m_cheap", 1.00, gpu=GM, avail=None, at=T), listing("m_volatile", 1.05, gpu=GM, at=T),
            listing("m_steady", 1.30, gpu=GM, at=T), listing("m_pricey", 2.50, gpu=GM, at=T),
            listing("m_nolevel", 0.95, gpu=GM, at=T)]
    for r in rows:
        r.first_seen_at = start
    obs = {"m_cheap:1x": [(start + timedelta(days=d), 1.00, None if d % 2 == 0 else False) for d in range(40)]
           + [(T - timedelta(hours=1), 1.00, None)],
           "m_volatile:1x": [(start + timedelta(hours=12 * k), 0.7 if k % 2 else 1.4, True) for k in range(80)]
           + [(T - timedelta(hours=1), 1.05, True)],
           "m_steady:1x": [(start, 1.30, True)], "m_pricey:1x": [(start, 2.50, True)],
           "m_nolevel:1x": [(start, 0.95, True)]}
    bf.history(rows, obs)
    rollups.refresh()
    scoring.history.cache_clear()


def main():
    S_ = bf.db(DB)
    for p in PROVIDERS + MODE_PROVIDERS:
        adapters.register(p, Sim)
    seed_modes_market()
    results = []
    try:
        for sc in SCENARIOS:
            try:
                ctx = prepare(sc)
                settings.routing_max_attempts = sc.get("attempts", 1)
                settings.route_live_check_candidates = sc.get("checks", 3)
                sc["check"](ctx)
                results.append((sc["name"], True, ""))
                print(f"ok   {sc['name']}")
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                results.append((sc["name"], False, str(exc)))
                print(f"FAIL {sc['name']}: {exc}")
    finally:
        for p in PROVIDERS + MODE_PROVIDERS:
            adapters.unregister(p)
        settings.routing_live_provisioning = False
        bf.drop(S_, DB)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed")
    sys.exit(1 if failed else 0)


def _client():
    import main as app_main
    from fastapi.testclient import TestClient
    return app_main, TestClient(app_main.app, headers={"X-OpenGrid-Request": "1"})


def _as(app_main, who):
    from accounts.auth import principal
    app_main.app.dependency_overrides[principal] = lambda: who


if __name__ == "__main__":
    main()
