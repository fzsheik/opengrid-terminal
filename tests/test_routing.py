"""Routing: best-execution scoring, preview/route audit, the live-provisioning gate, deployments,
failover, transaction records, the usage hook, and the HTTP endpoints with scope enforcement.

Runs against a scratch database with synthetic `syn_*` providers and a fake adapter.
No real provider is ever called.

Run:  .venv/Scripts/python tests/test_routing.py
"""

import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures
import scratchdb

import normalize
from accounts.auth import OPERATOR, Principal
from analytics import rollups
from config import settings
from routing import adapters, audit, control, deployments, engine, scoring, tracker, transactions
from routing.adapters.base import (
    CAPACITY, PROVISIONING, RUNNING, STOPPED, TERMINATED, TIMEOUT,
    Adapter, AdapterError, Availability, Instance,
)
from sqlalchemy import text
from store.routing import Deployment, ExecutionRecord
from tables import ComputeListingRow, ListingObservation

DB = "og_test_routing"
G = "NVIDIA RTX 4090 24GB"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
START = NOW - timedelta(days=40)
KEY = Principal(kind="api_key", account_id=7, key_id=3,
                scopes=frozenset({"data:read", "route:preview", "route:execute", "deployments:read",
                                  "deployments:write"}))

# provider -> (price, available now, gpu_count, region, country, market_type, observed offset)
SPEC = {
    "syn_stable": (1.20, True, 1, "us-west", "US", "on_demand", timedelta(0)),
    "syn_cheap": (1.00, None, 1, "us-east", "US", "on_demand", timedelta(0)),
    "syn_mid": (1.10, True, 1, "eu-west", None, "on_demand", timedelta(0)),
    "syn_8x": (0.90, True, 8, "us-east", "US", "on_demand", timedelta(0)),
    "syn_2x": (1.05, True, 2, "us-east", "US", "on_demand", timedelta(0)),
    "syn_stale": (0.50, True, 1, "us-east", "US", "on_demand", timedelta(days=2)),
    "syn_soldout": (0.60, False, 1, "us-east", "US", "on_demand", timedelta(0)),
    "syn_spot": (0.40, True, 1, "us-east", "US", "spot", timedelta(0)),
    "syn_pricey": (3.00, True, 1, "us-east", "US", "on_demand", timedelta(0)),
    "syn_eu": (0.95, True, 1, "frankfurt", "DE", "on_demand", timedelta(0)),
}


def _obs(prov, t, price, avail):
    return ListingObservation(provider=prov, listing_id=f"{prov}:4090", observed_at=t,
                              price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(price)),
                              available=avail, capacity=None, capacity_unit=None)


def seed_routing(Session):
    listings, obs = [], []
    for prov, (price, avail, n, region, country, mtype, back) in SPEC.items():
        seen = NOW - back
        listings.append(ComputeListingRow(
            provider=prov, listing_id=f"{prov}:4090", sku=f"{prov}-4090x{n}", raw_gpu_name="RTX 4090",
            canonical_gpu_name=G, gpu_count=n, region=region, country=country,
            price_per_gpu_hour=Decimal(str(price)), price_per_instance_hour=Decimal(str(price * n)), currency="USD",
            market_type=mtype, provider_tier=None, interruptible=mtype == "spot", available=avail, capacity=None,
            capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=seen, first_seen_at=START))
        if prov == "syn_cheap":
            # Sold out every other day: about half the hours have priced availability.
            for d in range(40):
                obs.append(_obs(prov, START + timedelta(days=d), price, None if d % 2 == 0 or d == 39 else False))
            obs.append(_obs(prov, NOW - timedelta(hours=1), price, None))
        elif prov == "syn_mid":
            for k in range(80):  # volatile: alternates every 12 hours
                obs.append(_obs(prov, START + timedelta(hours=12 * k), 0.8 if k % 2 else 1.4, True))
            obs.append(_obs(prov, NOW - timedelta(hours=1), price, avail))
        else:
            obs.append(_obs(prov, START, price, avail))
    with Session.begin() as s:
        s.add_all(listings)
        s.flush()
        s.add_all(obs)


