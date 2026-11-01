"""Google OAuth access tokens from a service-account key (the JWT-bearer grant, RFC 7523), for read-only Compute calls.

    assertion = base64url(header) . base64url(claims) . base64url(RS256 signature with the key's private key)
    POST https://oauth2.googleapis.com/token  grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=...

Header {"alg": "RS256", "typ": "JWT", "kid": <private_key_id>}; claims iss = client_email, scope, aud = the token
URI, iat = now, exp = now + 1 hour (Google's maximum). The response's access_token is a bearer token for an hour.

Signed with `cryptography` (already a direct dependency), so google-auth is not added. The token URI is fixed: a key
whose `token_uri` names any other host is refused, so a signed assertion is never sent anywhere but Google.

Secrets: the key JSON, the private key, the assertion and the access token are never logged, stored or put in an
error. A failed token call records only its HTTP status (the body is not read into any message).
"""

import base64
import json
import time
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

TOKEN_URI = "https://oauth2.googleapis.com/token"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# Read-only Compute for the zone lists. advice.calendarMode is a POST that creates nothing, but Google lists only the
# compute and cloud-platform scopes for it, so advice calls use ADVICE_SCOPE; IAM, not the scope, is what keeps this
# account read-only (roles/compute.viewer + a custom role holding compute.advice.calendarMode; creating a VM is denied).
SCOPE = "https://www.googleapis.com/auth/compute.readonly"
ADVICE_SCOPE = "https://www.googleapis.com/auth/compute"  # the advice methods list no read-only scope
LIFETIME = 3600
REFRESH_MARGIN = 300  # mint a new token when the current one has less than this left


class GcpAuthError(Exception):
    """A key that cannot be used or a token call that failed. The message never contains key material."""


@dataclass(frozen=True)
class ServiceAccount:
    client_email: str
    project_id: str | None
    private_key_id: str | None
    _key: rsa.RSAPrivateKey

    def __repr__(self) -> str:  # never show the key
        return f"ServiceAccount({self.client_email!r})"


def load(key_json: str) -> ServiceAccount:
    """Parse a service-account key file's JSON. Raises GcpAuthError (with no key material) when it is unusable."""
    try:
        info = json.loads(key_json)
    except (TypeError, ValueError):
        raise GcpAuthError("gcp: service-account key is not JSON") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise GcpAuthError("gcp: service-account key is not a service_account key")
    email, pem = info.get("client_email"), info.get("private_key")
    if not isinstance(email, str) or not email or not isinstance(pem, str) or not pem:
        raise GcpAuthError("gcp: service-account key lacks client_email or private_key")
    if info.get("token_uri", TOKEN_URI) != TOKEN_URI:
        raise GcpAuthError("gcp: service-account key names a token_uri other than Google's; refused")
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception:
        raise GcpAuthError("gcp: service-account private key could not be read") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GcpAuthError("gcp: service-account private key is not RSA")
    return ServiceAccount(email, info.get("project_id"), info.get("private_key_id"), key)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def assertion(sa: ServiceAccount, now: int | None = None, scope: str = SCOPE) -> str:
    """The signed JWT the token endpoint exchanges for an access token."""
    now = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT", **({"kid": sa.private_key_id} if sa.private_key_id else {})}
    claims = {"iss": sa.client_email, "scope": scope, "aud": TOKEN_URI, "iat": now, "exp": now + LIFETIME}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    sig = sa._key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(sig)}"


class TokenSource:
    """Mints and caches one access token per service account; refreshes it shortly before it expires."""

    def __init__(self, sa: ServiceAccount, client: httpx.AsyncClient | None = None, scope: str = SCOPE):
        self.sa, self.scope = sa, scope
        self._client = client
        self._token: str | None = None
        self._expires = 0.0

    def invalidate(self) -> None:
        self._token, self._expires = None, 0.0

    async def token(self) -> str:
        if self._token and time.time() < self._expires - REFRESH_MARGIN:
            return self._token
        body = {"grant_type": GRANT, "assertion": assertion(self.sa, scope=self.scope)}
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            resp = await client.post(TOKEN_URI, data=body, follow_redirects=False)
        except Exception as exc:
            raise GcpAuthError(f"gcp: token request failed: {type(exc).__name__}") from None
        finally:
            if self._client is None:
                await client.aclose()
        try:
            payload = resp.json() if resp.is_success else None
        except ValueError:
            payload = None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            why = f"HTTP {resp.status_code}" if not resp.is_success else "no access_token in the response"
            raise GcpAuthError(f"gcp: token request failed: {why}")
        self._token = token
        self._expires = time.time() + float(payload.get("expires_in") or LIFETIME)
        return token
