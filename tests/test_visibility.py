"""Data visibility (methodology/data-visibility.md): an INTERNAL row never reaches a public response or statistic,
and routing / recommendations / job placement still see it.

A scratch database holds synthetic public history (fixtures.seed) plus internal rows that would be impossible to
miss if they leaked:
    - a wholly internal provider `syn_hidden` (registered for the test as an ALL_SOURCES entry), selling every seeded
      GPU at SECRET = $0.004321 per GPU-hour, far below every public price, so it would be the market low;
    - an internal listing under a PUBLIC provider (`syn_alpha:zzsecret:...`, the DeepInfra container-API pattern),
      on demand and spot, with FX provenance (so /v1/fx/listings would list it);
    - a GPU sold ONLY by internal listings (ONLY_GPU);
    - internal capacity rows (count 4321) and an internal archived price.
Then every derived table is built (rollups -> indices, structure, events; settlement fixings) and EVERY public route
of the app is called: the routes are enumerated from the app itself and kept when api.pages.is_public says an
anonymous visitor may read them (plus the legacy JSON reads), so a new public endpoint is covered without editing
this file. A required parameter this file cannot fill fails the test, naming it.

Run:  .venv/bin/python tests/test_visibility.py
"""

import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

os.environ["OPENGRID_NO_JOBS"] = "1"
os.environ["POLLER_ENABLED"] = "false"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (first: isolates the process from real providers)
import fixtures  # noqa: E402

import base64  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from starlette.requests import Request  # noqa: E402

import cache  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import provider_meta  # noqa: E402
from accounts import ratelimit, security  # noqa: E402
from accounts.auth import OPERATOR, PUBLIC  # noqa: E402
from api import pages  # noqa: E402
from api.common import gpu_slug  # noqa: E402
from config import settings  # noqa: E402
from models import CapacityObservation, ComputeListing  # noqa: E402
from tables import ComputeListingRow, ListingObservation, RawSnapshot  # noqa: E402

DB = "og_test_visibility"
SECRET = Decimal("0.004321")
COUNT = 4321
HIDDEN = "syn_hidden"
TOKEN = "zzsecret"
H100 = "NVIDIA H100 80GB SXM5"
ONLY_GPU = "NVIDIA RTX 4090 24GB"          # no public listing sells it
ALPHA_SECRET = f"syn_alpha:{TOKEN}:h100"   # internal listing of a public provider
W: dict = {}

_HIDDEN_ENTRY = provider_meta.InternalSource(HIDDEN, provider_meta.ALL_SOURCES, provider_meta.DATA_KINDS,
                                             "test: a wholly internal provider", "test")


# ---------------------------------------------------------------- registry

def test_registry():
    from providers import PROVIDERS, aws, deepinfra

    for name in PROVIDERS:   # every existing source defaults to public
        assert provider_meta.visibility(name) == "public", name
        assert provider_meta.is_public_provider(name), name
    entries = {(e.provider, e.source): e for e in provider_meta.INTERNAL_SOURCES}
    assert ("deepinfra", deepinfra.API_ENDPOINT) in entries, "the registry names the module's own endpoint"
    assert ("aws", aws.SPOT_ADVISOR) in entries
    for e in provider_meta.INTERNAL_SOURCES:
        assert e.provider in PROVIDERS and len(e.reason) > 40 and e.used_by and set(e.data) <= set(provider_meta.DATA_KINDS), e
    assert provider_meta.visibility("deepinfra", deepinfra.ENDPOINT) == "public", "the price page stays public"
    assert provider_meta.visibility("deepinfra", deepinfra.API_ENDPOINT, "listings") == "internal"
    assert provider_meta.visibility("deepinfra", deepinfra.API_ENDPOINT, "capacity") == "public", "stock flag public"
    assert provider_meta.visibility("aws", aws.SPOT_ADVISOR, "signals") == "internal"
    assert provider_meta.visibility("aws", "/spot-prices") == "public"
    # enforce() fails closed
    assert provider_meta.enforce("lambda", "public") == "public"
    assert provider_meta.enforce("lambda", "internal") == "internal"
    for odd in (None, "", "Public", "secret"):
        assert provider_meta.enforce("lambda", odd) == "internal", odd
    saved = provider_meta.INTERNAL_SOURCES
    try:
        provider_meta.INTERNAL_SOURCES = saved + (_HIDDEN_ENTRY,)
        assert provider_meta.enforce(HIDDEN, "public") == "internal", "a wholly internal provider overrides a claim"
        assert not provider_meta.is_public_provider(HIDDEN) and provider_meta.visibility(HIDDEN, "/x") == "internal"
    finally:
        provider_meta.INTERNAL_SOURCES = saved
    # the methodology page explains the tiers and names every internal source, without confidential numbers
    doc = (ROOT / "methodology" / "data-visibility.md").read_text(encoding="utf-8")
    for e in provider_meta.INTERNAL_SOURCES:
        assert provider_meta.meta(e.provider).display_name in doc, e.provider
    assert "3.69" not in doc and "$" not in doc.split("## ")[0], "no confidential numbers"


