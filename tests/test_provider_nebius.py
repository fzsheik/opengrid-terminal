"""Nebius: on-demand from the price page (current design, with a preemptible column) and spot from the public
spot-price API, failing independently.

Fixtures are REAL responses fetched 2026-11-01 with unauthenticated GETs:
  nebius_prices_2026-11.html  nebius.com/prices trimmed to its __NEXT_DATA__ "NVIDIA GPU Instances" block (plus the
                              CPU-only table block as a decoy), wrapped the way the page wraps it
  nebius_spot_price.json      GET api.nebius.cloud/billing/v1alpha1/spot-price, whole (12 SKUs)
The older page design (separate preemptible table) is covered in tests/test_provider_pages.py.

Run:  .venv/bin/python tests/test_provider_nebius.py
"""

import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import scratchdb  # noqa: E402,F401  (blanks real credentials, refuses real network)
import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers import nebius as N  # noqa: E402
from providers.nebius import ENDPOINT, SPOT_ENDPOINT, NebiusProvider  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_providers"
NOW = datetime(2026, 11, 1, 21, tzinfo=timezone.utc)
PAGE = (FIX / "nebius_prices_2026-11.html").read_text(encoding="utf-8")
SPOT = json.loads((FIX / "nebius_spot_price.json").read_text(encoding="utf-8"))


def snap(payload, endpoint, at=NOW):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at, endpoint=endpoint, ok=True)


def by_id(listings):
    return {l.listing_id: l for l in listings}


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


