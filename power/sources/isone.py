"""ISO New England ISO Express web services: day-ahead and real-time (final) hourly LMP at the Internal Hub and the
eight load zones. Needs a free ISO Express account (isone_username, isone_password; HTTP Basic); off without it.

    GET https://webservices.iso-ne.com/api/v1.1/hourlylmp/da/final/day/YYYYMMDD/location/<LocId>
    GET https://webservices.iso-ne.com/api/v1.1/hourlylmp/rt/final/day/YYYYMMDD/location/<LocId>
        Accept: application/json, Authorization: Basic <username:password>

UNVERIFIED LIVE: the endpoints, auth and the HourlyLmps schema are from the v1.1 docs
(https://webservices.iso-ne.com/docs/v1.1/); no JSON example is published, so the JSON shape follows the XML schema
in the BadgerFish form open-source clients read ({"HourlyLmps": {"HourlyLmp": [{"BeginDate", "Location": {"@LocId",
"@LocType", "$"}, "LmpTotal", ...}]}}); a single record may come as an object, not a list. The test fixtures carry
REAL prices for 2025-11-02 (from ISO-NE's public hourly LMP CSV reports) in that shape. Verify on the first run.

One request per (day, location): ~25 records each, a few KB, against ~30,000 records (every node) for the
all-locations day. BeginDate carries its own offset (-04:00 / -05:00), so the fall-back day's repeated 01:00 hour
(the CSV reports' "02X") is two distinct instants and the DST days come out at 25 / 23 hours.

Depth: not documented for the web services; ISO-NE's public CSV reports reach DA 2016-01 and RT final 2018-06 by
sampling. The backfill starts at 2021-01. RT final posts about two days after the operating day. No numeric rate
limit (ISO-NE throttles "excessive concurrent requests" by IP); one request a second here. ~4 KB per series-day.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from power.registry import INTERNAL, Dataset, Fetched, Series, Terms
from power.storage import days

SOURCE = "isone"
DEFAULT_ON = False
REQUIRES: tuple[str, ...] = ("isone_username", "isone_password")
BASE = "https://webservices.iso-ne.com/api/v1.1/hourlylmp/"
TZ = "America/New_York"
ET = ZoneInfo(TZ)
UTC = timezone.utc
EARLIEST = date(2021, 1, 1)
DA_PUBLISHED_HOUR = 14     # the DA report is generated about 12:45 ET the day before
RT_FINAL_DAYS = 2

TERMS = Terms(
    operator="ISO New England",
    visibility=INTERNAL,
    reason=("ISO-NE Terms and Conditions: 'the Content is protected by copyright under United States laws. Any "
            "duplication of the Content or non-personal use may violate copyright, trademark, and other laws.' No "
            "clause grants redistribution or commercial use (permission requests go to info@iso-ne.com), so "
            "internal. Automated access is sanctioned through an ISO Express account ('To apply for automated data "
            "access: Sign up for ISO Express')."),
    terms_url="https://www.iso-ne.com/legal-privacy",
    checked="2026-11-05",
    signup=("Create an ISO Express account at https://www.iso-ne.com/signup (name, phone, company, email, an "
            "alphanumeric password); the account includes web services. Set ISONE_USERNAME (the email) and "
            "ISONE_PASSWORD."),
    attribution="Source: ISO New England",
)

LOCATIONS = {   # LocId -> (name, type, label)
    4000: (".H.INTERNAL_HUB", "hub", "Internal Hub"),
    4001: (".Z.MAINE", "zone", "Maine load zone"),
    4002: (".Z.NEWHAMPSHIRE", "zone", "New Hampshire load zone"),
    4003: (".Z.VERMONT", "zone", "Vermont load zone"),
    4004: (".Z.CONNECTICUT", "zone", "Connecticut load zone"),
    4005: (".Z.RHODEISLAND", "zone", "Rhode Island load zone"),
    4006: (".Z.SEMASS", "zone", "Southeast Massachusetts load zone"),
    4007: (".Z.WCMASS", "zone", "West/Central Massachusetts load zone"),
    4008: (".Z.NEMASSBOST", "zone", "Northeast Massachusetts / Boston load zone"),
}
_BY_NAME = {n: i for i, (n, _, _) in LOCATIONS.items()}


def series_key(market: str, loc_id: int) -> str:
    name = LOCATIONS[loc_id][0]
    return f"isone.{market}.lmp.{name.strip('.').replace('.', '_').lower()}"   # isone.da.lmp.h_internal_hub


def _series(market: str) -> tuple[Series, ...]:
    label = {"da": "day-ahead hourly", "rt": "real-time hourly (final)"}[market]
    return tuple(Series(key=series_key(market, i), name=f"ISO-NE {lab}, {label} LMP", kind="lmp", market=market,
                        location=n, location_type=t, unit="usd_per_mwh", iso="ISO-NE", meta={"loc_id": i})
                 for i, (n, t, lab) in LOCATIONS.items())


def earliest(now: datetime) -> date:
    return EARLIEST


def latest_da(now: datetime) -> date:
    t = now.astimezone(ET)
    return t.date() + timedelta(days=1) if t.hour >= DA_PUBLISHED_HOUR else t.date()


def latest_rt(now: datetime) -> date:
    return now.astimezone(ET).date() - timedelta(days=RT_FINAL_DAYS)


def url(market: str, d: date, loc_id: int) -> str:
    return f"{BASE}{market}/final/day/{d:%Y%m%d}/location/{loc_id}"


def _auth() -> str:
    from config import settings

    raw = f"{settings.isone_username or ''}:{settings.isone_password or ''}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _make_fetch(market: str):
    async def fetch(get, start: date, end: date) -> list[Fetched]:
        headers = {"Authorization": _auth(), "Accept": "application/json"}
        out = []
        for d in days(start, end):
            for loc in LOCATIONS:
                f = await get.get(url(market, d, loc), headers=headers)
                f.start = f.end = d
                out.append(f)
        return out
    return fetch


def _records(doc) -> list:
    if not isinstance(doc, dict) or "HourlyLmps" not in doc:
        raise ValueError(f"isone: no HourlyLmps in {str(doc)[:200]}")
    root = doc["HourlyLmps"]
    if not root:                       # "" / {} / null: no data for the selection
        return []
    if not isinstance(root, dict):
        raise ValueError(f"isone: HourlyLmps is {type(root).__name__}")
    recs = root.get("HourlyLmp") or []
    return [recs] if isinstance(recs, dict) else recs


def _loc(r: dict) -> int | None:
    loc = r.get("Location")
    if isinstance(loc, dict):
        if loc.get("@LocId") is not None:
            try:
                return int(loc["@LocId"])
            except (TypeError, ValueError):
                pass
        return _BY_NAME.get(str(loc.get("$", "")).strip())
    return _BY_NAME.get(str(loc or "").strip())


def _make_parse(market: str):
    def parse(f: Fetched) -> dict[str, dict[datetime, float | None]]:
        try:
            doc = json.loads(f.body)
        except ValueError as exc:
            raise ValueError(f"isone: not JSON: {f.body[:200]!r}") from exc
        out: dict[str, dict[datetime, float | None]] = {}
        for r in _records(doc):
            lid = _loc(r)
            if lid not in LOCATIONS:
                continue
            try:
                t = datetime.fromisoformat(r["BeginDate"]).astimezone(UTC)
                v = r.get("LmpTotal")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"isone: bad record {str(r)[:200]}: {exc}") from exc
            out.setdefault(series_key(market, lid), {})[t] = None if v in (None, "") else float(v)
        if not out:
            f.empty = True
        return out
    return parse


DATASETS = (
    Dataset(key="isone.da_lmp", source=SOURCE, series=_series("da"), interval_minutes=60, tz=TZ, chunk_days=7,
            earliest=earliest, latest=latest_da, fetch=_make_fetch("da"), parse=_make_parse("da"),
            pace_seconds=1.0, overlap_days=2, keep_raw=True,
            description="hourlylmp/da/final per location, hourly; hub + 8 zones; ~4 KB per series-day (bytes kept "
                        "forward); UNVERIFIED LIVE (no account yet)"),
    Dataset(key="isone.rt_lmp", source=SOURCE, series=_series("rt"), interval_minutes=60, tz=TZ, chunk_days=7,
            earliest=earliest, latest=latest_rt, fetch=_make_fetch("rt"), parse=_make_parse("rt"),
            pace_seconds=1.0, overlap_days=2, keep_raw=True,
            description="hourlylmp/rt/final per location, hourly; hub + 8 zones; ~4 KB per series-day (bytes kept "
                        "forward); UNVERIFIED LIVE (no account yet)"),
)
