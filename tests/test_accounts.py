"""Accounts: API keys (format, hash-only storage, verify, revoke, expiry, scopes), rate limits,
usage logging, BYO credentials, the operator account, and the /v1 endpoints through the app.

Run:  .venv/Scripts/python tests/test_accounts.py
"""

import base64
import hashlib
import hmac
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from accounts import accounts, credentials, keys, ratelimit, usage  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_accounts"
client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})  # CSRF header, as web/core.js sends
TABLES = ("accounts", "api_keys", "api_key_usage", "provider_credentials", "usage_records", "fee_policies",
          "charges", "credits", "invoices", "watchlists", "watchlist_items", "alert_rules", "alert_firings")
_Session = None


def setup():
    global _Session
    if _Session is None:
        _Session = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = _Session
    accounts.reset_cache()
    keys.reset_cache()
    ratelimit.reset()
    settings.app_password = None
    settings.api_key_pepper = "test-pepper"
    return _Session


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def all_db_text(Session) -> str:
    """Every row of every accounts table as text: what an attacker with the database would see."""
    with Session() as s:
        return "\n".join(r[0] for t in TABLES for r in s.execute(text(f"SELECT row_to_json(x)::text FROM {t} x")))


class Req:
    """Enough of a Request for keys.verify."""

    class _S:
        pass

    def __init__(self, method="GET", path="/v1/me"):
        self.method, self.headers, self.client = method, {}, None
        self.url = type("U", (), {"path": path})()
        self.state = Req._S()


def test_key_format():
    seen = set()
    for _ in range(200):
        k = keys.generate()
        assert k.startswith("opg_live_") and len(k) == len("opg_live_") + 43, k
        assert all(c.isalnum() or c in "-_" for c in k[9:])
        seen.add(k)
    assert len(seen) == 200
    # 32 random bytes = 256 bits: the url-safe body decodes back to 32 bytes.
    body = keys.generate()[9:]
    assert len(base64.urlsafe_b64decode(body + "=")) == 32


def test_hash_only_storage():
    S = setup()
    a = accounts.create_account("acme", "ops@acme.test")
    k = keys.create_key(a["id"], "ci", ["data:read"])
    secret = k["secret"]
    assert k["prefix"] == secret[:13] and k["prefix"].startswith("opg_live_")
    dump = all_db_text(S)
    assert secret not in dump and secret[13:] not in dump, "the key must not be stored"
    with S() as s:
        stored = s.execute(text("SELECT secret_hash FROM api_keys WHERE id = :i"), {"i": k["id"]}).scalar()
    assert stored == hmac.new(b"test-pepper", secret.encode(), hashlib.sha256).hexdigest()
    assert "secret" not in keys.get_key(k["id"]) and "secret_hash" not in keys.get_key(k["id"])
    settings.api_key_pepper = "other-pepper"
    try:
        keys.verify(secret, Req())
        raise AssertionError("a different pepper must not verify")
    except HTTPException as e:
        assert e.status_code == 401
    finally:
        settings.api_key_pepper = "test-pepper"


def _status(token, req=None):
    try:
        keys.verify(token, req or Req())
        return 200
    except HTTPException as e:
        return e.status_code


