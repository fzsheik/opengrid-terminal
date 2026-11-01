"""Verda: public prices always; Cloud API credentials add per-location capacity (yes/no) from instance-availability.

Parsers run on trimmed copies of the REAL responses (tests/fixtures_providers/verda_*.json, fetched 2026-11-01:
8 of the 84 instance types and 2 of the 56 cluster types; the availability, locations, ladder and 401 bodies are
whole). The credentials here are fakes; a mock Verda checks the token flow. Covers: no credentials = no keyed call,
the secret and the token are never stored, one token refresh on a 401, capacity failing open (prices still
returned), and the full pipeline (raw -> listings + capacity) in a scratch database.

Run:  .venv/bin/python tests/test_provider_verda.py
"""

import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (blanks real credentials, refuses real network)
import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
from config import settings  # noqa: E402
from providers.verda import (  # noqa: E402
    AVAILABILITY, CLUSTER_AVAILABILITY, CLUSTER_TYPES, ENDPOINT, LADDER, LOCATIONS, TOKEN, VerdaProvider,
)

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
CID, SECRET = "cid-test", "secret-test-do-not-store"


def load(name):
    return json.loads((FIX / f"verda_{name}.json").read_text())


def snap(payload, request=None, at=NOW):
    return SimpleNamespace(payload=payload, request=request, fetched_at=at)


def by_endpoint(**over):
    base = {
        ENDPOINT: [snap(load("instance_types"))],
        AVAILABILITY: [snap(load("instance_availability"), {"is_spot": False}),
                       snap(load("instance_availability_spot"), {"is_spot": True})],
        LOCATIONS: [snap(load("locations"))],
    }
    base.update(over)
    return base


class FakeVerda:
    """A mock api.verda.com: issues tokens, rejects stale ones, serves the fixtures."""

    def __init__(self, expire_first=0, token_status=200, fail_availability=False):
        self.issued, self.calls, self.expire_first = 0, [], expire_first
        self.token_status, self.fail_availability = token_status, fail_availability

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path, dict(request.url.params), request.headers.get("authorization")))
        if path == TOKEN:
            body = json.loads(request.content)
            if self.token_status != 200 or (body.get("client_id"), body.get("client_secret")) != (CID, SECRET):
                return httpx.Response(self.token_status if self.token_status != 200 else 401,
                                      json={"code": "unauthorized_request", "message": "bad client " + body.get("client_id", "")})
            self.issued += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.issued}", "refresh_token": "r", "expires_in": 3600,
                                             "token_type": "Bearer", "scope": "cloud-api-v1"})
        public = {ENDPOINT: "instance_types", LADDER: "long_term_periods_instances", CLUSTER_TYPES: "cluster_types"}
        if path in public:
            assert request.headers.get("authorization") is None, f"{path} is public: no credentials sent"
            return httpx.Response(200, json=load(public[path]))
        good = f"Bearer tok-{self.issued}"
        if request.headers.get("authorization") != good or self.issued <= self.expire_first:
            return httpx.Response(401, json=load("401"))
        if path == AVAILABILITY:
            if self.fail_availability:
                return httpx.Response(500, json={"code": "server_error", "message": "boom"})
            spot = request.url.params.get("is_spot") == "true"
            return httpx.Response(200, json=load("instance_availability_spot" if spot else "instance_availability"))
        if path == LOCATIONS:
            return httpx.Response(200, json=load("locations"))
        if path == CLUSTER_AVAILABILITY:
            return httpx.Response(200, json=load("cluster_availability"))
        return httpx.Response(404, json={"code": "not_found"})


def prov(fake, creds=True):
    client = httpx.AsyncClient(base_url="https://api.verda.com", transport=httpx.MockTransport(fake))
    return VerdaProvider(client, client_id=CID if creds else None, client_secret=SECRET if creds else None)


