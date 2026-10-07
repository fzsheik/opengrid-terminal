"""Watchlists and alerts: edge triggering, cooldown, unknown never fires, signed webhooks, metrics on
synthetic market data, and the endpoints.

Run:  .venv/Scripts/python tests/test_alerts.py
"""

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from accounts import accounts, keys, ratelimit  # noqa: E402
from alerts import evaluator, metrics, notifier, rules  # noqa: E402
from analytics import rollups  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_accounts_alerts"
client = TestClient(main.app, headers={"X-OpenGrid-Request": "1"})  # CSRF header, as web/core.js sends
GPU = "NVIDIA H100 80GB SXM5"
_Session = None
META = {}
FAKE = {"v": None}
metrics.REGISTRY["test_value"] = lambda p, ctx: metrics.Reading(FAKE["v"], "fake")


def setup():
    global _Session
    if _Session is None:
        _Session = fixtures.session(scratchdb.create(DB))
        normalize.SessionLocal = _Session
        META.update(fixtures.seed(_Session, days=10))
        rollups.refresh()
    normalize.SessionLocal = _Session
    accounts.reset_cache()
    keys.reset_cache()
    ratelimit.reset()
    settings.app_password = None
    settings.api_key_pepper = "test-pepper"
    with _Session.begin() as s:
        s.execute(text("DELETE FROM alert_rules"))
    return _Session


def firings(rule_id):
    with _Session() as s:
        return s.execute(text("SELECT count(*) FROM alert_firings WHERE rule_id = :r"), {"r": rule_id}).scalar()


def rule_row(rule_id):
    with _Session() as s:
        return s.execute(text("SELECT * FROM alert_rules WHERE id = :r"), {"r": rule_id}).one()


def test_edge_trigger_and_cooldown():
    setup()
    a = accounts.create_account("alerts")["id"]
    now = META["now"]
    low = metrics.market_low({"gpu": GPU}, metrics.Context(now=now)).value
    assert low is not None
    r = rules.create(a, {"metric": "market_low", "gpu": GPU, "op": "<", "value": low + 0.01}, cooldown_seconds=600)
    rid = r["id"]
    evaluator.run(now + timedelta(minutes=1))
    assert firings(rid) == 1, "first true evaluation fires"
    evaluator.run(now + timedelta(minutes=2))
    assert firings(rid) == 1, "still true: no re-fire (edge-triggered)"
    with _Session.begin() as s:
        saved = s.execute(text("SELECT provider, listing_id, price_per_gpu_hour FROM compute_listings WHERE canonical_gpu_name = :g"),
                          {"g": GPU}).all()
        s.execute(text("UPDATE compute_listings SET price_per_gpu_hour = 99 WHERE canonical_gpu_name = :g"), {"g": GPU})
    evaluator.run(now + timedelta(minutes=3))
    assert rule_row(rid).last_state == "false" and firings(rid) == 1
    with _Session.begin() as s:
        for p, lid, price in saved:
            s.execute(text("UPDATE compute_listings SET price_per_gpu_hour = :x WHERE provider = :p AND listing_id = :l"),
                      {"x": price, "p": p, "l": lid})
    evaluator.run(now + timedelta(minutes=4))
    assert firings(rid) == 1, "false -> true inside the 10-minute cooldown: suppressed"
    assert rule_row(rid).last_known_state == "true"

    # Cooldown passing, with a controllable metric (the synthetic market goes stale an hour on).
    f = rules.create(a, {"metric": "test_value", "op": ">", "value": 0}, cooldown_seconds=600)["id"]
    seq = [(1, 0, 1), (0, 1, 1), (1, 2, 1), (0, 20, 1), (1, 21, 2), (1, 22, 2)]  # (value, minute, expected firings)
    for v, minute, expect in seq:
        FAKE["v"] = float(v)
        evaluator.run(now + timedelta(minutes=minute))
        assert firings(f) == expect, (v, minute, firings(f))


