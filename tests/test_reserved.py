"""Reserved prices (reserved.py, api/reserved.py): the model from saved real responses for each provider, the saving
calculation and on-demand matching, Verda's ladder derivation, the hard rule (listings, hourly market rows and
index votes are identical with and without reserved data; nothing is written; nothing statistical imports the model),
and the API shape. No network: every response is a fixture.

Fixtures (all real responses, saved earlier by the provider tests):
    fixtures_providers/azure_{eastus,eastus2}.json, azure_reservation_*.json   Azure consumption + reservation
    fixtures_providers/together_pricing.html                                  Together GPU Clusters page
    fixtures_providers/verda_instance_types.json, verda_long_term_*.json      Verda prices + commitment ladder
    fixtures_providers/gcp_catalog_skus.json                                  GCP SKUs incl. Commit1Yr / Commit3Yr
    fixtures_providers/jarvislabs_server_meta.json                            Jarvislabs incl. reserved_pricing
    fixtures_capacity/latitude.json, latitude_plans.json                      Shadeform + Latitude plans

Run:  .venv/bin/python tests/test_reserved.py
"""

import copy
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import reserved  # noqa: E402
from models import RawResponse  # noqa: E402
from providers import PROVIDERS, azure, gcp, jarvislabs, shadeform, together, verda  # noqa: E402

