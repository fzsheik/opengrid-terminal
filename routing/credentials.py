"""Which credentials routing uses for (account, provider).

accounts.credentials.resolve() decides (BYO first, then OpenGrid-managed). It returns one
secret string; Verda needs a client id AND secret, stored BYO as "client_id:client_secret"
and OpenGrid-managed in settings.verda_client_id / verda_client_secret.

Fails closed: if the accounts lookup itself errors, no credentials (never silently fall
back to OpenGrid's own key for a caller who may have meant their own).
"""

from __future__ import annotations

import logging

from config import settings

log = logging.getLogger(__name__)

VIA = {"crusoe": "shadeform", "denvr": "shadeform", "latitude": "shadeform"}


def platform(provider: str) -> dict | None:
    """OpenGrid-managed credentials from settings, or None."""
    p = VIA.get(provider, provider)
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


def resolve(account_id: int | None, provider: str) -> tuple[dict | None, str | None]:
    """(credentials, source 'byo' | 'opengrid') or (None, None)."""
    try:
        from accounts import credentials as ac
    except ImportError:
        ac = None
    if ac is not None and hasattr(ac, "resolve"):
        try:
            r = ac.resolve(account_id, provider)
        except Exception:
            log.exception("credential lookup failed for account %s / %s", account_id, provider)
            return None, None
        if r:
            source, secret = r
            return _as_dict(provider, secret), source
    creds = platform(provider)
    return (creds, "opengrid") if creds else (None, None)
