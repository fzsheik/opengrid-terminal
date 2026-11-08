"""POST /v1/instant-key: the homepage's "Generate API key" button.

A visitor with no account gets a working key in one click: a new free account holding one key with the
`data:read` scope only (market data; nothing that spends money, routes, or reads anyone else's data). The
secret is returned once, like POST /v1/keys.

Abuse guards, since this writes rows for anonymous callers:
  * the browser must send X-OpenGrid-Request: 1 (a cross-site form cannot; a cross-site fetch with it needs a
    CORS preflight we never grant), so another site cannot mint keys through a visitor's browser;
  * per client IP: settings.instant_keys_per_ip_per_day (in process memory; a restart forgives, which is fine);
  * site-wide: settings.instant_keys_per_day new instant accounts in any rolling 24 hours (counted in the DB).
"""

from __future__ import annotations

import threading
import time
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
DAY = 86_400.0

_lock = threading.Lock()
_by_ip: dict[str, list[float]] = {}


def _ip_allowed(ip: str, now: float) -> bool:
    with _lock:
        recent = [t for t in _by_ip.get(ip, []) if now - t < DAY]
        if len(recent) >= max(1, settings.instant_keys_per_ip_per_day):
            _by_ip[ip] = recent
            return False
        recent.append(now)
        _by_ip[ip] = recent
        if len(_by_ip) > 50_000:  # bound memory: drop addresses with nothing in the window
            for k in [k for k, v in _by_ip.items() if not v or now - v[-1] >= DAY]:
                _by_ip.pop(k, None)
        return True


def _site_wide_count() -> int:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    with normalize.SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(Account).where(
            Account.created_at >= since, Account.settings["source"].astext == SOURCE)) or 0


def reset() -> None:
    with _lock:
        _by_ip.clear()


@router.post(PATH, tags=["accounts"], status_code=201, include_in_schema=False,
             summary="Create a free account with one read-only API key (the homepage button)")
def instant_key(request: Request):
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        keys.verify(auth[7:].strip(), request)  # a bad or revoked key is refused (401), as on every /v1 route
    if request.headers.get("x-opengrid-request") != "1":
        raise HTTPException(403, "send the header X-OpenGrid-Request: 1 (the OpenGrid homepage does)")
    if not settings.instant_keys_enabled:
        raise HTTPException(503, "Instant keys are turned off right now.")
    if _site_wide_count() >= settings.instant_keys_per_day:
        raise HTTPException(503, "OpenGrid has handed out all of today's instant keys. Try again tomorrow.")
    if not _ip_allowed(security.client_ip(request) or "?", time.time()):
        raise HTTPException(429, "You already made today's keys from this network. Try again tomorrow.",
                            headers={"Retry-After": "3600"})
    acct = create_account("Instant key", plan="free", settings={"source": SOURCE})
    key = keys.create_key(acct["id"], "instant", SCOPES, created_by=SOURCE)
    return envelope({"secret": key["secret"], "prefix": key["prefix"], "scopes": key["scopes"],
                     "account_id": acct["id"], "key_id": key["id"]},
                    note="store `secret` now: OpenGrid keeps only a hash and cannot show it again. "
                         "Send it as Authorization: Bearer <secret>.")