def test_verify_states():
    setup()
    a = accounts.create_account("verify-co")
    ok = keys.create_key(a["id"], "ok", ["data:read", "route:preview"])
    who = keys.verify(ok["secret"], Req())
    assert who.kind == "api_key" and who.account_id == a["id"] and who.key_id == ok["id"]
    assert who.scopes == frozenset({"data:read", "route:preview"}) and not who.has("admin")
    assert _status("opg_live_" + "x" * 43) == 401, "unknown"
    assert _status("not-a-key") == 401, "malformed"
    rev = keys.create_key(a["id"], "rev")
    keys.revoke_key(rev["id"])
    assert _status(rev["secret"]) == 401, "revoked"
    exp = keys.create_key(a["id"], "exp", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert _status(exp["secret"]) == 401, "expired"
    fut = keys.create_key(a["id"], "fut", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert _status(fut["secret"]) == 200
    accounts.set_status(a["id"], "suspended")
    assert _status(ok["secret"]) == 403, "suspended account"
    accounts.set_status(a["id"], "active")
    try:
        keys.create_key(a["id"], "bad", ["nope"])
        raise AssertionError("unknown scope accepted")
    except ValueError:
        pass


def test_last_used():
    S = setup()
    a = accounts.create_account("lu")
    k = keys.create_key(a["id"], "lu")
    assert keys.get_key(k["id"])["last_used_at"] is None
    r = Req()
    # SEC-P2-3: only the hop our trusted proxy appended (right-most) counts; the left part is client-controlled.
    r.headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.9"}
    settings.trust_proxy_headers = True
    try:
        keys.verify(k["secret"], r)
    finally:
        settings.trust_proxy_headers = False
    got = keys.get_key(k["id"])
    assert got["last_used_at"] is not None and got["last_used_ip"] == "203.0.113.9"


def test_rate_limit_and_usage_through_app():
    S = setup()
    usage.flush()
    a = accounts.create_account("limited")
    k = keys.create_key(a["id"], "small", ["data:read"], rate_limit_per_minute=3)
    t = [1000.0]
    ratelimit.clock = lambda: t[0]
    try:
        codes = []
        for _ in range(4):
            r = client.get("/v1/me", headers=bearer(k["secret"]))
            codes.append(r.status_code)
        assert codes == [200, 200, 200, 429], codes
        assert r.headers["retry-after"] == "20", r.headers  # 3/min: one token every 20 s
        assert r.headers["x-ratelimit-limit"] == "3" and r.headers["x-ratelimit-remaining"] == "0"
        t[0] += 21
        ok = client.get("/v1/me", headers=bearer(k["secret"]))
        assert ok.status_code == 200 and ok.headers["x-ratelimit-limit"] == "3"
        assert ok.headers["x-ratelimit-remaining"] == "0"
        assert ok.json()["data"]["rate_limit"]["read"]["limit_per_minute"] == 3
        # Writes have their own (smaller) budget, capped by the override.
        assert ratelimit.limit_for("write", 3) == 3 and ratelimit.limit_for("execute", None) == settings.rate_limit_execute_per_minute
    finally:
        ratelimit.clock = __import__("time").monotonic
    assert usage.pending() == 5
    usage.flush()
    with S() as s:
        rows = s.execute(text("SELECT status, method, path, duration_ms FROM api_key_usage WHERE key_id = :k ORDER BY id"),
                         {"k": k["id"]}).all()
    assert [r.status for r in rows] == [200, 200, 200, 429, 200], rows
    assert all(r.path == "/v1/me" and r.method == "GET" and r.duration_ms is not None for r in rows)
    summ = client.get("/v1/usage", headers=bearer(k["secret"]))  # this request is itself the 6th
    assert summ.status_code == 200
    d = summ.json()["data"]
    assert d["requests"] == 5 and d["rate_limited"] == 1, d
    # The operator is never logged.
    before = usage.pending()
    client.get("/v1/me")
    assert usage.pending() == before


def test_byo_credentials():
    S = setup()
    a = accounts.create_account("byo")
    secret = "sk-live-THIS-IS-SECRET-1234"
    c = credentials.add(a["id"], "Lambda", secret, "my lambda")
    assert c["provider"] == "lambda" and c["hint"] == "…1234" and "secret" not in c
    with S() as s:
        blob = s.execute(text("SELECT secret_encrypted FROM provider_credentials WHERE id = :i"), {"i": c["id"]}).scalar()
    assert secret.encode() not in bytes(blob) and credentials.decrypt(blob) == secret, "Fernet round trip"
    assert secret not in all_db_text(S)
    assert credentials.resolve(a["id"], "lambda") == ("byo", secret)
    settings.runpod_api_key = "platform-runpod-key"
    try:
        assert credentials.resolve(a["id"], "runpod") == ("opengrid", "platform-runpod-key"), "managed default"
    finally:
        settings.runpod_api_key = None
    assert credentials.resolve(a["id"], "nebius") is None, "no credential anywhere"
    # Replacing revokes the old one; revoking falls back to managed / none.
    c2 = credentials.add(a["id"], "lambda", "sk-second-key-5678")
    assert [x["id"] for x in credentials.list_for(a["id"])] == [c2["id"]]
    credentials.revoke(a["id"], c2["id"])
    assert credentials.resolve(a["id"], "lambda") is None
    # Through the API: never the secret.
    k = keys.create_key(a["id"], "mgr", ["account:manage"])
    r = client.post("/v1/credentials", json={"provider": "vast", "secret": "vast-SECRET-abcdefgh"}, headers=bearer(k["secret"]))
    assert r.status_code == 201 and "vast-SECRET" not in r.text, r.text
    r = client.get("/v1/credentials", headers=bearer(k["secret"]))
    assert r.status_code == 200 and "vast-SECRET" not in r.text and secret not in r.text
    assert [x["provider"] for x in r.json()["data"]["byo"]] == ["vast"]
    ro = keys.create_key(a["id"], "ro", ["data:read"])
    assert client.get("/v1/credentials", headers=bearer(ro["secret"])).status_code == 403


def test_operator_account():
    setup()
    r = client.get("/v1/me")  # APP_PASSWORD unset: the operator
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["principal"] == "operator" and d["account"]["is_operator"] and d["scopes"] == ["*"]
    first = d["account"]["id"]
    accounts.reset_cache()
    assert accounts.operator_account_id() == first, "found again, not created twice"
    assert sum(1 for x in accounts.list_accounts() if x["is_operator"]) == 1


def test_endpoints():
    setup()
    a = accounts.create_account("endpoints")
    mgr = keys.create_key(a["id"], "mgr", ["account:manage", "data:read"])
    h = bearer(mgr["secret"])
    me = client.get("/v1/me", headers=h).json()["data"]
    assert me["account"]["id"] == a["id"] and me["key"]["id"] == mgr["id"] and "secret" not in me["key"]
    r = client.post("/v1/keys", json={"name": "child", "scopes": ["data:read"]}, headers=h)
    assert r.status_code == 201 and r.json()["data"]["secret"].startswith("opg_live_")
    child = r.json()["data"]
    listed = client.get("/v1/keys", headers=h)
    assert child["secret"] not in listed.text and {x["id"] for x in listed.json()["data"]} >= {mgr["id"], child["id"]}
    assert client.post("/v1/keys", json={"scopes": ["admin"]}, headers=h).status_code == 403, "no escalation"
    assert client.post("/v1/keys", json={"scopes": ["data:read"]}, headers=bearer(child["secret"])).status_code == 403
    assert client.get("/v1/me", headers=bearer(child["secret"])).status_code == 200
    assert client.delete(f"/v1/keys/{child['id']}", headers=h).status_code == 200
    assert client.get("/v1/me", headers=bearer(child["secret"])).status_code == 401, "revoked immediately"
    other = accounts.create_account("other")
    ok = keys.create_key(other["id"], "x")
    assert client.delete(f"/v1/keys/{ok['id']}", headers=h).status_code == 404, "cannot touch another account's key"
    # Admin: the operator yes, a non-admin key no.
    assert client.get("/v1/admin/accounts", headers=h).status_code == 403
    r = client.post("/v1/admin/accounts", json={"name": "newco", "email": "a@b.test"})
    assert r.status_code == 201
    new_id = r.json()["data"]["id"]
    r = client.post(f"/v1/admin/accounts/{new_id}/keys", json={"name": "k", "scopes": ["data:read", "admin"],
                                                                    "platform_admin": True})  # SEC-P2-8: flag required
    assert r.status_code == 201
    adm = r.json()["data"]
    assert client.get("/v1/admin/accounts", headers=bearer(adm["secret"])).status_code == 200, "admin-scoped key"
    assert client.post(f"/v1/admin/keys/{adm['id']}/revoke").json()["data"]["state"] == "revoked"
    r = client.get("/v1/admin/usage")
    assert r.status_code == 200 and r.json()["data"]["requests"] >= 1


def test_bearer_bypasses_site_password():
    setup()
    a = accounts.create_account("gate")
    k = keys.create_key(a["id"], "gate")
    settings.app_user, settings.app_password = "opengrid", "s3cret-pass"
    try:
        assert client.get("/v1/me", headers=bearer(k["secret"])).status_code == 200, "a valid key needs no site password"
        r = client.get("/v1/me", headers=bearer("opg_live_" + "z" * 43))
        assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer", "invalid key: 401 from accounts"
        assert client.get("/v1/me", headers=bearer("garbage")).status_code == 401
        assert client.get("/v1/me").status_code == 401, "nothing: the site password applies"
        r = client.get("/v1/me", headers=basic("opengrid", "s3cret-pass"))
        assert r.status_code == 200 and r.json()["data"]["principal"] == "operator"
        assert client.get("/market", headers=bearer(k["secret"])).status_code == 401, "keys only open /v1"
    finally:
        settings.app_password = None


def test_deploy_requires_pepper():
    os.environ["RAILWAY_ENVIRONMENT"] = "production"
    saved = settings.api_key_pepper
    settings.api_key_pepper = None
    try:
        try:
            keys.check_config()
            raise AssertionError("started deployed without a pepper")
        except RuntimeError as e:
            assert "API_KEY_PEPPER" in str(e)
        try:
            keys.hash_key("opg_live_x")
            raise AssertionError("hashed without a pepper while deployed")
        except HTTPException as e:
            assert e.status_code == 503
    finally:
        del os.environ["RAILWAY_ENVIRONMENT"]
        settings.api_key_pepper = saved


TESTS = (test_key_format, test_hash_only_storage, test_verify_states, test_last_used,
         test_rate_limit_and_usage_through_app, test_byo_credentials, test_operator_account, test_endpoints,
         test_bearer_bypasses_site_password, test_deploy_requires_pepper)

if __name__ == "__main__":
    try:
        for t in TESTS:
            t(); print(t.__name__, "ok")
    finally:
        normalize.SessionLocal.kw["bind"].dispose() if _Session else None
        scratchdb.drop(DB)