class FakeAdapter(Adapter):
    """Synthetic provider adapter. BEHAVIOR[provider]: ok | capacity | timeout | unavailable | needs_image."""
    LEVEL = 3
    SUPPORTS_STOP = True
    CREDENTIALS = ()
    CHECK_NEEDS_CREDENTIALS = False
    BEHAVIOR: dict = {}
    CALLS: list = []
    STATE: dict = {}
    EXEC_PRICE = 1.25

    def missing_launch(self, launch, offer):
        return ["image"] if self.BEHAVIOR.get(self.provider) == "needs_image" and not launch.image else []

    def check_availability(self, offer):
        FakeAdapter.CALLS.append(("check", self.provider))
        if self.BEHAVIOR.get(self.provider) == "unavailable":
            return Availability(available=False, live=True, note="sold out on live check")
        return Availability(available=True, live=True, region=offer.region,
                            list_price_per_gpu_hour=offer.price_per_gpu_hour)

    def provision(self, offer, availability, launch, name):
        FakeAdapter.CALLS.append(("provision", self.provider))
        b = self.BEHAVIOR.get(self.provider, "ok")
        if b == "capacity":
            raise AdapterError(CAPACITY, f"{self.provider}: insufficient capacity")
        if b == "timeout":
            raise AdapterError(TIMEOUT, f"{self.provider}: timed out")
        iid = f"fake-{len(FakeAdapter.STATE) + 1}"
        FakeAdapter.STATE[iid] = PROVISIONING
        return Instance(iid, PROVISIONING, "booting", region=offer.region)

    def status(self, iid):
        FakeAdapter.CALLS.append(("status", self.provider))
        st = FakeAdapter.STATE[iid]
        return Instance(iid, st, st, price_per_gpu_hour=self.EXEC_PRICE, ip="10.0.0.1")

    def terminate(self, iid):
        FakeAdapter.CALLS.append(("terminate", self.provider))
        FakeAdapter.STATE[iid] = TERMINATED
        return Instance(iid, TERMINATED, "terminated")

    def stop(self, iid):
        FakeAdapter.STATE[iid] = STOPPED
        return Instance(iid, STOPPED, "stopped")


def reset_fake():
    FakeAdapter.BEHAVIOR.clear()
    FakeAdapter.CALLS.clear()


def spec(**kw):
    base = {"gpu": G, "count": 1, "region_group": None, "max_price_per_gpu_hour": None, "duration_hours": 10,
            "deadline_hours": None, "mode": "CHEAPEST", "weights": None, "preferences": {}, "launch": None}
    base.update(kw)
    return base


def live_on(*providers):
    """Execution core (0010): env ceiling on, mode LIVE, providers validated + live-enabled."""
    settings.routing_live_provisioning = True
    control.set_mode("LIVE", reason="test", by="test")
    for p in providers:
        control.mark_validated(p, "dep-test", {"test": "synthetic"}, "test")
        control.set_provider_flags(p, reason="test", by="test", live_enabled=True)


def live_off():
    settings.routing_live_provisioning = False
    settings.routing_max_attempts = 1
    control.set_mode("PREVIEW_ONLY", reason="test", by="test")


def state_events(d):
    return [e["to"] for e in d["events"] if e["from"] != e["to"]]


