"""ECB cross rates (fx.py) and the two providers they unlock: Yotta Shakti Cloud (INR) and Sakura Internet DOK (JPY).

Cross: X->USD = (EUR->USD) / (EUR->X), both legs from one ECB date, 8 decimals, the same fail-closed staleness rule,
source "ecb_cross", the legs kept (Rate.legs; GET /v1/fx/listings fx_legs; GET /v1/fx/cross). Rates come from the
REAL ECB files in tests/fixtures_fx (daily 2026-10-30, history 19-30 Oct): JPY 175.68, INR 106.0840, IDR 20275.17,
USD 1.1172 on Friday 30 Oct.

Providers run on their REAL pages, trimmed to the priced sections (fetched 2026-11-01):
    fixtures_providers/yotta_pricing.html           shakticloud.ai/pricing/: AI Workspace VMs + the Bare Metal table
    fixtures_providers/sakura_koukaryoku_dok.html   ai.sakura.ad.jp/gpu/koukaryoku-dok/: the price-spec section

No network. Run:  .venv/bin/python tests/test_fx_cross.py
"""

import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scratchdb  # noqa: E402  (isolates the process from real providers before config loads)
import httpx  # noqa: E402

import fetch as fetch_mod  # noqa: E402
import fixtures  # noqa: E402
import fx  # noqa: E402
import mapping  # noqa: E402
import normalize  # noqa: E402
import provider_meta  # noqa: E402
from providers import PROVIDERS  # noqa: E402
from providers import sakura, yotta  # noqa: E402

FIX = ROOT / "tests" / "fixtures_fx"
PROV = ROOT / "tests" / "fixtures_providers"
DAILY = (FIX / "eurofxref-daily-2026-10-30.xml").read_text()
HIST = (FIX / "eurofxref-hist-2026-10-19_30.xml").read_text()
FRI = date(2026, 10, 30)
SUN = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
Q8 = D("0.00000001")
JPY = (D("1.1172") / D("175.68")).quantize(Q8)
INR = (D("1.1172") / D("106.0840")).quantize(Q8)
DB = "og_test_fx_cross"


def leg(quote, rate, day=FRI):
    return fx.Rate("EUR", quote, D(rate), day)