class FakeNebius:
    """Serves the page and the spot API; records the spot calls."""

    def __init__(self, page=PAGE, spot=None, spot_status=200, pages=1):
        self.page, self.spot, self.spot_status, self.pages, self.calls = page, spot or SPOT, spot_status, pages, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == ENDPOINT:
            return httpx.Response(200, text=self.page, headers={"content-type": "text/html"})
        if request.url.path == SPOT_ENDPOINT:
            assert request.headers.get("authorization") is None, "the spot API is public: no credentials sent"
            self.calls.append(dict(request.url.params))
            if self.spot_status != 200:
                return httpx.Response(self.spot_status, json={"code": 13, "message": "internal"})
            if self.pages == 1:
                return httpx.Response(200, json=self.spot)
            # Split the items over `pages` pages, chained by nextPageToken.
            n = int(request.url.params.get("pageToken") or 0)
            items = self.spot["items"]
            size = -(-len(items) // self.pages)
            body = {"items": items[n * size:(n + 1) * size], "snapshotTime": self.spot["snapshotTime"]}
            if n + 1 < self.pages:
                body["nextPageToken"] = str(n + 1)
            return httpx.Response(200, json=body)
        return httpx.Response(404, text="not found")


def prov(fake):
    t = httpx.MockTransport(fake)
    return NebiusProvider(httpx.AsyncClient(base_url=N.BASE_URL, transport=t),
                          httpx.AsyncClient(base_url=N.API_BASE_URL, transport=t))


def run(p):
    try:
        return asyncio.run(p.fetch())
    finally:
        asyncio.run(p.aclose())


# --------------------------------------------------------------------------
# The page: on-demand is back
# --------------------------------------------------------------------------


def test_page_with_preemptible_column():
    payload = N.extract(PAGE)
    # Raw-first: the whole table is stored, preemptible column included.
    assert payload["header"] == ["Item", "vCPUs", "RAM, GB", "Preemptible, GPU-hour\xa0*", "On-demand, GPU-hour"]
    assert len(payload["rows"]) == 9 and payload["rows"][4][3] == "from $0.79"
    out = by_id(NebiusProvider.normalize({ENDPOINT: [snap(payload, ENDPOINT)]}))
    assert len(out) == 7, sorted(out)
    # The same ids, names and prices as the Oct 13 design (tests/test_provider_pages.py), so history continues.
    assert {k: v.price_per_gpu_hour for k, v in out.items()} == {
        "NVIDIA HGX B300": D("9.50"), "NVIDIA HGX B200": D("8.50"), "NVIDIA HGX H200": D("5.40"),
        "NVIDIA HGX H100": D("4.50"), "NVIDIA RTX PRO 6000": D("1.80"), "NVIDIA L40S with Intel CPU": D("1.55"),
        "NVIDIA L40S with AMD CPU": D("1.82")}
    h100 = out["NVIDIA HGX H100"]
    assert h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5" and h100.vcpu == 16 and h100.ram_gb == 200
    assert out["NVIDIA L40S with Intel CPU"].provider_tier == "from_price"
    assert out["NVIDIA RTX PRO 6000"].canonical_gpu_name is None, "edition not stated on the page: unmapped"
    assert all(l.market_type == "on_demand" and not l.interruptible and l.region is None for l in out.values())
    # The preemptible floors ($0.99 / $0.79 / $0.74 / $0.90) never become listings.
    assert {l.price_per_gpu_hour for l in out.values()}.isdisjoint({D("0.99"), D("0.79"), D("0.74"), D("0.90")})

    # Fail closed on columns that are not on-demand or preemptible.
    def edit(old, new):
        q = lambda s: s.replace('"', '\\"')
        assert q(old) in PAGE, old
        return PAGE.replace(q(old), q(new))

    assert raises(N.extract, edit('"On-demand, GPU-hour"', '"Reserved, GPU-hour"')), "a reserved column"
    assert raises(N.extract, edit('"On-demand, GPU-hour"', '"Preemptible, GPU-hour (2)"')), "no on-demand column"
    assert raises(N.extract, edit('"RAM, GB"', '"Memory"')), "lead columns changed"
    assert raises(N.extract, edit('"title":"NVIDIA GPU Instances"', '"title":"GPUs"')), "no table: zero rows fails"
    # A dated on-demand column next to the preemptible one is still picked by date.
    dated = {"header": payload["header"] + ["GPU-hour (Effective November 15, 2026)"],
             "rows": [r + ["$4.95"] for r in payload["rows"]]}
    later = by_id(NebiusProvider.normalize({ENDPOINT: [snap(dated, ENDPOINT, datetime(2026, 11, 15, tzinfo=timezone.utc))]}))
    assert by_id(NebiusProvider.normalize({ENDPOINT: [snap(dated, ENDPOINT)]}))["NVIDIA HGX H100"].price_per_gpu_hour == D("4.50")
    assert later["NVIDIA HGX H100"].price_per_gpu_hour == D("4.95")


# --------------------------------------------------------------------------
# The spot API
# --------------------------------------------------------------------------


def test_spot_listings():
    out = by_id(NebiusProvider.normalize({SPOT_ENDPOINT: [snap(SPOT, SPOT_ENDPOINT)]}))
    assert len(out) == 12, sorted(out)
    h100 = out["NVIDIA HGX H100:gpu-h100-sxm:eu-north1:spot"]
    assert h100.price_per_gpu_hour == D("0.79") == h100.price_per_instance_hour and h100.gpu_count == 1
    assert h100.market_type == "spot" and h100.interruptible and h100.region == "eu-north1" and h100.country == "FI"
    assert h100.raw_gpu_name == "NVIDIA HGX H100" and h100.canonical_gpu_name == "NVIDIA H100 80GB SXM5"
    assert h100.sku == "NVIDIA HGX H100:gpu-h100-sxm" and h100.currency == "USD" and h100.observed_at == NOW
    # The two B200 host variants are the same GPU, one listing per (platform, region).
    b200 = out["NVIDIA HGX B200:gpu-b200-sxm-a:me-west1:spot"]
    assert b200.canonical_gpu_name == "NVIDIA B200 180GB SXM" and b200.price_per_gpu_hour == D("2.956")
    assert b200.country == "IL"
    assert out["NVIDIA HGX B300:gpu-b300-sxm:uk-south1:spot"].country == "GB"
    assert out["NVIDIA HGX H200:gpu-h200-sxm:eu-west1:spot"].price_per_gpu_hour == D("2")
    # Same raw names as the page's rows, so spot and on-demand map to one canonical GPU.
    page = {l.raw_gpu_name: l.canonical_gpu_name for l in NebiusProvider.normalize({ENDPOINT: [snap(N.extract(PAGE), ENDPOINT)]})}
    assert all(page[l.raw_gpu_name] == l.canonical_gpu_name for l in out.values()), "a spot name the page lacks"
    rtx = [l for l in out.values() if l.raw_gpu_name == "NVIDIA RTX PRO 6000"]
    assert len(rtx) == 3 and all(l.canonical_gpu_name is None for l in rtx), "unmapped, like the page row"
    assert all(l.listing_id.endswith(":spot") and l.vcpu is None and l.provider_tier is None for l in out.values())

    # An unknown platform keeps the API's own title and stays unmapped, never guessed.
    new = copy.deepcopy(SPOT)
    new["items"][0]["computeInstance"]["platform"] = "gpu-x900-sxm"
    new["items"][0]["productTitle"] = "Preemptible NVIDIA® X900 NVLink"
    x = by_id(NebiusProvider.normalize({SPOT_ENDPOINT: [snap(new, SPOT_ENDPOINT)]}))["NVIDIA X900 NVLink:gpu-x900-sxm:eu-north1:spot"]
    assert x.raw_gpu_name == "NVIDIA X900 NVLink" and x.canonical_gpu_name is None


def test_spot_rows_fail_closed():
    def bad(mutate):
        body = copy.deepcopy(SPOT)
        mutate(body)
        return raises(N.spot_rows, body)

    assert bad(lambda b: b.update(items=[])), "zero rows is a failure, not an empty market"
    assert bad(lambda b: b.pop("items")), "no items list"
    assert raises(N.spot_rows, None) and raises(N.spot_rows, []), "not an object"
    assert bad(lambda b: b["items"][0]["price"].update(unitTitle="GPU month")), "not per GPU hour"
    assert bad(lambda b: b["items"][0]["price"].update(currency="eur")), "not USD"
    assert bad(lambda b: b["items"][0]["price"].update(value="n/a")), "not a decimal"
    assert bad(lambda b: b["items"][0]["price"].update(value="0")), "not positive"
    assert bad(lambda b: b["items"][0].pop("region")), "no region"
    assert bad(lambda b: b["items"][0].pop("computeInstance")), "no platform"
    assert bad(lambda b: b["items"].append(copy.deepcopy(b["items"][0]))), "two prices for one platform/region"


# --------------------------------------------------------------------------
# Fetch: two sources, failing independently
# --------------------------------------------------------------------------


def test_fetch_both():
    fake = FakeNebius()
    page, spot = run(prov(fake))
    assert page.ok and page.endpoint == ENDPOINT and page.payload == N.extract(PAGE)
    assert spot.ok and spot.endpoint == SPOT_ENDPOINT and spot.payload["items"] == SPOT["items"], "items verbatim"
    assert spot.payload["snapshotTime"] == SPOT["snapshotTime"]
    assert fake.calls == [{"pageSize": "100"}]


def test_fetch_pages_through_spot():
    fake = FakeNebius(pages=3)
    _, spot = run(prov(fake))
    assert spot.ok and spot.payload["items"] == SPOT["items"], "every page in one stored response"
    assert [c.get("pageToken") for c in fake.calls] == [None, "1", "2"]


def test_spot_failure_keeps_on_demand():
    for fake in (FakeNebius(spot_status=500), FakeNebius(spot={"items": [], "snapshotTime": "2026-11-01T21:00:04Z"}),
                 FakeNebius(spot={"unexpected": True})):
        page, spot = run(prov(fake))
        assert page.ok and not spot.ok and spot.error, spot
        listings = NebiusProvider.normalize({ENDPOINT: [snap(page.payload, ENDPOINT)]})
        assert len(listings) == 7
    _, empty = run(prov(FakeNebius(spot={"items": [], "snapshotTime": "x"})))
    assert "no items" in empty.error, "zero spot rows is recorded as a failure"
    # A page-level failure mid-pagination stores nothing partial.
    class Flaky(FakeNebius):
        def __call__(self, request):
            if request.url.params.get("pageToken") == "1":
                return httpx.Response(503, text="unavailable")
            return super().__call__(request)
    _, spot = run(prov(Flaky(pages=2)))
    assert not spot.ok and spot.status_code == 503


def test_page_failure_keeps_spot():
    page, spot = run(prov(FakeNebius(page="<html>redesigned</html>")))
    assert not page.ok and "ValueError" in page.error and spot.ok
    assert len(NebiusProvider.normalize({SPOT_ENDPOINT: [snap(spot.payload, SPOT_ENDPOINT)]})) == 12


def test_normalize_halves_independent():
    page_ok, spot_ok = snap(N.extract(PAGE), ENDPOINT), snap(SPOT, SPOT_ENDPOINT)
    page_bad, spot_bad = snap({"header": ["x"], "rows": []}, ENDPOINT), snap({"items": []}, SPOT_ENDPOINT)
    assert len(NebiusProvider.normalize({ENDPOINT: [page_ok], SPOT_ENDPOINT: [spot_ok]})) == 19
    assert len(NebiusProvider.normalize({ENDPOINT: [page_ok], SPOT_ENDPOINT: [spot_bad]})) == 7
    assert len(NebiusProvider.normalize({ENDPOINT: [page_bad], SPOT_ENDPOINT: [spot_ok]})) == 12
    assert raises(NebiusProvider.normalize, {ENDPOINT: [page_bad], SPOT_ENDPOINT: [spot_bad]}), "both broken: raise"
    assert raises(NebiusProvider.normalize, {SPOT_ENDPOINT: [spot_bad]}), "the only stored half broken: raise"
    assert NebiusProvider.normalize({}) == []


def test_registry():
    assert PROVIDERS["nebius"] is NebiusProvider
    import mapping
    assert any(SPOT_ENDPOINT in e for e in mapping.PROVIDER_MAPPINGS["nebius"].endpoints), "spot endpoint documented"


# --------------------------------------------------------------------------
# Pipeline: raw -> listings in a scratch database; a spot outage keeps on-demand current
# --------------------------------------------------------------------------


def test_pipeline():
    import fixtures
    import normalize
    from sqlalchemy import text as sql

    db = "opengrid_test_nebius"
    url = scratchdb.create(db)
    try:
        Session = fixtures.session(url)
        fetch_mod.SessionLocal = Session
        normalize.SessionLocal = Session
        now = datetime.now(timezone.utc).replace(microsecond=0)
        first = [r.model_copy(update={"fetched_at": now - timedelta(hours=1)}) for r in run(prov(FakeNebius()))]
        fetch_mod.save(first)
        normalize.refresh(["nebius"])
        # Next poll: the spot API is down. On-demand moves on; spot keeps its last good rows (and their time).
        second = [r.model_copy(update={"fetched_at": now}) for r in run(prov(FakeNebius(spot_status=500)))]
        fetch_mod.save(second)
        normalize.refresh(["nebius"])
        with Session() as s:
            rows = s.execute(sql("SELECT market_type, count(*), max(observed_at) FROM compute_listings "
                                 "WHERE provider = 'nebius' GROUP BY 1 ORDER BY 1")).all()
            failed = s.execute(sql("SELECT count(*) FROM raw_snapshots WHERE provider = 'nebius' AND NOT ok")).scalar()
        got = {m: (n, t) for m, n, t in rows}
        assert got["on_demand"][0] == 7 and got["on_demand"][1] == now, got
        assert got["spot"][0] == 12 and got["spot"][1] == now - timedelta(hours=1), got
        assert failed == 1, "the spot outage is stored as a failed response"
    finally:
        scratchdb.drop(db)


if __name__ == "__main__":
    for t in (test_page_with_preemptible_column, test_spot_listings, test_spot_rows_fail_closed, test_fetch_both,
              test_fetch_pages_through_spot, test_spot_failure_keeps_on_demand, test_page_failure_keeps_spot,
              test_normalize_halves_independent, test_registry, test_pipeline):
        t()
        print(t.__name__, "ok")
