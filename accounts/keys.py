"""OpenGrid API keys: generation, storage, verification.

Format     opg_live_<43 url-safe chars>  (secrets.token_urlsafe(32): 256 bits of entropy)
Prefix     the first 13 chars, e.g. "opg_live_ab12", stored and shown so a user can tell keys apart
Storage    ONLY HMAC-SHA256(pepper, key) as hex. A slow password hash (bcrypt/argon2) buys nothing
           for a 256-bit random secret, which cannot be brute-forced; a keyed hash means a stolen
           database alone does not even allow offline guessing, and lookup is one indexed equality.
           The comparison after lookup is constant-time anyway.
Pepper     settings.api_key_pepper. Required when deployed (accounts.security.deployed()): without it
           verify() and create_key() fail closed (503). In dev a pepper is derived from the database
           URL, with a warning. Rotating the pepper invalidates every key.

verify(token, request) -> Principal, or raises:
    401  malformed / unknown / revoked / expired key
    403  the account is suspended
    429  over the per-key OR the per-account rate limit (Retry-After + X-RateLimit-* headers)
A key's `admin` scope is honoured only when its lineage row says platform_admin (set by the
operator); otherwise it is dropped from the Principal.
It also updates last_used_at / last_used_ip (at most once a minute per key, so a busy key does
not write on every request) and hands the request to accounts.usage for logging.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, text, update

import normalize
from accounts import ratelimit
from accounts.auth import ALL, SCOPES, Principal
from accounts.security import client_ip as _security_client_ip
from accounts.security import deployed  # noqa: F401  (re-exported: other modules import it from here)
from config import settings
from store.accounts import Account, ApiKey
from store.security import ApiKeyLineage, SecurityEvent

log = logging.getLogger(__name__)

KEY_PREFIX = "opg_live_"
PREFIX_SHOWN = len(KEY_PREFIX) + 4
LAST_USED_EVERY = timedelta(seconds=60)
DEFAULT_SCOPES = ("data:read",)

_warned = False
_last_write: dict[int, datetime] = {}
_lw_lock = threading.Lock()


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


class KeyLimitError(ValueError):
    """The account already holds settings.max_keys_per_account active keys."""


def as_dict(k: ApiKey, lineage: ApiKeyLineage | None = None) -> dict:
    """Key metadata. Never includes the secret or its hash."""
    now = datetime.now(timezone.utc)
    state = "revoked" if k.revoked_at else "expired" if k.expires_at and k.expires_at <= now else "active"
    return {"id": k.id, "account_id": k.account_id, "name": k.name, "prefix": k.prefix, "scopes": list(k.scopes),
            "state": state, "created_at": k.created_at, "last_used_at": k.last_used_at,
            "last_used_ip": k.last_used_ip, "expires_at": k.expires_at, "revoked_at": k.revoked_at,
            "rate_limit_per_minute": k.rate_limit_per_minute,
            "parent_key_id": lineage.parent_key_id if lineage else None,
            "platform_admin": bool(lineage and lineage.platform_admin)}


def audit(s, kind: str, actor: str, account_id: int | None = None, key_id: int | None = None, **detail) -> None:
    """Append a security_events row in the caller's transaction. Never pass a secret."""
    s.add(SecurityEvent(kind=kind, actor=actor[:64], account_id=account_id, key_id=key_id, detail=detail,
                        ts=datetime.now(timezone.utc)))


def active_key_count(s, account_id: int) -> int:
    now = datetime.now(timezone.utc)
    return s.scalar(select(func.count()).select_from(ApiKey).where(
        ApiKey.account_id == account_id, ApiKey.revoked_at.is_(None),
        (ApiKey.expires_at.is_(None)) | (ApiKey.expires_at > now))) or 0


def create_key(account_id: int, name: str = "default", scopes=None, expires_at: datetime | None = None,
               rate_limit_per_minute: int | None = None, *, parent_key_id: int | None = None,
               platform_admin: bool = False, created_by: str = "operator") -> dict:
    """Returns the key's metadata plus `secret`: the only time the full key exists outside the caller.

    At most settings.max_keys_per_account active keys per account (KeyLimitError). The account row
    is locked while counting, so two concurrent creations cannot both slip under the cap.
    `parent_key_id`: the key that created this one (revoking the parent revokes it too)."""
    scopes = validate_scopes(scopes)
    if rate_limit_per_minute is not None and rate_limit_per_minute < 1:
        raise ValueError("rate_limit_per_minute must be >= 1")
    token = generate()
    digest = hash_key(token)
    with normalize.SessionLocal.begin() as s:
        acct = s.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if acct is None:
            raise KeyError(f"account {account_id} not found")
        if active_key_count(s, account_id) >= settings.max_keys_per_account:
            raise KeyLimitError(f"account {account_id} already has {settings.max_keys_per_account} active keys "
                                "(settings.max_keys_per_account); revoke one first")
        now = datetime.now(timezone.utc)
        k = ApiKey(account_id=account_id, name=(name or "default")[:200], prefix=token[:PREFIX_SHOWN],
                   secret_hash=digest, scopes=scopes, expires_at=expires_at,
                   rate_limit_per_minute=rate_limit_per_minute, created_at=now)
        s.add(k)
        s.flush()
        lin = ApiKeyLineage(key_id=k.id, parent_key_id=parent_key_id, platform_admin=bool(platform_admin),
                            created_by=created_by, created_at=now)
        s.add(lin)
        audit(s, "key_created", f"key:{parent_key_id}" if parent_key_id else created_by, account_id, k.id,
              scopes=scopes, platform_admin=bool(platform_admin), rate_limit_per_minute=rate_limit_per_minute,
              expires_at=expires_at.isoformat() if expires_at else None, parent_key_id=parent_key_id)
        s.flush()
        out = as_dict(k, lin)
    out["secret"] = token
    return out


