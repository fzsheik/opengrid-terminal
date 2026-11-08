"""POST /v1/instant-key: the landing page's "Generate API key" button.

A visitor with no account gets a working key in one click: a new free account holding one key with the
`data:read` scope only (market data; nothing that spends money, routes, or reads anyone else's data). The
secret is returned once, like POST /v1/keys.

Abuse guards, since this writes rows for anonymous callers:
  * the browser must send X-OpenGrid-Request: 1 (a cross-site form cannot; a cross-site fetch with it needs a
    CORS preflight we never grant), so another site cannot mint keys through a visitor's browser;
  * a Bearer token, if sent, must be a valid key (a bad one is refused like on every /v1 route);
  * per client IP: settings.instant_keys_per_ip_per_day in any rolling 24 hours. Counted in the database (the
    account's settings hold a keyed hash of the IP, never the IP), so a restart or a second worker forgives nothing;
  * site-wide: settings.instant_keys_per_day new instant accounts in any rolling 24 hours.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

import normalize
from accounts import keys, security
from accounts.accounts import create_account
from api.common import envelope
from config import settings
from store.accounts import Account

router = APIRouter()

PATH = "/v1/instant-key"
SOURCE = "instant_key"
SCOPES = ["data:read"]
WINDOW = timedelta(days=1)


def ip_hash(ip: str) -> str:
    """A keyed hash of the client IP: lets us count per address without storing it."""
    return hmac.new(keys.pepper(), b"instant-key-ip:" + ip.encode(), hashlib.sha256).hexdigest()[:32]


def _instant_since(since: datetime):
    return (Account.created_at >= since) & (Account.settings["source"].astext == SOURCE)


def _counts(ip_h: str, now: datetime) -> tuple[int, int, datetime | None]:
    """(site-wide count, this IP's count, this IP's oldest creation) within the window."""
    since = now - WINDOW
    with normalize.SessionLocal() as s:
        site = s.scalar(select(func.count()).select_from(Account).where(_instant_since(since))) or 0
        mine, oldest = s.execute(select(func.count(), func.min(Account.created_at)).where(
            _instant_since(since), Account.settings["ip_hash"].astext == ip_h)).one()
    return site, mine or 0, oldest


def _hours(td: timedelta) -> str:
    h = max(1, math.ceil(td.total_seconds() / 3600))
    return "1 hour" if h == 1 else f"{h} hours"


@router.post(PATH, tags=["accounts"], status_code=201, include_in_schema=False,
             summary="Create a free account with one read-only API key (the landing page button)")
def instant_key(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        keys.verify(auth[7:].strip(), request)  # a bad or revoked key is refused (401), as on every /v1 route
    if request.headers.get("x-opengrid-request") != "1":
        raise HTTPException(403, "send the header X-OpenGrid-Request: 1 (the OpenGrid homepage does)")
    if not settings.instant_keys_enabled:
        raise HTTPException(503, "Instant keys are turned off right now. You can still browse live prices without one.")
    now = datetime.now(timezone.utc)
    ip_h = ip_hash(security.client_ip(request) or "?")
    site, mine, oldest = _counts(ip_h, now)
    if site >= settings.instant_keys_per_day:
        raise HTTPException(503, "OpenGrid has handed out all of today's instant keys. Try again later, or browse live "
                                 "prices without a key.", headers={"Retry-After": "3600"})
    limit = max(1, settings.instant_keys_per_ip_per_day)
    if mine >= limit:
        wait = (oldest + WINDOW - now) if oldest else timedelta(hours=1)
        raise HTTPException(429, f"You've made {limit} keys from this network in the last day. Try again in "
                                 f"{_hours(wait)}.", headers={"Retry-After": str(max(60, int(wait.total_seconds())))})
    acct = create_account("Instant key", plan="free", settings={"source": SOURCE, "ip_hash": ip_h})
    key = keys.create_key(acct["id"], "instant", SCOPES, created_by=SOURCE)
    return envelope({"secret": key["secret"], "prefix": key["prefix"], "scopes": key["scopes"],
                     "account_id": acct["id"], "key_id": key["id"]},
                    note="store `secret` now: OpenGrid keeps only a hash and cannot show it again. "
                         "Send it as Authorization: Bearer <secret>. It reads market data only (data:read).")