def run(p):
    async def go():
        try:
            return await p.fetch()
        finally:
            await p.aclose()
    return asyncio.run(go())


def stored_text(raws):
    return json.dumps([r.model_dump(mode="json") for r in raws])


def to_by_endpoint(raws):
    out = {}
    for r in raws:
        if r.ok:
            out.setdefault(r.endpoint, []).append(snap(r.payload, r.request, r.fetched_at))
    return out


# --------------------------------------------------------------------------- normalize_capacity

def test_capacity_rows():
    rows = VerdaProvider.normalize_capacity(by_endpoint())
    # 7 GPU types in the fixture, all priced on-demand and spot, x 3 locations x 2 markets; the CPU type is skipped.
    assert len(rows) == 7 * 3 * 2, len(rows)
    assert not any(r.sku.startswith("CPU") for r in rows)
    assert {r.region for r in rows} == {"FIN-01", "FIN-02", "FIN-03"} and {r.country for r in rows} == {"FI"}
    assert all(r.tier is None and r.available_count is None and r.total_count is None and r.count_unit is None
               for r in rows), "yes/no only: Verda never says how much"
    key = {(r.sku, r.region): r for r in rows}
    h100 = key[("8H100.80S.176V", "FIN-02")]
    assert h100.in_stock is True and h100.gpu_count == 8 and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.raw_value == "listed at FIN-02 (on-demand)" and h100.source == AVAILABILITY
    assert key[("8H100.80S.176V", "FIN-01")].in_stock is False, "a location that does not list the type: not in stock"
    assert key[("8H100.80S.176V", "FIN-01")].raw_value == "not listed at FIN-01 (on-demand)"
    # The two markets differ, and spot keys on the spot listing_id so it joins to the spot price.
    assert key[("8B200.240V", "FIN-03")].in_stock is False and key[("8B200.240V:spot", "FIN-03")].in_stock is True
    assert key[("1H100.80S.30V:spot", "FIN-01")].source == AVAILABILITY + "?is_spot=true"
    assert all(key[("1A100.22V", loc)].in_stock for loc in ("FIN-01", "FIN-02", "FIN-03"))
    assert sum(r.in_stock for r in rows) == 5 + 8, "on-demand 5 stocked rows, spot 8, in this snapshot"
    # Capacity skus are exactly the listing ids, so capacity joins to price.
    listing_ids = {l.listing_id for l in VerdaProvider.normalize(by_endpoint())}
    assert {r.sku for r in rows} == listing_ids


def test_capacity_edges():
    # No credentials (no availability snapshot): no capacity, prices unaffected.
    only_prices = {ENDPOINT: [snap(load("instance_types"))]}
    assert VerdaProvider.normalize_capacity(only_prices) == []
    assert len(VerdaProvider.normalize(only_prices)) == 14
    # Without /v1/locations the country is unknown, not guessed.
    no_locs = by_endpoint()
    del no_locs[LOCATIONS]
    assert {r.country for r in VerdaProvider.normalize_capacity(no_locs)} == {None}
    # Only the spot half came back this round: spot rows only.
    spot_only = by_endpoint(**{AVAILABILITY: [snap(load("instance_availability_spot"), {"is_spot": True})]})
    assert all(r.sku.endswith(":spot") for r in VerdaProvider.normalize_capacity(spot_only))
    # A type with no spot price has no spot listing, so no spot capacity row either.
    types = copy.deepcopy(load("instance_types"))
    next(t for t in types if t["instance_type"] == "8B200.240V")["spot_price"] = None
    rows = VerdaProvider.normalize_capacity(by_endpoint(**{ENDPOINT: [snap(types)]}))
    assert "8B200.240V:spot" not in {r.sku for r in rows} and "8B200.240V" in {r.sku for r in rows}
    # A reshaped response raises (capacity.py logs it and keeps the previous state) rather than reading as sold out.
    for bad in ({"data": []}, [{"location": "FIN-01"}]):
        try:
            VerdaProvider.normalize_capacity(by_endpoint(**{AVAILABILITY: [snap(bad, {"is_spot": False})]}))
            raise AssertionError(f"reshaped availability must raise: {bad}")
        except ValueError:
            pass


