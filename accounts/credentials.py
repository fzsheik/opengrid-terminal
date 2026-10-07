"""Provider credentials: OpenGrid-managed by default, bring-your-own (BYO) optional.

OpenGrid-managed   the platform's own provider keys in config.py settings (`<provider>_api_key`).
                   Default for every account: OpenGrid provisions, pays the provider and bills
                   usage through billing/.
BYO                an account stores its own key for a provider; routing then provisions with
                   it and the provider bills the account directly (OpenGrid bills only its fees,
                   record_usage(kind="byo")).

Secrets are Fernet-encrypted (AES-128-CBC + HMAC-SHA256) with settings.credentials_encryption_key.
That setting may be a Fernet key or any passphrase (stretched with SHA-256). Deployed without it,
storing/reading BYO secrets fails closed; in dev a derived key is used with a warning.
No API ever returns a secret: listings carry only provider, label, the last 4 characters and dates.

    resolve(account_id, provider) -> ("byo" | "opengrid", secret) | None      (for routing)
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select, update

import normalize
from accounts.keys import deployed
from config import settings
from store.accounts import Account, ProviderCredential

log = logging.getLogger(__name__)
_warned = False


def check_config() -> None:
    if deployed() and not settings.credentials_encryption_key:
        raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY must be set when deployed")


def _fernet() -> Fernet:
    global _warned
    raw = settings.credentials_encryption_key
    if not raw:
        if deployed():
            raise HTTPException(503, "credential storage is disabled: CREDENTIALS_ENCRYPTION_KEY is not configured")
        if not _warned:
            log.warning("credentials_encryption_key unset: using a dev key derived from DATABASE_URL")
            _warned = True
        raw = "opengrid-dev-credentials:" + settings.database_url
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def encrypt(secret: str) -> bytes:
    return _fernet().encrypt(secret.encode())


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(bytes(token)).decode()


def _hint(secret: str) -> str:
    return "…" + secret[-4:] if len(secret) >= 12 else "…"


def as_dict(c: ProviderCredential) -> dict:
    return {"id": c.id, "account_id": c.account_id, "provider": c.provider, "label": c.label, "hint": c.hint,
            "source": "byo", "state": "revoked" if c.revoked_at else "active", "created_at": c.created_at,
            "last_used_at": c.last_used_at, "revoked_at": c.revoked_at}


# Providers OpenGrid reaches through another platform's single key.
MANAGED_VIA = {"crusoe": "shadeform", "denvr": "shadeform", "latitude": "shadeform"}


def managed_secret(provider: str) -> str | None:
    """The platform's own key for a provider, from settings (e.g. lambda_api_key).

    Providers whose credentials are not one API key (Verda's client id + secret) return None
    here; their routing adapter reads its settings directly.
    """
    p = provider.lower().replace("-", "_")
    return getattr(settings, f"{MANAGED_VIA.get(p, p)}_api_key", None) or None


def managed_providers() -> list[str]:
    return sorted(k[: -len("_api_key")] for k in type(settings).model_fields if k.endswith("_api_key")
                  and k != "api_key_pepper" and getattr(settings, k))


def add(account_id: int, provider: str, secret: str, label: str | None = None) -> dict:
    """Store a BYO credential. An existing active one for the same provider is revoked (one active each)."""
    provider = (provider or "").strip().lower()
    if not provider or not secret or len(secret) > 4096:
        raise ValueError("provider and secret are required")
    blob = encrypt(secret)
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        if s.get(Account, account_id) is None:
            raise KeyError(account_id)
        s.execute(update(ProviderCredential)
                  .where(ProviderCredential.account_id == account_id, ProviderCredential.provider == provider,
                         ProviderCredential.revoked_at.is_(None))
                  .values(revoked_at=now))
        c = ProviderCredential(account_id=account_id, provider=provider, secret_encrypted=blob,
                               hint=_hint(secret), label=label, created_at=now)
        s.add(c)
        s.flush()
        return as_dict(c)


def list_for(account_id: int, include_revoked: bool = False) -> list[dict]:
    with normalize.SessionLocal() as s:
        q = select(ProviderCredential).where(ProviderCredential.account_id == account_id)
        if not include_revoked:
            q = q.where(ProviderCredential.revoked_at.is_(None))
        return [as_dict(c) for c in s.scalars(q.order_by(ProviderCredential.id))]


def revoke(account_id: int, credential_id: int) -> dict:
    with normalize.SessionLocal.begin() as s:
        c = s.get(ProviderCredential, credential_id)
        if c is None or c.account_id != account_id:
            raise KeyError(credential_id)
        if c.revoked_at is None:
            c.revoked_at = datetime.now(timezone.utc)
        return as_dict(c)


def resolve(account_id: int | None, provider: str) -> tuple[str, str] | None:
    """The secret routing should use for (account, provider): BYO if the account has an active one,
    else OpenGrid-managed if configured, else None (cannot provision there).

    A BYO secret that no longer decrypts (key rotated) is logged and skipped, falling back to managed.
    """
    provider = provider.lower()
    if account_id is not None:
        with normalize.SessionLocal() as s:
            c = s.scalars(select(ProviderCredential)
                          .where(ProviderCredential.account_id == account_id, ProviderCredential.provider == provider,
                                 ProviderCredential.revoked_at.is_(None))
                          .order_by(ProviderCredential.id.desc()).limit(1)).first()
        if c is not None:
            try:
                secret = decrypt(c.secret_encrypted)
            except InvalidToken:
                log.error("BYO credential %s for account %s does not decrypt; falling back", c.id, account_id)
            else:
                with normalize.SessionLocal.begin() as s:
                    s.execute(update(ProviderCredential).where(ProviderCredential.id == c.id)
                              .values(last_used_at=datetime.now(timezone.utc)))
                return "byo", secret
    managed = managed_secret(provider)
    return ("opengrid", managed) if managed else None