FP, FC = ROOT / "tests" / "fixtures_providers", ROOT / "tests" / "fixtures_capacity"
T = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def _j(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def stored(at=T) -> dict[str, dict[str, object]]:
    """provider -> {endpoint: payload}, as each provider stores its responses (on-demand and reserved)."""
    out = {"azure": {}}
    for r in azure.REGIONS:
        rows, _ = azure.extract(_j(FP / f"azure_{r}.json"), r)
        out["azure"][azure.endpoint(r)] = {"region": r, "items": rows}
        rows, _ = azure.extract(_j(FP / f"azure_reservation_{r}.json"), r, azure.RESERVATION)
        out["azure"][azure.endpoint(r, azure.RESERVATION)] = {"region": r, "price_type": azure.RESERVATION, "items": rows}
    html = (FP / "together_pricing.html").read_text(encoding="utf-8")
    out["together"] = {together.ENDPOINT: together.extract(html), together.RESERVED_ENDPOINT: together.extract_reserved(html)}
    out["verda"] = {verda.ENDPOINT: _j(FP / "verda_instance_types.json"), verda.LADDER: _j(FP / "verda_long_term_periods_instances.json")}
    out["gcp"] = {gcp.ENDPOINT: _j(FP / "gcp_catalog_skus.json")}
    out["jarvislabs"] = {jarvislabs.ENDPOINT: _j(FP / "jarvislabs_server_meta.json")}
    out["latitude"] = {**{ep: p[0] for ep, p in _j(FC / "latitude.json").items()}, shadeform.LATITUDE_PLANS: _j(FC / "latitude_plans.json")}
    return out


# Endpoints that hold ONLY reserved data (GCP and Jarvislabs keep theirs inside the on-demand body).
RESERVED_ONLY = {(p, ep) for p in ("azure",) for ep in (azure.endpoint(r, azure.RESERVATION) for r in azure.REGIONS)} | {
    ("together", together.RESERVED_ENDPOINT), ("verda", verda.LADDER), ("latitude", shadeform.LATITUDE_PLANS)}


def by_endpoint(payloads: dict, at=T) -> dict:
    return {ep: [SimpleNamespace(payload=copy.deepcopy(p), fetched_at=at, request=None, endpoint=ep)] for ep, p in payloads.items()}


def model(edit=None, at=T):
    raw = {p: by_endpoint(eps, at) for p, eps in stored(at).items()}
    if edit:
        edit(raw)
    listings = []
    for p, be in raw.items():
        try:  # as normalize_all: a reshaped response costs that provider its listings, nothing else
            listings += [lst.model_dump() for lst in PROVIDERS[p].normalize(be)]
        except ValueError:
            pass
    return reserved.build(raw, listings, at)


def pick(rows, **kw):
    return [r for r in rows if all(r[k] == v for k, v in kw.items())]


# --------------------------------------------------------------------------- per provider

def test_azure():
    rows, notes = model()
    h100 = {r["term_bucket"]: r for r in pick(rows, provider="azure", sku="Standard_ND96isr_H100_v5", region="eastus")}
    assert set(h100) == {"1y", "3y", "5y"}
    for key, years, term_price, per_gpu, saving in (("1y", 1, 551221.0, 7.8656, 36.0), ("3y", 3, 1134310.0, 5.3953, 56.1),
                                                     ("5y", 5, 1722566.0, 4.9160, 60.0)):
        r = h100[key]
        assert r["term_price"] == term_price and r["term_hours"] == 8760 * years and r["term_months"] == 12 * years
        assert abs(r["price_per_gpu_hour"] - term_price / (8760 * years) / 8) < 1e-6 and round(r["price_per_gpu_hour"], 4) == per_gpu
        assert r["on_demand_per_gpu_hour"] == 12.29 and r["on_demand_match"] == "same_listing"
        assert r["on_demand_listing_id"] == "Standard_ND96isr_H100_v5:eastus" and r["saving_pct"] == saving
        assert r["derived"] is False and r["kind"] == "observed" and r["scope"] == "instance" and r["gpu_count"] == 8
        assert r["gpu"] == "NVIDIA H100 80GB SXM5" and r["term_label"] == ("1 Year" if years == 1 else f"{years} Years")
    assert len(pick(rows, provider="azure")) == 22 and not notes[:0]


def test_gcp_gpu_only():
    rows, _ = model()
    g = pick(rows, provider="gcp")
    assert g and all(r["scope"] == "gpu_only" and r["gpu_count"] == 1 and r["region"] == "us-central1" for r in g)
    assert all(r["on_demand_match"] == "same_source" and r["on_demand_listing_id"] is None for r in g), \
        "a GPU-only commitment is never compared with a whole-VM listing"
    h = pick(rows, provider="gcp", sku="Commitment v1: Nvidia H100 80GB GPU running in Americas for 3 Years")[0]
    assert h["term_bucket"] == "3y" and h["term_label"] == "Commit3Yr" and h["price_per_gpu_hour"] == 4.299899
    assert h["on_demand_per_gpu_hour"] == 9.796551 and h["saving_pct"] == 56.1 and h["term_price"] is None
    summ = reserved.summary(rows)
    assert not any(t["provider"] == "gcp" for s in summ for t in s["terms"].values()), "GPU-only rows are not 'cheapest'"
    assert not any(r["sku"] and "Calendar" in r["sku"] for r in g), "Calendar Mode is not a term commitment"


def test_jarvislabs():
    rows, _ = model()
    a30 = {r["term_label"]: r for r in pick(rows, provider="jarvislabs", region="india-noida-01", sku="NVIDIA A30 24GB:1x:container")}
    assert set(a30) == {"reserved_1m", "reserved_3m", "reserved_6m", "reserved_1y", "reserved_2y"}
    assert [a30[k]["term_months"] for k in ("reserved_1m", "reserved_3m", "reserved_6m", "reserved_1y", "reserved_2y")] == [1, 3, 6, 12, 24]
    r = a30["reserved_1y"]
    assert r["price_per_gpu_hour"] == 0.38 and r["on_demand_per_gpu_hour"] == 0.41 and r["on_demand_match"] == "same_listing"
    assert r["saving_pct"] == round((0.41 - 0.38) / 0.41 * 100, 1)

    def inr(raw):
        raw["jarvislabs"][jarvislabs.ENDPOINT][0].payload["currency"] = "INR"
    rows, notes = model(inr)
    assert not pick(rows, provider="jarvislabs") and any(n.startswith("jarvislabs:") and "INR" in n for n in notes), "non-USD skipped, with a note"


def test_latitude():
    rows, notes = model()
    big = {r["term_label"]: r for r in pick(rows, provider="latitude", sku="g4-rtx6kpro-large", region="United States")}
    assert set(big) == {"month", "year"}
    y = big["year"]
    assert y["term_price"] == 147168.0 and y["term_hours"] == 8760 and y["gpu_count"] == 8 and y["price_per_gpu_hour"] == 2.1
    assert big["month"]["term_hours"] == 730 and big["month"]["price_per_gpu_hour"] == 3.0
    assert y["on_demand_per_gpu_hour"] == 6.0 and y["on_demand_match"] == "same_source" and y["saving_pct"] == 65.0, \
        "the same plan's own hourly price beats a Shadeform listing that only shares the GPU"
    small = pick(rows, provider="latitude", sku="g3-h100-small", term_label="year")[0]
    assert small["gpu"] == "NVIDIA H100 80GB PCIe" and small["on_demand_per_gpu_hour"] == 3.37
    assert any(n.startswith("latitude:") and "BRL" in n for n in notes), "BRL prices skipped with a note"
    assert all(r["currency"] == "USD" for r in rows)


def test_together():
    rows, _ = model()
    t = pick(rows, provider="together")
    assert t and all(r["gpu_count"] == 1 and r["region"] is None and r["term_price"] is None for r in t)
    h = {r["term_label"]: r for r in t if r["sku"] == "NVIDIA HGX H100"}
    assert {k: v["term_bucket"] for k, v in h.items()} == {"3-30 days": "lt1m", "31-90 days": "1m", "91-180 days": "3m"}
    assert h["3-30 days"]["term_min_days"] == 3 and h["3-30 days"]["term_max_days"] == 30
    assert h["91-180 days"]["price_per_gpu_hour"] == 3.19 and h["91-180 days"]["on_demand_per_gpu_hour"] == 3.99
    assert h["91-180 days"]["on_demand_listing_id"] == "nvidia-hgx-h100" and h["91-180 days"]["saving_pct"] == 20.1
    assert "181+ days" not in h, "'Contact us' is not a price"
    assert reserved.term_bucket(D(181) / reserved.DAYS_PER_MONTH) == "6m"


# --------------------------------------------------------------------------- ladder derivation

def test_verda_ladder():
    rows, _ = model()
    v = pick(rows, provider="verda")
    listings = [lst for lst in PROVIDERS["verda"].normalize(by_endpoint(stored()["verda"])) if lst.market_type == "on_demand"]
    assert len(v) == 5 * len(listings), "every rung for every current on-demand listing"
    assert all(r["derived"] and r["kind"] == "derived" and r["on_demand_match"] == "same_listing" for r in v)
    one = {r["term_label"]: r for r in v if r["sku"] == listings[0].sku}
    od = float(listings[0].price_per_gpu_hour)
    for label, pct in (("1 month", 2), ("3 months", 3), ("6 months", 4), ("1 year", 8), ("2 years", 25)):
        r = one[label]
        assert r["discount_pct"] == pct and r["saving_pct"] == pct
        assert abs(r["price_per_gpu_hour"] - od * (1 - pct / 100)) < 1e-6 and r["on_demand_per_gpu_hour"] == round(od, 6)

    def ladder_edit(raw):
        rungs = raw["verda"][verda.LADDER][0].payload
        rungs[0]["is_enabled"] = False             # disabled: not offered
        rungs[1]["unit_name"] = "fortnight"         # unknown unit: kept raw, never guessed
        rungs[2]["discount_percentage"] = 140       # nonsense: skipped
    rows, _ = model(ladder_edit)
    assert {r["term_label"] for r in pick(rows, provider="verda")} == {"1 year", "2 years"}

    def no_listings(raw):
        raw["verda"].pop(verda.ENDPOINT)
    rows, _ = model(no_listings)
    assert not pick(rows, provider="verda"), "no on-demand price: nothing to derive from"


# --------------------------------------------------------------------------- saving + matching

def _r(**kw):
    base = dict(gpu="G", raw_gpu_name="G", gpu_count=8, region="r1", sku="s", term_label="1y",
                term_months=D(12), price_per_gpu_hour=D("3"), observed_at=T)
    base.update(kw)
    return reserved._row("azure", **base)


def _l(**kw):
    base = dict(provider="azure", listing_id="x", canonical_gpu_name="G", gpu_count=8, region="r1",
                price_per_gpu_hour=D("4"), currency="USD", market_type="on_demand", interruptible=False, observed_at=T)
    base.update(kw)
    return base


def test_saving_and_matching():
    rows = reserved.attach_on_demand([_r(listing_hint="exact")], [_l(listing_id="exact", price_per_gpu_hour=D("5")), _l()])
    assert rows[0]["on_demand_match"] == "same_listing" and rows[0]["on_demand_per_gpu_hour"] == D("5")
    assert rows[0]["saving_pct"] == (D(5) - D(3)) / D(5) * 100 == 40
    rows = reserved.attach_on_demand([_r()], [_l(region="r2", price_per_gpu_hour=D("2")), _l(listing_id="y")])
    assert rows[0]["on_demand_match"] == "same_size_region" and rows[0]["on_demand_listing_id"] == "y"
    rows = reserved.attach_on_demand([_r()], [_l(region="r2", listing_id="a", price_per_gpu_hour=D("6")),
                                              _l(region="r3", listing_id="b", price_per_gpu_hour=D("5"))])
    assert rows[0]["on_demand_match"] == "same_size" and rows[0]["on_demand_listing_id"] == "b", "cheapest other region"
    nothing = [_l(gpu_count=1), _l(canonical_gpu_name="Other"), _l(provider="gcp"), _l(market_type="spot"),
               _l(interruptible=True), _l(currency="EUR")]
    rows = reserved.attach_on_demand([_r()], nothing)
    assert rows[0]["on_demand_match"] is None and rows[0]["saving_pct"] is None, \
        "never another size, GPU, provider, market or currency"
    rows = reserved.attach_on_demand([_r(price_per_gpu_hour=D("5"))], [_l()])
    assert rows[0]["saving_pct"] == -25, "a commitment dearer than on-demand is shown as it is"
    rows = reserved.attach_on_demand([_r(scope="gpu_only", same_source_on_demand=D("6"))], [_l()])
    assert rows[0]["on_demand_match"] == "same_source" and rows[0]["on_demand_per_gpu_hour"] == D("6")
    assert [reserved.term_bucket(m) for m in (0.1, 0.99, 1, 2.99, 5.95, 12, 24, 36, 60, 100)] == \
        ["lt1m", "1m", "1m", "3m", "6m", "1y", "2y", "3y", "5y", "5y"]


def test_freshness_and_failures():
    rows, _ = reserved.build({p: by_endpoint(e) for p, e in stored().items()}, [], T + timedelta(hours=4),
                             {"azure": 3600, "together": 3600})
    assert all(r["stale"] for r in pick(rows, provider="azure")), "older than 3 polls: stale"

    def broken(raw):
        raw["jarvislabs"][jarvislabs.ENDPOINT][0].payload = {"server_meta": "nope"}
    rows, notes = model(broken)
    assert pick(rows, provider="azure") and not pick(rows, provider="jarvislabs")
    assert any(n.startswith("jarvislabs:") for n in notes), "a reshaped source drops out with a note, never takes the rest"
    rows, notes = reserved.build({}, [])
    assert rows == [] and notes == [], "nothing stored: empty, not an error"


# --------------------------------------------------------------------------- the hard rule

def test_never_imported_by_statistics():
    pat = re.compile(r"^\s*(import reserved\b|from reserved import)", re.M)
    users = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                   if ".venv" not in p.parts and "tests" not in p.parts and pat.search(p.read_text(encoding="utf-8", errors="ignore")))
    assert users == ["api/reserved.py"], f"only the read-only API imports the reserved model: {users}"