def test_unknown_never_fires():
    setup()
    a = accounts.create_account("unknowns")["id"]
    now = META["now"]
    unk = [
        rules.create(a, {"metric": "index_new_low", "index_id": "no-such-index", "window": "30d"})["id"],
        rules.create(a, {"metric": "provider_price_change_pct", "gpu": GPU, "provider": "syn_nobody",
                         "window_hours": 24, "op": "<=", "value": 1000})["id"],
        rules.create(a, {"metric": "market_low", "gpu": "NVIDIA GH200 96GB", "op": ">", "value": -1})["id"],
        rules.create(a, {"metric": "test_value", "op": "<", "value": 1e9})["id"],
    ]
    FAKE["v"] = None
    for m in range(3):
        evaluator.run(now + timedelta(minutes=m))
    for rid in unk:
        assert firings(rid) == 0 and rule_row(rid).last_state == "unknown", (rid, rule_row(rid).last_detail)
    # true -> unknown -> true is not a new transition
    f = unk[-1]
    FAKE["v"] = 1.0
    evaluator.run(now + timedelta(hours=2))
    FAKE["v"] = None
    evaluator.run(now + timedelta(hours=4))
    FAKE["v"] = 1.0
    evaluator.run(now + timedelta(hours=6))
    assert firings(f) == 1
    try:
        rules.create(a, {"metric": "no_such_metric", "op": "<", "value": 1})
        raise AssertionError("unknown metric accepted")
    except ValueError:
        pass
    assert metrics.resolve({"metric": "no_such_metric"}).unknown


def test_metrics_on_synthetic_market():
    setup()
    now = META["now"]
    ctx = metrics.Context(now=now)
    lows = metrics._provider_lows(ctx, GPU)
    assert lows and metrics.market_low({"gpu": GPU}, ctx).value == min(lows.values())
    prov = sorted(lows)[0]
    r = metrics.provider_price_change_pct({"gpu": GPU, "provider": prov, "window_hours": 24}, ctx)
    then = metrics._provider_low_at(GPU, prov, now - timedelta(hours=24), now)
    if isinstance(then, float):
        assert abs(r.value - (lows[prov] - then) / then * 100) < 1e-9, r
    # Longer windows read the rollup; a provider that joined 4 days ago has no 5-day history.
    first = rollups.first_hours()
    checked = 0
    for g, p in first:
        lows_g = metrics._provider_lows(ctx, g)
        if p == "syn_eps" and p in lows_g:
            late = metrics.provider_price_change_pct({"gpu": g, "provider": p, "window_hours": 120}, ctx)
            assert late.unknown and "coverage" in late.detail, late
            early = sorted(q for q in lows_g if first.get((g, q)) and first[(g, q)] < now - timedelta(hours=121))
            if early:
                r = metrics.provider_price_change_pct({"gpu": g, "provider": early[0], "window_hours": 120}, ctx)
                assert r.value is not None or "no live priced" in r.detail, r
            checked += 1
    assert checked, "the late joiner is live for at least one GPU"
    av = metrics.available({"gpu": GPU}, ctx)
    assert av.value is not None and av.value >= 0