def count(table, where="true", **params):
    with normalize.SessionLocal() as s:
        return s.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params).scalar()


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def test_cheapest_and_exclusions():
    r = scoring.rank_listings(G, mode="CHEAPEST", now=NOW)
    names = [c["provider"] for c in r["candidates"]]
    assert names == ["syn_eu", "syn_cheap", "syn_mid", "syn_stable", "syn_pricey"], names
    codes = {e["provider"]: e["code"] for e in r["exclusions"]}
    assert codes["syn_stale"] == "stale" and codes["syn_soldout"] == "sold_out"
    assert codes["syn_spot"] == "not_on_demand" and codes["syn_8x"] == "wrong_count"
    assert codes["syn_2x"] == "wrong_count", "a 2-GPU instance cannot serve a 1-GPU request"
    # market median: each provider's lowest current eligible price, across shapes
    assert r["market"]["median"] == statistics.median([1.2, 1.0, 1.1, 0.9, 1.05, 3.0, 0.95]) == 1.05
    assert r["market"]["providers"] == 7 and r["market"]["low_provider"] == "syn_8x"
    assert all(c["price_kind"] == "observed_market_price" for c in r["candidates"])
    assert r["weights"]["price"] == 1.0 and r["weights"]["reliability"] == 0


def test_count_and_multi_instance():
    r = scoring.rank_listings(G, count=8, mode="CHEAPEST", now=NOW)
    assert [c["provider"] for c in r["candidates"]] == ["syn_8x"]
    multi = {c["provider"]: c["instances"] for c in r["multi_instance_alternatives"]}
    assert multi["syn_2x"] == 4 and multi["syn_stable"] == 8, multi
    assert "not provisioned automatically" in r["multi_instance_alternatives"][0]["note"]
    r3 = scoring.rank_listings(G, count=3, mode="CHEAPEST", now=NOW)
    assert {c["provider"] for c in r3["multi_instance_alternatives"]} >= {"syn_stable"}
    assert any(e["provider"] == "syn_2x" and e["code"] == "wrong_count" for e in r3["exclusions"]), "2 does not divide 3"


def test_max_price_and_region():
    r = scoring.rank_listings(G, max_price=1.15, mode="CHEAPEST", now=NOW)
    over = {e["provider"] for e in r["exclusions"] if e["code"] == "over_max_price"}
    assert over == {"syn_stable", "syn_pricey"}, over
    us = scoring.rank_listings(G, region_group="US", mode="BALANCED", now=NOW)
    wrong = {e["provider"] for e in us["exclusions"] if e["code"] == "wrong_region"}
    assert wrong == {"syn_eu"}, wrong
    by = {c["provider"]: c for c in us["candidates"]}
    assert by["syn_mid"]["factors"]["region_match"]["value"] == 0.5, "eu-west of an unknown provider: not confirmed"
    assert by["syn_stable"]["factors"]["region_match"]["value"] == 1.0
    assert "region matched (US)" in by["syn_stable"]["explanation"]
    assert us["weights"]["region_match"] > 0 and abs(sum(us["weights"].values()) - 1) < 1e-4


def test_factors_and_explanations():
    adapters.register("syn_stable", FakeAdapter)
    try:
        r = scoring.rank_listings(G, mode="BALANCED", now=NOW)
    finally:
        adapters.unregister("syn_stable")
    by = {c["provider"]: c for c in r["candidates"]}
    st, ch, mid = by["syn_stable"], by["syn_cheap"], by["syn_mid"]
    p = st["factors"]["availability_persistence"]
    assert p["kind"] == "inferred" and p["value"] > 0.99, p
    assert 0.4 < ch["factors"]["availability_persistence"]["value"] < 0.65, ch["factors"]["availability_persistence"]
    assert st["factors"]["price_stability"]["value"] == 1.0
    assert mid["factors"]["price_stability"]["value"] == 0.0, "alternating 0.8/1.4 is far past 25% CV"
    assert ch["factors"]["availability_now"]["value"] == 0.5 and st["factors"]["availability_now"]["value"] == 1.0
    assert st["factors"]["integration_level"]["raw"]["level"] == 3 and ch["factors"]["integration_level"]["value"] == 0
    for f in ("reliability", "performance"):
        assert st["factors"][f]["value"] is None and st["factors"][f]["weight"] == 0
        assert st["factors"][f]["note"] == "no data: not used"
    # total = sum of contributions
    for c in r["candidates"]:
        assert abs(c["score"] - sum(f["contribution"] for f in c["factors"].values())) < 1e-3
    assert [c["score"] for c in r["candidates"]] == sorted((c["score"] for c in r["candidates"]), reverse=True)
    assert "5% below current market median ($1.05)" in ch["explanation"], ch["explanation"]
    assert "14% above current market median" in st["explanation"], st["explanation"]
    assert "available now (explicit)" in st["explanation"] and "priced availability 100% of last 30 days" in st["explanation"]
    assert "availability unknown" in ch["explanation"]
    assert "old)" in st["explanation"] and "OpenGrid can provision via API (level 3)" in st["explanation"]
    assert "market data only" in ch["explanation"]
    top = r["candidates"][0]
    assert "vs_selected" not in top and all("vs_selected" in c for c in r["candidates"][1:])
    # the alternative explanation names price and the stronger/weaker factors
    if top["provider"] != "syn_stable":
        assert "more expensive" in st["vs_selected"] or "cheaper" in st["vs_selected"]