# ---------------------------------------------------------------- world

def _listing(provider, lid, gpu, price, now, start, *, market="on_demand", vis="internal", **kw):
    return ComputeListingRow(
        provider=provider, listing_id=lid, sku=f"{TOKEN}-{lid}", raw_gpu_name=f"{TOKEN} {gpu}", canonical_gpu_name=gpu,
        gpu_count=1, region="us-east", country="US", price_per_gpu_hour=price, price_per_instance_hour=price,
        currency="USD", market_type=market, provider_tier=None, interruptible=market == "spot", available=True,
        capacity=None, capacity_unit=None, vcpu=None, ram_gb=None, storage_gb=None, observed_at=now,
        first_seen_at=start, visibility=vis, **kw)


def setup_world():
    from analytics import rollups
    from analytics import settlement as S
    from store.archive import ArchivedPrice
    from store.capacity import CapacityChange, CapacityCurrent

    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    W["Session"] = Session
    normalize.SessionLocal = Session
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(days=10)
    W["now"], W["start"] = now, start
    fixtures.seed(Session, days=10, now=now)
    provider_meta.INTERNAL_SOURCES = provider_meta.INTERNAL_SOURCES + (_HIDDEN_ENTRY,)
    with Session.begin() as s:
        s.execute(text("UPDATE listing_observations SET available = true"))
        s.execute(text("UPDATE compute_listings SET available = true"))
        rows = [_listing(HIDDEN, f"{HIDDEN}:{TOKEN}:{gpu_slug(g)}", g, SECRET, now, start)
                for g in list(fixtures.GPUS) + [ONLY_GPU]]
        rows += [_listing("syn_alpha", ALPHA_SECRET, H100, SECRET, now, start, original_currency="EUR",
                          original_price_per_gpu_hour=SECRET, original_price_per_instance_hour=SECRET,
                          fx_rate=Decimal("1.08"), fx_rate_date=now.date(), fx_source="ecb"),
                 _listing("syn_alpha", f"syn_alpha:{TOKEN}:h100-spot", H100, SECRET, now, start, market="spot"),
                 _listing("syn_beta", f"syn_beta:{TOKEN}:only", ONLY_GPU, SECRET, now, start)]
        s.add_all(rows)
        s.flush()
        s.add_all([ListingObservation(provider=r.provider, listing_id=r.listing_id, observed_at=start,
                                      price_per_gpu_hour=SECRET, price_per_instance_hour=SECRET, available=True)
                   for r in rows])
        # one successful raw snapshot per provider per hour (settlement evidence), the hidden provider too
        snaps, t = [], start
        while t <= now:
            for p in fixtures.PROVIDERS + [HIDDEN]:
                snaps.append(RawSnapshot(provider=p, endpoint="/synthetic/hourly", fetched_at=t - timedelta(minutes=10),
                                         ok=True, status_code=200, payload={"provider": p, "at": t.isoformat()}))
            t += timedelta(hours=1)
        s.add_all(snaps)
        cap = dict(region_key="", region=None, gpu_count=1, in_stock=True, available_count=COUNT, total_count=COUNT,
                   count_unit="gpu", source="/synthetic/capacity", observed_at=now, first_seen_at=start, changed_at=now)
        s.add_all([
            CapacityCurrent(provider=HIDDEN, sku=f"{TOKEN}-cap", raw_gpu_name=f"{TOKEN} H100", canonical_gpu_name=H100,
                            raw_value=f"{TOKEN} {COUNT}", visibility="internal", **cap),
            CapacityCurrent(provider="syn_alpha", sku=f"{TOKEN}-alpha", raw_gpu_name=f"{TOKEN} H100",
                            canonical_gpu_name=H100, raw_value=f"{TOKEN} {COUNT}", visibility="internal", **cap),
            CapacityCurrent(provider="syn_alpha", sku="syn_alpha:public", raw_gpu_name="H100", canonical_gpu_name=H100,
                            raw_value="in stock", visibility="public",
                            **{**cap, "available_count": 7, "total_count": 9}),
        ])
        s.add_all([CapacityChange(provider=p, sku=f"{TOKEN}-{p}", region_key="", canonical_gpu_name=H100, observed_at=now,
                                  in_stock=True, available_count=COUNT, total_count=COUNT, raw_value=TOKEN,
                                  visibility="internal") for p in (HIDDEN, "syn_alpha")])
        arch = dict(source="wayback", source_sha256="0" * 64, parser="test", gpu=H100, gpu_count=1, region=None,
                    country=None, market_type="on_demand", interruptible=False, observed_at=start - timedelta(days=40),
                    imported_at=now)
        s.add_all([ArchivedPrice(source_url=f"https://web.archive.org/web/1/{TOKEN}", provider="syn_alpha",
                                 listing_id=f"{TOKEN}-arch", raw_gpu_name=f"{TOKEN} H100", price_per_gpu_hour=SECRET,
                                 price_per_instance_hour=SECRET, visibility="internal", **arch),
                   ArchivedPrice(source_url="https://web.archive.org/web/1/public", provider="syn_alpha",
                                 listing_id="public-arch", raw_gpu_name="H100", price_per_gpu_hour=Decimal("2.5"),
                                 price_per_instance_hour=Decimal("2.5"), visibility="public", **arch)])
    cache.clear_all()
    rollups.refresh()   # market_hourly, then index_levels, structure daily, events (AFTER_REFRESH hooks)
    ft_start = S.floor_day(now - S.FINALITY_LAG) - timedelta(days=2)
    for _ in range(30):
        r = S.run(now=now, start=ft_start)
        if not r["hourly"] and not r["daily"]:
            break
    cache.clear_all()


