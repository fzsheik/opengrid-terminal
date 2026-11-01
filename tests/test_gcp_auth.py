"""Google service-account OAuth (providers/gcp_auth.py): the JWT-bearer grant, with a throwaway key made here.

No network: the token endpoint is an httpx MockTransport. The key is generated per run, so nothing secret is in the
repository.

Run:  .venv/bin/python tests/test_gcp_auth.py
"""

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from providers import gcp_auth  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
EMAIL = "reader@example-project.iam.gserviceaccount.com"


def key_json(**over) -> str:
    info = {"type": "service_account", "project_id": "example-project", "private_key_id": "kid-123",
            "private_key": PEM, "client_email": EMAIL, "token_uri": "https://oauth2.googleapis.com/token", **over}
    return json.dumps(info)


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def raises(fn, *a) -> str | None:
    try:
        fn(*a)
    except gcp_auth.GcpAuthError as exc:
        return str(exc)
    return None


def test_load():
    sa = gcp_auth.load(key_json())
    assert sa.client_email == EMAIL and sa.project_id == "example-project" and sa.private_key_id == "kid-123"
    assert "PRIVATE" not in repr(sa) and EMAIL in repr(sa), "repr never shows the key"
    for bad in ("not json", json.dumps({"type": "authorized_user"}), key_json(private_key=""),
                key_json(private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"),
                key_json(token_uri="https://evil.example/token")):
        msg = raises(gcp_auth.load, bad)
        assert msg and "BEGIN" not in msg and "AAAA" not in msg, (bad[:30], msg)
    assert "token_uri" in raises(gcp_auth.load, key_json(token_uri="https://evil.example/token")), \
        "a signed assertion is never sent to a host other than Google's"


def test_assertion_is_a_valid_rs256_jwt():
    sa = gcp_auth.load(key_json())
    jwt = gcp_auth.assertion(sa, now=1_800_000_000)
    h, c, sig = jwt.split(".")
    header, claims = json.loads(_unb64(h)), json.loads(_unb64(c))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid-123"}
    assert claims == {"iss": EMAIL, "scope": gcp_auth.SCOPE, "aud": gcp_auth.TOKEN_URI,
                      "iat": 1_800_000_000, "exp": 1_800_000_000 + 3600}
    # verify() raises on a bad signature
    KEY.public_key().verify(_unb64(sig), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(_unb64(gcp_auth.assertion(sa, scope=gcp_auth.ADVICE_SCOPE).split(".")[1]))["scope"] \
        == "https://www.googleapis.com/auth/compute"


def _token_server(status=200, body=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) != gcp_auth.TOKEN_URI:
            return httpx.Response(404)
        return httpx.Response(status, json=body if body is not None else
                              {"access_token": f"tok-{len(seen)}", "expires_in": 3599, "token_type": "Bearer"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def test_token_minted_and_cached():
    client, seen = _token_server()
    ts = gcp_auth.TokenSource(gcp_auth.load(key_json()), client)

    async def run():
        return await ts.token(), await ts.token()
    a, b = asyncio.run(run())
    assert a == b == "tok-1" and len(seen) == 1, "one token call; the second use is cached"
    form = parse_qs(seen[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"] and len(form["assertion"]) == 1
    assert seen[0].method == "POST" and seen[0].url.host == "oauth2.googleapis.com"
    ts._expires = time.time() + 10  # near expiry: refreshed
    assert asyncio.run(ts.token()) == "tok-2" and len(seen) == 2
    ts.invalidate()
    assert asyncio.run(ts.token()) == "tok-3"


def test_token_failure_has_no_secrets():
    for status, body in ((401, {"error": "invalid_grant", "error_description": "assertion: eyJ..."}),
                         (200, {"no": "token"})):
        client, _ = _token_server(status, body)
        ts = gcp_auth.TokenSource(gcp_auth.load(key_json()), client)
        try:
            asyncio.run(ts.token())
            raise AssertionError("should fail")
        except gcp_auth.GcpAuthError as exc:
            msg = str(exc)
        assert msg.startswith("gcp: token request failed") and "eyJ" not in msg and "BEGIN" not in msg, msg
        assert ("HTTP 401" in msg) if status == 401 else ("no access_token" in msg)


if __name__ == "__main__":
    for t in (test_load, test_assertion_is_a_valid_rs256_jwt, test_token_minted_and_cached,
              test_token_failure_has_no_secrets):
        t()
        print(t.__name__, "ok")
    print("all passed")