# --------------------------------------------------------------------------- fetch

def test_fetch_without_credentials():
    fake = FakeVerda()
    raws = run(prov(fake, creds=False))
    assert [r.endpoint for r in raws] == [ENDPOINT, LADDER, CLUSTER_TYPES] and all(r.ok for r in raws)
    assert all(path != TOKEN and auth is None for _, path, _, auth in fake.calls), "no keyed call without credentials"
    # The ladder and the cluster catalogue are stored raw; neither becomes a listing.
    listings = VerdaProvider.normalize(to_by_endpoint(raws))
    assert len(listings) == 14 and {l.sku for l in listings} == {t["instance_type"] for t in load("instance_types")} - {"CPU.4V.16G"}


def test_fetch_with_credentials():
    fake = FakeVerda()
    raws = run(prov(fake))
    assert [(r.method, r.endpoint, r.request) for r in raws] == [
        ("GET", ENDPOINT, None), ("GET", LADDER, None), ("GET", CLUSTER_TYPES, None),
        ("GET", AVAILABILITY, {"is_spot": False}), ("GET", AVAILABILITY, {"is_spot": True}),
        ("GET", LOCATIONS, None), ("GET", CLUSTER_AVAILABILITY, None)], [(r.endpoint, r.request) for r in raws]
    assert all(r.ok for r in raws)
    assert raws[3].payload == load("instance_availability"), "stored verbatim (raw-first)"
    assert [c[2] for c in fake.calls if c[1] == AVAILABILITY] == [{"is_spot": "false"}, {"is_spot": "true"}]
    assert sum(1 for c in fake.calls if c[1] == TOKEN) == 1, "one token per poll"
    text = stored_text(raws)
    assert SECRET not in text and "tok-1" not in text and CID not in text, "neither the secret nor the token is stored"
    assert TOKEN not in {r.endpoint for r in raws}, "a successful token call is not stored at all"
    assert len(VerdaProvider.normalize_capacity(to_by_endpoint(raws))) == 42


def test_token_refreshed_once_on_401():
    fake = FakeVerda(expire_first=1)  # the first token is already expired
    raws = run(prov(fake))
    assert all(r.ok for r in raws), [(r.endpoint, r.status_code) for r in raws]
    assert sum(1 for c in fake.calls if c[1] == TOKEN) == 2, "refreshed exactly once"
    # A token that keeps being refused: one refresh per poll, then each 401 is stored as a failure; prices stand.
    fake = FakeVerda(expire_first=10**6)
    raws = run(prov(fake))
    keyed = [r for r in raws if r.endpoint not in (ENDPOINT, LADDER, CLUSTER_TYPES)]
    assert keyed and all(not r.ok and r.status_code == 401 for r in keyed)
    assert sum(1 for c in fake.calls if c[1] == AVAILABILITY) == 3, "only the first 401 is retried"
    assert sum(1 for c in fake.calls if c[1] == TOKEN) == 2, "one refresh per poll, never a loop"
    assert raws[0].ok and len(VerdaProvider.normalize(to_by_endpoint(raws))) == 14
    assert VerdaProvider.normalize_capacity(to_by_endpoint(raws)) == []
    assert SECRET not in stored_text(raws)


