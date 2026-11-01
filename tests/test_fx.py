"""FX: the ECB euro reference rate (fx.py; methodology/fx.md).

Real ECB files (tests/fixtures_fx/, fetched 2026-11-01: the daily file, dated Friday 30 Oct 2026, and the first ten
days of the 90-day file). Covers parsing, which rate is in force when (publication at 16:00 Frankfurt, weekends,
Easter), the fail-closed stale rule, conversion rounding, raw-first storage and its failures, the /v1/fx endpoints,
and Scaleway end to end: raw catalogue -> converted listings with provenance, counted once, nothing when stale.
No network: every HTTP call goes to an httpx.MockTransport.

Run:  .venv/bin/python tests/test_fx.py
"""

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import scratchdb  # noqa: E402  (isolates the process from real providers and the network)
import fixtures  # noqa: E402
import fx  # noqa: E402
import normalize  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures_fx"
PROV = Path(__file__).resolve().parent / "fixtures_providers"
CAP = Path(__file__).resolve().parent / "fixtures_capacity"
DAILY = (FIX / "eurofxref-daily-2026-10-30.xml").read_text()
HIST = (FIX / "eurofxref-hist-2026-10-19_30.xml").read_text()
DB = "og_test_fx"
UTC = timezone.utc
FRI = date(2026, 10, 30)


def utc(*a):
    return datetime(*a, tzinfo=UTC)


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def __enter__(self):
        logging.getLogger("fx").addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger("fx").removeHandler(self)


def _setup_db():
    url = scratchdb.create(DB)
    Session = fixtures.session(url)
    normalize.SessionLocal = Session
    return Session


