"""Follow-up gaps: GPU families in the API, strict region routing, scopes / me, market regions,
per-provider 24h change, news families + relevance components, methodology summaries.

Runs against a scratch database with synthetic `syn_*` providers. No provider is ever called.

Run:  .venv/Scripts/python tests/test_followup.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
import scratchdb  # noqa: E402

import families  # noqa: E402
import normalize  # noqa: E402
from analytics import rollups  # noqa: E402
from api.common import gpu_slugs, resolve_gpu_or_family  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from news import classify  # noqa: E402
from routing import scoring  # noqa: E402
from sqlalchemy import text  # noqa: E402
from tables import ComputeListingRow, ListingObservation, RawSnapshot  # noqa: E402

DB = "og_test_bf"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
L4 = "NVIDIA L4 24GB"
PCIE = "NVIDIA H100 80GB PCIe"
_client_cache = {}


def _listing(prov, gpu, price, region, country, steps, first_fetch):
    """steps: [(time, price)] ascending (change-only observations); the listing is seen now."""
    lid = f"{prov}:{gpu}"
    rows = [RawSnapshot(provider=prov, endpoint="/synthetic", fetched_at=first_fetch, ok=True, status_code=200,
                        payload={}),
            RawSnapshot(provider=prov, endpoint="/synthetic", fetched_at=NOW, ok=True, status_code=200, payload={})]
    listing = ComputeListingRow(
        provider=prov, listing_id=lid, sku=lid, raw_gpu_name=gpu, canonical_gpu_name=gpu, gpu_count=1,
        region=region, country=country, price_per_gpu_hour=Decimal(str(price)),
        price_per_instance_hour=Decimal(str(price)), currency="USD", market_type="on_demand", provider_tier=None,
        interruptible=False, available=True, capacity=None, capacity_unit=None, vcpu=None, ram_gb=None,
        storage_gb=None, observed_at=NOW, first_seen_at=steps[0][0])
    obs = [ListingObservation(provider=prov, listing_id=lid, observed_at=t, price_per_gpu_hour=Decimal(str(p)),
                              price_per_instance_hour=Decimal(str(p)), available=True, capacity=None,
                              capacity_unit=None) for t, p in steps]
    return rows, listing, obs


def seed_followup(Session):
    long_ago = NOW - timedelta(days=40)
    specs = [
        # L4: one US listing that moved 1.20 -> 1.00 two hours ago (recorded 30h ago: a real 24h change),
        ("syn_us", L4, 1.00, "us-east", "US", [(NOW - timedelta(hours=30), 1.20), (NOW - timedelta(hours=2), 1.00)],
         long_ago),
        # one with no location, recorded only for 5h (no 24h basis: nodata),
        ("syn_unknown", L4, 1.50, None, None, [(NOW - timedelta(hours=5), 1.50)], NOW - timedelta(hours=5)),
        # one in Europe, a provider watched for 40 days whose L4 listing is 3h old (status new).
        ("syn_eu", L4, 1.20, "frankfurt", "DE", [(NOW - timedelta(hours=3), 1.20)], long_ago),
        # H100 PCIe much cheaper than any H100 SXM5 fixture price: a family route should pick it.
        ("syn_pcie", PCIE, 1.00, "us-west", "US", [(long_ago, 1.00)], long_ago),
    ]
    raws, listings, obs = [], [], []
    for spec in specs:
        r, lst, o = _listing(*spec)
        raws += r
        listings.append(lst)
        obs += o
    with Session.begin() as s:
        s.add_all(raws)
        s.add_all(listings)
        s.flush()
        s.add_all(obs)


def _client():
    if "c" not in _client_cache:
        import main
        from fastapi.testclient import TestClient
        _client_cache["c"] = TestClient(main.app)
    return _client_cache["c"]


# --------------------------------------------------------------------------
# families.py
# --------------------------------------------------------------------------

def test_family_registry_and_slugs():
    fams = families.all_families()
    ids = {f["id"] for f in fams}
    assert {"H100", "A100", "Blackwell", "Hopper", "RTX PRO 6000"} <= ids, ids
    assert "H20" not in ids and "Rubin" not in ids, "families without a canonical variant are not listed"
    gslugs = set(gpu_slugs())
    for f in fams:
        assert f["variants"], f
        assert f["slug"] not in gslugs, f"family slug {f['slug']!r} collides with a GPU slug"
        assert f["variants"] == classify.FAMILY_VARIANTS[f["id"]]
        assert all("MIG" not in v for v in f["variants"])
    assert families.family_slug("H100") == "h100" and families.family_slug("RTX PRO 6000") == "rtx-pro-6000"
    for v in ("h100", "H100", " h100 "):
        assert families.resolve_family(v) == "H100", v
    assert families.resolve_family("rtx-pro-6000") == families.resolve_family("RTX PRO 6000") == "RTX PRO 6000"
    assert families.resolve_family("h20") is None and families.resolve_family("nope") is None
    assert families.family("blackwell")["kind"] == "architecture" and families.family("h100")["kind"] == "model"
    assert families.family("blackwell")["member_families"] and families.variants("H100") == families.family("h100")["variants"]
    assert resolve_gpu_or_family("h100-80gb-sxm5") == ("gpu", "NVIDIA H100 80GB SXM5")
    assert resolve_gpu_or_family("h100") == ("family", "H100")
    assert resolve_gpu_or_family("l40s") == ("family", "L40S"), "one-variant families resolve as families"
    try:
        resolve_gpu_or_family("not-a-gpu")
        raise AssertionError("expected 404")
    except HTTPException as e:
        assert e.status_code == 404
    cf = {f["id"]: f for f in families.classifier_families()}
    assert cf["H20"]["tracked"] is False and cf["H100"]["tracked"] is True


def test_family_endpoints():
    c = _client()
    r = c.get("/v1/families")
    assert r.status_code == 200, r.text
    rows = {f["id"]: f for f in r.json()["data"]}
    assert "H100" in rows and r.json()["meta"]["kind"] == "family"
    r = c.get("/v1/families/h100")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["note"] == families.NOTE and d["kind"] == "model" and d["architecture"] == "Hopper"
    assert "median" not in d and "low" not in d, "a family has no merged price"
    vs = {v["gpu"]: v for v in d["variants"]}
    assert set(vs) == set(families.variants("H100"))
    for v in vs.values():
        for k in ("slug", "low", "median", "high", "providers", "available_listings", "index_id", "index_level",
                  "change_24h", "links"):
            assert k in v, (k, v)
        assert v["change_24h"]["pct"] is not None or v["change_24h"]["reason"]
        assert v["links"]["market"] == f"/v1/markets/{v['slug']}"
    priced = [v for v in vs.values() if v["low"] is not None]
    ch = d["cheapest_variant_now"]
    assert ch["gpu"] == PCIE and ch["low"] == min(v["low"] for v in priced) == 1.0
    assert "not a family price" in ch["label"]
    assert c.get("/v1/families/h20").status_code == 404
    assert c.get("/v1/families/blackwell").json()["data"]["kind"] == "architecture"


def test_family_in_gpu_paths():
    c = _client()
    for path, endpoint in (("/v1/markets/h100", "market"), ("/v1/history/h100", "history"),
                           ("/v1/gpus/h100", "gpu"), ("/v1/markets/h100/context", "context"),
                           ("/v1/markets/h100/regions", "regions")):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text)
        b = r.json()
        assert b["meta"]["kind"] == "family" and b["meta"]["resolved_as"] == "family", (path, b["meta"])
        assert b["meta"]["endpoint"] == endpoint and b["meta"]["requested"] == "h100"
        assert all(endpoint in v["links"] for v in b["data"]["variants"])
        assert "median" not in b["data"]
    # A canonical GPU still gets its own market, unchanged in shape.
    r = c.get("/v1/markets/h100-80gb-sxm5")
    assert r.status_code == 200 and r.json()["meta"]["kind"] == "inferred" and "by_provider" in r.json()["data"]
    assert c.get("/v1/markets/not-a-gpu").status_code == 404
    # best: each variant ranked separately, its top candidates are its own.
    r = c.get("/v1/best/h100?mode=cheapest&per_variant=2")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["meta"]["kind"] == "family" and b["meta"]["resolved_as"] == "family"
    by = {v["gpu"]: v for v in b["data"]["by_variant"]}
    assert set(by) == set(families.variants("H100"))
    for g, v in by.items():
        assert len(v["top"]) <= 2 and all(cand["gpu"] == g for cand in v["top"])
        assert v["market"]["kind"] == "observed_market_price"
    assert by[PCIE]["top"][0]["provider"] == "syn_pcie"


def test_route_family_requires_allow_variants():
    c = _client()
    body = {"gpu": "h100", "mode": "cheapest", "duration_hours": 2}
    r = c.post("/v1/route/preview", json=body)
    assert r.status_code == 422, r.text
    det = r.json()["detail"]
    assert det["code"] == "family_needs_variant" and det["family"] == "H100"
    assert {v["slug"] for v in det["variants"]} >= {"h100-80gb-sxm5", "h100-80gb-pcie"}
    assert "allow_variants" in det["message"]
    r = c.post("/v1/route/preview", json={**body, "allow_variants": True})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["family"] == "H100" and d["variants"] == families.variants("H100")
    sel = d["selected"]
    assert sel["variant"] == PCIE and sel["gpu"] == PCIE and sel["provider"] == "syn_pcie", sel
    assert all(a["variant"] in d["variants"] for a in d["alternatives"])
    assert d["market"]["median"] == next(v["market"]["median"] for v in d["by_variant"] if v["gpu"] == PCIE)
    assert d["quote"]["kind"] == "quote"
    # The audit record keeps the family request and the per-variant candidates.
    rec = c.get(f"/v1/route/{d['route_request_id']}").json()["data"]
    assert rec["gpu"] == "H100" and rec["request"]["variants"] == d["variants"]
    assert all(cand.get("variant") for cand in rec["decision"]["candidates"])
    # Execute: live provisioning is off and syn_* cannot be provisioned -> no deployment.
    with normalize.SessionLocal() as s:
        before = s.execute(text("SELECT count(*) FROM deployments")).scalar()
    r = c.post("/v1/route", json={**body, "allow_variants": True})
    assert r.status_code == 200 and r.json()["data"]["status"] in ("not_provisioned", "no_candidates"), r.text
    assert r.json()["data"]["family"] == "H100"
    with normalize.SessionLocal() as s:
        assert s.execute(text("SELECT count(*) FROM deployments")).scalar() == before
    # A canonical GPU is unaffected by allow_variants.
    r = c.post("/v1/route/preview", json={"gpu": "h100-80gb-pcie", "mode": "cheapest", "allow_variants": True})
    assert r.status_code == 200 and "family" not in r.json()["data"]


# --------------------------------------------------------------------------
# strict region
# --------------------------------------------------------------------------

def test_strict_region_scoring():
    loose = scoring.rank_listings(L4, region_group="US", mode="BALANCED", now=NOW)
    by = {c["provider"]: c for c in loose["candidates"]}
    assert set(by) == {"syn_us", "syn_unknown"}, set(by)
    assert by["syn_unknown"]["factors"]["region_match"]["value"] == 0.5
    assert by["syn_us"]["factors"]["region_match"]["value"] == 1.0
    assert {e["provider"]: e["code"] for e in loose["exclusions"]}["syn_eu"] == "wrong_region"
    assert loose["strict_region"] is False
    strict = scoring.rank_listings(L4, region_group="US", mode="BALANCED", now=NOW, strict_region=True)
    assert [c["provider"] for c in strict["candidates"]] == ["syn_us"]
    ex = {e["provider"]: e for e in strict["exclusions"]}
    assert ex["syn_unknown"]["code"] == "region_unconfirmed"
    assert ex["syn_unknown"]["reason"] == "region not confirmed as US"
    assert ex["syn_eu"]["code"] == "wrong_region" and strict["strict_region"] is True
    # Without a region, strict changes nothing at the scoring level.
    assert scoring.rank_listings(L4, mode="CHEAPEST", now=NOW, strict_region=True)["candidates_total"] == 3


def test_strict_region_endpoints():
    c = _client()
    slug = "l4-24gb"
    r = c.get(f"/v1/best/{slug}?region=US&strict_region=true&mode=cheapest")
    assert r.status_code == 200, r.text
    assert [x["provider"] for x in r.json()["data"]["candidates"]] == ["syn_us"]
    r = c.get(f"/v1/best/{slug}?region=US&mode=cheapest")
    assert {x["provider"] for x in r.json()["data"]["candidates"]} == {"syn_us", "syn_unknown"}
    assert c.get(f"/v1/best/{slug}?strict_region=true").status_code == 422, "strict needs a region"
    body = {"gpu": slug, "mode": "cheapest", "region": "US", "strict_region": True}
    r = c.post("/v1/route/preview", json=body)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["strict_region"] is True and d["selected"]["provider"] == "syn_us"
    assert d["exclusions_by_code"].get("region_unconfirmed") == 1
    assert c.post("/v1/route/preview", json={"gpu": slug, "strict_region": True}).status_code == 422
    r = c.post("/v1/route/preview", json={**body, "strict_region": False})
    assert r.json()["data"]["strict_region"] is False and "region_unconfirmed" not in r.json()["data"]["exclusions_by_code"]


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def test_scopes_me_capabilities():
    from accounts.auth import SCOPES
    from config import settings

    c = _client()
    r = c.get("/v1/scopes")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert [x["scope"] for x in rows] == list(SCOPES) and all(x["description"] for x in rows)
    assert [x["scope"] for x in rows if x["spends_money"]] == ["route:execute"]
    me = c.get("/v1/me").json()["data"]
    assert [x["scope"] for x in me["scopes_catalog"]] == list(SCOPES)
    rl = me["rate_limit_defaults"]
    assert rl["classes"]["read"]["limit_per_minute"] == settings.rate_limit_read_per_minute
    assert rl["classes"]["execute"]["limit_per_minute"] == settings.rate_limit_execute_per_minute
    assert "default" in rl["label"] and me["principal"] == "operator"
    cap = c.get("/v1/capabilities").json()["meta"]
    assert cap["live_provisioning_enabled"] is False


# --------------------------------------------------------------------------
# market regions and per-provider 24h change
# --------------------------------------------------------------------------

def test_market_regions():
    c = _client()
    r = c.get("/v1/markets/l4-24gb/regions")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["global_median"] == 1.2 and d["global_providers"] == 3
    g = {x["region_group"]: x for x in d["regions"]}
    assert g["US"]["cheapest"] == 1.0 and g["US"]["cheapest_provider"] == "syn_us" and g["US"]["providers"] == 1
    assert abs(g["US"]["premium_vs_global_median"] - (1.0 / 1.2 - 1)) < 1e-6
    assert g["Europe"]["cheapest_provider"] == "syn_eu" and g["Europe"]["premium_vs_global_median"] == 0.0
    assert g["Unassigned"]["provider_names"] == ["syn_unknown"]
    assert g["Canada"]["cheapest"] is None and g["Canada"]["premium_vs_global_median"] is None
    assert g["Canada"]["reason"] == "no live listing in Canada"
    assert g["US"]["available_listings"] == 1 and g["US"]["reason"] is None


def test_provider_change_24h():
    c = _client()
    r = c.get("/v1/markets/l4-24gb")
    assert r.status_code == 200, r.text
    rows = {x["provider"]: x["change_24h"] for x in r.json()["data"]["by_provider"]}
    us = rows["syn_us"]
    assert us["status"] == "down" and abs(us["pct"] - (1.0 / 1.2 - 1)) < 1e-6 and us["from"] == 1.2, us
    assert rows["syn_unknown"]["status"] == "nodata" and rows["syn_unknown"]["pct"] is None
    assert "not recorded 24h ago" in rows["syn_unknown"]["reason"]
    assert rows["syn_eu"]["status"] == "new" and rows["syn_eu"]["pct"] is None and rows["syn_eu"]["reason"]


# --------------------------------------------------------------------------
# news and methodology
# --------------------------------------------------------------------------

def test_news_families_and_components():
    from news import store

    c = _client()
    r = c.get("/v1/news/families")
    assert r.status_code == 200, r.text
    by = {f["id"]: f for f in r.json()["data"]}
    assert by["H100"]["tracked"] and by["H20"]["tracked"] is False and by["Blackwell"]["kind"] == "architecture"
    assert c.get("/v1/news/12345").status_code == 404, "item ids still route to the item endpoint"
    comps = {"entity_points": 15.0, "topic_points": 10, "signal": 25.0, "tier_mult": 1.0, "recency_mult": 1.0}
    item = SimpleNamespace(id=1, story_id=1, source_id="nope", title="t", url="u", summary="s", author=None,
                           published_at=NOW, published_at_inferred=False, first_seen_at=NOW, relevance=31,
                           topics=[], entities={}, relevance_components=comps)
    assert store.item_dict(item)["relevance_components"] == comps
    assert c.get("/v1/news").status_code == 200


def test_methodology_summaries():
    from methodology_index import methodology_summaries, shorten

    s = methodology_summaries()
    assert {"families", "routing", "events", "data-kinds"} <= set(s), sorted(s)
    for name, text_ in s.items():
        assert text_ and len(text_) <= 200, (name, text_)
        assert "**" not in text_ and "`" not in text_ and "](" not in text_, (name, text_)
    assert s["data-kinds"].startswith("OpenGrid labels every number")
    assert s["families"].startswith("A GPU family")
    assert len(shorten("word " * 100)) <= 200 and shorten("short") == "short"


def main():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    fixtures.seed(Session, days=40)
    seed_followup(Session)
    rollups.refresh()
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
