"""Design partners (profiles, one-step onboarding, feedback, onboarding state machine, scopes) and
product analytics (event intake, sanitizing, rate limit, server events, funnel math, summary).

Scratch database; the HTTP surface through main.app with real API keys. Run:
    .venv/Scripts/python tests/test_partners.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
os.environ["POLLER_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import observability as obs  # noqa: E402
import scratchdb  # noqa: E402
from accounts import accounts, keys, ratelimit  # noqa: E402
from analytics import product  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_metrics_p"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
H100 = "NVIDIA H100 80GB SXM5"
client = TestClient(main.app)
_Session = None
OP = {"X-OpenGrid-Request": "1"}  # the operator's CSRF header (harmless if the security layer does not need it)

_FILL = {"character varying": "x", "text": "x", "integer": 0, "bigint": 0, "smallint": 0, "numeric": 0,
         "boolean": False, "jsonb": {}, "json": {}, "timestamp with time zone": NOW, "ARRAY": []}


def put(table: str, **vals):
    with normalize.SessionLocal.begin() as s:
        meta = s.execute(text("SELECT column_name, data_type, is_nullable, column_default FROM "
                              "information_schema.columns WHERE table_schema='public' AND table_name=:t"),
                         {"t": table}).all()
        types = {c: t for c, t, _, _ in meta}
        for c, t, nullable, default in meta:
            if c not in vals and nullable == "NO" and default is None:
                vals[c] = _FILL.get(t, "x")
        cols = list(vals)
        ph = [f"CAST(:{c} AS jsonb)" if types[c] in ("jsonb", "json") else f":{c}" for c in cols]
        params = {c: json.dumps(v, default=str) if types[c] in ("jsonb", "json") else v for c, v in vals.items()}
        s.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(ph)})"), params)


def setup():
    global _Session
    if _Session is None:
        _Session = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = _Session
    with _Session.begin() as s:
        for t in ("accounts", "api_keys", "api_key_usage", "provider_credentials", "partner_profiles",
                  "deployment_feedback", "product_events", "route_requests", "deployments", "deployment_events",
                  "provision_attempts"):
            s.execute(text(f"TRUNCATE {t} RESTART IDENTITY CASCADE"))
    accounts.reset_cache()
    keys.reset_cache() if hasattr(keys, "reset_cache") else None
    ratelimit.reset()
    product.reset_limits()
    settings.app_password = None
    settings.api_key_pepper = "test-pepper-" + "x" * 40
    return _Session


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def deployment(dep, account, *, ran=True, created=None, purpose="customer", approved=True, terminated=False):
    created = created or NOW - timedelta(hours=3)
    put("route_requests", id="rr_" + dep, account_id=account, principal_kind="api_key", preview=False,
        mode="CHEAPEST", gpu=H100, request={}, status="routing", created_at=created)
    put("deployments", deployment_id=dep, account_id=account, route_request_id="rr_" + dep, provider="lambda", gpu=H100,
        gpu_count=1, status="terminated" if terminated else ("running" if ran else "provision_failed"),
        created_at=created, uptime_seconds=0, interruptions=0, purpose=purpose, effective_max_runtime_minutes=60,
        approved_at=created + timedelta(minutes=1) if approved else None)
    if ran:
        put("deployment_events", deployment_id=dep, at=created + timedelta(minutes=3), to_status="running")
    if terminated:
        put("deployment_events", deployment_id=dep, at=created + timedelta(hours=1), to_status="terminated")


# ---------------------------------------------------------------- partners

PROFILE = {"company": "Acme AI", "technical_contact_name": "Ada", "technical_contact_email": "ada@acme.example",
           "preferred_gpus": ["h100-80gb-sxm5"], "regions": ["US"], "workload_type": "training",
           "normal_provider": "aws", "normal_price_per_gpu_hour": 4.1, "max_price_per_gpu_hour": 3.0,
           "expected_gpu_count": 8, "expected_duration_hours": 48, "network_requirements": "100G between nodes"}


def onboard(**extra):
    r = client.post("/v1/admin/partners", json={**PROFILE, **extra}, headers=OP)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_admin_onboards_partner_in_one_step():
    setup()
    d = onboard()
    assert d["account"]["name"] == "Acme AI" and d["account"]["plan"] == "design_partner"
    assert d["profile"]["status"] == "invited" and d["profile"]["normal_price_per_gpu_hour"] == 4.1
    key = d["api_key"]
    assert key["secret"].startswith("opg_live_") and "route:execute" not in key["scopes"]
    assert set(key["scopes"]) >= {"route:preview", "account:manage", "deployments:write"}
    assert d["onboarding"]["onboarding_status_url"].endswith("/v1/onboarding")
    with normalize.SessionLocal() as s:
        raw = s.execute(text("SELECT row_to_json(k)::text FROM api_keys k")).scalar()
    assert key["secret"] not in raw, "only the hash is stored"
    ex = onboard(company="Beta Labs", grant_execute=True, technical_contact_email=None)
    assert "route:execute" in ex["api_key"]["scopes"]
    nokey = onboard(company="Gamma", create_key=False)
    assert nokey["api_key"] is None
    bad = client.post("/v1/admin/partners", json={**PROFILE, "technical_contact_email": "not-an-email"}, headers=OP)
    assert bad.status_code == 400
    lst = client.get("/v1/admin/partners").json()["data"]
    assert {p["company"] for p in lst} == {"Acme AI", "Beta Labs", "Gamma"}
    assert all("onboarding" in p for p in lst)


def test_partner_self_service_and_scopes():
    setup()
    d = onboard()
    h = bearer(d["api_key"]["secret"])
    me = client.get("/v1/partners/me", headers=h)
    assert me.status_code == 200 and me.json()["data"]["company"] == "Acme AI"
    p = client.patch("/v1/partners/me", json={"status": "onboarding", "regions": ["US", "Canada"]}, headers=h)
    assert p.status_code == 200 and p.json()["data"]["status"] == "onboarding"
    assert p.json()["data"]["regions"] == ["US", "Canada"] and p.json()["data"]["company"] == "Acme AI"
    assert client.patch("/v1/partners/me", json={"status": "vip"}, headers=h).status_code == 422
    assert client.patch("/v1/partners/me", json={"unknown": 1}, headers=h).status_code == 422
    assert client.post("/v1/partners/me", json=PROFILE, headers=h).status_code == 409
    # a key without account:manage
    acct = d["account"]["id"]
    ro = keys.create_key(acct, "ro", ["data:read"])
    assert client.get("/v1/partners/me", headers=bearer(ro["secret"])).status_code == 403
    assert client.get("/v1/admin/partners", headers=h).status_code == 403
    assert client.post("/v1/admin/partners", json=PROFILE, headers=h).status_code == 403
    # a fresh account creates its own profile
    other = accounts.create_account("Solo")
    k = keys.create_key(other["id"], "k", ["account:manage"])
    assert client.get("/v1/partners/me", headers=bearer(k["secret"])).status_code == 404
    r = client.post("/v1/partners/me", json={"company": "Solo Inc"}, headers=bearer(k["secret"]))
    assert r.status_code == 201 and r.json()["data"]["account_id"] == other["id"]
    assert client.post("/v1/partners/me", json={"workload_type": "x"},
                       headers=bearer(keys.create_key(accounts.create_account("NoCo")["id"], "k",
                                                      ["account:manage"])["secret"])).status_code == 400
    # the operator edits any partner
    r = client.patch(f"/v1/admin/partners/{acct}", json={"status": "active"}, headers=OP)
    assert r.status_code == 200 and r.json()["data"]["status"] == "active"


def test_onboarding_state_machine():
    setup()
    d = onboard(create_key=False)
    acct = d["account"]["id"]
    from partners import onboarding

    st = onboarding.status(acct)
    assert st["next_step"]["step"] == "key_created" and not st["complete"]
    k = keys.create_key(acct, "k", ["data:read", "route:preview", "deployments:read", "deployments:write"])
    h = bearer(k["secret"])
    st = client.get("/v1/onboarding", headers=h).json()["data"]
    done = {x["step"]: x["done"] for x in st["steps"]}
    assert done["account_created"] and done["key_created"] and not done["credentials_connected"]
    assert st["next_step"]["step"] == "first_preview", "credentials are optional, never the next required step"
    put("route_requests", id="rr_p1", account_id=acct, principal_kind="api_key", preview=True, mode="BALANCED",
        gpu=H100, request={}, status="previewed", created_at=NOW)
    assert onboarding.status(acct)["next_step"]["step"] == "first_supervised_deployment"
    deployment("dep-x1", acct, ran=False)
    assert onboarding.status(acct)["next_step"]["step"] == "first_supervised_deployment", "a failed launch is not done"
    deployment("dep-x2", acct, ran=True)
    assert onboarding.status(acct)["next_step"]["step"] == "feedback_given"
    r = client.post("/v1/deployments/dep-x2/feedback", json={"price_better": True, "would_route_next": True}, headers=h)
    assert r.status_code == 201
    st = onboarding.status(acct)
    assert st["complete"] and st["next_step"] is None and st["progress"] == "5/5 required steps"
    # a validation deployment never counts as the partner's first supervised deployment
    other = accounts.create_account("V")["id"]
    deployment("dep-v1", other, purpose="validation")
    assert not {x["step"]: x["done"] for x in onboarding.status(other)["steps"]}["first_supervised_deployment"]


def test_feedback_flow():
    setup()
    a = onboard()
    b = onboard(company="Other")
    ha, hb = bearer(a["api_key"]["secret"]), bearer(b["api_key"]["secret"])
    deployment("dep-a", a["account"]["id"])
    deployment("dep-b", b["account"]["id"])
    r = client.post("/v1/deployments/dep-a/feedback", json={"would_have_chosen_provider": False, "price_better": True,
                                                             "setup_easier": True, "what_broke": "ssh took 5 min"},
                    headers=ha)
    assert r.status_code == 201 and r.json()["data"]["account_id"] == a["account"]["id"]
    r = client.post("/v1/deployments/dep-a/feedback", json={"would_route_next": True, "notes": "good"}, headers=ha)
    assert r.status_code == 200, "second POST edits"
    f = client.get("/v1/deployments/dep-a/feedback", headers=ha).json()["data"]
    assert f["price_better"] is True and f["would_route_next"] is True and f["what_broke"] == "ssh took 5 min"
    assert f["updated_at"] is not None
    assert client.post("/v1/deployments/dep-b/feedback", json={"price_better": True}, headers=ha).status_code == 404
    assert client.get("/v1/deployments/dep-b/feedback", headers=ha).status_code == 404
    assert client.post("/v1/deployments/nope/feedback", json={}, headers=ha).status_code == 404
    assert client.post("/v1/deployments/dep-a/feedback", json={"price_better": "yes"}, headers=ha).status_code == 422
    ro = keys.create_key(a["account"]["id"], "ro", ["deployments:read"])
    assert client.post("/v1/deployments/dep-a/feedback", json={}, headers=bearer(ro["secret"])).status_code == 403
    client.post("/v1/deployments/dep-b/feedback", json={"price_better": False}, headers=hb)
    adm = client.get("/v1/admin/feedback").json()["data"]
    assert len(adm["items"]) == 2 and adm["summary"]["price_better"] == {"yes": 1, "no": 1, "n": 2}
    assert client.get("/v1/admin/feedback", headers=ha).status_code == 403


# ---------------------------------------------------------------- product analytics

def test_sanitize_props():
    p = product.sanitize_props({
        "page": "/gpus/h100?email=ada@acme.example&utm=x", "q": "h100 near ada@acme.example", "email": "ada@x.io",
        "user_name": "Ada", "ip": "1.2.3.4", "api_key": "opg_live_abc", "Bad Key": 1, "count": 3, "ok": True,
        "nested": {"a": 1}, "url": "https://evil.example/path/x?token=zzz", "list": ["a", {"b": 1}, 2],
        "note": "Authorization: Bearer abcdefgh12345678"})
    assert p["page"] == "/gpus/h100", p
    assert "ada@acme.example" not in json.dumps(p) and "[email]" in p["q"]
    for k in ("email", "user_name", "ip", "api_key", "Bad Key", "nested"):
        assert k not in p, k
    assert p["url"] == "/path/x" and p["count"] == 3 and p["ok"] is True and p["list"] == ["a", 2]
    assert "abcdefgh12345678" not in p["note"]
    big = product.sanitize_props({f"k{i}": "v" * 200 for i in range(30)})
    assert len(json.dumps(big)) <= product.MAX_PROPS_BYTES and len(big) <= 20


def test_track_endpoint():
    setup()
    ev = [{"event": "page_view", "anon_id": "anon_aaaaaaaa", "props": {"page": "/"}},
          {"event": "gpu_view", "anon_id": "anon_aaaaaaaa", "props": {"gpu": "h100-80gb-sxm5"}},
          {"event": "route_completed", "anon_id": "anon_aaaaaaaa"},           # server-only: rejected
          {"event": "page_view", "anon_id": "bad id!"},
          {"event": "page_view"}]
    r = client.post("/v1/events/track", json={"events": ev}, headers=OP)
    assert r.status_code == 202, r.text
    d = r.json()["data"]
    assert d["accepted"] == 2 and len(d["rejected"]) == 3, d
    with normalize.SessionLocal() as s:
        rows = s.execute(text("SELECT event, anon_id, account_id, props, source FROM product_events")).all()
    assert all(r.source == "client" and r.props.get("internal") is True for r in rows), "operator traffic is tagged"
    with normalize.SessionLocal() as s:
        cols = {c for (c,) in s.execute(text("SELECT column_name FROM information_schema.columns "
                                             "WHERE table_name = 'product_events'"))}
    assert not any("ip" == c or "ip_" in c for c in cols), "no IP column exists"
    assert client.post("/v1/events/track", json={"events": []}, headers=OP).status_code == 422
    assert client.post("/v1/events/track", json={"events": [{"event": "page_view", "anon_id": "anon_aaaaaaaa"}] * 51},
                       headers=OP).status_code == 422
    # an API key's events carry its account
    acct = accounts.create_account("K")["id"]
    k = keys.create_key(acct, "k", ["data:read"])
    r = client.post("/v1/events/track", json={"events": [{"event": "search", "props": {"q": "B200"}}]},
                    headers=bearer(k["secret"]))
    assert r.status_code == 202 and r.json()["data"]["accepted"] == 1
    with normalize.SessionLocal() as s:
        assert s.execute(text("SELECT account_id FROM product_events WHERE event='search'")).scalar() == acct
    assert client.post("/v1/events/track", json={"events": []}, headers=bearer("opg_live_bogus")).status_code == 401
    # anonymous (no principal at all) via the module, as the public-page path will call it
    out = product.track([{"event": "page_view", "anon_id": "anon_zzzzzzzz", "props": {"page": "/gpus"}}],
                        account_id=None)
    assert out["accepted"] == 1


def test_track_rate_limit():
    setup()
    old = settings.product_events_per_minute
    settings.product_events_per_minute = 3
    try:
        body = {"events": [{"event": "page_view", "anon_id": "anon_rate0001"}]}
        codes = [client.post("/v1/events/track", json=body, headers=OP).status_code for _ in range(5)]
        assert codes == [202, 202, 202, 429, 429], codes
        assert product.allow("someone-else"), "per client"
        assert product.allow("k1", now=0) and product.allow("k1", now=1) and product.allow("k1", now=2)
        assert not product.allow("k1", now=3) and product.allow("k1", now=61), "window slides"
    finally:
        settings.product_events_per_minute = old


def test_server_events_idempotent():
    setup()
    acct = accounts.create_account("S")["id"]
    k = keys.create_key(acct, "k", ["data:read"])
    for i in range(3):
        put("api_key_usage", key_id=k["id"], account_id=acct, ts=NOW - timedelta(minutes=i), method="GET",
            path="/v1/gpus", status=200)
    put("route_requests", id="rr_s1", account_id=acct, principal_kind="api_key", preview=True, mode="CHEAPEST", gpu=H100,
        request={}, status="previewed", created_at=NOW)
    deployment("dep-s1", acct, terminated=True)
    n1 = product.sync_server_events()
    n2 = product.sync_server_events()
    assert n1["route_preview"] == 1 and n1["route_approved"] == 1 and n1["route_completed"] == 1, n1
    with normalize.SessionLocal() as s:
        by = dict(s.execute(text("SELECT event, count(*) FROM product_events GROUP BY 1")).all())
        props = s.execute(text("SELECT props FROM product_events WHERE event = 'api_call'")).scalar()
    assert by == {"api_call": 1, "route_preview": 1, "route_approved": 1, "route_completed": 1}, by
    assert props["count"] == 3
    put("api_key_usage", key_id=k["id"], account_id=acct, ts=NOW, method="GET", path="/v1/gpus", status=200)
    product.sync_server_events()
    with normalize.SessionLocal() as s:
        assert s.execute(text("SELECT props->>'count' FROM product_events WHERE event='api_call'")).scalar() == "4"
    assert n2 == n1


def test_funnel_math():
    setup()
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)                  # a Wednesday
    this_w, last_w = now - timedelta(days=1), now - timedelta(days=8)     # Tue this week, Tue last week
    rows = []
    for i in range(10):                                                   # 10 visitors this week, 4 view 2+ market pages
        rows.append({"event": "page_view", "anon_id": f"anon_v{i:06d}", "props": {"page": "/"}})
        if i < 4:
            rows.append({"event": "gpu_view", "anon_id": f"anon_v{i:06d}", "props": {"gpu": "h100"}})
    with normalize.SessionLocal.begin() as s:
        for r in rows:
            s.execute(text("INSERT INTO product_events (ts, anon_id, event, props, source) VALUES (:t, :a, :e, "
                           "CAST(:p AS jsonb), 'client')"), {"t": this_w, "a": r["anon_id"], "e": r["event"],
                                                            "p": json.dumps(r["props"])})
        s.execute(text("INSERT INTO product_events (ts, anon_id, event, props, source) VALUES (:t, 'anon_last0001', "
                       "'page_view', '{}', 'client'), (:t, 'anon_opop0001', 'page_view', '{\"internal\": true}', "
                       "'client')"), {"t": last_w})
    # accounts: 3 created this week (2 with keys, 1 preview, 1 real deployment twice), 1 last week, + the operator
    ids = []
    for name in ("a1", "a2", "a3"):
        a = accounts.create_account(name)
        ids.append(a["id"])
        with normalize.SessionLocal.begin() as s:
            s.execute(text("UPDATE accounts SET created_at = :t WHERE id = :i"), {"t": this_w, "i": a["id"]})
    keys.create_key(ids[0], "k")
    keys.create_key(ids[1], "k")
    put("route_requests", id="rr_f1", account_id=ids[0], principal_kind="api_key", preview=True, mode="CHEAPEST",
        gpu=H100, request={}, status="previewed", created_at=this_w)
    deployment("dep-f1", ids[0])
    deployment("dep-f2", ids[0])
    deployment("dep-f3", ids[1], ran=False)
    old = accounts.create_account("old")
    with normalize.SessionLocal.begin() as s:
        s.execute(text("UPDATE accounts SET created_at = :t WHERE id = :i"), {"t": last_w, "i": old["id"]})
    accounts.operator_account_id()
    f = product.funnel(weeks=2, now=now)
    wk = {w["week"]: w for w in f["weeks"]}
    cur, prev = wk["2026-10-05"], wk["2026-09-28"]
    assert (cur["visitors"], cur["market_users"], cur["accounts"], cur["api_key"], cur["route_preview"],
            cur["real_deployment"], cur["second_deployment"]) == (10, 4, 3, 2, 1, 1, 1), cur
    assert cur["conversion"]["visitors->market_users"] == 0.4 and cur["conversion"]["api_key->route_preview"] == 0.5
    assert cur["conversion"]["accounts->api_key"] == round(2 / 3, 4)
    assert prev["visitors"] == 1 and prev["accounts"] == 1, "operator traffic and the operator account excluded"
    assert f["total"]["visitors"] == 11 and f["total"]["accounts"] == 4
    assert product.funnel(weeks=1, now=now)["weeks"][0]["week"] == "2026-10-05"


def test_product_summary():
    setup()
    with normalize.SessionLocal.begin() as s:
        for anon, day, page in (("anon_r0000001", 0, "/gpus"), ("anon_r0000001", 1, "/gpus"),
                                ("anon_r0000002", 0, "/"), ("anon_r0000002", 0, "/gpus")):
            s.execute(text("INSERT INTO product_events (ts, anon_id, event, props, source) VALUES "
                           "(:t, :a, 'page_view', CAST(:p AS jsonb), 'client')"),
                      {"t": NOW - timedelta(days=day), "a": anon, "p": json.dumps({"page": page})})
        s.execute(text("INSERT INTO product_events (ts, anon_id, event, props, source) VALUES "
                       "(now(), 'anon_r0000002', 'search', '{\"q\": \"H100\"}', 'client'), "
                       "(now(), 'anon_r0000003', 'search', '{\"q\": \"h100\"}', 'client')"))
    r = client.get("/v1/admin/product").json()["data"]
    assert r["top_pages"][0] == {"page": "/gpus", "views": 3, "visitors": 2}
    assert r["top_searches"][0] == {"q": "h100", "count": 2}
    assert r["repeat_visitors"] == 1 and r["visitors"] == 3
    assert client.get("/v1/admin/funnel").status_code == 200
    k = keys.create_key(accounts.create_account("Z")["id"], "k", ["data:read"])
    assert client.get("/v1/admin/product", headers=bearer(k["secret"])).status_code == 403
    assert client.post("/v1/admin/product/sync", headers=OP).status_code == 200


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    try:
        for t in tests:
            t()
            print("ok", t.__name__)
    finally:
        normalize.SessionLocal.kw["bind"].dispose()
        scratchdb.drop(DB)
    print(f"{len(tests)} passed")
