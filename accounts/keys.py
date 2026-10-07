"""OpenGrid API keys: generation, storage, verification.

Format     opg_live_<43 url-safe chars>  (secrets.token_urlsafe(32): 256 bits of entropy)
Prefix     the first 13 chars, e.g. "opg_live_ab12", stored and shown so a user can tell keys apart
Storage    ONLY HMAC-SHA256(pepper, key) as hex. A slow password hash (bcrypt/argon2) buys nothing
           for a 256-bit random secret, which cannot be brute-forced; a keyed hash means a stolen
           database alone does not even allow offline guessing, and lookup is one indexed equality.
           The comparison after lookup is constant-time anyway.
Pepper     settings.api_key_pepper. Required when deployed (RAILWAY_ENVIRONMENT): without it
           verify() and create_key() fail closed (503). In dev a pepper is derived from the database
           URL, with a warning. Rotating the pepper invalidates every key.

verify(token, request) -> Principal, or raises:
    401  malformed / unknown / revoked / expired key
    403  the account is suspended
    429  over the per-key rate limit (Retry-After + X-RateLimit-* headers)
It also updates last_used_at / last_used_ip (at most once a minute per key, so a busy key does
not write on every request) and hands the request to accounts.usage for logging.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, update

import normalize
from accounts import ratelimit
from accounts.auth import ALL, SCOPES, Principal
from config import settings
from store.accounts import Account, ApiKey

log = logging.getLogger(__name__)

KEY_PREFIX = "opg_live_"
PREFIX_SHOWN = len(KEY_PREFIX) + 4
LAST_USED_EVERY = timedelta(seconds=60)
DEFAULT_SCOPES = ("data:read",)

_warned = False
_last_write: dict[int, datetime] = {}
_lw_lock = threading.Lock()


def deployed() -> bool:
    return bool(os.environ.get("RAILWAY_ENVIRONMENT"))


def check_config() -> None:
    """Called at startup: deployed without a pepper is refused, like APP_PASSWORD."""
    if deployed() and not settings.api_key_pepper:
        raise RuntimeError("API_KEY_PEPPER must be set when deployed")


def pepper() -> bytes:
    global _warned
    if settings.api_key_pepper:
        return settings.api_key_pepper.encode()
    if deployed():
        raise HTTPException(503, "API keys are disabled: API_KEY_PEPPER is not configured")
    if not _warned:
        log.warning("api_key_pepper unset: using a dev pepper derived from DATABASE_URL (never do this deployed)")
        _warned = True
    return hashlib.sha256(b"opengrid-dev-pepper:" + settings.database_url.encode()).digest()


def hash_key(token: str) -> str:
    return hmac.new(pepper(), token.encode(), hashlib.sha256).hexdigest()


def generate() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def validate_scopes(scopes) -> list[str]:
    scopes = sorted(set(scopes or DEFAULT_SCOPES))
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scopes {bad}; valid: {sorted(SCOPES)}")
    return scopes


def as_dict(k: ApiKey) -> dict:
    """Key metadata. Never includes the secret or its hash."""
    now = datetime.now(timezone.utc)
    state = "revoked" if k.revoked_at else "expired" if k.expires_at and k.expires_at <= now else "active"
    return {"id": k.id, "account_id": k.account_id, "name": k.name, "prefix": k.prefix, "scopes": list(k.scopes),
            "state": state, "created_at": k.created_at, "last_used_at": k.last_used_at,
            "last_used_ip": k.last_used_ip, "expires_at": k.expires_at, "revoked_at": k.revoked_at,
            "rate_limit_per_minute": k.rate_limit_per_minute}


def create_key(account_id: int, name: str = "default", scopes=None, expires_at: datetime | None = None,
               rate_limit_per_minute: int | None = None) -> dict:
    """Returns the key's metadata plus `secret`: the only time the full key exists outside the caller."""
    scopes = validate_scopes(scopes)
    if rate_limit_per_minute is not None and rate_limit_per_minute < 1:
        raise ValueError("rate_limit_per_minute must be >= 1")
    token = generate()
    digest = hash_key(token)
    with normalize.SessionLocal.begin() as s:
        if s.get(Account, account_id) is None:
            raise KeyError(f"account {account_id} not found")
        k = ApiKey(account_id=account_id, name=(name or "default")[:200], prefix=token[:PREFIX_SHOWN],
                   secret_hash=digest, scopes=scopes, expires_at=expires_at,
                   rate_limit_per_minute=rate_limit_per_minute, created_at=datetime.now(timezone.utc))
        s.add(k)
        s.flush()
        out = as_dict(k)
    out["secret"] = token
    return out