def test_modes_order():
    stable = scoring.rank_listings(G, mode="MOST_STABLE", now=NOW)
    order = [c["provider"] for c in stable["candidates"]]
    assert set(order[:3]) == {"syn_eu", "syn_stable", "syn_pricey"}, ("always available, constant price first", order)
    assert set(order[3:]) == {"syn_mid", "syn_cheap"}, order
    adapters.register("syn_pricey", FakeAdapter)
    try:
        fast = scoring.rank_listings(G, mode="FASTEST_AVAILABLE", now=NOW)
    finally:
        adapters.unregister("syn_pricey")
    assert fast["candidates"][0]["provider"] == "syn_pricey", "explicit availability + integration dominate price"
    w = scoring.rank_listings(G, mode="USER_DEFINED", weights={"price": 3, "price_stability": 1}, now=NOW)
    assert w["weights"]["price"] == 0.75 and w["weights"]["price_stability"] == 0.25


def test_weight_validation():
    bad = [({"speed": 1}, "unknown factor"), ({"price": -1}, ">= 0"), ({"price": float("nan")}, "finite"),
           ({"reliability": 1}, "no data"), ({"price": 0}, "all be zero"), ({}, "non-empty"),
           ({"price": "x"}, "number")]
    for weights, msg in bad:
        try:
            scoring.rank_listings(G, mode="USER_DEFINED", weights=weights, now=NOW)
        except scoring.ScoringError as e:
            assert msg in str(e), (weights, str(e))
        else:
            raise AssertionError(f"weights {weights} should be rejected")
    for kw, msg in [({"mode": "BALANCED", "weights": {"price": 1}}, "only accepted"), ({"mode": "FAST"}, "unknown mode"),
                    ({"mode": "USER_DEFINED", "weights": {"region_match": 1}}, "needs a region")]:
        try:
            scoring.rank_listings(G, now=NOW, **kw)
        except scoring.ScoringError as e:
            assert msg in str(e), (kw, str(e))
        else:
            raise AssertionError(f"{kw} should be rejected")


def test_thin_history_is_neutral_not_invented():
    scoring.history.cache_clear()
    with normalize.SessionLocal.begin() as s:
        s.execute(text("DELETE FROM market_hourly WHERE hour < :t"), {"t": NOW - timedelta(hours=10)})
    try:
        r = scoring.rank_listings(G, mode="BALANCED", now=NOW)
        f = r["candidates"][0]["factors"]["availability_persistence"]
        assert f["value"] is None and f["imputed"] and "insufficient" in f["note"] and "neutral 0.5" in f["note"]
    finally:
        rollups.rebuild()
        scoring.history.cache_clear()


# --------------------------------------------------------------------------
# Preview / route / deployments
# --------------------------------------------------------------------------

