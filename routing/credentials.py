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


# --------------------------------------------------------------------------
# The exact secret pinned at launch (money-loss invariant)
# --------------------------------------------------------------------------
# A ref like 'platform:lambda' names WHERE the key comes from, not WHICH key. If the OpenGrid-managed key in
# settings is replaced by a key of a DIFFERENT provider account, that account lists nothing and answers 404
# for the instance -- two "signals" that would mark a live, billing instance terminated. So the launch also
# pins a one-way fingerprint of the secret (deployments.provider_metadata.credential_fingerprint; never the
# secret) and every management call checks it: a changed secret -> CredentialsUnavailable (state kept, alert),
# never "terminated". Restoring the launch key resumes management. Rows without a fingerprint (launched
# before this check) are not checked.

def secret_fingerprint(creds: dict | None) -> str | None:
    """A short one-way fingerprint of a credential dict (domain-separated SHA-256; not reversible)."""
    import hashlib
    import json

    if not creds:
        return None
    blob = json.dumps(sorted((str(k), str(v)) for k, v in creds.items() if v), separators=(",", ":"))
    return "sha256:" + hashlib.sha256(b"opengrid-credential-pin:" + blob.encode()).hexdigest()[:24]


def check_pinned(creds: dict | None, pinned: str | None, ref: str | None) -> None:
    """Raise CredentialsUnavailable when `creds` is not the secret the deployment was launched with."""
    if pinned and secret_fingerprint(creds) != pinned:
        raise CredentialsUnavailable(
            "the credential pinned at launch was replaced (its secret changed since launch); OpenGrid will not "
            "manage this instance with a different key -- it may belong to another provider account. Restore the "
            "launch key (or manage the instance in the provider console)", ref=ref)


def pinned_fingerprint(d) -> str | None:
    return ((getattr(d, "provider_metadata", None) or {}).get("credential_fingerprint")) if d is not None else None


# --------------------------------------------------------------------------
# Customer SSH public keys (0014_limits; methodology/execution-safety.md section 6)
# --------------------------------------------------------------------------
# Only a customer's explicitly supplied PUBLIC key is ever installed on a customer machine. It is validated
# here (type, structure, RSA >= 2048 bits, single line, no authorized_keys options / command prefix) and
# identified by its SHA256 fingerprint, which is what OpenGrid persists in deployments.ssh_key_fingerprint
# and logs. Anything that looks like a PRIVATE key is rejected with an error that never repeats the value.

SSH_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
SSH_MIN_RSA_BITS = 2048
_PRIVATE_MARKERS = ("private key", "begin openssh", "putty-user-key-file", "private-lines", "begin ssh2 encrypted",
                    "begin rsa", "begin dsa", "begin ec ", "begin encrypted")
PRIVATE_KEY_MESSAGE = ("this looks like a PRIVATE key. Never send a private key to OpenGrid: send your PUBLIC key "
                       "(the one-line .pub file starting with ssh-ed25519, ecdsa-sha2-... or ssh-rsa). The value was "
                       "discarded: it was not stored or logged")


class SSHKeyError(ValueError):
    """A rejected public key. The message never contains the submitted value."""


def looks_private(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    low = value.lower()
    return low.lstrip().startswith("-----begin") or any(m in low for m in _PRIVATE_MARKERS)


def _ssh_string(blob: bytes, off: int) -> tuple[bytes, int]:
    import struct
    if off + 4 > len(blob):
        raise SSHKeyError("ssh public key data is truncated")
    (n,) = struct.unpack(">I", blob[off:off + 4])
    off += 4
    if n > len(blob) - off:
        raise SSHKeyError("ssh public key data is truncated")
    return blob[off:off + n], off + n


def parse_public_key(value) -> dict:
    """Validate one OpenSSH public key line. {type, bits, fingerprint ('SHA256:...'), public_key (normalised),
    comment}. Raises SSHKeyError (message safe to return: it never echoes the input)."""
    import base64
    import hashlib
    import re

    if not isinstance(value, str) or not value.strip():
        raise SSHKeyError("ssh_public_key is empty")
    if looks_private(value):
        raise SSHKeyError(PRIVATE_KEY_MESSAGE)
    s = value.strip()
    if "\n" in s or "\r" in s:
        raise SSHKeyError("ssh_public_key must be a single line: '<type> <base64> [comment]'")
    if len(s) > 16384:
        raise SSHKeyError("ssh_public_key is too long")
    parts = s.split(None, 2)
    kt = parts[0]
    if kt not in SSH_KEY_TYPES:
        if any(t in s for t in SSH_KEY_TYPES):
            raise SSHKeyError("authorized_keys options / command prefixes (command=, from=, no-pty, ...) are not "
                              "allowed: send only '<type> <base64> [comment]'")
        raise SSHKeyError("unsupported ssh key type; use one of " + ", ".join(SSH_KEY_TYPES))
    if len(parts) < 2 or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1]):
        raise SSHKeyError("ssh_public_key key data is not valid base64")
    b64 = parts[1]
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:  # noqa: BLE001
        raise SSHKeyError("ssh_public_key key data is not valid base64") from None
    inner, off = _ssh_string(blob, 0)
    if inner != kt.encode():
        raise SSHKeyError("ssh_public_key key data does not match its declared type")
    if kt == "ssh-ed25519":
        pk, off = _ssh_string(blob, off)
        if len(pk) != 32:
            raise SSHKeyError("ssh-ed25519 key data has the wrong length")
        bits = 256
    elif kt == "ssh-rsa":
        _e, off = _ssh_string(blob, off)
        n, off = _ssh_string(blob, off)
        bits = int.from_bytes(n, "big").bit_length()
        if bits < SSH_MIN_RSA_BITS:
            raise SSHKeyError(f"RSA keys must be at least {SSH_MIN_RSA_BITS} bits (this one has {bits}); "
                              "ssh-ed25519 is recommended")
    else:
        curve, off = _ssh_string(blob, off)
        if curve != kt.rsplit("-", 1)[1].encode():
            raise SSHKeyError("ecdsa key data does not match its declared curve")
        _q, off = _ssh_string(blob, off)
        bits = {"nistp256": 256, "nistp384": 384, "nistp521": 521}[kt.rsplit("-", 1)[1]]
    if off != len(blob):
        raise SSHKeyError("ssh_public_key key data has trailing bytes")
    try:  # structural check by the cryptography library too (e.g. an ECDSA point must be on its curve)
        from cryptography.hazmat.primitives.serialization import load_ssh_public_key
        load_ssh_public_key(f"{kt} {b64}".encode())
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        raise SSHKeyError("ssh_public_key is not a valid public key") from None
    comment = parts[2].strip() if len(parts) > 2 else ""
    if len(comment) > 200 or any(ord(ch) < 32 for ch in comment):
        raise SSHKeyError("ssh_public_key comment must be printable and at most 200 characters")
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return {"type": kt, "bits": bits, "fingerprint": fp, "comment": comment,
            "public_key": f"{kt} {b64}" + (f" {comment}" if comment else "")}


def public_key_fingerprint(value) -> str | None:
    """SHA256 fingerprint of a public key, or None when it does not parse (never raises, never logs it)."""
    try:
        return parse_public_key(value)["fingerprint"]
    except SSHKeyError:
        return None
