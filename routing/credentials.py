"""Which provider credentials routing uses, and the credential PINNED to each deployment.

Launch (resolve_for_launch):
    The credential provider is the adapter's CREDENTIAL_PROVIDER (default: the provider itself). Crusoe /
    Denvr / Latitude are reached through Shadeform, so their credential provider is 'shadeform': a
    customer's NATIVE Crusoe key stored under 'crusoe' is never looked up for, nor sent to, Shadeform.
    1. the account has an active BYO credential for that credential provider -> use it. If it does not
       decrypt (key rotated, corrupted) -> FAIL CLOSED (CredentialsUnavailable). Never fall back to
       OpenGrid's own key for a caller who set up their own.
    2. else OpenGrid-managed credentials from settings (platform()).
    3. else none: the candidate is skipped.
    The result carries credential_ref ('byo:<row id>' | 'platform:<credential provider>') which the engine
    stores on the deployment with credential_source and credential_account_id.

Management (for_ref / routing.deployments.credentials_for):
    status, stop, terminate and reconciliation use EXACTLY the pinned credential_ref. A revoked or
    undecryptable BYO row, or a missing platform key, raises CredentialsUnavailable: the deployment is
    flagged credentials_unavailable, never marked terminated, and never retried with other credentials.
    Adding, replacing or revoking a BYO key after launch therefore never changes which account
    OpenGrid asks about an existing instance.

Verda needs a client id AND secret: stored BYO as "client_id:client_secret", OpenGrid-managed in
settings.verda_client_id / verda_client_secret.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from config import settings

log = logging.getLogger(__name__)

VIA = {"crusoe": "shadeform", "denvr": "shadeform", "latitude": "shadeform"}


class CredentialsUnavailable(Exception):
    """The credential a deployment is pinned to (or the BYO credential an account configured) cannot be used."""

    def __init__(self, message: str, ref: str | None = None):
        super().__init__(message)
        self.message = message
        self.ref = ref


@dataclass
class Resolved:
    credentials: dict
    source: str                      # 'byo' | 'opengrid'
    ref: str                         # 'byo:<id>' | 'platform:<credential provider>'
    credential_account_id: int | None
    credential_provider: str


def credential_provider(provider: str) -> str:
    """Whose API the credential is for: the adapter's CREDENTIAL_PROVIDER, else the provider."""
    from routing import adapters
    cls = adapters.get(provider)
    cp = getattr(cls, "CREDENTIAL_PROVIDER", None) if cls is not None else None
    return (cp or VIA.get(provider) or provider).lower()


def _needs_no_credentials(provider: str) -> bool:
    from routing import adapters
    cls = adapters.get(provider)
    return cls is not None and not tuple(getattr(cls, "CREDENTIALS", ("api_key",)) or ())


def platform(provider: str) -> dict | None:
    """OpenGrid-managed credentials from settings, or None. ({} for an adapter that needs none.)"""
    p = VIA.get(provider, provider)
    if _needs_no_credentials(p):
        return {}
    if p == "verda":
        if settings.verda_client_id and settings.verda_client_secret:
            return {"client_id": settings.verda_client_id, "client_secret": settings.verda_client_secret}
        return None
    key = getattr(settings, f"{p}_api_key", None)
    return {"api_key": key} if key else None


def _as_dict(provider: str, secret: str) -> dict:
    if VIA.get(provider, provider) == "verda":
        cid, _, csecret = secret.partition(":")
        return {"client_id": cid, "client_secret": csecret}
    return {"api_key": secret}


def _byo_rows():
    from store.accounts import ProviderCredential
    return ProviderCredential


def _decrypt(blob) -> str:
    from accounts import credentials as ac
    return ac.decrypt(blob)


def resolve_for_launch(account_id: int | None, provider: str) -> Resolved | None:
    """The credential a NEW launch on `provider` would use, or None. Raises CredentialsUnavailable when the
    account has a BYO credential for that provider that cannot be used (fail closed)."""
    import normalize
    from sqlalchemy import select, update

    cp = credential_provider(provider)
    if account_id is not None:
        PC = _byo_rows()
        try:
            with normalize.SessionLocal() as s:
                row = s.scalars(select(PC).where(PC.account_id == account_id, PC.provider == cp,
                                                 PC.revoked_at.is_(None))
                                .order_by(PC.id.desc()).limit(1)).first()
        except Exception as exc:  # noqa: BLE001 - lookup failure must not fall back to platform keys
            log.exception("credential lookup failed for account %s / %s", account_id, cp)
            raise CredentialsUnavailable(f"credential lookup failed for {cp}") from exc
        if row is not None:
            try:
                secret = _decrypt(row.secret_encrypted)
            except Exception as exc:  # noqa: BLE001 - InvalidToken, missing key, HTTPException(503)
                log.error("BYO credential %s (account %s, %s) does not decrypt: failing closed", row.id, account_id, cp)
                raise CredentialsUnavailable(f"your {cp} credential could not be decrypted; OpenGrid will not "
                                             f"fall back to its own key", ref=f"byo:{row.id}") from exc
            try:
                with normalize.SessionLocal.begin() as s:
                    s.execute(update(PC).where(PC.id == row.id).values(last_used_at=datetime.now(timezone.utc)))
            except Exception:  # noqa: BLE001
                log.exception("could not touch last_used_at of credential %s", row.id)
            return Resolved(_as_dict(cp, secret), "byo", f"byo:{row.id}", account_id, cp)
    creds = platform(cp)
    if creds is not None and (creds or _needs_no_credentials(provider)):
        return Resolved(creds, "opengrid", f"platform:{cp}", None, cp)
    return None


def resolve(account_id: int | None, provider: str) -> tuple[dict | None, str | None]:
    """Back-compat: (credentials, source) or (None, None). Fails closed on an unusable BYO credential."""
    try:
        r = resolve_for_launch(account_id, provider)
    except CredentialsUnavailable:
        return None, None
    return (r.credentials, r.source) if r else (None, None)


def for_ref(ref: str | None, provider: str) -> dict:
    """Exactly the pinned credential. Raises CredentialsUnavailable; never substitutes another."""
    import normalize

    if not ref:
        raise CredentialsUnavailable("no credential is pinned to this deployment", ref=ref)
    kind, _, rest = ref.partition(":")
    if kind == "platform":
        creds = platform(rest or credential_provider(provider))
        if creds is None or (not creds and not _needs_no_credentials(provider)):
            raise CredentialsUnavailable(f"OpenGrid-managed {rest} credentials are no longer configured", ref=ref)
        return creds
    if kind == "byo":
        try:
            row_id = int(rest)
        except ValueError:
            raise CredentialsUnavailable("the BYO credential used at launch is not recorded", ref=ref)
        PC = _byo_rows()
        with normalize.SessionLocal() as s:
            row = s.get(PC, row_id)
        if row is None:
            raise CredentialsUnavailable("the BYO credential used at launch no longer exists", ref=ref)
        if row.revoked_at is not None:
            raise CredentialsUnavailable("the BYO credential used at launch was revoked; restore it (or manage the "
                                         "instance in the provider console) - OpenGrid will not use other keys", ref=ref)
        try:
            return _as_dict(row.provider, _decrypt(row.secret_encrypted))
        except Exception as exc:  # noqa: BLE001
            raise CredentialsUnavailable("the BYO credential used at launch does not decrypt", ref=ref) from exc
    raise CredentialsUnavailable(f"unknown credential reference {ref!r}", ref=ref)