DB = "og_test_reserved"


def _responses(at, with_reserved: bool) -> list[RawResponse]:
    out = []
    for p, eps in stored(at).items():
        for ep, payload in eps.items():
            if (p, ep) in RESERVED_ONLY and not with_reserved:
                continue
            out.append(RawResponse(provider=p, endpoint=ep, fetched_at=at, status_code=200, ok=True, payload=payload))
    return out


def _state(Session):
    from sqlalchemy import text as sql

    from analytics import indices
    with Session() as s:
        listings = sorted(tuple(r) for r in s.execute(sql(
            "SELECT provider, listing_id, market_type, price_per_gpu_hour, gpu_count FROM compute_listings")))
        hourly = [dict(r._mapping) for r in s.execute(sql(
            "SELECT segment, gpu, provider, region, country, hour, min_price FROM market_hourly WHERE min_price IS NOT NULL "
            "ORDER BY segment, gpu, provider, region, country, hour"))]
    return listings, hourly, indices.votes_by_index(hourly)


def _counts(Session):
    from sqlalchemy import text as sql
    with Session() as s:
        tables = [t for (t,) in s.execute(sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))]
        return {t: s.execute(sql(f'SELECT count(*) FROM "{t}"')).scalar() for t in tables}


def test_hard_rule_and_api():
    import fixtures
    import config
    import fetch
    import normalize
    from analytics import rollups

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        fetch.SessionLocal = Session
        normalize.SessionLocal = Session
        providers = sorted(stored())
        now = datetime.now(timezone.utc).replace(microsecond=0)
        polls = (now - timedelta(hours=3), now - timedelta(minutes=5))  # two polls, so the rollup has whole hours

        for at in polls:
            fetch.save(_responses(at, with_reserved=False))
            normalize.refresh(providers)
        rollups.rebuild()
        before = _state(Session)
        assert before[0] and before[1] and before[2], "the on-demand pipeline produced listings, hourly rows and votes"

        for at in polls:  # reserved responses beside the same on-demand ones, same fetch times
            fetch.save([r for r in _responses(at, with_reserved=True) if (r.provider, r.endpoint) in RESERVED_ONLY])
        normalize.refresh(providers)
        rollups.rebuild()
        after = _state(Session)
        assert after[0] == before[0], "listings identical with reserved data stored"
        assert after[1] == before[1], "hourly market rows identical"
        assert after[2] == before[2], "index votes identical"
        assert {r["segment"] for r in after[1]} <= {"on_demand", "spot"}
        assert not any("reserv" in i or "commit" in i for i in after[2])
        assert not any(mt not in ("on_demand", "spot") for _, _, mt, _, _ in after[0])

        # the model reads it, and writes nothing
        counts = _counts(Session)
        rows, notes, newest = reserved.load()
        assert {r["provider"] for r in rows} == set(reserved.PROVIDERS) and newest is not None
        h100 = pick(rows, provider="azure", sku="Standard_ND96isr_H100_v5", region="eastus", term_bucket="3y")[0]
        assert h100["on_demand_per_gpu_hour"] == 12.29 and h100["on_demand_match"] == "same_listing" and not h100["stale"]

        config.settings.app_password = None
        import main
        from api import pages
        from api import reserved as reserved_api
        from fastapi.testclient import TestClient

        reserved_api._model.cache_clear()
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        body = client.get("/v1/reserved").json()
        assert set(body) == {"data", "meta"}
        m = body["meta"]
        assert m["methodology"] == "/methodology/reserved-prices" and m["kind"] == "observed" and m["count"] == len(rows)
        assert m["providers"] == list(reserved.PROVIDERS) and m["derived"] == len(pick(rows, provider="verda")) > 0
        assert [t["key"] for t in m["terms"]] == list(reserved.BUCKET_KEYS) and "never" in m["rule"]
        assert any("BRL" in n for n in m["notes"]) and set(m["sources"]) == set(reserved.PROVIDERS)
        r0 = body["data"][0]
        for k in ("provider", "gpu", "gpu_count", "region", "term_label", "term_months", "term_bucket", "price_per_gpu_hour",
                  "term_price", "on_demand_per_gpu_hour", "on_demand_match", "saving_pct", "derived", "kind", "scope",
                  "source", "method", "observed_at", "stale", "currency"):
            assert k in r0, k
        assert not any(k.startswith("_") for k in r0), "internal hints are never served"
        az = client.get("/v1/reserved", params={"provider": "azure", "term": "5y", "gpu": "h100-80gb-sxm5"}).json()["data"]
        assert az and all(r["provider"] == "azure" and r["term_bucket"] == "5y" and r["gpu"] == "NVIDIA H100 80GB SXM5" for r in az)
        assert client.get("/v1/reserved", params={"provider": "aws"}).status_code == 404
        assert client.get("/v1/reserved", params={"term": "7y"}).status_code == 422
        assert client.get("/v1/reserved", params={"gpu": "not-a-gpu"}).status_code == 404
        s = client.get("/v1/reserved/summary", params={"gpu": "h100-80gb-sxm5"}).json()
        assert s["meta"]["count"] == 1 and s["data"][0]["gpu"] == "NVIDIA H100 80GB SXM5"
        terms = s["data"][0]["terms"]
        assert list(terms) == [k for k in reserved.BUCKET_KEYS if k in terms] and {"1y", "3y", "5y"} <= set(terms)
        assert terms["5y"]["provider"] == "azure" and all(t["provider"] != "gcp" for t in terms.values())
        assert client.get("/reserved").status_code == 200, "the page is served"
        assert _counts(Session) == counts, "reading the model and serving the API wrote nothing"

        # public like /v1/capacity; the private price-lock area stays private
        from starlette.requests import Request

        def req(path):
            return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})
        assert pages.is_public(req("/v1/reserved")) and pages.is_public(req("/v1/reserved/summary"))
        assert pages.is_public(req("/v1/capacity")) and not pages.is_public(req("/v1/reservations"))
    finally:
        scratchdb.drop(DB)


if __name__ == "__main__":
    for t in (test_azure, test_gcp_gpu_only, test_jarvislabs, test_latitude, test_together, test_verda_ladder,
              test_saving_and_matching, test_freshness_and_failures, test_never_imported_by_statistics,
              test_hard_rule_and_api):
        t()
        print(t.__name__, "ok")
    print("all passed")
