"""Currency conversion: the European Central Bank's daily euro reference rates (methodology/fx.md).

OpenGrid publishes every price in USD. A provider that prices only in another currency (Scaleway's own catalogue,
EUR) is converted at the ECB euro foreign exchange reference rate, and the converted listing carries its
provenance: the original currency and amount, the rate and the rate's date (ComputeListing.original_* / fx_*).

Raw first, like provider prices:
    refresh()      fetches the ECB feed (eurofxref-daily.xml; the 90-day file once, when no rate is stored yet),
                   stores every response verbatim in fx_snapshots (failures too), then parses EUR->USD into
                   fx_rates. Every other quote stays in the raw body, unparsed.
    rate_at()      the EUR->USD rate in force at an instant: the newest stored rate already PUBLISHED then. The
                   ECB publishes on TARGET business days at about 16:00 CET, so a rate dated D is used from
                   D 16:00 Europe/Berlin; on weekends and TARGET holidays the latest published rate stays in force.
    usd_rate()     rate_at() plus the fail-closed rule: a rate more than MAX_AGE_DAYS calendar days older than
                   the instant (in Frankfurt) is stale, and no listing is converted with it (logged).
    convert()      original amounts -> the USD fields and provenance fields of a ComputeListing.

Normalizers call usd_rate() with the raw snapshot's fetch time, never "now", so re-running a normalizer over
stored history converts each snapshot at the rate that was in force when it was fetched.
"""

from __future__ import annotations

import hashlib
import logging
import time as _time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from store.fx import FxRate, FxSnapshot

log = logging.getLogger(__name__)

