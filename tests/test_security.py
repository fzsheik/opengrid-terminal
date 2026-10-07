"""Security hardening (audit_security.md): CSRF, stored-XSS links, key/rate-limit escalation, webhook
SSRF, deploy detection, credential encryption, X-Forwarded-For, read scoping, security headers,
login throttling, public rate limits, platform-admin keys.

Run:  .venv/Scripts/python tests/test_security.py
"""

import base64
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
os.environ["POLLER_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from accounts import accounts, credentials, keys, ratelimit, security  # noqa: E402
from alerts import notifier  # noqa: E402
from config import settings  # noqa: E402
from news import parse  # noqa: E402

DB = "og_test_security"
client = TestClient(main.app)  # deliberately WITHOUT the CSRF header
HDR = {"X-OpenGrid-Request": "1"}
_Session = None


def setup(password="pw"):
    global _Session
    if _Session is None:
        _Session = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = _Session
    accounts.reset_cache()
    keys.reset_cache()
    ratelimit.reset()
    security.reset()
    settings.app_password = password
    settings.api_key_pepper = "test-pepper"
    settings.public_pages = False
    settings.trust_proxy_headers = False
    settings.environment = "development"
    return _Session


def basic(user="opengrid", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- SEC-P1-1 CSRF

def test_csrf_basic_auth():
    setup()
    B = basic()
    # the audit's exploit: a cross-site <form> POST, browser attaching cached basic auth
    form = {**B, "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site"}
    for path in ["/v1/admin/keys/999/revoke", "/v1/deployments/dep_x/terminate", "/v1/ops/quarantine/1/accept",
                 "/v1/news/refresh", "/fetch", "/normalize", "/v1/admin/invoices/draft?period=2026-01"]:
        r = client.post(path, headers=form, content="a=b")
        assert r.status_code == 403 and "cross-site" in r.json()["detail"], (path, r.status_code, r.text)
    # JSON without the header (even same-origin) -> 403
    r = client.post("/v1/keys", headers=B, json={"name": "x"})
    assert r.status_code == 403, r.text
    r = client.delete("/v1/keys/1", headers=B)
    assert r.status_code == 403
    # header present but the browser says cross-site -> 403
    r = client.post("/v1/keys", headers={**B, **HDR, "Sec-Fetch-Site": "cross-site"}, json={"name": "x"})
    assert r.status_code == 403
    r = client.post("/v1/keys", headers={**B, **HDR, "Origin": "https://evil.example"}, json={"name": "x"})
    assert r.status_code == 403
    # header + same origin -> passes
    r = client.post("/v1/keys", headers={**B, **HDR, "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
                    json={"name": "ok"})
    assert r.status_code == 201, r.text
    r = client.post("/v1/admin/keys/999999/revoke", headers={**B, **HDR})
    assert r.status_code == 404, "passes CSRF, then the handler answers"
    # GET is never affected
    assert client.get("/v1/me", headers=B).status_code == 200
    # Bearer keys are exempt (not ambient credentials)
    a = accounts.create_account("csrf")
    k = keys.create_key(a["id"], "m", ["data:read", "account:manage"])
    r = client.post("/v1/keys", headers={**bearer(k["secret"]), "Origin": "https://evil.example"}, json={"name": "b"})
    assert r.status_code == 201, r.text
    # open local dev (no password) is ambient too
    settings.app_password = None
    try:
        assert client.post("/normalize").status_code == 403
        assert client.post("/v1/keys", json={"name": "x"}).status_code == 403
        assert client.post("/v1/keys", json={"name": "dev"}, headers=HDR).status_code == 201
    finally:
        settings.app_password = "pw"


# ---------------------------------------------------------------- SEC-P1-2 stored XSS

def test_feed_links_sanitized():
    xml = '<rss><channel><item><title>t</title><link>javascript:fetch("/v1/admin/accounts")</link></item>' \
          '<item><title>u</title><link> https://ok.example/a </link></item>' \
          '<item><title>v</title><link>JaVa&#9;ScRiPt:alert(1)</link></item></channel></rss>'
    got = [i["url"] for i in parse.parse_xml(xml)]
    assert got == [None, "https://ok.example/a", None], got
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>a</title><link href="data:text/html,x"/></entry></feed>'
    assert parse.parse_xml(atom)[0]["url"] is None
    feed = '{"version":"https://jsonfeed.org/version/1.1","items":[{"id":"1","title":"x","url":"vbscript:x"}]}'
    assert parse.parse_json_feed(feed)[0]["url"] is None
    for bad in ["javascript:x", "java\nscript:x", "\x01javascript:x", "//evil", "/relative", "https:x", None, 5]:
        assert parse.safe_url(bad) is None, bad


def test_web_client_url_guard():
    """web/core.js: OG.safeUrl (used by OG.h / OG.s for href/src/action, OG.go and row clicks)."""
    import subprocess

    js = ("const OG=require('./web/core.js');const c=['javascript:alert(1)',' JaVa\\tscript:x','data:text/html,x',"
          "'vbscript:x','\\u0001javascript:x','https://ok/a','/gpu/x','#a','?q=1'];"
          "process.stdout.write(JSON.stringify(c.map(OG.safeUrl)))")
    out = subprocess.run(["node", "-e", js], cwd=str(Path(__file__).resolve().parent.parent), capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json

    assert json.loads(out.stdout) == ["#", "#", "#", "#", "#", "https://ok/a", "/gpu/x", "#a", "?q=1"], out.stdout
    src = (Path(__file__).resolve().parent.parent / "web" / "pages" / "news.js").read_text(encoding="utf-8")
    assert "href: n.url" not in src and "href: s.url" not in src and "safeHref(n.url)" in src
    core = (Path(__file__).resolve().parent.parent / "web" / "core.js").read_text(encoding="utf-8")
    assert '"x-opengrid-request"' in core, "OG.api sends the CSRF header"


# ---------------------------------------------------------------- SEC-P1-3 keys and rate limits

def test_key_escalation_blocked():
    setup()
    a = accounts.create_account("tenant-a")
    k = keys.create_key(a["id"], "mgr", ["data:read", "account:manage"], rate_limit_per_minute=5,
                        expires_at=datetime.now(timezone.utc) + timedelta(days=2))
    H = bearer(k["secret"])
    r = client.post("/v1/keys", headers=H, json={"name": "big", "scopes": ["data:read"], "rate_limit_per_minute": 100000})
    assert r.status_code == 403 and "rate limit" in r.json()["detail"], r.text
    r = client.post("/v1/keys", headers=H, json={"scopes": ["route:execute"]})
    assert r.status_code == 403
    r = client.post("/v1/keys", headers=H, json={"name": "kid", "scopes": ["data:read"], "expires_in_days": 365})
    assert r.status_code == 201, r.text
    kid = r.json()["data"]
    assert kid["rate_limit_per_minute"] == 5, "defaults to the creator's own limit"
    assert kid["parent_key_id"] == k["id"]
    assert datetime.fromisoformat(kid["expires_at"]) <= datetime.fromisoformat(k["expires_at"].isoformat()), \
        "expiry capped to the creator's"
    # a grandchild, then revoke the root: everything below is revoked (cascade)
    g = client.post("/v1/keys", headers=H, json={"name": "kid2", "scopes": ["data:read", "account:manage"]}).json()["data"]
    gg = client.post("/v1/keys", headers=bearer(g["secret"]), json={"name": "gg", "scopes": ["data:read"]})
    assert gg.status_code == 201, gg.text
    out = keys.revoke_key(k["id"], a["id"])
    assert set(out["cascaded"]) == {kid["id"], g["id"], gg.json()["data"]["id"]}, out
    for s in (kid["secret"], gg.json()["data"]["secret"]):
        try:
            keys.verify(s, None)
            raise AssertionError("child key survived its parent's revocation")
        except HTTPException as e:
            assert e.status_code == 401
    # platform_admin cannot be minted by a key
    m = keys.create_key(a["id"], "m2", ["data:read", "account:manage"])
    assert client.post("/v1/keys", headers=bearer(m["secret"]), json={"platform_admin": True}).status_code == 403


def test_key_cap():
    setup()
    old = settings.max_keys_per_account
    settings.max_keys_per_account = 3
    try:
        a = accounts.create_account("cap")
        m = keys.create_key(a["id"], "m", ["data:read", "account:manage"])
        H = bearer(m["secret"])
        assert client.post("/v1/keys", headers=H, json={"name": "1"}).status_code == 201
        assert client.post("/v1/keys", headers=H, json={"name": "2"}).status_code == 201
        r = client.post("/v1/keys", headers=H, json={"name": "3"})
        assert r.status_code == 409 and "active keys" in r.json()["detail"], r.text
        try:
            keys.create_key(a["id"], "cli")
            raise AssertionError("cap bypassed through create_key")
        except keys.KeyLimitError:
            pass
    finally:
        settings.max_keys_per_account = old


def test_account_bucket():
    """N keys do not give N x the budget: every key also spends from its account's bucket."""
    setup()
    old = settings.account_rate_limit_read_per_minute
    settings.account_rate_limit_read_per_minute = 10
    t = [1000.0]
    ratelimit.clock = lambda: t[0]
    try:
        a = accounts.create_account("bucket")
        ks = [keys.create_key(a["id"], f"k{i}", ["data:read"], rate_limit_per_minute=100) for i in range(3)]
        codes = [client.get("/v1/me", headers=bearer(ks[i % 3]["secret"])).status_code for i in range(15)]
        assert codes.count(200) == 10 and codes.count(429) == 5, codes
        # a per-account override set by the operator
        with _Session.begin() as s:
            from store.accounts import Account
            s.get(Account, a["id"]).settings = {"rate_limit_account": {"read": 12}}
        client.get("/v1/me", headers=bearer(ks[0]["secret"]))  # picks up the new capacity
        t[0] += 60  # full refill at the new capacity
        codes = [client.get("/v1/me", headers=bearer(ks[i % 3]["secret"])).status_code for i in range(15)]
        assert codes.count(200) == 12, codes
        # a bucket whose limit changes keeps what was already spent (no fresh bucket by switching limits)
        ratelimit.reset()
        for _ in range(5):
            assert ratelimit.take(("x",), "read", 5).allowed
        assert not ratelimit.take(("x",), "read", 100000).allowed
    finally:
        ratelimit.clock = __import__("time").monotonic
        settings.account_rate_limit_read_per_minute = old


# ---------------------------------------------------------------- SEC-P1-5 webhook SSRF

def test_webhook_address_checks():
    for a in ["100.64.0.1", "100.100.100.200", "10.0.0.1", "127.0.0.1", "169.254.169.254", "224.0.0.1",
              "::ffff:127.0.0.1", "fd00::1", "fe80::1", "2002:7f00:1::", "0.0.0.0", "240.0.0.1", "::1",
              "64:ff9b::a00:1", "192.168.1.1", "172.16.0.1"]:
        assert notifier.blocked_ip(a), a
    for a in ["8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8"]:
        assert not notifier.blocked_ip(a), a


def test_webhook_pinned_and_generic_status():
    real = notifier.getaddrinfo
    seen = []
    answers = {"n": 0}

    def rebinding(host, port):
        # first answer public, every later one private: a TOCTOU resolver would connect to 127.0.0.1
        answers["n"] += 1
        ip = "93.184.215.14" if answers["n"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    def handler(request):
        seen.append(request)
        return httpx.Response(404, text="internal secret body")

    notifier.getaddrinfo = rebinding
    notifier.http_client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    try:
        via, status = notifier.deliver([{"type": "webhook", "url": "https://hooks.example.com/in"}], {"x": 1}, "whsec_x")
        assert len(seen) == 1, "one resolution, one connection"
        req = seen[0]
        assert req.url.host == "93.184.215.14", "connected to the validated address, not a second lookup"
        assert req.headers["host"] == "hooks.example.com" and req.extensions.get("sni_hostname") == "hooks.example.com"
        assert status["webhook"] == "delivery_failed" and via == ["in_app"], status  # no 'http 404' oracle
        # second delivery: resolver now says 127.0.0.1 -> refused before any connection, same generic status
        via, status = notifier.deliver([{"type": "webhook", "url": "https://hooks.example.com/in"}], {"x": 1}, "whsec_x")
        assert len(seen) == 1 and status["webhook"] == "delivery_failed"
        # network errors look the same
        notifier.getaddrinfo = lambda h, p: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", p))]

        def boom(request):
            raise httpx.ConnectError("refused")

        notifier.http_client = lambda: httpx.Client(transport=httpx.MockTransport(boom))
        _, status = notifier.deliver([{"type": "webhook", "url": "https://hooks.example.com/in"}], {}, "whsec_x")
        assert status["webhook"] == "delivery_failed"
        notifier.http_client = lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(204)))
        via, status = notifier.deliver([{"type": "webhook", "url": "https://hooks.example.com/in"}], {}, "whsec_x")
        assert status["webhook"] == "ok" and via == ["in_app", "webhook"]
        # CGNAT and IPv4-mapped loopback refused at rule creation
        for host_ip in ["100.64.0.1", "::ffff:127.0.0.1"]:
            notifier.getaddrinfo = lambda h, p, ip=host_ip: [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                                                              socket.SOCK_STREAM, 6, "", (ip, p))]
            try:
                notifier.check_url("https://cgnat.example/x")
                raise AssertionError(f"{host_ip} accepted")
            except ValueError:
                pass
    finally:
        notifier.getaddrinfo = real
        notifier.http_client = lambda: httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=False,
                                                    trust_env=False)


# ---------------------------------------------------------------- P2s

def test_deployed_detection():
    setup()
    url = settings.database_url
    try:
        assert not security.deployed()
        settings.environment = "production"
        assert security.deployed()
        settings.environment = "staging"
        assert security.deployed(), "an unknown environment fails closed"
        settings.environment = "development"
        settings.database_url = "postgresql+psycopg://db.internal:5432/x"
        assert security.deployed() and keys.deployed()
        settings.database_url = url
        os.environ["RAILWAY_ENVIRONMENT"] = "production"
        assert security.deployed()
    finally:
        os.environ.pop("RAILWAY_ENVIRONMENT", None)
        settings.database_url = url
        settings.environment = "development"
    # deployed without a password: closed, not open
    settings.app_password = None
    settings.environment = "production"
    try:
        assert client.get("/v1/me").status_code in (401, 503)
        assert client.get("/").status_code == 503
    finally:
        settings.environment = "development"
        settings.app_password = "pw"


def test_credentials_encryption():
    S = setup()
    k_old, k_new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    saved = (settings.credentials_encryption_key, settings.credentials_encryption_keys_old)
    try:
        settings.environment = "production"
        settings.credentials_encryption_key = "a passphrase"
        try:
            credentials.encrypt("x")
            raise AssertionError("passphrase accepted when deployed")
        except HTTPException as e:
            assert e.status_code == 503
        try:
            credentials.check_config()
            raise AssertionError("check_config accepted a passphrase")
        except RuntimeError:
            pass
        settings.environment = "development"
        settings.credentials_encryption_key = k_old
        a = accounts.create_account("byo")
        c = credentials.add(a["id"], "runpod", "SECRET-RUNPOD-KEY-1234")
        # rotate: new primary, old still decrypts
        settings.credentials_encryption_key, settings.credentials_encryption_keys_old = k_new, k_old
        assert credentials.resolve(a["id"], "runpod") == ("byo", "SECRET-RUNPOD-KEY-1234")
        out = credentials.rotate()
        assert out["provider_credentials"] >= 1 and not out["undecryptable"], out
        settings.credentials_encryption_keys_old = None
        assert credentials.resolve(a["id"], "runpod") == ("byo", "SECRET-RUNPOD-KEY-1234"), "re-encrypted under new"
        # a BYO row that does not decrypt fails closed: never OpenGrid's managed key instead
        settings.credentials_encryption_key = Fernet.generate_key().decode()
        settings.runpod_api_key = "MANAGED"
        try:
            credentials.resolve(a["id"], "runpod")
            raise AssertionError("fell back to managed credentials")
        except credentials.CredentialUnavailable:
            pass
        assert c["id"]
    finally:
        settings.credentials_encryption_key, settings.credentials_encryption_keys_old = saved
        settings.runpod_api_key = None
        settings.environment = "development"


def test_forwarded_for():
    class R:
        def __init__(self, xff, peer="10.9.8.7"):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": peer})()

    settings.trust_proxy_headers = False
    assert security.client_ip(R("6.6.6.6, 10.0.0.1")) == "10.9.8.7", "XFF ignored unless trusted"
    settings.trust_proxy_headers = True
    try:
        assert security.client_ip(R("6.6.6.6, 203.0.113.9")) == "203.0.113.9", "right-most hop"
        settings.trusted_proxy_count = 2
        assert security.client_ip(R("6.6.6.6, 203.0.113.9, 10.0.0.2")) == "203.0.113.9"
        assert security.client_ip(R("garbage")) == "10.9.8.7"
    finally:
        settings.trust_proxy_headers = False
        settings.trusted_proxy_count = 1
    # through the app: the audit's spoof no longer lands in last_used_ip
    setup()
    a = accounts.create_account("xff")
    k = keys.create_key(a["id"], "x", ["data:read"])
    client.get("/v1/me", headers={**bearer(k["secret"]), "X-Forwarded-For": "6.6.6.6, 10.0.0.1"})
    assert keys.get_key(k["id"])["last_used_ip"] != "6.6.6.6"


def test_keys_listing_scope():
    setup()
    a = accounts.create_account("scoped")
    ro = keys.create_key(a["id"], "ro", ["data:read"])
    kr = keys.create_key(a["id"], "kr", ["keys:read"])
    assert client.get("/v1/keys", headers=bearer(ro["secret"])).status_code == 403
    r = client.get("/v1/keys", headers=bearer(kr["secret"]))
    assert r.status_code == 200 and len(r.json()["data"]) == 2


def test_security_headers():
    setup()
    r = client.get("/", headers=basic())
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "'unsafe-inline'" not in csp.split(";")[1]
    assert "'sha256-" in csp, "the page's own inline boot script is allowed by hash"
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    j = client.get("/v1/me", headers=basic())
    assert j.headers["x-content-type-options"] == "nosniff" and "content-security-policy" not in j.headers
    # the hash matches exactly the inline script in the page and nothing else
    import re

    body = r.text
    inline = [m for m in re.findall(r"<script>(.*?)</script>", body, re.S)]
    assert inline and all(security.inline_script_hashes(f"<script>{s}</script>")[0] in csp for s in inline)


def test_login_throttle():
    setup()
    for _ in range(settings.login_max_failures):
        assert client.get("/v1/me", headers=basic(password="wrong")).status_code == 401
    r = client.get("/v1/me", headers=basic())
    assert r.status_code == 429 and "Retry-After" in r.headers, "locked even with the right password"
    t = [security.clock() + settings.login_failure_window_seconds + 1]
    security.clock = lambda: t[0]
    try:
        assert client.get("/v1/me", headers=basic()).status_code == 200, "lock expires"
    finally:
        security.clock = __import__("time").monotonic
        security.reset()


def test_public_rate_limit():
    setup()
    settings.public_pages = True
    old = settings.public_rate_limit_per_minute
    settings.public_rate_limit_per_minute = 5
    try:
        codes = [client.get("/robots.txt").status_code for _ in range(8)]
        assert codes.count(200) == 5 and codes[-1] == 429, codes
        assert client.get("/static/core.js").status_code == 200, "static assets are not counted"
        assert client.get("/robots.txt", headers=basic()).status_code == 200, "the operator is not limited"
    finally:
        settings.public_pages = False
        settings.public_rate_limit_per_minute = old


def test_platform_admin():
    setup()
    a = accounts.create_account("adm")
    plain = keys.create_key(a["id"], "admin-no-flag", ["admin", "data:read"])
    r = client.get("/v1/admin/accounts", headers=bearer(plain["secret"]))
    assert r.status_code == 403, "an API key's admin scope needs platform_admin"
    r = client.post(f"/v1/admin/accounts/{a['id']}/keys", headers={**basic(), **HDR},
                    json={"name": "pa", "scopes": ["admin"], "platform_admin": True})
    assert r.status_code == 201 and r.json()["data"]["platform_admin"] is True, r.text
    pa = r.json()["data"]
    assert client.get("/v1/admin/accounts", headers=bearer(pa["secret"])).status_code == 200
    # a platform admin key cannot mint another platform admin key
    r = client.post(f"/v1/admin/accounts/{a['id']}/keys", headers=bearer(pa["secret"]),
                    json={"scopes": ["admin"], "platform_admin": True})
    assert r.status_code == 403
    with _Session() as s:
        from sqlalchemy import text

        kinds = s.execute(text("SELECT kind FROM security_events WHERE key_id = :k"), {"k": pa["id"]}).scalars().all()
    assert "key_created" in kinds, "admin key creation is audited"


if __name__ == "__main__":
    try:
        for t in (test_csrf_basic_auth, test_feed_links_sanitized, test_web_client_url_guard, test_key_escalation_blocked,
                  test_key_cap, test_account_bucket, test_webhook_address_checks, test_webhook_pinned_and_generic_status,
                  test_deployed_detection, test_credentials_encryption, test_forwarded_for, test_keys_listing_scope,
                  test_security_headers, test_login_throttle, test_public_rate_limit, test_platform_admin):
            t(); print(t.__name__, "ok")
    finally:
        settings.app_password = None
        scratchdb.drop(DB)
