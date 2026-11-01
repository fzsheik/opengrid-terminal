"""AWS Signature Version 4, header form, for the handful of read-only EC2 Query API calls in providers/aws.py.

A small signer instead of botocore/boto3 (neither is a dependency): one canonical request, one string to sign,
one derived key (docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html). Only what
the EC2 Query API needs: POST of a form body (or GET with a query), no chunked or unsigned payloads.

    headers = sign("POST", url, region, "ec2", access_key_id, secret_access_key, body=b"Action=...", headers={...})

The returned headers carry X-Amz-Date, X-Amz-Content-Sha256 (only when asked) and Authorization. Keys are
never logged, stored or put into an error; tests pin AWS's own published example vectors.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone

ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret_access_key: str, date: str, region: str, service: str) -> bytes:
    """kSigning = HMAC(HMAC(HMAC(HMAC("AWS4" + secret, date), region), service), "aws4_request")."""
    k = _hmac(("AWS4" + secret_access_key).encode(), date)
    return _hmac(_hmac(_hmac(k, region), service), "aws4_request")


def _quote(s: str) -> str:
    # RFC 3986 unreserved characters stay; everything else is percent-encoded (uppercase hex).
    return urllib.parse.quote(s, safe="-_.~")


def canonical_query(query: str) -> str:
    """Parameters sorted by encoded name then value, each name and value URI-encoded once."""
    pairs = [(_quote(urllib.parse.unquote_plus(k)), _quote(urllib.parse.unquote_plus(v)))
             for k, _, v in (p.partition("=") for p in query.split("&") if p)]
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def canonical_request(method: str, url: str, headers: dict[str, str], payload_hash: str) -> tuple[str, str]:
    """(canonical request, signed header list). `headers` must include host."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path or "/", safe="/-_.~")
    canon = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(canon)
    signed = ";".join(names)
    text = "\n".join([method.upper(), path, canonical_query(parts.query),
                      "".join(f"{n}:{canon[n]}\n" for n in names), signed, payload_hash])
    return text, signed


def sign(method: str, url: str, region: str, service: str, access_key_id: str, secret_access_key: str, *,
         body: bytes = b"", headers: dict[str, str] | None = None, now: datetime | None = None,
         content_sha256_header: bool = False) -> dict[str, str]:
    """The headers to add to one request (everything in `headers` is signed, plus host and x-amz-date)."""
    t = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date, date = t.strftime("%Y%m%dT%H%M%SZ"), t.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    out = {"X-Amz-Date": amz_date}
    if content_sha256_header:
        out["X-Amz-Content-Sha256"] = payload_hash
    to_sign = {"host": urllib.parse.urlsplit(url).netloc, **(headers or {}), **out}
    creq, signed = canonical_request(method, url, to_sign, payload_hash)
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(creq.encode()).hexdigest()])
    sig = hmac.new(signing_key(secret_access_key, date, region, service), string_to_sign.encode(),
                   hashlib.sha256).hexdigest()
    out["Authorization"] = f"{ALGORITHM} Credential={access_key_id}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out