def _lineage(s, key_ids) -> dict:
    ids = list(key_ids)
    if not ids:
        return {}
    return {r.key_id: r for r in s.scalars(select(ApiKeyLineage).where(ApiKeyLineage.key_id.in_(ids)))}


def list_keys(account_id: int | None = None) -> list[dict]:
    with normalize.SessionLocal() as s:
        q = select(ApiKey).order_by(ApiKey.id)
        if account_id is not None:
            q = q.where(ApiKey.account_id == account_id)
        ks = list(s.scalars(q))
        lin = _lineage(s, (k.id for k in ks))
        return [as_dict(k, lin.get(k.id)) for k in ks]


def get_key(key_id: int) -> dict | None:
    with normalize.SessionLocal() as s:
        k = s.get(ApiKey, key_id)
        return as_dict(k, s.get(ApiKeyLineage, key_id)) if k else None


_DESCENDANTS = text("""
    WITH RECURSIVE d(id) AS (
        SELECT key_id FROM api_key_lineage WHERE parent_key_id = :root
        UNION
        SELECT l.key_id FROM api_key_lineage l JOIN d ON l.parent_key_id = d.id
    ) SELECT id FROM d
""")


def revoke_key(key_id: int, account_id: int | None = None, actor: str = "operator") -> dict:
    """Revoke (idempotent), and every key it created, transitively (cascade). With `account_id`,
    only that account's key; else KeyError. `cascaded` in the result lists the child keys revoked now."""
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        k = s.get(ApiKey, key_id)
        if k is None or (account_id is not None and k.account_id != account_id):
            raise KeyError(key_id)
        if k.revoked_at is None:
            k.revoked_at = now
            audit(s, "key_revoked", actor, k.account_id, k.id)
        kids = [r[0] for r in s.execute(_DESCENDANTS, {"root": key_id})]
        cascaded = []
        if kids:
            cascaded = [r[0] for r in s.execute(
                update(ApiKey).where(ApiKey.id.in_(kids), ApiKey.revoked_at.is_(None)).values(revoked_at=now)
                .returning(ApiKey.id))]
            for cid in cascaded:
                audit(s, "key_revoked", actor, k.account_id, cid, cascade_from=key_id)
        out = as_dict(k, s.get(ApiKeyLineage, key_id))
        out["cascaded"] = sorted(cascaded)
        return out


def _client_ip(request) -> str | None:
    return _security_client_ip(request)


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


# A suspended account may still see and shut down what it is running: suspension must never
# strand live, billing compute. Everything else (new routes, keys, data) is refused.
_SUSPENDED_OK = re.compile(r"^/v1/deployments(?:/[A-Za-z0-9_-]+(?:/(?:terminate|stop))?)?$")


def _allowed_while_suspended(request) -> bool:
    if request is None:
        return False
    path, method = request.url.path, request.method
    if not _SUSPENDED_OK.match(path):
        return False
    return method in ("GET", "HEAD") or path.endswith(("/terminate", "/stop"))


def verify(token: str, request) -> Principal:
    if not token.startswith("opg_") or len(token) > 200:
        _deny("invalid API key")
    digest = hash_key(token)
    with normalize.SessionLocal() as s:
        row = s.execute(
            select(ApiKey, Account.status, Account.settings, ApiKeyLineage.platform_admin)
            .join(Account, Account.id == ApiKey.account_id)
            .outerjoin(ApiKeyLineage, ApiKeyLineage.key_id == ApiKey.id)
            .where(ApiKey.secret_hash == digest)
        ).first()
    if row is None or not hmac.compare_digest(row[0].secret_hash, digest):
        _deny("invalid API key")
    k, account_status, account_settings, platform_admin = row
    now = datetime.now(timezone.utc)
    if k.revoked_at is not None:
        _deny("API key revoked")
    if k.expires_at is not None and k.expires_at <= now:
        _deny("API key expired")
    if account_status != "active" and not _allowed_while_suspended(request):
        _deny("account suspended", 403)

    state = request.state if request is not None else None
    if state is not None:
        state.usage_key = (k.id, k.account_id)  # from here on the request is logged, even if limited
    method = request.method if request is not None else "GET"
    path = request.url.path if request is not None else "/v1/"
    cls = ratelimit.request_class(method, path)
    # Both the key's own bucket and its ACCOUNT's bucket must have a token: more keys, same budget.
    decision = ratelimit.take_all([((k.id, cls), ratelimit.limit_for(cls, k.rate_limit_per_minute)),
                                   (("account", k.account_id, cls), ratelimit.account_limit_for(cls, account_settings))],
                                  cls)
    if state is not None:
        state.ratelimit = decision
    if not decision.allowed:
        raise HTTPException(429, f"rate limit exceeded ({decision.limit}/min for {cls} requests)",
                            headers=decision.headers())
    _touch(k.id, _client_ip(request), now)
    scopes = frozenset(s for s in k.scopes if s != ALL)  # a stored "*" never grants everything
    if not platform_admin:
        scopes = scopes - {"admin"}  # cross-tenant admin only for keys the operator flagged platform_admin
    return Principal(kind="api_key", account_id=k.account_id, key_id=k.id, scopes=scopes,
                     platform_admin=bool(platform_admin))


def reset_cache() -> None:
    with _lw_lock:
        _last_write.clear()