def _transport(routes: dict[str, tuple[int, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = routes.get(str(request.url), (404, "not found"))
        return httpx.Response(status, text=body, headers={"content-type": "text/xml"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _store(*bodies: str):
    for i, body in enumerate(bodies):
        fx.save(fx.Fetched(f"https://ecb.test/{i}.xml", utc(2026, 11, 1, 12, i), 200, 5, True, None, body))


# --------------------------------------------------------------------------- parsing (pure)

def test_parse_real_daily():
    days = fx.parse(DAILY)
    assert list(days) == [FRI], days.keys()
    rates = days[FRI]
    assert rates["USD"] == D("1.1172") and rates["JPY"] == D("175.68") and rates["GBP"] == D("0.83380")
    assert len(rates) == 29 and "EUR" not in rates, "29 quotes against the euro, as published"


def test_parse_real_history():
    days = fx.parse(HIST)
    assert len(days) == 10 and max(days) == FRI and min(days) == date(2026, 10, 19)
    assert date(2026, 10, 24) not in days and date(2026, 10, 25) not in days, "no rate on a weekend"
    assert days[date(2026, 10, 29)]["USD"] == D("1.113") and all("USD" in r for r in days.values())


def test_parse_refuses_what_is_not_the_ecb_shape():
    bad = [
        "", "not xml", "<html><body>maintenance</body></html>",
        '<!DOCTYPE x [<!ENTITY a "b">]>' + DAILY.split("?>", 1)[1],
        DAILY.replace("<Cube time='2026-10-30'>", "<Cube time='30/10/2026'>"),
        DAILY.replace("rate='1.1172'", "rate='n/a'"),
        DAILY.replace("rate='1.1172'", "rate='-1'"),
        DAILY.replace("<Cube time='2026-10-30'>", "<Cube>").replace("</Cube>\n\t</Cube>", "</Cube>"),
    ]
    for body in bad:
        try:
            fx.parse(body)
        except ValueError:
            continue
        raise AssertionError(f"accepted: {body[:80]!r}")


# --------------------------------------------------------------------------- which rate is in force (pure)

def test_publication_time_and_weekend():
    thu, fri = fx.Rate("EUR", "USD", D("1.113"), date(2026, 10, 29)), fx.Rate("EUR", "USD", D("1.1172"), FRI)
    rates = [thu, fri]
    # 30 Oct 2026 is CET (UTC+1): published 16:00 Frankfurt = 15:00 UTC
    assert fri.published_at == utc(2026, 10, 30, 15, 0)
    assert fx.pick(rates, utc(2026, 10, 30, 14, 59)) == thu, "before 16:00 CET Friday's rate is not out yet"
    assert fx.pick(rates, utc(2026, 10, 30, 15, 0)) == fri
    for at in (utc(2026, 10, 31, 12), utc(2026, 11, 1, 23, 59), utc(2026, 11, 2, 9)):  # Sat, Sun, Mon morning
        r = fx.pick(rates, at)
        assert r == fri and not fx.is_stale(r, at), f"weekend {at}: Friday's rate stays in force"
    assert fx.age_days(FRI, utc(2026, 11, 2, 9)) == 3
    assert fx.pick(rates, utc(2026, 10, 28)) is None, "nothing published yet"
    # summer time: 16:00 CEST = 14:00 UTC
    assert fx.published_at(date(2026, 7, 1)) == utc(2026, 7, 1, 14, 0)


def test_stale_rule_and_easter():
    fri = fx.Rate("EUR", "USD", D("1.1172"), FRI)
    assert not fx.is_stale(fri, utc(2026, 11, 4, 22, 0)), "Wednesday: 5 days, still in force"
    assert fx.is_stale(fri, utc(2026, 11, 4, 23, 30)), "after midnight Frankfurt it is Thursday: 6 days, stale"
    # Easter 2027: Good Friday 26 Mar and Easter Monday 29 Mar are TARGET holidays. Thursday's rate must last
    # until Tuesday's publication; 4 days would stop conversion every Easter Tuesday morning.
    thu = fx.Rate("EUR", "USD", D("1.08"), date(2027, 3, 25))
    tue_morning = datetime(2027, 3, 30, 10, tzinfo=fx.FRANKFURT)
    assert fx.pick([thu], tue_morning) == thu and not fx.is_stale(thu, tue_morning)
    # Christmas on a Thursday (2025): 24 Dec's rate until Monday 29 Dec.
    eve = fx.Rate("EUR", "USD", D("1.17"), date(2025, 12, 24))
    assert not fx.is_stale(eve, datetime(2025, 12, 29, 15, tzinfo=fx.FRANKFURT))


def test_convert_rounding_and_provenance():
    fri = fx.Rate("EUR", "USD", D("1.1172"), FRI)
    out = fx.convert(D("2.8665"), 1, "EUR", fri)
    assert out["price_per_instance_hour"] == D("3.202454"), "2.8665 x 1.1172 = 3.2024538, rounded half-up to 6 dp"
    assert out["currency"] == "USD" and out["original_currency"] == "EUR" and out["original_price_per_instance_hour"] == D("2.866500")
    assert out["fx_rate"] == D("1.1172") and out["fx_rate_date"] == FRI and out["fx_source"] == "ecb"
    eight = fx.convert(D("25.3308"), 8, "EUR", fri)
    assert eight["price_per_gpu_hour"] == D("3.537446") and eight["original_price_per_gpu_hour"] == D("3.166350")
    for wrong in ((D(1), 1, "GBP", fri), (D(1), 0, "EUR", fri)):
        try:
            fx.convert(*wrong)
            raise AssertionError(wrong)
        except ValueError:
            pass


# --------------------------------------------------------------------------- storage (scratch DB)

def test_refresh_stores_raw_first_and_parses_usd_only():
    Session = _setup_db()
    try:
        from sqlalchemy import func, select
        from store.fx import FxRate, FxSnapshot

        client = _transport({fx.HIST_90D_URL: (200, HIST), fx.DAILY_URL: (200, DAILY)})
        out = asyncio.run(fx.refresh(client))
        assert [f["url"] for f in out["fetches"]] == [fx.HIST_90D_URL, fx.DAILY_URL], "empty table: 90 days first"
        with Session() as s:
            snaps = s.execute(select(FxSnapshot).order_by(FxSnapshot.id)).scalars().all()
            rates = s.execute(select(FxRate).order_by(FxRate.rate_date)).scalars().all()
        assert [x.body for x in snaps] == [HIST, DAILY], "every response body stored verbatim"
        assert all(x.ok and x.sha256 and x.status_code == 200 for x in snaps)
        assert len(rates) == 10 and {(r.base, r.quote) for r in rates} == {("EUR", "USD")}, "USD parsed, the rest raw"
        assert rates[-1].rate_date == FRI and rates[-1].rate == D("1.1172") and rates[-1].snapshot_id == snaps[0].id

        out = asyncio.run(fx.refresh(client))
        assert [f["url"] for f in out["fetches"]] == [fx.DAILY_URL], "rates stored: the daily file only"
        # a failed fetch and a reshaped body are stored as failures; the rates are untouched
        for status, body in ((503, "maintenance"), (200, "<html>moved</html>")):
            bad = _transport({fx.DAILY_URL: (status, body)})
            with Logs():
                res = asyncio.run(fx.refresh(bad))["fetches"][0]
            assert not res["ok"] and res["error"], res
        with Session() as s:
            last = s.execute(select(FxSnapshot).order_by(FxSnapshot.id.desc())).scalars().first()
            n = s.execute(select(func.count()).select_from(FxRate)).scalar()
        assert last.body == "<html>moved</html>" and not last.ok and "Envelope" in last.error
        assert n == 10
        # an ECB revision of a stored day replaces the rate, and says so
        with Logs() as logs:
            res = fx.save(fx.Fetched(fx.DAILY_URL, utc(2026, 11, 1), 200, 1, True, None, DAILY.replace("1.1172", "1.1180")))
        assert res["revised"] == 1 and any("revised" in m for m in logs.lines)
        assert fx.rate_at(utc(2026, 11, 1)).rate == D("1.1180")
    finally:
        scratchdb.drop(DB)


def test_usd_rate_weekend_and_stale_fail_closed():
    _setup_db()
    try:
        _store(HIST)
        sun = utc(2026, 11, 1, 10)
        assert fx.usd_rate("EUR", sun).rate_date == FRI, "Sunday uses Friday's rate"
        assert fx.usd_rate("EUR", utc(2026, 10, 30, 14)).rate_date == date(2026, 10, 29), "Friday before 16:00 CET"
        assert fx.usd_rate("EUR", utc(2026, 10, 25, 12)).rate_date == date(2026, 10, 23), "Sunday 25 Oct: Friday 23"
        with Logs() as logs:
            assert fx.usd_rate("EUR", utc(2026, 11, 6, 9), context="test") is None, "Friday's rate 7 days later"
            assert fx.usd_rate("EUR", utc(2026, 10, 1)) is None, "before any stored rate"
            assert fx.usd_rate("GBP", sun) is None, "only EUR is parsed"
        assert any("7 days old" in m and "fail closed" in m and "test" in m for m in logs.lines), logs.lines
        assert sum("fail closed" in m for m in logs.lines) == 2
        st = fx.status(utc(2026, 11, 6, 9))
        assert st["state"] == "stale" and st["rate_date"] == FRI and st["age_days"] == 7
        assert fx.status(sun)["state"] == "ok"
    finally:
        scratchdb.drop(DB)


# --------------------------------------------------------------------------- Scaleway end to end (scratch DB)

def _scaleway_raws(at: datetime):
    """One Scaleway poll on the saved real responses (catalogue + 9 zones x 2), all stamped `at`."""
    from providers.scaleway import SCALEWAY_CATALOG, ScalewayProvider

    saved = json.loads((CAP / "scaleway.json").read_text())
    routes = {SCALEWAY_CATALOG: (PROV / "scaleway_catalog_instance.json").read_text()}
    routes |= {k: json.dumps({"servers": v[0]["servers"]}) for k, v in saved.items() if "api.scaleway.com" in k}

    def handler(request):
        url = str(request.url).split("?")[0]
        if url in routes:
            return httpx.Response(200, text=routes[url], headers={"content-type": "application/json"})
        return httpx.Response(404, text="nf")
    prov = ScalewayProvider(httpx.AsyncClient(base_url="https://api.scaleway.com", transport=httpx.MockTransport(handler)))
    raws = asyncio.run(prov.fetch())
    assert all(r.ok for r in raws), [r.error for r in raws if not r.ok]
    return [r.model_copy(update={"fetched_at": at}) for r in raws]


def _shadeform_scaleway_raw(at: datetime):
    """An OLD Shadeform response stored under provider scaleway (as before the switch), to prove it is ignored."""
    from models import RawResponse

    body = json.loads((PROV / "shadeform_scaleway.json").read_text())
    return RawResponse(provider="scaleway", endpoint="/v1/instances/types", fetched_at=at, status_code=200, payload=body)


def test_scaleway_end_to_end_with_provenance_and_no_double_count():
    Session = _setup_db()
    try:
        import config
        config.settings.app_password = None
        import fetch as fetch_mod
        import main
        from fastapi.testclient import TestClient
        from sqlalchemy import text as sql

        fetch_mod.SessionLocal = Session
        now = datetime.now(UTC).replace(microsecond=0)
        today = now.astimezone(fx.FRANKFURT).date()
        # an ECB file dated two days ago (the real body, re-dated): published, in force, fresh
        fx.save(fx.Fetched(fx.DAILY_URL, now, 200, 3, True, None,
                           DAILY.replace("2026-10-30", str(today - timedelta(days=2)))))
        fetch_mod.save([_shadeform_scaleway_raw(now - timedelta(days=1))] + _scaleway_raws(now - timedelta(minutes=5)))
        normalize.refresh(["scaleway"])
        with Session() as s:
            rows = {r.listing_id: r for r in s.execute(sql("SELECT * FROM compute_listings WHERE provider='scaleway'"))}
            obs = s.execute(sql("SELECT listing_id, original_currency, fx_rate, fx_rate_date, original_price_per_instance_hour "
                                "FROM listing_observations WHERE provider='scaleway' ORDER BY 1")).all()
        assert set(rows) == {"H100", "B300x8", "L40Sx8", "L4", "H100-SXM-8-80G", "RENDER-S"}, set(rows)
        assert not any(r.provider_tier.endswith("via-shadeform") for r in rows.values()), "nothing from Shadeform"
        h = rows["H100"]
        assert h.currency == "USD" and h.original_currency == "EUR" and h.fx_source == "ecb"
        assert h.fx_rate == D("1.1172") and h.fx_rate_date == today - timedelta(days=2)
        assert h.original_price_per_instance_hour == D("2.866500") and h.price_per_instance_hour == D("3.202454")
        assert len(obs) == 6 and all(o.original_currency == "EUR" and o.fx_rate == D("1.1172") for o in obs)

        # the API exposes the provenance, and the converted listing stays a plain USD listing for statistics
        client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})
        cur = client.get("/v1/fx").json()
        assert cur["data"]["state"] == "ok" and cur["data"]["rate"] == 1.1172 and cur["meta"]["methodology"] == "/methodology/fx"
        conv = client.get("/v1/fx/listings", params={"provider": "scaleway"}).json()["data"]
        assert {r["listing_id"] for r in conv} == set(rows) and all(r["original_currency"] == "EUR" for r in conv)
        listed = [r for r in client.get("/listings", params={"q": "scaleway"}).json() if r["provider"] == "scaleway"]
        assert len(listed) == 6 and all(r["currency"] == "USD" and r["fx_rate"] for r in listed)
        hist = client.get("/history", params={"q": "scaleway"}).json()
        assert hist and all(r["original_currency"] == "EUR" and r["fx_rate_date"] for r in hist)
        assert client.get("/v1/fx/rates", params={"days": 30}).json()["data"][0]["rate"] == 1.1172

        # Same raw, same rate: no new observation. A newer rate moves the USD price (one row each, provenance kept).
        before = len(obs)
        normalize.refresh(["scaleway"])
        fx.save(fx.Fetched(fx.DAILY_URL, now, 200, 3, True, None,
                           DAILY.replace("2026-10-30", str(today - timedelta(days=1))).replace("1.1172", "1.1200")))
        fetch_mod.save(_scaleway_raws(now))
        normalize.refresh(["scaleway"])
        with Session() as s:
            n = s.execute(sql("SELECT count(*) FROM listing_observations WHERE provider='scaleway'")).scalar()
            h2 = s.execute(sql("SELECT price_per_instance_hour, fx_rate FROM compute_listings "
                               "WHERE provider='scaleway' AND listing_id='H100'")).one()
        assert n == before + 6 and h2.fx_rate == D("1.12") and h2.price_per_instance_hour == D("3.210480")

        # Stale: the newest stored rate is 6 days old at the next poll -> nothing converted, old state kept, logged.
        with Session.begin() as s:
            s.execute(sql("DELETE FROM fx_rates"))
        fx.save(fx.Fetched(fx.DAILY_URL, now, 200, 3, True, None, DAILY.replace("2026-10-30", str(today - timedelta(days=6)))))
        later = now + timedelta(minutes=1)
        fetch_mod.save(_scaleway_raws(later))
        with Logs() as logs:
            normalize.refresh(["scaleway"])
        with Session() as s:
            seen = s.execute(sql("SELECT max(observed_at), count(*) FROM compute_listings WHERE provider='scaleway'")).one()
        assert seen[0] < later and seen[1] == 6, "no converted listing from the stale poll: the previous state ages out"
        assert any("6 days old" in m and "fail closed" in m for m in logs.lines), logs.lines
        assert client.get("/v1/fx").json()["data"]["state"] == "stale"
    finally:
        scratchdb.drop(DB)


def test_migration_matches_models():
    """Alembic's 0026_fx builds what tables.py declares (columns of the new tables and provenance columns)."""
    url = scratchdb.create(DB + "_mig")
    try:
        from alembic import command
        from sqlalchemy import create_engine, inspect

        import db
        cfg = db._alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        import config
        real = config.settings.database_url
        config.settings.database_url = url
        try:
            command.upgrade(cfg, "head")
        finally:
            config.settings.database_url = real
        insp = inspect(create_engine(url))
        from tables import ComputeListingRow, ListingObservation
        from store.fx import FxRate, FxSnapshot
        for model in (ComputeListingRow, ListingObservation, FxRate, FxSnapshot):
            have = {c["name"] for c in insp.get_columns(model.__tablename__)}
            want = {c.name for c in model.__table__.columns}
            assert want == have, (model.__tablename__, want ^ have)
    finally:
        scratchdb.drop(DB + "_mig")


if __name__ == "__main__":
    for t in (test_parse_real_daily, test_parse_real_history, test_parse_refuses_what_is_not_the_ecb_shape,
              test_publication_time_and_weekend, test_stale_rule_and_easter, test_convert_rounding_and_provenance,
              test_refresh_stores_raw_first_and_parses_usd_only, test_usd_rate_weekend_and_stale_fail_closed,
              test_scaleway_end_to_end_with_provenance_and_no_double_count, test_migration_matches_models):
        t()
        print(t.__name__, "ok")
    print("all passed")
