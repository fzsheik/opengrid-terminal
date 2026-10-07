"""Provider credentials: OpenGrid-managed by default, bring-your-own (BYO) optional.

OpenGrid-managed   the platform's own provider keys in config.py settings (`<provider>_api_key`).
                   Default for every account: OpenGrid provisions, pays the provider and bills
                   usage through billing/.
BYO                an account stores its own key for a provider; routing then provisions with
                   it and the provider bills the account directly (OpenGrid bills only its fees,
                   record_usage(kind="byo")).

Secrets are Fernet-encrypted (AES-128-CBC + HMAC-SHA256) with settings.credentials_encryption_key.
Deployed (accounts.security.deployed()), that setting MUST be a real Fernet key
(Fernet.generate_key(); deploy/make_env.py writes one): a passphrase is refused (503), and so is a
missing key. Only on a developer's machine may it be a passphrase (stretched) or unset (a key derived
from DATABASE_URL, with a warning).
Rotation: put the new key in CREDENTIALS_ENCRYPTION_KEY and the old one(s) in
CREDENTIALS_ENCRYPTION_KEYS_OLD (comma-separated). Decryption accepts all of them (MultiFernet);
encryption always uses the new one. `python admin.py rotate-credentials` re-encrypts every stored
secret (BYO credentials and alert webhook secrets) under the new key; then drop the old keys.
No API ever returns a secret: listings carry only provider, label, the last 4 characters and dates.

    resolve(account_id, provider) -> ("byo" | "opengrid", secret) | None
        Fails closed: an account's BYO credential that does not decrypt raises CredentialUnavailable,
        it never falls back to OpenGrid-managed credentials. (Routing uses routing/credentials.py,
        which follows the same rule and pins the credential to each deployment.)
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastapi import HTTPException
from sqlalchemy import select, update

import normalize
from accounts.keys import deployed
from config import settings
from store.accounts import Account, ProviderCredential

log = logging.getLogger(__name__)
_warned = False


class CredentialUnavailable(RuntimeError):
    """An account's BYO credential exists but cannot be used (does not decrypt). Never fall back."""


def check_config() -> None:
    """Startup check (api/accounts.install): deployed needs a real Fernet key, and old keys must parse."""
    if deployed():
        if not settings.credentials_encryption_key:
            raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY must be set when deployed")
        _real_fernet(settings.credentials_encryption_key, "CREDENTIALS_ENCRYPTION_KEY")
    for i, old in enumerate(_old_keys()):
        _key_to_fernet(old, f"CREDENTIALS_ENCRYPTION_KEYS_OLD[{i}]")


def _old_keys() -> list[str]:
    return [k.strip() for k in (settings.credentials_encryption_keys_old or "").split(",") if k.strip()]


def _real_fernet(raw: str, name: str) -> Fernet:
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        raise RuntimeError(f"{name} is not a Fernet key (32 url-safe base64 bytes; generate one with "
                           "cryptography.fernet.Fernet.generate_key()); passphrases are refused when deployed")


def _key_to_fernet(raw: str, name: str) -> Fernet:
    if deployed():
        return _real_fernet(raw, name)
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):  # dev only: a passphrase, stretched
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def _fernet() -> MultiFernet:
    """MultiFernet([current, *old]): encrypts with the current key, decrypts with any."""
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
        keys = [_key_to_fernet(raw, "CREDENTIALS_ENCRYPTION_KEY")]
        keys += [_key_to_fernet(k, "CREDENTIALS_ENCRYPTION_KEYS_OLD") for k in _old_keys()]
    except RuntimeError as e:
        log.error("%s", e)
        raise HTTPException(503, "credential storage is disabled: the encryption key is not a valid Fernet key")
    return MultiFernet(keys)


def encrypt(secret: str) -> bytes:
    return _fernet().encrypt(secret.encode())


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(bytes(token)).decode()


def rotate() -> dict:
    """Re-encrypt every stored secret under the CURRENT key (after moving the previous key to
    CREDENTIALS_ENCRYPTION_KEYS_OLD). Rows that decrypt under none of the keys are left untouched
    and counted, never dropped. Idempotent."""
    from store.accounts import AlertRule

    f = _fernet()
    out = {"provider_credentials": 0, "alert_rules": 0, "undecryptable": []}
    with normalize.SessionLocal.begin() as s:
        for c in s.scalars(select(ProviderCredential)):
            try:
                c.secret_encrypted = f.rotate(bytes(c.secret_encrypted))
                out["provider_credentials"] += 1
            except InvalidToken:
                out["undecryptable"].append(f"provider_credentials:{c.id}")
        for r in s.scalars(select(AlertRule).where(AlertRule.webhook_secret_encrypted.is_not(None))):
            try:
                r.webhook_secret_encrypted = f.rotate(bytes(r.webhook_secret_encrypted))
                out["alert_rules"] += 1
            except InvalidToken:
                out["undecryptable"].append(f"alert_rules:{r.id}")
        from accounts.keys import audit

        audit(s, "credentials_rotated", "cli", detail_counts={k: v for k, v in out.items() if k != "undecryptable"},
              undecryptable=out["undecryptable"])
    return out


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

    A BYO secret that no longer decrypts raises CredentialUnavailable (fail closed): the account
    set up its own credential, so OpenGrid must not silently provision (and pay) with its own.
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
                log.error("BYO credential %s for account %s does not decrypt; failing closed", c.id, account_id)
                raise CredentialUnavailable(f"the {provider} credential stored for this account does not decrypt")
            else:
                with normalize.SessionLocal.begin() as s:
                    s.execute(update(ProviderCredential).where(ProviderCredential.id == c.id)
                              .values(last_used_at=datetime.now(timezone.utc)))
                return "byo", secret
    managed = managed_secret(provider)
    return ("opengrid", managed) if managed else None