def test_cross_pure():
    r = fx.cross(leg("USD", "1.1172"), leg("JPY", "175.68"))
    assert (r.base, r.quote, r.rate, r.rate_date, r.source) == ("JPY", "USD", JPY, FRI, "ecb_cross") and JPY == D("0.00635929")
    assert [l["quote"] for l in r.legs] == ["USD", "JPY"] and r.legs[1]["rate"] == D("175.68")
    assert fx.cross(leg("USD", "1.1172"), leg("INR", "106.0840")).rate == INR == D("0.01053128")
    for bad in ((leg("USD", "1.1"), leg("JPY", "175", FRI - timedelta(days=1))),   # legs from two dates
                (leg("JPY", "175"), leg("USD", "1.1")),                             # legs swapped
                (leg("USD", "1.1"), leg("JPY", "0"))):
        try:
            fx.cross(*bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    # convert() with a cross rate: same rounding and provenance as EUR, source ecb_cross
    c = fx.convert(D("2988"), 8, "JPY", r)
    assert c["price_per_instance_hour"] == (D("2988") * JPY).quantize(D("0.000001")) and c["fx_source"] == "ecb_cross"
    assert c["original_currency"] == "JPY" and c["original_price_per_gpu_hour"] == D("373.500000") and c["fx_rate"] == JPY
    print("test_cross_pure ok")


# --------------------------------------------------------------------------- providers (pure: the rate is stubbed)

class stub_rate:
    def __init__(self, currency, rate, day=FRI):
        self.r = fx.Rate(currency, "USD", D(rate), day, "ecb_cross", ({"quote": "USD"}, {"quote": currency}))

    def __enter__(self):
        self.real = fx.cross_at
        fx.cross_at = lambda at, cur: self.r if cur == self.r.base else None
        return self

    def __exit__(self, *a):
        fx.cross_at = self.real


def snap(payload, at=SUN):
    return SimpleNamespace(payload=payload, request=None, fetched_at=at)


def test_yotta():
    page = (PROV / "yotta_pricing.html").read_text(encoding="utf-8")
    body = yotta.extract(page)
    assert "Per Hour, Per Compute" in body["unit_note"] and len(body["rows"]) == 5
    with stub_rate("INR", INR):
        out = {l.listing_id: l for l in yotta.YottaProvider.normalize({yotta.ENDPOINT: [snap(body)]})}
    assert sorted(out) == ["vm-1-x-h100-on-demand", "vm-1-x-l40s-on-demand", "vm-2-x-h100-on-demand",
                           "vm-2-x-l40s-on-demand", "vm-4-x-h100-on-demand"]
    h1 = out["vm-1-x-h100-on-demand"]
    assert (h1.raw_gpu_name, h1.canonical_gpu_name, h1.gpu_count) == ("NVIDIA H100 SXM 80GB", "NVIDIA H100 80GB SXM5", 1)
    assert h1.original_currency == "INR" and h1.original_price_per_instance_hour == D("356.000000")
    assert h1.price_per_instance_hour == (D("356") * INR).quantize(D("0.000001")) and h1.fx_source == "ecb_cross"
    assert (h1.vcpu, h1.ram_gb, h1.storage_gb, h1.country, h1.region, h1.market_type) == (24, 240.0, 100.0, "IN", None, "on_demand")
    h4 = out["vm-4-x-h100-on-demand"]
    assert h4.raw_gpu_name == "NVIDIA H100 SXM 80GB" and h4.gpu_count == 4, "the printed 320 GB is the VM total"
    assert h4.original_price_per_gpu_hour == D("355.750000")
    l2 = out["vm-2-x-l40s-on-demand"]
    assert l2.canonical_gpu_name == "NVIDIA L40S 48GB" and l2.original_price_per_instance_hour == D("275.000000")
    assert not any("b200" in k or "bare" in k for k in out), "bare metal (monthly terms) is never read"
    # fail closed and reshaped pages
    with stub_rate("JPY", JPY):   # no INR pair
        assert yotta.YottaProvider.normalize({yotta.ENDPOINT: [snap(body)]}) == []
    for bad in ("<html>moved</html>", page.replace("Per Hour, Per Compute", "Per Month, Per Compute")):
        try:
            yotta.extract(bad)
            raise AssertionError("reshaped page accepted")
        except ValueError:
            pass
    print("test_yotta ok")


def test_sakura():
    page = (PROV / "sakura_koukaryoku_dok.html").read_text(encoding="utf-8")
    body = sakura.extract(page)
    assert body["tax_inclusive"] is True
    plans = {p["plan"]: p for p in sakura.plans(body)}
    assert sorted(plans) == ["h100-80gb", "h100-8gpu-80gb", "v100-32gb"]
    box = plans["h100-8gpu-80gb"]
    assert (box["gpu_count"], box["jpy_per_second"], box["jpy_per_hour"], box["beta"]) == (8, D("0.83"), D("2988"), True)
    assert plans["h100-80gb"]["jpy_per_hour"] == D("1008") and plans["h100-80gb"]["minimum_seconds"] == 60
    with stub_rate("JPY", JPY):
        out = {l.listing_id: l for l in sakura.SakuraProvider.normalize({sakura.ENDPOINT: [snap(body)]})}
    b = out["h100-8gpu-80gb"]
    assert b.gpu_count == 8 and b.original_price_per_instance_hour == D("2988.000000"), "priced per box"
    assert b.original_price_per_gpu_hour == D("373.500000") and b.price_per_instance_hour == (D("2988") * JPY).quantize(D("0.000001"))
    assert all(l.provider_tier == "tax_inclusive" and l.country == "JP" and l.region is None for l in out.values())
    assert b.canonical_gpu_name is None and out["v100-32gb"].canonical_gpu_name is None, "form factor not stated: unmapped"
    assert (b.vcpu, b.ram_gb) == (160, 1760.0)
    # without the tax note the price basis is unknown: refuse rather than mislabel
    nb = dict(body, tax_inclusive=False)
    try:
        sakura.SakuraProvider.normalize({sakura.ENDPOINT: [snap(nb)]})
        raise AssertionError("price basis unknown, accepted")
    except ValueError:
        pass
    # per-second and per-hour must agree
    bad = {**body, "price": [r[:] for r in body["price"]]}
    bad["price"][1][3] = bad["price"][1][3].replace("2,988", "3,500")
    assert "h100-8gpu-80gb" not in {p["plan"] for p in sakura.plans(bad)}
    try:
        sakura.extract("<html>no section</html>")
        raise AssertionError("reshaped page accepted")
    except ValueError:
        pass
    print("test_sakura ok")


def test_registry():
    for name, cls, host in (("yotta", yotta.YottaProvider, "shakticloud.ai"), ("sakura", sakura.SakuraProvider, "ai.sakura.ad.jp")):
        assert PROVIDERS[name] is cls and cls.polling.interval_seconds == 3600
        assert cls.__name__ in {type(f).__name__ for f in fetch_mod.build_fetchers()}
        assert name in mapping.PROVIDER_MAPPINGS and provider_meta.meta(name).source == host
    assert mapping.check_mapping_coverage() == {}
    assert "tax" in provider_meta.meta("sakura").note.lower()

    def handler(request):
        return httpx.Response(200, text=(PROV / "sakura_koukaryoku_dok.html").read_text(encoding="utf-8"),
                              headers={"content-type": "text/html"})
    raws = asyncio.run(sakura.SakuraProvider(httpx.AsyncClient(base_url=sakura.BASE_URL,
                                                               transport=httpx.MockTransport(handler))).fetch())
    assert raws[0].ok and raws[0].endpoint == sakura.ENDPOINT and raws[0].payload["tax_inclusive"]
    print("test_registry ok")


# --------------------------------------------------------------------------- database

def test_db_cross_rates_reparse_api_and_legs():
    import config
    from sqlalchemy import delete, func, select

    from store.fx import FxRate

    url = scratchdb.create(DB)
    try:
        Session = fixtures.session(url)
        normalize.SessionLocal = Session
        for i, body in enumerate((HIST, DAILY)):
            fx.save(fx.Fetched(f"https://ecb.test/{i}.xml", datetime(2026, 11, 1, 12, i, tzinfo=timezone.utc), 200, 5, True, None, body))
        jpy, inr = fx.usd_rate("JPY", SUN), fx.usd_rate("INR", SUN)
        assert (jpy.rate, jpy.rate_date, jpy.source) == (JPY, FRI, "ecb_cross") and inr.rate == INR
        assert [l["rate"] for l in jpy.legs] == [D("1.1172"), D("175.68")]
        assert fx.usd_rate("KRW", SUN) is not None, "KRW (0.00075 USD) keeps enough digits"
        assert fx.usd_rate("IDR", SUN) is None, "IDR is below MIN_CROSS_RATE: refused, never imprecise"
        assert fx.usd_rate("JPY", datetime(2026, 11, 7, 9, tzinfo=timezone.utc)) is None, "stale: 8 days"
        assert fx.usd_rate("JPY", datetime(2026, 10, 30, 14, tzinfo=timezone.utc)).rate_date == date(2026, 10, 29), \
            "before 16:00 CET Friday: Thursday's legs"
        # one leg missing on the newest date: the cross falls back to the newest date with BOTH legs
        with Session.begin() as s:
            s.execute(delete(FxRate).where(FxRate.quote == "JPY", FxRate.rate_date == FRI))
        assert fx.usd_rate("JPY", SUN).rate_date == date(2026, 10, 29)
        # rates stored before cross rates existed (USD only) are completed from the raw bodies
        with Session.begin() as s:
            s.execute(delete(FxRate).where(FxRate.quote != "USD"))
        out = fx.reparse_stored()
        with Session() as s:
            quotes = s.execute(select(func.count(func.distinct(FxRate.quote)))).scalar()
        assert out["snapshots"] == 2 and quotes == 29 and fx.usd_rate("JPY", SUN).rate == JPY
        # a Sakura listing converted at the cross rate carries both legs on /v1/fx/listings
        body = sakura.extract((PROV / "sakura_koukaryoku_dok.html").read_text(encoding="utf-8"))
        normalize.save(sakura.SakuraProvider.normalize({sakura.ENDPOINT: [snap(body, datetime.now(timezone.utc).replace(year=2026, month=11, day=1, hour=10))]}))
        conv = [r for r in fx.converted_listings("sakura") if r["listing_id"] == "h100-8gpu-80gb"][0]
        assert conv["fx_source"] == "ecb_cross" and conv["fx_rate"] == JPY
        assert [l["quote"] for l in conv["fx_legs"]] == ["USD", "JPY"] and conv["fx_legs"][1]["rate"] == D("175.68")
        # API
        config.settings.app_password = None
        import main
        from fastapi.testclient import TestClient
        c = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        st = fx.status(SUN)
        assert "JPY" in st["cross_currencies"] and "INR" in st["cross_currencies"] and "USD" not in st["cross_currencies"]
        assert len(fx.rates(30, "JPY")) == 10 and fx.rates(30)[0]["quote"] == "USD"
        cr = {r["currency"]: r for r in fx.cross_rates(SUN)}
        assert cr["JPY"]["rate"] == JPY and cr["IDR"]["state"] == "too_small" and len(cr["INR"]["legs"]) == 2
        r = c.get("/v1/fx/cross")
        assert r.status_code == 200 and "EUR->USD" in r.json()["meta"]["method"]
        assert c.get("/v1/fx/rates?quote=JPY&days=3650").json()["meta"]["quote"] == "JPY"
        assert c.get("/v1/fx/rates?quote=jpy").status_code == 422
        legs = c.get("/v1/fx/listings?provider=sakura").json()["data"]
        assert all(len(x["fx_legs"]) == 2 for x in legs)
    finally:
        scratchdb.drop(DB)
    print("test_db_cross_rates_reparse_api_and_legs ok")


if __name__ == "__main__":
    test_cross_pure()
    test_yotta()
    test_sakura()
    test_registry()
    test_db_cross_rates_reparse_api_and_legs()
    print("all passed")