SOURCE = "ecb"
SOURCE_NAME = "European Central Bank euro foreign exchange reference rate"
DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
HIST_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
BASE = "EUR"           # every ECB reference rate is quoted against the euro
PARSED = ("USD",)      # the quotes parsed into fx_rates; the rest stay raw
FRANKFURT = ZoneInfo("Europe/Berlin")
PUBLISHED_AT = time(16, 0)  # "around 16:00 CET" on TARGET business days
# Fail closed after this many calendar days. The longest normal gap between two ECB publications is Easter
# (Thursday's rate is still in force on Easter Tuesday before 16:00: 5 days) and a Thursday Christmas
# (24 Dec -> 29 Dec: 5 days), so 5 never stops conversion on a normal calendar and 6 means the feed is broken.
MAX_AGE_DAYS = 5
POLL_SECONDS = 3600
TIMEOUT = 20
USER_AGENT = "opengrid-terminal/0.1 (+fx; ECB eurofxref)"
_Q6 = Decimal("0.000001")  # compute_listings prices are Numeric(14, 6): convert to that, so a re-read compares equal
_NS = {"gesmes": "http://www.gesmes.org/xml/2002-08-01", "e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}


def _session():
    import normalize  # late: providers import this module, and normalize imports providers

    return normalize.SessionLocal


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------


def parse(body: str) -> dict[date, dict[str, Decimal]]:
    """{rate date: {currency: units per 1 EUR}} from an ECB eurofxref XML file (daily or historical).

    Raises ValueError on anything that is not the ECB's shape: no DOCTYPE / entities, a gesmes Envelope, at
    least one dated Cube, every rate a positive decimal. Nothing is guessed.
    """
    if not isinstance(body, str) or not body.strip():
        raise ValueError("ecb: empty body")
    if "<!DOCTYPE" in body or "<!ENTITY" in body:
        raise ValueError("ecb: DOCTYPE / ENTITY in the feed - refused")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"ecb: not XML: {exc}") from exc
    if root.tag != f"{{{_NS['gesmes']}}}Envelope":
        raise ValueError(f"ecb: root is {root.tag!r}, not a gesmes Envelope - reshaped")
    out: dict[date, dict[str, Decimal]] = {}
    for day in root.iterfind("e:Cube/e:Cube[@time]", _NS):
        try:
            d = date.fromisoformat(day.get("time"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"ecb: bad Cube time {day.get('time')!r}") from exc
        rates: dict[str, Decimal] = {}
        for cube in day.iterfind("e:Cube[@currency]", _NS):
            cur, raw = cube.get("currency") or "", cube.get("rate")
            try:
                value = Decimal(raw)
            except (TypeError, InvalidOperation) as exc:
                raise ValueError(f"ecb: {d} {cur}: rate {raw!r} is not a number") from exc
            if len(cur) != 3 or not value.is_finite() or value <= 0:
                raise ValueError(f"ecb: {d} {cur!r}: rate {raw!r} rejected")
            rates[cur] = value
        out[d] = rates
    if not out:
        raise ValueError("ecb: no dated Cube in the feed - reshaped")
    return out


# --------------------------------------------------------------------------
# Which rate applies
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rate:
    base: str
    quote: str
    rate: Decimal          # quote units per one base unit
    rate_date: date
    source: str = SOURCE

    @property
    def published_at(self) -> datetime:
        return published_at(self.rate_date)


def published_at(rate_date: date) -> datetime:
    """When OpenGrid treats a rate as published: its date at 16:00 in Frankfurt, as a UTC instant."""
    return datetime.combine(rate_date, PUBLISHED_AT, FRANKFURT).astimezone(timezone.utc)


def age_days(rate_date: date, at: datetime) -> int:
    """Calendar days from the rate's date to `at`'s date in Frankfurt."""
    return (at.astimezone(FRANKFURT).date() - rate_date).days


def pick(rates: list[Rate], at: datetime) -> Rate | None:
    """The newest rate already published at `at` (pure; rate_at() feeds it from the database)."""
    usable = [r for r in rates if r.published_at <= at]
    return max(usable, key=lambda r: r.rate_date) if usable else None


def is_stale(rate: Rate, at: datetime) -> bool:
    return age_days(rate.rate_date, at) > MAX_AGE_DAYS


def rate_at(at: datetime, base: str = BASE, quote: str = "USD") -> Rate | None:
    """The stored rate in force at `at` (published at or before it), stale or not."""
    if at.tzinfo is None:
        raise ValueError("fx.rate_at needs an aware datetime")
    with _session()() as s:
        rows = s.execute(
            select(FxRate.rate_date, FxRate.rate, FxRate.source)
            .where(FxRate.base == base, FxRate.quote == quote,
                   FxRate.rate_date <= at.astimezone(FRANKFURT).date())
            .order_by(FxRate.rate_date.desc()).limit(2)
        ).all()
    return pick([Rate(base, quote, r.rate, r.rate_date, r.source) for r in rows], at)


def usd_rate(currency: str, at: datetime, context: str = "") -> Rate | None:
    """The rate to convert `currency` -> USD at `at`, or None: fail closed (logged) when missing or stale."""
    if currency == "USD":
        return Rate("USD", "USD", Decimal(1), at.astimezone(FRANKFURT).date(), "identity")
    if currency != BASE:
        log.warning("fx: no %s->USD reference rate (only %s is parsed)%s; not converted", currency, BASE,
                    f" ({context})" if context else "")
        return None
    where = f" ({context})" if context else ""
    try:
        rate = rate_at(at, currency, "USD")
    except Exception:  # noqa: BLE001 - a lookup that fails converts nothing (fail closed), it never breaks a feed
        log.exception("fx: %s->USD rate lookup failed%s; conversion stopped (fail closed)", currency, where)
        return None
    if rate is None:
        log.warning("fx: no ECB %s->USD rate published by %s%s; conversion stopped (fail closed)",
                    currency, at.isoformat(), where)
        return None
    if is_stale(rate, at):
        log.warning("fx: ECB %s->USD rate of %s is %d days old at %s (max %d)%s; conversion stopped (fail closed)",
                    currency, rate.rate_date, age_days(rate.rate_date, at), at.isoformat(), MAX_AGE_DAYS, where)
        return None
    return rate


def _q6(v: Decimal) -> Decimal:
    return v.quantize(_Q6, rounding=ROUND_HALF_UP)


def convert(instance_hour: Decimal, gpu_count: int, currency: str, rate: Rate) -> dict:
    """The ComputeListing price + provenance fields for one instance-hour price in `currency`, at `rate`.

    USD amounts are rounded to 6 decimals (the column scale), the per-GPU one from the unrounded product, so the
    same raw price at the same rate always gives the same stored value.
    """
    if rate.base != currency or rate.quote != "USD":
        raise ValueError(f"fx: rate {rate.base}->{rate.quote} cannot convert {currency}")
    if gpu_count < 1:
        raise ValueError("fx: gpu_count must be >= 1")
    usd = instance_hour * rate.rate
    return {
        "price_per_instance_hour": _q6(usd),
        "price_per_gpu_hour": _q6(usd / gpu_count),
        "currency": "USD",
        "original_currency": currency,
        "original_price_per_instance_hour": _q6(instance_hour),
        "original_price_per_gpu_hour": _q6(instance_hour / gpu_count),
        "fx_rate": rate.rate,
        "fx_rate_date": rate.rate_date,
        "fx_source": rate.source,
    }


# --------------------------------------------------------------------------
# Fetch and store (raw first)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    url: str
    fetched_at: datetime
    status_code: int | None
    duration_ms: int
    ok: bool
    error: str | None
    body: str | None


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> Fetched:
    """One GET of an ECB file, recorded whether or not it worked."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, headers={"user-agent": USER_AGENT}, follow_redirects=True)
    started_wall, started = datetime.now(timezone.utc), _time.perf_counter()
    try:
        resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - a failed fetch is stored, never raised
        return Fetched(url, started_wall, None, int((_time.perf_counter() - started) * 1000), False,
                       f"{type(exc).__name__}: {exc}"[:500], None)
    finally:
        if own:
            await client.aclose()
    ms = int((_time.perf_counter() - started) * 1000)
    return Fetched(url, started_wall, resp.status_code, ms, resp.is_success,
                   None if resp.is_success else resp.text[:500], resp.text)


def save(f: Fetched) -> dict:
    """Store one fetch raw, then parse EUR->USD from it. A body that will not parse is kept and marked failed."""
    now = datetime.now(timezone.utc)
    parsed, error = None, f.error
    if f.ok:
        try:
            parsed = parse(f.body)
        except ValueError as exc:
            error = str(exc)[:500]
            log.warning("fx: %s: %s", f.url, error)
    Session = _session()
    with Session.begin() as s:
        snap = FxSnapshot(source=SOURCE, url=f.url, fetched_at=f.fetched_at, status_code=f.status_code,
                          duration_ms=f.duration_ms, ok=parsed is not None, error=error,
                          sha256=hashlib.sha256(f.body.encode()).hexdigest() if f.body is not None else None,
                          body=f.body)
        s.add(snap)
        s.flush()
        rows = [{"rate_date": d, "base": BASE, "quote": q, "rate": rates[q], "source": SOURCE,
                 "snapshot_id": snap.id, "first_seen_at": now, "updated_at": now}
                for d, rates in sorted((parsed or {}).items()) for q in PARSED if q in rates]
        revised = 0
        if rows:
            old = {(r.rate_date, r.quote): r.rate for r in s.execute(
                select(FxRate).where(FxRate.base == BASE, FxRate.rate_date.in_({r["rate_date"] for r in rows}))
            ).scalars()}
            for r in rows:
                prev = old.get((r["rate_date"], r["quote"]))
                if prev is not None and Decimal(prev) != r["rate"]:
                    revised += 1
                    log.warning("fx: ECB revised %s %s->%s from %s to %s", r["rate_date"], BASE, r["quote"], prev, r["rate"])
            stmt = insert(FxRate).values(rows)
            s.execute(stmt.on_conflict_do_update(
                index_elements=["rate_date", "base", "quote"],
                set_={"rate": stmt.excluded.rate, "snapshot_id": stmt.excluded.snapshot_id,
                      "updated_at": stmt.excluded.updated_at},
                where=FxRate.rate != stmt.excluded.rate,
            ))
    return {"url": f.url, "ok": parsed is not None, "error": error, "dates": len(parsed or {}),
            "rates": len(rows), "revised": revised}


async def refresh(client: httpx.AsyncClient | None = None) -> dict:
    """Fetch the daily file (and the 90-day file when no rate is stored yet), store raw, parse."""
    with _session()() as s:
        empty = not s.execute(select(func.count()).select_from(FxRate).where(FxRate.base == BASE)).scalar()
    urls = [HIST_90D_URL, DAILY_URL] if empty else [DAILY_URL]
    results = [save(await fetch(u, client)) for u in urls]
    out = {"fetches": results, **status()}
    if out["state"] != "ok":
        log.warning("fx: EUR->USD is %s after refresh (rate date %s)", out["state"], out.get("rate_date"))
    return out


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def status(now: datetime | None = None) -> dict:
    """The EUR->USD rate in force now, and whether conversion is running (ok) or stopped (stale / missing)."""
    now = now or datetime.now(timezone.utc)
    rate = rate_at(now)
    with _session()() as s:
        last = s.execute(select(FxSnapshot).order_by(FxSnapshot.fetched_at.desc(), FxSnapshot.id.desc()).limit(1)).scalar()
        last_ok = s.execute(select(func.max(FxSnapshot.fetched_at)).where(FxSnapshot.ok.is_(True))).scalar()
    out = {"base": BASE, "quote": "USD", "source": SOURCE, "source_name": SOURCE_NAME, "source_url": DAILY_URL,
           "max_age_days": MAX_AGE_DAYS, "rate": None, "rate_date": None, "published_at": None, "age_days": None,
           "state": "missing", "last_fetch_at": last.fetched_at if last else None,
           "last_fetch_ok": last.ok if last else None, "last_fetch_error": last.error if last else None,
           "last_ok_fetch_at": last_ok}
    if rate is not None:
        out.update(rate=rate.rate, rate_date=rate.rate_date, published_at=rate.published_at,
                   age_days=age_days(rate.rate_date, now), state="stale" if is_stale(rate, now) else "ok")
    return out


def rates(days: int = 30) -> list[dict]:
    """Stored EUR->USD rates, newest first."""
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    with _session()() as s:
        rows = s.execute(select(FxRate).where(FxRate.base == BASE, FxRate.rate_date >= since)
                         .order_by(FxRate.rate_date.desc())).scalars().all()
    return [{"rate_date": r.rate_date, "base": r.base, "quote": r.quote, "rate": r.rate, "source": r.source,
             "published_at": published_at(r.rate_date), "first_seen_at": r.first_seen_at} for r in rows]


def converted_listings(provider: str | None = None) -> list[dict]:
    """Current listings whose USD price was converted, with their provenance."""
    from tables import ComputeListingRow as c

    stmt = (select(c).where(c.original_currency.is_not(None), c.visibility == "public")  # public: /v1/fx/listings
            .order_by(c.provider, c.listing_id))
    if provider:
        stmt = stmt.where(c.provider == provider)
    cols = ("provider", "listing_id", "sku", "canonical_gpu_name", "gpu_count", "region", "price_per_gpu_hour",
            "price_per_instance_hour", "currency", "original_currency", "original_price_per_gpu_hour",
            "original_price_per_instance_hour", "fx_rate", "fx_rate_date", "fx_source", "observed_at")
    with _session()() as s:
        return [{k: getattr(r, k) for k in cols} for r in s.execute(stmt).scalars()]


# --------------------------------------------------------------------------
# Background job
# --------------------------------------------------------------------------

_registered = False


def register() -> None:
    """Idempotent: importing api.fx calls this once. Off when the poller is off (no prices to convert)."""
    global _registered
    from config import settings
    from jobs import job

    if _registered or not settings.poller_enabled:
        return
    _registered = True
    job("fx_rates", every_seconds=POLL_SECONDS, initial_delay_seconds=5)(refresh)