def list_keys(account_id: int | None = None) -> list[dict]:
    with normalize.SessionLocal() as s:
        q = select(ApiKey).order_by(ApiKey.id)
        if account_id is not None:
            q = q.where(ApiKey.account_id == account_id)
        return [as_dict(k) for k in s.scalars(q)]


def get_key(key_id: int) -> dict | None:
    with normalize.SessionLocal() as s:
        k = s.get(ApiKey, key_id)
        return as_dict(k) if k else None


def revoke_key(key_id: int, account_id: int | None = None) -> dict:
    """Revoke (idempotent). With `account_id`, only that account's key; else KeyError."""
    with normalize.SessionLocal.begin() as s:
        k = s.get(ApiKey, key_id)
        if k is None or (account_id is not None and k.account_id != account_id):
            raise KeyError(key_id)
        if k.revoked_at is None:
            k.revoked_at = datetime.now(timezone.utc)
        return as_dict(k)


def _client_ip(request) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


def _deny(detail: str, status: int = 401):
    raise HTTPException(status, detail, headers={"WWW-Authenticate": "Bearer"} if status == 401 else None)


def _touch(key_id: int, ip: str | None, now: datetime) -> None:
    with _lw_lock:
        last = _last_write.get(key_id)
        if last is not None and now - last < LAST_USED_EVERY:
            return
        _last_write[key_id] = now
    with normalize.SessionLocal.begin() as s:
        s.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now, last_used_ip=ip))


def verify(token: str, request) -> Principal:
    if not token.startswith("opg_") or len(token) > 200:
        _deny("invalid API key")
    digest = hash_key(token)
    with normalize.SessionLocal() as s:
        row = s.execute(
            select(ApiKey, Account.status).join(Account, Account.id == ApiKey.account_id)
            .where(ApiKey.secret_hash == digest)
        ).first()
    if row is None or not hmac.compare_digest(row[0].secret_hash, digest):
        _deny("invalid API key")
    k, account_status = row
    now = datetime.now(timezone.utc)
    if k.revoked_at is not None:
        _deny("API key revoked")
    if k.expires_at is not None and k.expires_at <= now:
        _deny("API key expired")
    if account_status != "active":
        _deny("account suspended", 403)

    state = request.state if request is not None else None
    if state is not None:
        state.usage_key = (k.id, k.account_id)  # from here on the request is logged, even if limited
    method = request.method if request is not None else "GET"
    path = request.url.path if request is not None else "/v1/"
    cls = ratelimit.request_class(method, path)
    decision = ratelimit.take(k.id, cls, ratelimit.limit_for(cls, k.rate_limit_per_minute))
    if state is not None:
        state.ratelimit = decision
    if not decision.allowed:
        raise HTTPException(429, f"rate limit exceeded ({decision.limit}/min for {cls} requests)",
                            headers=decision.headers())
    _touch(k.id, _client_ip(request), now)
    scopes = frozenset(s for s in k.scopes if s != ALL)  # a stored "*" never grants everything
    return Principal(kind="api_key", account_id=k.account_id, key_id=k.id, scopes=scopes)


def reset_cache() -> None:
    with _lw_lock:
        _last_write.clear()