def test_preview_writes_audit():
    before = count("route_requests")
    out = engine.preview(spec(), OPERATOR)
    assert count("route_requests") == before + 1
    rr = audit.get(out["route_request_id"], OPERATOR)
    assert rr["preview"] is True and rr["status"] == "previewed"
    d = rr["decision"]
    assert d["selected_provider"] == "syn_eu" and len(d["candidates"]) == 5
    assert {e["code"] for e in d["exclusions"]} >= {"stale", "sold_out", "wrong_count", "not_on_demand"}
    assert d["market_snapshot"]["median"] == 1.05 and d["methodology_version"] == scoring.METHODOLOGY_VERSION
    q = out["quote"]
    assert q["kind"] == "quote" and q["basis"] == "observed_listing" and q["expected_cost_usd"] == 9.5
    assert abs(q["savings_vs_median"]["pct"] - (1.05 - 0.95) / 1.05) < 1e-3
    assert out["can_provision_selected"] is False and out["best_provisionable"] is None
    assert count("deployments") == 0, "a preview never creates a deployment"
    # someone else cannot read the audit record
    assert audit.get(out["route_request_id"], KEY) is None


def test_route_live_disabled_never_provisions():
    reset_fake()
    for p in ("syn_mid", "syn_stable"):
        adapters.register(p, FakeAdapter)
    settings.routing_live_provisioning = False
    try:
        code, out = engine.route(spec(), OPERATOR)
    finally:
        for p in ("syn_mid", "syn_stable"):
            adapters.unregister(p)
    assert out["status"] == "not_provisioned" and out["reason"] == "live provisioning disabled in this environment"
    assert out["deployment"] is None and count("deployments") == 0
    assert ("provision", "syn_mid") not in FakeAdapter.CALLS and ("check", "syn_mid") in FakeAdapter.CALLS
    steps = [(c["provider"], c["outcome"]) for c in out["considered"]]
    assert steps[:2] == [("syn_eu", "skipped"), ("syn_cheap", "skipped")], steps
    assert steps[2] == ("syn_mid", "not_attempted")
    assert out["quote"]["basis"] == "live_provider_api" and out["quote"]["provider"] == "syn_mid"
    rr = audit.get(out["route_request_id"], OPERATOR)
    assert rr["preview"] is False and rr["status"] == "not_provisioned"