def test_capacity_fails_open():
    # Bad credentials: the token failure is stored (status only, no body), prices are untouched.
    fake = FakeVerda(token_status=401)
    raws = run(prov(fake))
    assert [r.endpoint for r in raws] == [ENDPOINT, LADDER, CLUSTER_TYPES, TOKEN]
    tok = raws[-1]
    assert not tok.ok and tok.status_code == 401 and tok.payload is None and tok.request is None
    assert tok.error == "verda: token request failed: HTTP 401", "the token endpoint's body is never stored"
    assert CID not in stored_text(raws) and SECRET not in stored_text(raws)
    assert len(VerdaProvider.normalize(to_by_endpoint(raws))) == 14
    # Availability down: stored as failed, no capacity, prices fine.
    raws = run(prov(FakeVerda(fail_availability=True)))
    assert [r.ok for r in raws if r.endpoint == AVAILABILITY] == [False, False]
    assert VerdaProvider.normalize_capacity(to_by_endpoint(raws)) == [] and raws[0].ok
    # Anything raising inside the capacity half (here: a transport error on the token call) never loses prices.
    def flaky(request):
        if request.url.path == TOKEN:
            raise httpx.ConnectError("down", request=request)
        return FakeVerda()(request)
    raws = run(prov(flaky))
    assert raws[0].ok and raws[0].endpoint == ENDPOINT and not raws[-1].ok
    p = prov(FakeVerda())
    async def boom():
        raise RuntimeError("unexpected")
    p._capacity = boom
    raws = run(p)
    assert [r.endpoint for r in raws] == [ENDPOINT, LADDER, CLUSTER_TYPES], "an exception in capacity drops only capacity"


def test_registry():
    saved = settings.verda_client_id, settings.verda_client_secret
    try:
        for cid, secret, keyed in ((None, None, False), (CID, None, False), (CID, SECRET, True)):
            settings.verda_client_id, settings.verda_client_secret = cid, secret
            verda = [p for p in fetch_mod.build_fetchers() if p.name == "verda"]
            assert len(verda) == 1, "Verda is polled with or without credentials (public prices)"
            assert (verda[0]._creds is not None) is keyed
            for p in fetch_mod.build_fetchers():
                asyncio.run(p.aclose())
    finally:
        settings.verda_client_id, settings.verda_client_secret = saved


# --------------------------------------------------------------------------- pipeline

def test_pipeline():
    import capacity
    import fixtures
    import normalize
    from sqlalchemy import text as sql

    db = "opengrid_test_verda"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        for when in (now - timedelta(hours=1), now - timedelta(minutes=5)):  # two identical polls
            raws = [r.model_copy(update={"fetched_at": when}) for r in run(prov(FakeVerda()))]
            fetch_mod.save(raws)
            out = normalize.refresh(["verda"])
        assert out["capacity"] == {"current": 42, "changes": 0, "pruned": 0}, out["capacity"]
        with Session() as s:
            listed = s.execute(sql("SELECT count(*) FROM compute_listings WHERE provider = 'verda'")).scalar()
            cap = s.execute(sql("SELECT count(*), count(*) FILTER (WHERE in_stock) FROM capacity_current")).one()
            changes = s.execute(sql("SELECT count(*) FROM capacity_changes")).scalar()
            leaked = s.execute(sql("SELECT count(*) FROM raw_snapshots r LEFT JOIN raw_payloads p ON p.sha256 = r.sha256 "
                                   "WHERE r.request::text LIKE :k OR p.payload::text LIKE :k OR r.error LIKE :k "
                                   "OR p.payload::text LIKE :t"), {"k": f"%{SECRET}%", "t": "%tok-%"}).scalar()
            joined = s.execute(sql("SELECT count(*) FROM capacity_current c JOIN compute_listings l "
                                   "ON l.provider = c.provider AND l.listing_id = c.sku")).scalar()
        assert listed == 14 and tuple(cap) == (42, 13) and changes == 42, (listed, cap, changes)
        assert leaked == 0, "no secret or token in raw storage"
        assert joined == 42, "every capacity row joins to a price listing"
        assert "verda" in capacity.supported()
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_capacity_rows, test_capacity_edges, test_fetch_without_credentials, test_fetch_with_credentials,
              test_token_refreshed_once_on_401, test_capacity_fails_open, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