def test_webhook_signature():
    setup()
    a = accounts.create_account("hooks")["id"]
    try:
        rules.create(a, {"metric": "test_value", "op": ">", "value": 0}, channels=[{"type": "webhook", "url": "https://127.0.0.1/x"}])
        raise AssertionError("private webhook target accepted")
    except ValueError:
        pass
    settings.alerts_webhook_allow_private = True
    got = []

    def handler(request):
        got.append(request)
        return httpx.Response(204)

    notifier.http_client = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    try:
        r = rules.create(a, {"metric": "test_value", "op": ">", "value": 0}, name="hook",
                         channels=[{"type": "webhook", "url": "http://hook.test/in"}, {"type": "email", "to": "x@y.test"}])
        secret = r["webhook_secret"]
        assert secret.startswith("whsec_") and "webhook_secret" not in rules.get_rule(a, r["id"])
        with _Session() as s:
            dump = s.execute(text("SELECT row_to_json(x)::text FROM alert_rules x")).scalars().all()
        assert not any(secret in d for d in dump), "webhook secret stored encrypted"
        FAKE["v"] = 5.0
        evaluator.run(META["now"])
        assert len(got) == 1
        req = got[0]
        ts, sig = req.headers["x-opengrid-timestamp"], req.headers["x-opengrid-signature"]
        assert notifier.verify_signature(secret, int(ts), req.content, sig)
        assert not notifier.verify_signature("whsec_wrong", int(ts), req.content, sig)
        assert not notifier.verify_signature(secret, int(ts), req.content + b" ", sig), "body tampering detected"
        assert json.loads(req.content)["rule_id"] == r["id"]
        f = rules.firings(a, r["id"])[0]
        assert f["delivered_via"] == ["in_app", "webhook"] and f["delivery_status"]["email"] == "not_implemented"
    finally:
        settings.alerts_webhook_allow_private = False
        notifier.http_client = lambda: httpx.Client(timeout=5.0, follow_redirects=False)


def test_endpoints():
    setup()
    # The operator (no key) uses the implicit operator account.
    r = client.post("/v1/watchlists", json={"name": "my gpus"})
    assert r.status_code == 201, r.text
    wid = r.json()["data"]["id"]
    assert client.post(f"/v1/watchlists/{wid}/items", json={"kind": "gpu", "gpu": "h100-80gb-sxm5"}).status_code == 201
    assert client.post(f"/v1/watchlists/{wid}/items", json={"kind": "gpu_provider", "gpu": "h100-80gb-sxm5"}).status_code == 400
    assert client.post(f"/v1/watchlists/{wid}/items", json={"kind": "region", "region_group": "Mars"}).status_code == 400
    assert client.post(f"/v1/watchlists/{wid}/items", json={"kind": "gpu", "gpu": "no-such-gpu"}).status_code == 404
    w = client.get(f"/v1/watchlists/{wid}").json()["data"]
    assert w["items"][0]["gpu"] == GPU and "current" in w["items"][0]
    r = client.post("/v1/alerts", json={"params": {"metric": "market_low", "gpu": "h100-80gb-sxm5", "op": "<", "value": 1000}})
    assert r.status_code == 201, r.text
    rid = r.json()["data"]["id"]
    t = client.post(f"/v1/alerts/{rid}/test").json()["data"]
    assert t["dry_run"] and t["state"] in ("true", "unknown")
    assert rule_row(rid).last_evaluated_at is None, "test is dry"
    assert client.get("/v1/alerts/firings").status_code == 200
    assert client.patch(f"/v1/alerts/{rid}", json={"status": "paused"}).json()["data"]["status"] == "paused"
    assert client.post("/v1/alerts", json={"params": {"metric": "market_low", "gpu": GPU}}).status_code == 400, "needs op/value"
    # Another account cannot see the operator's watchlist; a key without the scope gets 403.
    other = accounts.create_account("other")["id"]
    k = keys.create_key(other, "w", ["watchlists"])
    ro = keys.create_key(other, "r", ["data:read"])
    assert client.get(f"/v1/watchlists/{wid}", headers={"Authorization": f"Bearer {k['secret']}"}).status_code == 404
    assert client.get("/v1/watchlists", headers={"Authorization": f"Bearer {ro['secret']}"}).status_code == 403
    assert client.delete(f"/v1/alerts/{rid}").status_code == 204
    assert client.delete(f"/v1/watchlists/{wid}").status_code == 204


TESTS = (test_edge_trigger_and_cooldown, test_unknown_never_fires, test_metrics_on_synthetic_market,
         test_webhook_signature, test_endpoints)

if __name__ == "__main__":
    try:
        for t in TESTS:
            t(); print(t.__name__, "ok")
    finally:
        if _Session:
            _Session.kw["bind"].dispose()
        scratchdb.drop(DB)