def test_route_live_provisions_tracks_and_terminates():
    reset_fake()
    adapters.register("syn_mid", FakeAdapter)
    live_on("syn_mid")
    try:
        code, out = engine.route(spec(), KEY)
        assert out["status"] == "provisioned", out
        dep = out["deployment"]
        assert dep["provider"] == "syn_mid" and dep["status"] == "provisioning" and dep["provider_instance_id"]
        assert dep["prices"]["quote"] == 1.10 and dep["prices"]["observed_market_price"] == 1.10
        assert dep["prices"]["execution_price"] is None, "unknown until the provider reports it"
        assert "provider_metadata" not in dep
        dep_id = dep["deployment_id"]
        # someone else (operator excluded) cannot see it; the owner can
        other = Principal(kind="api_key", account_id=8, scopes=frozenset({"deployments:read"}))
        try:
            deployments.get(dep_id, other)
            raise AssertionError("another account must not see this deployment")
        except Exception as e:
            assert getattr(e, "status_code", None) == 404
        FakeAdapter.STATE[dep["provider_instance_id"]] = RUNNING
        tracker.track()
        d = deployments.get(dep_id, KEY)
        assert d["status"] == "running" and d["prices"]["execution_price"] == 1.25 and d["ip"] == "10.0.0.1"
        # pretend it has been running an hour
        with normalize.SessionLocal.begin() as s:
            row = s.get(Deployment, dep_id)
            row.running_since = row.running_since - timedelta(hours=1)
            row.provisioned_at = row.provisioned_at - timedelta(hours=1, minutes=5)
            # metering (routing/tracker.py) bills from the state events: move them back too
            s.execute(text("UPDATE deployment_events SET at = at - CASE WHEN to_status = 'running' "
                           "THEN interval '60 minutes' ELSE interval '65 minutes' END WHERE deployment_id = :d"),
                      {"d": dep_id})
        d = deployments.terminate(dep_id, KEY)
        assert d["status"] == "terminating", "never terminated without provider confirmation"
        tracker.track()  # the provider now reports it terminated: confirmed
        d = deployments.get(dep_id, KEY)
        assert d["status"] == "terminated" and d["termination_reason"] == "user_requested"
        assert 3590 <= d["uptime_seconds"] <= 3700 and d["interruptions"] == 0
        t = d["transaction"]
        assert t["kind"] == "transaction" and t["provision_ok"] and t["attempts"] == 1
        assert t["quoted_price_per_gpu_hour"] == 1.10 and t["execution_price_per_gpu_hour"] == 1.25
        # metered usage (hour slices, routing/tracker.py + billing.usage): running time at the execution price
        assert t["cost_basis"] == "metered", t
        assert 1.25 - 0.01 <= t["provider_cost_usd"] <= 1.25 * 65 / 60 + 0.01, t
        assert t["usage_recorded"], t
        with normalize.SessionLocal() as s:
            urs = s.execute(text("SELECT account_id, provider, gpu, gpu_count, kind, provider_cost_usd, period_start, "
                                 "period_end FROM usage_records WHERE deployment_id = :d"), {"d": dep_id}).all()
        assert urs and all(u[0] == 7 and u[1] == "syn_mid" and u[2] == G and u[3] == 1 and u[4] == "compute"
                           and isinstance(u[5], Decimal) and u[7] > u[6] for u in urs), urs
        assert abs(float(sum(u[5] for u in urs)) - t["provider_cost_usd"]) < 0.001
        assert state_events(d) == ["created", "quoted", "approved", "provisioning", "running", "terminating",
                                   "terminated"], state_events(d)
        tracker.track()
        assert count("usage_records", "deployment_id = :d", d=dep_id) == len(urs), "billing is never repeated"
    finally:
        adapters.unregister("syn_mid")
        live_off()


def test_failover_and_interruption():
    reset_fake()
    for p in ("syn_mid", "syn_stable"):
        adapters.register(p, FakeAdapter)
    FakeAdapter.BEHAVIOR["syn_mid"] = "capacity"
    live_on("syn_mid", "syn_stable")
    settings.routing_max_attempts = 2
    try:
        code, out = engine.route(spec(), OPERATOR)
        assert out["status"] == "provisioned" and out["deployment"]["provider"] == "syn_stable", out["considered"]
        # failover after a DEFINITIVE rejection creates a NEW deployment; one provision call each
        assert len(out["deployments"]) == 2
        first = deployments.public(out["deployments"][0])
        assert first["status"] == "provision_failed"
        assert [(a["provider"], a["ok"], a["error_kind"]) for a in first["provision_attempts"]] == [
            ("syn_mid", False, "capacity")]
        att = out["deployment"]["provision_attempts"]
        assert [(a["provider"], a["ok"], a["error_kind"]) for a in att] == [("syn_stable", True, None)]
        dep_id = out["deployment"]["deployment_id"]
        iid = out["deployment"]["provider_instance_id"]
        FakeAdapter.STATE[iid] = RUNNING
        deployments.refresh(dep_id)
        FakeAdapter.STATE[iid] = TERMINATED  # gone without anyone asking: an interruption
        deployments.refresh(dep_id)
        d = deployments.get(dep_id, OPERATOR)
        assert d["status"] == "terminated" and d["interruptions"] == 1
        assert d["termination_reason"] == "provider_terminated"
        with normalize.SessionLocal() as s:
            assert s.get(ExecutionRecord, dep_id).interruptions == 1
    finally:
        for p in ("syn_mid", "syn_stable"):
            adapters.unregister(p)
        live_off()