# ---------------------------------------------------------------- leak detection

_TEXT_PRICE = re.compile(r"0\.00(43|432)\d*")
_TEXT_COUNT = re.compile(r"\b4,?321\b")  # a word of its own: not the inside of a hex hash


def leaks(obj, echo: tuple[str, ...] = (), path="$") -> list[str]:
    """Every place in a JSON value where an internal row shows: its names, its price, its capacity count."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += leaks(k, echo, f"{path}.<key>")
            out += leaks(v, echo, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += leaks(v, echo, f"{path}[{i}]")
    elif isinstance(obj, bool) or obj is None:
        pass
    elif isinstance(obj, (int, float)):
        if 0.00425 <= obj <= 0.0044 or obj == COUNT:
            out.append(f"{path} = {obj}")
    elif isinstance(obj, str):
        s = obj
        for e in echo:
            s = s.replace(e, "")
        low = s.lower()
        if TOKEN in low or HIDDEN in low or _TEXT_PRICE.search(s) or _TEXT_COUNT.search(s):
            out.append(f"{path} = {obj[:160]!r}")
    return out


def body_leaks(r, echo=()) -> list[str]:
    ct = r.headers.get("content-type", "")
    if "json" in ct:
        try:
            return leaks(r.json(), echo)
        except ValueError:
            pass
    return leaks(r.text, echo)


# ---------------------------------------------------------------- route enumeration

def all_routes():
    def walk(rs):
        for r in rs:
            if hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)
            elif hasattr(r, "routes") and not hasattr(r, "methods") and not hasattr(r, "app"):
                yield from walk(r.routes)
            else:
                yield r
    return [r for r in walk(main.app.routes) if getattr(r, "methods", None) and getattr(r, "dependant", None)]


def _is_public(method: str, path: str) -> bool:
    return pages.is_public(Request({"type": "http", "method": method, "path": path, "query_string": b"",
                                    "headers": [], "server": ("testserver", 80), "scheme": "http"}))


def _required_query(route) -> list[str]:
    def deps(d):
        yield d
        for s in d.dependencies:
            yield from deps(s)
    return [q.name for d in deps(route.dependant) for q in d.query_params if q.field_info.is_required()]


# Operator-only JSON reads of the first UI that still serve market data: public tier too.
LEGACY_OPERATOR = ("/history", "/reference-prices")
# Public GETs that serve no market data and need no check (docs).
SKIP = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")


def _values(name: str, route_path: str) -> list[tuple[str, tuple[str, ...]]]:
    """Values for a path / query parameter: (value, strings the response may echo back)."""
    h100, only = gpu_slug(H100), gpu_slug(ONLY_GPU)
    if name in ("gpu", "slug", "a", "b"):
        if route_path.startswith("/v1/compare") and name in ("a", "b"):
            return [(h100 if name == "a" else gpu_slug("NVIDIA A100 80GB SXM4"), ())]
        return [(h100, ()), (only, ())]
    if name == "provider":
        return [("syn_alpha", ()), (HIDDEN, (HIDDEN,))]
    if name == "name":
        if "provider" in route_path:
            return [("syn_alpha", ()), (HIDDEN, (HIDDEN,))]
        return [("data-visibility", ()), ("data-kinds", ())]
    if name == "listing_id":
        return [(ALPHA_SECRET, (ALPHA_SECRET,)), ("syn_alpha:NVIDIA H100 80GB SXM5", ())]
    if name == "index_id" or (name == "id" and route_path.startswith("/indices")):
        return [(i, ()) for i in W["index_ids"]]
    if name == "kind":
        from analytics import heatmaps
        return [(k, ()) for k in heatmaps.KINDS]
    if name == "pair":
        return [(f"{h100}-vs-{gpu_slug('NVIDIA A100 80GB SXM4')}", ()), (f"{h100}-vs-{only}", ())]
    if name == "family":
        return [(W.get("family", "h100"), ())]
    if name == "fixing_id":
        return [(f, ()) for f in W.get("fixing_ids") or ["1"]]
    if name == "at":
        return [(W["now"].isoformat(), ())]
    if name in ("item_id", "service_id", "target_id", "target", "id"):
        return [("1", ())]
    return []


def public_requests():
    """[(path_template, url, echo)] for every public GET (+ the legacy operator reads), parameters filled."""
    out, unfilled = [], []
    for r in all_routes():
        if "GET" not in r.methods or r.path in SKIP:
            continue
        probe = re.sub(r"\{[^}]+\}", "x", r.path)
        if not (_is_public("GET", probe) or r.path in LEGACY_OPERATOR):
            continue
        params = re.findall(r"\{([^}:]+)(?::[^}]*)?\}", r.path)
        combos = [("", r.path, ())]
        for p in params:
            vals = _values(p, r.path)
            if not vals:
                unfilled.append(f"{r.path}: path {p}")
                vals = [("1", ())]
            combos = [(q, re.sub(r"\{" + p + r"(?::[^}]*)?\}", quote(v, safe=""), u), e + ev)
                      for q, u, e in combos for v, ev in vals]
        query = []
        for q in _required_query(r):
            vals = _values(q, r.path)
            if not vals:
                unfilled.append(f"{r.path}: query {q}")
                continue
            query.append([(q, v, ev) for v, ev in vals])
        for _, url, echo in combos:
            if not query:
                out.append((r.path, url, echo))
                continue
            first = [{q: v for q, v, _ in [opts[0] for opts in query]}]
            # every value of each required query parameter, the others at their first value
            variants = first + [{**first[0], q: v} for opts in query for q, v, _ in opts[1:]]
            for qs in variants:
                ev = tuple(e for opts in query for q, v, es in opts if qs.get(q) == v for e in es)
                out.append((r.path, url + "?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in qs.items()),
                            echo + ev))
    assert not unfilled, ("public routes with a parameter this test cannot fill; add a value in _values() so the "
                          f"route is checked for internal-data leaks: {unfilled}")
    return out


def _client():
    settings.app_password = "pw"
    settings.public_pages = True
    settings.public_rate_limit_per_minute = 100_000
    settings.assistant_public_rate_limit_per_minute = 100_000
    settings.api_key_pepper = settings.api_key_pepper or "test-pepper"
    ratelimit.reset()
    security.reset()
    if not hasattr(main.app.state, "ingest"):  # no lifespan (no poller) in tests: /polling reads this
        main.app.state.ingest = type("NoIngest", (), {"running": False, "providers": []})()
    return TestClient(main.app, raise_server_exceptions=False, headers={"X-OpenGrid-Request": "1"})


BASIC = {"Authorization": "Basic " + base64.b64encode(b"opengrid:pw").decode()}


def test_derived_tables_have_no_internal_rows():
    """Rollups, indices (votes, constituents), fixings and their evidence, structure, events: nothing internal."""
    with W["Session"]() as s:
        tables = [r[0] for r in s.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(:t)"), {"t": [
                "market_hourly", "market_gpu_hourly", "index_levels", "index_fixings", "fixing_evidence",
                "structure_gpu_daily", "structure_provider_daily", "market_events", "event_detector_state"]})]
        counts = {}
        for t in tables:
            rows = [r[0] for r in s.execute(text(f"SELECT row_to_json(x)::text FROM {t} x"))]
            counts[t] = len(rows)
            bad = [f"{t}: {lk}" for row in rows for lk in leaks(json.loads(row))]
            assert not bad, bad[:5]
    assert counts.get("market_hourly", 0) > 1000 and counts.get("index_levels", 0) > 100, counts
    assert counts.get("index_fixings", 0) > 0, ("settlement ran", counts)
    with W["Session"]() as s:
        hidden_hours = s.execute(text("SELECT count(*) FROM market_hourly WHERE provider = :p OR gpu = :g"),
                                 {"p": HIDDEN, "g": ONLY_GPU}).scalar()
    assert hidden_hours == 0


def test_every_public_route_is_clean():
    from analytics import indices

    c = _client()
    with W["Session"]() as s:
        W["index_ids"] = list(s.execute(text("SELECT DISTINCT index_id FROM index_levels ORDER BY 1")).scalars())
        # fixings of the GPU index whose market the internal listings would have moved, and of a composite
        W["fixing_ids"] = list(s.execute(text(
            "SELECT DISTINCT ON (index_id) fixing_id FROM index_fixings ORDER BY index_id, seq DESC")).scalars())
    assert len(W["index_ids"]) >= 5 and W["fixing_ids"], W
    fams = c.get("/v1/families").json().get("data") or []
    if fams:
        W["family"] = fams[0].get("slug") or fams[0].get("id")
    reqs = public_requests()
    templates = {t for t, _, _ in reqs}
    assert len(templates) >= 80, f"route enumeration found only {len(templates)} public GET routes"
    for must in ("/v1/gpus", "/v1/markets/{gpu}", "/v1/indices/{index_id}", "/v1/fixings", "/v1/capacity",
                 "/v1/fx/listings", "/v1/reserved", "/v1/history/{gpu}/archived", "/v1/best/{gpu}", "/listings",
                 "/history", "/market", "/gpu/{slug}", "/v1/trust/listings", "/v1/spreads"):
        assert must in templates, must
    status, bad = {}, []
    for tmpl, url, echo in reqs:
        r = c.get(url, headers=BASIC if tmpl in LEGACY_OPERATOR else {})
        status.setdefault(r.status_code, []).append(url)
        if r.status_code >= 500:
            bad.append(f"{url}: HTTP {r.status_code} {r.text[:200]}")
            continue
        bad += [f"{url}: {lk}" for lk in body_leaks(r, echo)[:3]]
    ok = len(status.get(200, []))
    print(f"   {len(reqs)} public requests over {len(templates)} routes: "
          f"{ {k: len(v) for k, v in sorted(status.items())} }")
    assert not bad, "\n".join(bad[:40])
    assert ok >= 0.8 * len(reqs), {k: v[:8] for k, v in status.items() if k != 200}
    # the internal-only GPU has no public price anywhere
    d = c.get(f"/v1/markets/{gpu_slug(ONLY_GPU)}").json().get("data") or {}
    assert not d.get("low") and not d.get("providers"), d
    assert indices.index_level is not None


def test_public_posts_are_clean():
    """Anonymous POST reads: a recommendation and the assistant (both quote prices)."""
    c = _client()
    lora = {"workload_type": "fine_tuning", "frameworks": ["pytorch", "transformers", "peft"],
            "model": {"name": "mistralai/Mistral-7B-v0.1"}, "training": {"method": "lora"}, "limit": 20}
    r = c.post("/v1/recommend", json=lora)
    assert r.status_code == 200, r.text[:300]
    d = r.json()["data"]
    assert d["options"] and d["tier"] == "public" and all(o["visibility"] == "public" for o in d["options"])
    assert not body_leaks(r), body_leaks(r)[:5]
    for msg in ("cheapest H100 SXM", "compare H100 and RTX 4090", "price of the RTX 4090",
                "What GPU should I use for LoRA fine-tuning of Mistral 7B?", "market context for H100"):
        r = c.post("/v1/assistant", json={"message": msg})
        assert r.status_code == 200, (msg, r.text[:300])
        assert not body_leaks(r), (msg, body_leaks(r)[:5])
        # the operator asking the assistant still gets a public answer
        r = c.post("/v1/assistant", json={"message": msg}, headers=BASIC)
        assert r.status_code == 200 and not body_leaks(r), (msg, body_leaks(r)[:5])


# ---------------------------------------------------------------- both tiers where OpenGrid executes

def test_routing_and_recommend_see_internal():
    from intelligence.recommend import recommend
    from intelligence.requirements import WorkloadRequirements
    from routing import engine, scoring
    from workloads import select as wsel

    cache.clear_all()
    pub = scoring.rank_listings(H100, mode="CHEAPEST", limit=100)
    allt = scoring.rank_listings(H100, mode="CHEAPEST", limit=100, tier="all")
    assert not leaks(pub), leaks(pub)[:5]
    assert {c["visibility"] for c in pub["candidates"]} == {"public"}
    ids = {c["listing_id"]: c for c in allt["candidates"]}
    assert ALPHA_SECRET in ids and any(c["provider"] == HIDDEN for c in allt["candidates"]), sorted(ids)[:10]
    assert ids[ALPHA_SECRET]["visibility"] == "internal" and allt["candidates"][0]["visibility"] == "internal", \
        "the internal listing is the cheapest and wins CHEAPEST"
    assert allt["market"] == pub["market"], "the market reference (median, low) is public-tier only"
    assert pub["candidates_total"] + 2 == allt["candidates_total"], (pub["candidates_total"], allt["candidates_total"])
    assert not any(TOKEN in e["listing_id"] for e in pub["exclusions"]), "an internal row is not even an exclusion"
    # route preview / execution rank both tiers
    spec = {"gpu": H100, "count": 1, "mode": "CHEAPEST", "preferences": {}}
    assert engine._rank(spec)["candidates"][0]["listing_id"] in (ALPHA_SECRET,) or \
        engine._rank(spec)["candidates"][0]["provider"] == HIDDEN
    # job placement: the internal-only GPU is a listed GPU
    assert ONLY_GPU in wsel._listed_gpus("all") and ONLY_GPU not in wsel._listed_gpus("public")
    # recommendations: a key / the operator sees internal options (labelled), an anonymous caller does not
    req = WorkloadRequirements.model_validate({"workload_type": "inference", "model": {"name": "x", "parameters_b": 7}})
    keyed = recommend(req, OPERATOR, limit=20)
    anon = recommend(req, PUBLIC, limit=20)
    assert keyed["tier"] == "all" and anon["tier"] == "public"
    assert any(o["visibility"] == "internal" for o in keyed["options"]), [o["listing_id"] for o in keyed["options"]]
    assert anon["options"] and all(o["visibility"] == "public" for o in anon["options"])
    assert not leaks(anon), leaks(anon)[:5]


def test_admin_view_labels_internal_rows():
    c = _client()
    assert c.get("/v1/ops/internal-data").status_code in (401, 403), "never public"
    r = c.get("/v1/ops/internal-data", headers=BASIC)
    assert r.status_code == 200, r.text[:300]
    d = r.json()["data"]
    assert d["visibility"] == "internal" and {x["provider"] for x in d["registry"]} >= {"deepinfra", "aws"}
    assert {x["listing_id"] for x in d["listings"]} >= {ALPHA_SECRET} and all(
        x["visibility"] == "internal" for x in d["listings"] + d["capacity"])
    assert any(x["provider"] == HIDDEN for x in d["capacity"])


# ---------------------------------------------------------------- the pipeline stamps the tier

def test_save_stamps_the_tier():
    import capacity

    now = W["now"]
    base = dict(sku="s", raw_gpu_name="H100", canonical_gpu_name=H100, gpu_count=1, region=None, country=None,
                price_per_gpu_hour=Decimal("2"), price_per_instance_hour=Decimal("2"), market_type="on_demand",
                provider_tier=None, interruptible=False, available=True, capacity=None, capacity_unit=None, vcpu=None,
                ram_gb=None, storage_gb=None, observed_at=now)
    normalize.save([ComputeListing(provider=HIDDEN, listing_id="claims-public", **base),  # wholly internal provider
                    ComputeListing(provider="syn_gamma", listing_id="claims-internal", visibility="internal", **base),
                    ComputeListing(provider="syn_gamma", listing_id="plain", **base)])
    with W["Session"]() as s:
        got = dict(s.execute(text("SELECT listing_id, visibility FROM compute_listings WHERE listing_id = ANY(:i)"),
                             {"i": ["claims-public", "claims-internal", "plain"]}).all())
    assert got == {"claims-public": "internal", "claims-internal": "internal", "plain": "public"}, got
    capacity.save([CapacityObservation(provider=HIDDEN, sku="cap-claims-public", region=None, raw_gpu_name="H100",
                                       canonical_gpu_name=H100, gpu_count=1, in_stock=True, raw_value="x",
                                       source="/x", observed_at=now)])
    with W["Session"]() as s:
        v = s.execute(text("SELECT visibility FROM capacity_current WHERE sku = 'cap-claims-public'")).scalar()
        ch = s.execute(text("SELECT visibility FROM capacity_changes WHERE sku = 'cap-claims-public'")).scalar()
    assert v == ch == "internal", (v, ch)
    with W["Session"].begin() as s:  # the database refuses a tier that does not exist
        try:
            with s.begin_nested():
                s.execute(text("UPDATE compute_listings SET visibility = 'secret' WHERE listing_id = 'plain'"))
            raise AssertionError("CHECK constraint missing")
        except Exception as exc:  # noqa: BLE001
            assert "ck_listing_visibility" in str(exc), exc


def test_deepinfra_internal_listings_route():
    """DeepInfra's container-API prices: internal listings next to the page's public ones, end to end."""
    from datetime import timedelta as td

    import fetch as fetch_mod
    from providers.deepinfra import API_ENDPOINT, ENDPOINT, DeepInfraProvider
    from routing import scoring

    page = (ROOT / "tests" / "fixtures_providers" / "deepinfra_gpu_instances.html").read_text(encoding="utf-8")
    from providers.deepinfra import extract

    api = json.loads((ROOT / "tests" / "fixtures_providers" / "deepinfra_gpu_availability.json").read_text())
    for g in api["gpus"]:
        g["available"] = True
    t = W["now"] - td(minutes=2)
    fetch_mod.SessionLocal = W["Session"]
    from models import RawResponse
    fetch_mod.save([RawResponse(provider="deepinfra", endpoint=ENDPOINT, fetched_at=t, status_code=200,
                                payload=extract(page)),
                    RawResponse(provider="deepinfra", endpoint=API_ENDPOINT, fetched_at=t, status_code=200, payload=api)])
    normalize.refresh(["deepinfra"])
    with W["Session"]() as s:
        rows = {r.listing_id: (r.visibility, float(r.price_per_gpu_hour)) for r in s.execute(text(
            "SELECT listing_id, visibility, price_per_gpu_hour FROM compute_listings WHERE provider = 'deepinfra'"))}
    assert {k: v[0] for k, v in rows.items()} == {**{f"nvidia-b200:{n}x": "public" for n in (1, 2, 4, 8)},
                                                  **{f"nvidia-b200:{n}x:api": "internal" for n in (1, 2, 4, 8)}}, rows
    cache.clear_all()
    b200 = DeepInfraProvider.normalize({})  # noqa: F841  (no raw: no listings, nothing raised)
    g = "NVIDIA B200 180GB SXM"
    pub = scoring.rank_listings(g, count=8, mode="CHEAPEST")
    allt = scoring.rank_listings(g, count=8, mode="CHEAPEST", tier="all")
    api_price = rows["nvidia-b200:8x:api"][1]
    assert all(c["listing_id"] != "nvidia-b200:8x:api" for c in pub["candidates"])
    assert any(c["listing_id"] == "nvidia-b200:8x:api" and c["visibility"] == "internal" for c in allt["candidates"])
    c = _client()
    for url in ("/v1/gpus/b200-180gb-sxm", "/v1/markets/b200-180gb-sxm", "/v1/best/b200-180gb-sxm?count=8",
                "/listings?q=deepinfra", "/v1/trust/listings?provider=deepinfra"):
        r = c.get(url, headers=BASIC if url.startswith("/listings") else {})
        assert r.status_code == 200, (url, r.status_code)
        assert ":api" not in r.text and f"{api_price:g}" not in r.text, url


TESTS = [test_registry, test_derived_tables_have_no_internal_rows, test_every_public_route_is_clean,
         test_public_posts_are_clean, test_routing_and_recommend_see_internal, test_admin_view_labels_internal_rows,
         test_save_stamps_the_tier, test_deepinfra_internal_listings_route]

if __name__ == "__main__":
    saved = (settings.app_password, settings.public_pages, settings.public_rate_limit_per_minute,
             settings.assistant_public_rate_limit_per_minute, provider_meta.INTERNAL_SOURCES)
    failed = 0
    try:
        test_registry()
        print("ok   test_registry")
        setup_world()
        for t in TESTS[1:]:
            try:
                t()
                print(f"ok   {t.__name__}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}: {exc}")
                traceback.print_exc()
    finally:
        (settings.app_password, settings.public_pages, settings.public_rate_limit_per_minute,
         settings.assistant_public_rate_limit_per_minute, provider_meta.INTERNAL_SOURCES) = saved
        scratchdb.drop(DB)
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)