def test_timeout_does_not_fail_over():
    reset_fake()
    for p in ("syn_mid", "syn_stable"):
        adapters.register(p, FakeAdapter)
    FakeAdapter.BEHAVIOR["syn_mid"] = "timeout"
    live_on("syn_mid", "syn_stable")
    settings.routing_max_attempts = 3  # even with attempts left, an ambiguous outcome never fails over
    try:
        code, out = engine.route(spec(), OPERATOR)
    finally:
        for p in ("syn_mid", "syn_stable"):
            adapters.unregister(p)
        live_off()
    assert out["status"] == "provider_timeout" and "NOT failing over" in out["reason"] and code == 202
    assert ("provision", "syn_stable") not in FakeAdapter.CALLS
    dep_id = out["deployment"]["deployment_id"]
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, dep_id)
        assert d.status == "provider_timeout" and d.provider_metadata["needs_reconciliation"] is True
        assert s.get(ExecutionRecord, dep_id).provision_ok is False


def test_all_fail_and_launch_spec_and_unavailable():
    reset_fake()
    for p in ("syn_mid", "syn_stable", "syn_pricey"):
        adapters.register(p, FakeAdapter)
    FakeAdapter.BEHAVIOR.update(syn_mid="unavailable", syn_stable="needs_image", syn_pricey="capacity")
    live_on("syn_mid", "syn_stable", "syn_pricey")
    settings.routing_max_attempts = 3
    try:
        code, out = engine.route(spec(), OPERATOR)
    finally:
        for p in ("syn_mid", "syn_stable", "syn_pricey"):
            adapters.unregister(p)
        live_off()
    by = {c["provider"]: c for c in out["considered"]}
    assert by["syn_mid"]["outcome"] == "unavailable"
    assert by["syn_stable"]["outcome"] == "skipped" and "image" in by["syn_stable"]["reason"]
    assert by["syn_pricey"]["outcome"] == "rejected"
    assert out["status"] == "provision_failed" and out["deployment"]["status"] == "provision_failed"
    assert ("provision", "syn_stable") not in FakeAdapter.CALLS


def test_live_quote_over_max_price_is_not_provisioned():
    reset_fake()

    class Pricier(FakeAdapter):
        def check_availability(self, offer):
            return Availability(available=True, live=True, list_price_per_gpu_hour=2.0)

    adapters.register("syn_mid", Pricier)
    live_on("syn_mid")
    try:
        code, out = engine.route(spec(max_price_per_gpu_hour=1.15), OPERATOR)
    finally:
        adapters.unregister("syn_mid")
        live_off()
    assert out["considered"][-1]["outcome"] == "over_max_price" and ("provision", "syn_mid") not in FakeAdapter.CALLS
    assert out["status"] == "not_provisioned"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _client():
    import main
    from fastapi.testclient import TestClient
    return main, TestClient(main.app, headers={"X-OpenGrid-Request": "1"})  # CSRF header, as web/core.js sends


def _as(main, who):
    from accounts.auth import principal
    main.app.dependency_overrides[principal] = lambda: who


def test_endpoints_and_scopes():
    main, c = _client()
    slug = "rtx-4090-24gb"
    try:
        r = c.get("/v1/capabilities")
        assert r.status_code == 200
        caps = {x["provider"]: x for x in r.json()["data"]}
        assert caps["lambda"]["level_implemented"] == 3 and caps["aws"]["level_implemented"] == 0
        assert all(x["verified_live"] is False for x in caps.values())
        r = c.get(f"/v1/best/{slug}?mode=cheapest")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["meta"]["methodology"] == "/methodology/best-execution" and body["meta"]["kind"] == "inferred"
        assert body["data"]["candidates"][0]["provider"] == "syn_eu"
        assert c.get("/v1/best/not-a-gpu").status_code == 404
        assert c.get(f"/v1/best/{slug}?region=Mars").status_code == 422
        assert c.get(f"/v1/best/{slug}?count=0").status_code == 422

        body = {"gpu": slug, "count": 1, "mode": "balanced", "duration_hours": 5}
        r = c.post("/v1/route/preview", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["route_request_id"].startswith("rr_")
        for bad in [{**body, "count": 0}, {**body, "mode": "FAST"}, {**body, "surprise": 1},
                    {**body, "mode": "USER_DEFINED", "weights": {"speed": 1}}, {**body, "region": "Mars"},
                    {**body, "max_price_per_gpu_hour": -1}, {**body, "launch": {"name": "bad name!"}}]:
            assert c.post("/v1/route/preview", json=bad).status_code == 422, bad
        assert c.post("/v1/route/preview", json={**body, "gpu": "nope"}).status_code == 404

        _as(main, Principal(kind="api_key", account_id=7, scopes=frozenset({"data:read"})))
        assert c.post("/v1/route/preview", json=body).status_code == 403
        assert c.get(f"/v1/best/{slug}").status_code == 200
        _as(main, Principal(kind="api_key", account_id=7, scopes=frozenset({"route:preview"})))
        assert c.post("/v1/route/preview", json=body).status_code == 200
        assert c.post("/v1/route", json=body).status_code == 403, "preview scope cannot execute"
        assert c.get("/v1/best/" + slug).status_code == 403
        assert c.get("/v1/deployments").status_code == 403
        _as(main, KEY)
        before = count("deployments")
        assert c.post("/v1/route", json=body).status_code == 428, "Idempotency-Key is required"
        r = c.post("/v1/route", json=body, headers={"Idempotency-Key": "scopes-1"})
        assert r.status_code == 200 and r.json()["data"]["status"] in ("not_provisioned", "no_candidates"), r.text
        assert count("deployments") == before, "live provisioning is off: no deployment"
        mine = c.get("/v1/deployments").json()["data"]
        assert all(d["deployment_id"] for d in mine)
        with normalize.SessionLocal() as s:
            ops = s.execute(text("SELECT deployment_id FROM deployments WHERE account_id IS NULL LIMIT 1")).scalar()
        if ops:
            assert c.get(f"/v1/deployments/{ops}").status_code == 404, "another account's deployment is hidden"
        _as(main, Principal(kind="api_key", account_id=7, scopes=frozenset({"deployments:read"})))
        if mine:
            assert c.post(f"/v1/deployments/{mine[0]['deployment_id']}/terminate").status_code == 403
    finally:
        main.app.dependency_overrides.clear()


def test_fixture_market_ranks():
    """The standard synthetic fixtures rank without error in every mode."""
    for mode in ("CHEAPEST", "FASTEST_AVAILABLE", "BALANCED", "MOST_STABLE"):
        r = scoring.rank_listings("NVIDIA H100 80GB SXM5", mode=mode)
        assert r["candidates_total"] + r["exclusions_total"] > 0
        for c in r["candidates"]:
            assert 0 <= c["score"] <= 1


def main():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    fixtures.seed(Session, days=40)
    seed_routing(Session)
    rollups.refresh()
    scoring.history.cache_clear()
    settings.routing_live_provisioning = False
    from store.accounts import Account
    with Session.begin() as s:  # the accounts the test principals act as (route() refuses unknown/suspended ones)
        for aid in (7, 8):
            s.add(Account(id=aid, name=f"acct{aid}", status="active", plan="free", settings={}, is_operator=False))
    try:
        from accounts import accounts as acc
        acc.reset_cache()
    except Exception:
        pass
    try:
        tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
        failed = 0
        for name, fn in tests:
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name}: {exc}")
        print(f"\n{len(tests) - failed}/{len(tests)} passed")
        if failed:
            sys.exit(1)
    finally:
        Session.kw["bind"].dispose()
        scratchdb.drop(DB)


if __name__ == "__main__":
    main()
