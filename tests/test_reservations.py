"""Reserved-compute price-lock requests (methodology/reservations.md): validation, the status machine, offers
require a supply basis, tenant isolation, scopes, idempotency, the indicative reference (labelled, never a
quote; index_fixings tolerated when absent), acceptance launches / bills nothing, suspended accounts read-only,
expiry, the Discord feed query, the /reserve page, and the 0019 migration round trip.

TestClient (OPENGRID_NO_JOBS=1, POLLER_ENABLED=false), scratch DBs og_test_rsv / og_test_rsv_mig. No network.
Run:  .venv/Scripts/python tests/test_reservations.py
"""

from __future__ import annotations

import os
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("OPENGRID_NO_JOBS", "1")
os.environ.setdefault("POLLER_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bench_fixtures as bf  # noqa: E402

DB, MIG_DB = "og_test_rsv", "og_test_rsv_mig"
S_ = bf.db(DB)

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import normalize  # noqa: E402
from accounts import accounts as acc  # noqa: E402
from accounts import keys, ratelimit  # noqa: E402
from accounts.auth import Principal  # noqa: E402
from analytics import indices as I  # noqa: E402
from api import pages  # noqa: E402
from bench_fixtures import q, scalar  # noqa: E402
from config import settings  # noqa: E402
from reservations import service as R  # noqa: E402
from reservations import states as ST  # noqa: E402

client = TestClient(main.app)
G = "NVIDIA H100 80GB SXM5"
SLUG = "h100-80gb-sxm5"
IID = I.gpu_index_id(G)
CUSTOMER = ["data:read", "route:preview", "route:execute", "deployments:read"]
NOW = datetime.now(timezone.utc)
SPEND_TABLES = ("deployments", "quotes", "route_requests", "provision_attempts", "usage_records", "usage_slices",
                "execution_records", "jobs")


def iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def setup():
    normalize.SessionLocal = S_
    with S_.begin() as s:
        for t in ("reservation_requests", "reservation_events", "idempotency_keys", "index_levels", "market_hourly"):
            s.execute(text(f"DELETE FROM {t}"))
        s.execute(text("UPDATE accounts SET status = 'active'"))
    settings.app_password = None
    settings.public_pages = False
    settings.environment = "development"
    settings.api_key_pepper = "test-pepper-rsv"
    settings.deployments_webhook_url = None   # never post anywhere from a test
    for cls in ("read", "write", "execute"):   # budgets are tested in test_limits; here they would only throttle
        setattr(settings, f"rate_limit_{cls}_per_minute", 10000)
        setattr(settings, f"account_rate_limit_{cls}_per_minute", 10000)
    settings.ops_alert_webhook_url = None
    settings.ops_alert_webhook_secret = None
    keys.reset_cache()
    ratelimit.reset()
    acc.reset_cache()
    I.clear_caches()
    bf.market(G, [bf.listing("lambda", 2.49, gpu=G, count=8, lid="lambda:h100x8"),
                  bf.listing("runpod", 2.19, gpu=G, count=1, lid="runpod:h100x1"),
                  bf.listing("vast", 1.89, gpu=G, count=1, lid="vast:h100x1", region="eu-west", country="DE")])


def key(scopes=CUSTOMER, account_id=None):
    aid = account_id or bf.fresh_account()
    k = keys.create_key(aid, "rsv-test", scopes, rate_limit_per_minute=1000)
    return {"Authorization": f"Bearer {k['secret']}"}, aid


def body(**kw):
    b = {"gpu": SLUG, "gpu_count": 8, "total_gpu_hours": 5376, "start_after": iso(NOW + timedelta(days=7)),
         "start_before": iso(NOW + timedelta(days=14)), "region_group": "US", "max_price_per_gpu_hour": 2.25,
         "notes": "64-GPU cluster, NVLink"}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def create(h, b=None, ik=None):
    return client.post("/v1/reservations/requests", headers={**h, "Idempotency-Key": ik or str(uuid.uuid4())},
                       json=b or body())


def idem(h):
    return {**h, "Idempotency-Key": str(uuid.uuid4())}


OP = {"X-OpenGrid-Request": "1"}   # the operator (open dev, no APP_PASSWORD) + the CSRF header


def offer_body(**kw):
    b = {"price_per_gpu_hour": 2.20, "valid_until": iso(datetime.now(timezone.utc) + timedelta(days=2)),
         "terms": "Delivery Nov 1-30, 30-day minimum term, payment per signed order form.",
         "supply_basis": "Lambda written capacity commitment ref LMB-2026-114, 64x H100 SXM Nov 1-30"}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def spend_rows() -> int:
    return sum(scalar(f"SELECT count(*) FROM {t}") for t in SPEND_TABLES)


def seed_index(level=2.31, raw=2.29):
    from store.analytics import MarketHourly
    hour = NOW.replace(minute=0, second=0, microsecond=0)
    with S_.begin() as s:
        s.add(MarketHourly(segment="on_demand", gpu=G, provider="lambda", region="us-east", hour=hour,
                           min_price=2.49, max_price=2.49, avg_price=2.49, live_listings=1))
        from store.analytics import IndexLevel
        s.add(IndexLevel(index_id=IID, hour=hour, published=True, level=level, raw_level=raw, constituents=4,
                         method="median", segment_no=1, reason=None,
                         **{k: v for k, v in (("methodology_version", I.METHODOLOGY_VERSION), ("computed_at", NOW))
                            if hasattr(IndexLevel, k)}))   # columns the settlement work added (0018)
    I.clear_caches()


def fixings_table() -> bool:
    return scalar("SELECT to_regclass('index_fixings') IS NOT NULL")


def ensure_fixings_table():
    """index_fixings belongs to the settlement work (0018); create its shape here only if this DB lacks it."""
    if fixings_table():
        return
    with S_.begin() as s:
        s.execute(text("""CREATE TABLE index_fixings (seq bigserial PRIMARY KEY, fixing_id varchar(48) UNIQUE NOT NULL,
            index_id varchar(96) NOT NULL, fixing_type varchar(8) NOT NULL, fixing_time timestamptz NOT NULL,
            status varchar(16) NOT NULL, value double precision, reason text, methodology_version varchar(16) NOT NULL,
            settlement_version varchar(16) NOT NULL, inputs jsonb NOT NULL, input_hash varchar(64) NOT NULL,
            flags jsonb NOT NULL, restates varchar(48), actor varchar(64) NOT NULL, prev_hash varchar(64) NOT NULL,
            row_hash varchar(64) UNIQUE NOT NULL, created_at timestamptz NOT NULL)"""))


def walk_keys(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield path + "." + k, k, v
            yield from walk_keys(v, path + "." + k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk_keys(v, f"{path}[{i}]")


# --------------------------------------------------------------------------

def test_states_pure():
    assert ST.can("requested", "under_review") and ST.can("under_review", "offered") and ST.can("offered", "accepted")
    assert ST.can("accepted", "fulfilled") and ST.can("accepted", "cancelled") and ST.can("offered", "offered")
    for t in ST.TERMINAL:
        assert not ST.ALLOWED[t], t
    assert not ST.can("requested", "accepted"), "nothing is accepted without an offer"
    assert not ST.can("accepted", "withdrawn") and not ST.can("accepted", "expired")
    assert set(ST.ALLOWED) == set(ST.STATES)


def test_validation():
    setup()
    h, _ = key()
    cases = [
        (body(command="rm -rf /"), "unknown_field"),
        (body(gpu="nvidia-h900"), "unknown_gpu"),
        (body(gpu="auto"), "unknown_gpu"),
        (body(gpu=None), "missing_field"),
        (body(gpu_count=0), "out_of_range"),
        (body(gpu_count=2000), "out_of_range"),
        (body(gpu_count=2.5), "invalid_number"),
        (body(gpu_count=True), "invalid_number"),
        (body(gpu_count="8"), "invalid_number"),
        (body(total_gpu_hours=4), "out_of_range"),            # < one hour on each of 8 GPUs
        (body(total_gpu_hours=10.123), "invalid_number"),
        (body(total_gpu_hours=10**8), "out_of_range"),
        (body(start_after=iso(NOW - timedelta(days=1))), "start_in_past"),
        (body(start_before=iso(NOW + timedelta(days=7, minutes=30))), "window_too_short"),
        (body(start_before=iso(NOW + timedelta(days=800))), "out_of_range"),
        (body(start_after="2026-12-01T00:00:00"), "timezone_required"),
        (body(start_after="next tuesday"), "invalid_time"),
        (body(max_price_per_gpu_hour=0), "out_of_range"),
        (body(max_price_per_gpu_hour=-1), "out_of_range"),
        (body(max_price_per_gpu_hour=5000), "out_of_range"),
        (body(max_price_per_gpu_hour=None), "missing_field"),
        (body(region_group="Mars"), "unknown_region"),
        (body(notes="x" * 2001), "out_of_range"),
        (body(notes="bad\x00byte"), "invalid_text"),
    ]
    for b, code in cases:
        r = create(h, b)
        assert r.status_code == 422, (b, r.text)
        d = r.json()["detail"]
        assert d["code"] == code and d["message"], (code, d)
    r = client.post("/v1/reservations/requests", headers=h, json=body())
    assert r.status_code == 428 and r.json()["detail"]["code"] == "idempotency_key_required", r.text
    r = client.post("/v1/reservations/requests", headers={**idem(h), "Content-Type": "text/plain"}, content="{}")
    assert r.status_code == 415, r.text
    r = client.post("/v1/reservations/requests", headers=idem(h), json=[1, 2])
    assert r.status_code == 422, r.text
    assert scalar("SELECT count(*) FROM reservation_requests") == 0
    # region alias and canonical names are accepted
    r = create(h, body(region_group=None, region="europe", gpu=G))
    assert r.status_code == 201, r.text
    assert r.json()["data"]["region_group"] == "Europe" and r.json()["data"]["gpu"] == G


def test_create_and_indicative_reference():
    setup()
    seed_index()
    h, aid = key()
    hidden = False
    if fixings_table():   # absence must be tolerated: hide the settlement table for this part
        with S_.begin() as s:
            s.execute(text("ALTER TABLE index_fixings RENAME TO index_fixings_hidden_rsv"))
        hidden = True
    try:
        r = create(h)
    finally:
        if hidden:
            with S_.begin() as s:
                s.execute(text("ALTER TABLE index_fixings_hidden_rsv RENAME TO index_fixings"))
    assert r.status_code == 201, r.text
    env = r.json()
    assert env["meta"]["methodology"] == "/methodology/reservations" and env["meta"]["binding"] is False
    d = env["data"]
    assert d["status"] == "requested" and d["request_id"].startswith("rsv-") and d["account_id"] == aid
    assert d["launched"] is False and d["billed"] is False and "not a financial contract" in d["note"]
    assert d["max_total_usd"] == round(2.25 * 5376, 2) and d["offer"] is None
    ref = d["indicative"]
    assert ref["label"] == "indicative" and ref["is_quote"] is False and ref["binding"] is False, ref
    assert ref["index"]["label"] == "indicative" and ref["index"]["level"] == 2.31 and ref["index"]["raw_level"] == 2.29
    assert ref["index"]["index_id"] == IID and ref["index"]["published"] is True
    best = ref["best_on_demand"]
    assert best["label"] == "indicative" and best["region_group"] == "US"
    assert [x["provider"] for x in best["listings"]] == ["runpod", "lambda"], best   # vast is in DE, not US
    assert all(x["label"] == "indicative" and x["price_kind"] == "observed_market_price" for x in best["listings"])
    assert best["listings"][0]["price_per_gpu_hour"] == 2.19
    fx = ref["settlement_fixing"]
    assert fx["available"] is False and fx["label"] == "indicative" and "not available" in fx["reason"], fx
    assert ref["limit_vs_index"]["pct"] == round(2.25 / 2.31 - 1, 4)
    # never a quote: no key or label anywhere says "quote" (other than the explicit is_quote: false)
    for path, k, v in walk_keys(ref):
        assert "quote" not in k.lower() or k == "is_quote", path
        if k in ("label", "price_kind", "price_concept", "kind"):
            assert "quote" not in str(v).lower(), (path, v)
    # the request event
    ev = q("SELECT from_status, to_status, actor, reason FROM reservation_events WHERE request_id = :i",
           i=d["request_id"])
    assert len(ev) == 1 and ev[0].from_status is None and ev[0].to_status == "requested" and ev[0].actor == "user"
    # a reference with a settlement fixing present: the latest daily wins; a restatement supersedes its original
    ensure_fixings_table()
    with S_.begin() as s:
        for i, (fid, t, typ, val, status, restates) in enumerate((
                ("fx-h-1", NOW - timedelta(hours=5), "hourly", 2.30, "final", None),
                ("fx-d-1", NOW - timedelta(days=1), "daily", 2.28, "final", None),
                ("fx-d-1r", NOW - timedelta(days=1), "daily", 2.27, "restated", "fx-d-1"))):
            s.execute(text("""INSERT INTO index_fixings (fixing_id, index_id, fixing_type, fixing_time, status, value,
                              reason, methodology_version, settlement_version, inputs, input_hash, flags, restates,
                              actor, prev_hash, row_hash, created_at)
                              VALUES (:f, :i, :ty, :t, :st, :v, NULL, '1.0', '1.0', '{}'::jsonb, :ih, '[]'::jsonb,
                                      :rs, 'system', :ph, :rh, now())"""),
                      {"f": fid, "i": IID, "ty": typ, "t": t, "st": status, "v": val, "ih": "0" * 64, "rs": restates,
                       "ph": str(i) * 64, "rh": f"{uuid.uuid4().hex}{uuid.uuid4().hex}"})
    r = client.get("/v1/reservations/reference", headers=h, params={"gpu": SLUG, "gpu_count": 8})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["kind"] == "indicative"
    fx = r.json()["data"]["settlement_fixing"]
    assert fx["available"] and fx["daily"]["fixing_id"] == "fx-d-1r" and fx["daily"]["value"] == 2.27, fx
    assert fx["hourly"]["value"] == 2.30 and fx["daily"]["link"] == "/v1/fixings/fx-d-1r"
    r = client.get("/v1/reservations/reference", headers=h, params={"gpu": "nope-9000"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_gpu"
    # a GPU without an OpenGrid index still gets a (labelled) answer, never an invented level
    r = client.get("/v1/reservations/reference", headers=h, params={"gpu": "NVIDIA A10 24GB PCIe"})
    assert r.status_code == 200, r.text
    ix = r.json()["data"]["index"]
    assert ix["level"] is None and ix["reason"], ix


def test_idempotency():
    setup()
    h, _ = key()
    ik = str(uuid.uuid4())
    r1 = create(h, ik=ik)
    r2 = create(h, ik=ik)
    assert r1.status_code == 201 and r2.status_code == 201 and r2.headers.get("Idempotent-Replayed") == "true"
    assert r1.json()["data"]["request_id"] == r2.json()["data"]["request_id"]
    r3 = create(h, body(gpu_count=16, total_gpu_hours=10752), ik=ik)
    assert r3.status_code == 422 and r3.json()["detail"]["code"] == "idempotency_key_reused", r3.text
    assert scalar("SELECT count(*) FROM reservation_requests") == 1
    # a refusal is stored and replayed as well
    ik2 = str(uuid.uuid4())
    a = create(h, body(gpu="nope"), ik=ik2)
    b = create(h, body(gpu="nope"), ik=ik2)
    assert a.status_code == b.status_code == 422 and b.headers.get("Idempotent-Replayed") == "true"
    # accept replays too (one event, same answer)
    rid = r1.json()["data"]["request_id"]
    assert client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=offer_body()).status_code == 200
    ik3 = str(uuid.uuid4())
    a1 = client.post(f"/v1/reservations/{rid}/accept", headers={**h, "Idempotency-Key": ik3}, json={"offer_seq": 1})
    a2 = client.post(f"/v1/reservations/{rid}/accept", headers={**h, "Idempotency-Key": ik3}, json={"offer_seq": 1})
    assert a1.status_code == a2.status_code == 200 and a2.headers.get("Idempotent-Replayed") == "true", a2.text
    assert scalar("SELECT count(*) FROM reservation_events WHERE request_id = :i AND to_status = 'accepted'", i=rid) == 1


def test_scopes():
    setup()
    ro, _ = key(["data:read"])
    assert create(ro).status_code == 403
    assert client.get("/v1/reservations", headers=ro).status_code == 403
    assert client.get("/v1/reservations/reference", headers=ro, params={"gpu": SLUG}).status_code == 403
    pv, _ = key(["route:preview"])
    r = create(pv)
    assert r.status_code == 201, r.text
    rid = r.json()["data"]["request_id"]
    assert client.get("/v1/reservations", headers=pv).status_code == 200
    assert client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=offer_body()).status_code == 200
    r = client.post(f"/v1/reservations/{rid}/accept", headers=idem(pv), json={"offer_seq": 1})
    assert r.status_code == 403, "accept needs route:execute"
    # admin endpoints: customer keys (even with every customer scope, or an unflagged admin scope) are refused
    full, _ = key(CUSTOMER + ["deployments:write"])
    fake_admin, _ = key(CUSTOMER + ["admin"])   # not platform_admin: the admin scope is stripped
    for hh in (full, fake_admin):
        assert client.get("/v1/admin/reservations", headers=hh).status_code == 403
        assert client.post(f"/v1/admin/reservations/{rid}/offer", headers=hh, json=offer_body()).status_code == 403
        assert client.post(f"/v1/admin/reservations/{rid}/decline", headers=hh, json={"reason": "no"}).status_code == 403
    # CSRF: an operator POST without the header is refused
    assert client.post(f"/v1/admin/reservations/{rid}/decline", json={"reason": "nope"}).status_code == 403
    # a platform_admin key acts cross-tenant on the admin side
    pa = keys.create_key(bf.fresh_account(), "rsv-pa", ["admin"], rate_limit_per_minute=1000, platform_admin=True)
    ph = {"Authorization": f"Bearer {pa['secret']}"}
    r = client.get("/v1/admin/reservations", headers=ph)
    assert r.status_code == 200 and any(x["request_id"] == rid for x in r.json()["data"]), r.text


def test_tenant_isolation():
    setup()
    ha, a = key()
    hb, b = key()
    ra = create(ha).json()["data"]["request_id"]
    rb = create(hb).json()["data"]["request_id"]
    la = client.get("/v1/reservations", headers=ha).json()["data"]
    assert [x["request_id"] for x in la] == [ra], la
    assert client.get(f"/v1/reservations/{rb}", headers=ha).status_code == 404
    assert client.get(f"/v1/reservations/{ra}", headers=ha).status_code == 200
    assert client.post(f"/v1/admin/reservations/{rb}/offer", headers=OP, json=offer_body()).status_code == 200
    r = client.post(f"/v1/reservations/{rb}/accept", headers=idem(ha), json={"offer_seq": 1})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "reservation_not_found", r.text
    r = client.post(f"/v1/reservations/{rb}/withdraw", headers=idem(ha), json={})
    assert r.status_code == 404, r.text
    assert q("SELECT status FROM reservation_requests WHERE request_id = :i", i=rb)[0].status == "offered"
    assert client.get("/v1/reservations/rsv-zz", headers=ha).status_code == 404
    # the operator sees all (customer list and admin list), but cannot accept a customer's offer
    ops = {x["request_id"] for x in client.get("/v1/reservations").json()["data"]}
    assert {ra, rb} <= ops
    adm = client.get("/v1/admin/reservations", params={"status": "offered"}).json()["data"]
    assert [x["request_id"] for x in adm] == [rb], adm
    r = client.post(f"/v1/reservations/{rb}/accept", headers=idem(OP), json={"offer_seq": 1})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "not_owner", r.text
    # the owner's view lists its actions; the other tenant never sees them
    d = client.get(f"/v1/reservations/{rb}", headers=hb).json()["data"]
    assert set(d["actions"]) == {"withdraw", "accept"}, d["actions"]


def test_state_machine_offer_accept():
    setup()
    h, aid = key()
    rid = create(h).json()["data"]["request_id"]
    before = spend_rows()
    # an offer without a supply basis cannot be made
    for bad in (offer_body(supply_basis=None), offer_body(supply_basis="   "), offer_body(supply_basis="trust me")):
        r = client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "supply_basis_required", r.text
    for bad, code in ((offer_body(price_per_gpu_hour=0), "out_of_range"), (offer_body(terms=None), "missing_field"),
                      (offer_body(valid_until=iso(NOW - timedelta(hours=1))), "out_of_range"),
                      (offer_body(valid_until=iso(NOW + timedelta(days=60))), "out_of_range"),
                      (offer_body(extra=1), "unknown_field")):
        r = client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code, (code, r.text)
    row = q("SELECT status, offer_seq, offer_price_per_gpu_hour FROM reservation_requests WHERE request_id = :i", i=rid)[0]
    assert row.status == "requested" and row.offer_seq == 0 and row.offer_price_per_gpu_hour is None
    # the DB itself refuses an offer price without a supply basis
    try:
        with S_.begin() as s:
            s.execute(text("UPDATE reservation_requests SET offer_price_per_gpu_hour = 1 WHERE request_id = :i"),
                      {"i": rid})
        raise AssertionError("the check constraint should have refused an offer without a supply basis")
    except Exception as e:  # noqa: BLE001
        assert "ck_rsv_offer_has_supply_basis" in str(e), e
    # accept before any offer
    r = client.post(f"/v1/reservations/{rid}/accept", headers=idem(h), json={"offer_seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_open_offer", r.text
    # offer from requested passes through under_review
    r = client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=offer_body())
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["status"] == "offered" and d["offer"]["offer_seq"] == 1 and d["offer"]["valid"] is True
    assert d["offer"]["binding"] is False and d["offer"]["supply_basis"].startswith("Lambda written")
    assert d["offer"]["total_usd"] == round(2.20 * 5376, 2) and d["offer"]["above_customer_limit"] is False
    assert [e["to"] for e in d["events"]] == ["requested", "under_review", "offered"]
    # a revised offer supersedes; accepting the stale one is refused
    r = client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=offer_body(price_per_gpu_hour=2.40))
    d = r.json()["data"]
    assert r.status_code == 200 and d["offer"]["offer_seq"] == 2 and d["offer"]["above_customer_limit"] is True
    assert d["events"][-1]["evidence"]["supersedes"]["offer_seq"] == 1
    r = client.post(f"/v1/reservations/{rid}/accept", headers=idem(h), json={"offer_seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "offer_changed", r.text
    assert r.json()["detail"]["current_offer_seq"] == 2
    for bad in ({}, {"offer_seq": "2"}, {"offer_seq": True}, {"offer_seq": 2, "pay": True}):
        r = client.post(f"/v1/reservations/{rid}/accept", headers=idem(h), json=bad)
        assert r.status_code == 422, (bad, r.text)
    # accept: intent only
    r = client.post(f"/v1/reservations/{rid}/accept", headers=idem(h), json={"offer_seq": 2})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["status"] == "accepted" and d["message"] == R.ACCEPTED_MESSAGE
    assert d["message"] == "Accepted — OpenGrid will contact you to sign the order form; nothing is billed by this action."
    assert d["launched"] is False and d["billed"] is False and d["deployment_id"] is None and d["charge"] is None
    assert spend_rows() == before, "acceptance must not create a deployment, quote, route, usage or job row"
    ev = d["events"][-1]
    assert ev["to"] == "accepted" and ev["actor"] == "user" and ev["evidence"]["launched"] is False
    # after acceptance: customer cannot withdraw; operator fulfils or cancels; then terminal
    r = client.post(f"/v1/reservations/{rid}/withdraw", headers=idem(h), json={})
    assert r.status_code == 409 and "order form" in r.json()["detail"]["message"], r.text
    r = client.post(f"/v1/admin/reservations/{rid}/fulfil", headers=OP, json={})
    assert r.status_code == 422 and r.json()["detail"]["field"] == "reason"
    r = client.post(f"/v1/admin/reservations/{rid}/fulfil", headers=OP, json={"reason": "order form OF-77 signed, delivered"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "fulfilled" and r.json()["data"]["terminal"]
    for act, b in (("offer", offer_body()), ("decline", {"reason": "late"}), ("cancel", {"reason": "late"}),
                   ("review", {})):
        r = client.post(f"/v1/admin/reservations/{rid}/{act}", headers=OP, json=b)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "invalid_transition", (act, r.text)
    evs = q("SELECT from_status, to_status, actor, actor_id, reason FROM reservation_events WHERE request_id = :i "
            "ORDER BY at, id", i=rid)
    assert [e.to_status for e in evs] == ["requested", "under_review", "offered", "offered", "accepted", "fulfilled"]
    assert all(e.actor and e.actor_id and e.reason for e in evs), evs
    assert [e.from_status for e in evs][1:] == [e.to_status for e in evs][:-1], "every event links from the previous"
    assert spend_rows() == before


def test_decline_withdraw_cancel_review():
    setup()
    h, _ = key()
    r1 = create(h).json()["data"]["request_id"]
    r = client.post(f"/v1/admin/reservations/{r1}/review", headers=OP, json={})
    assert r.status_code == 200 and r.json()["data"]["status"] == "under_review"
    r = client.post(f"/v1/admin/reservations/{r1}/decline", headers=OP, json={})
    assert r.status_code == 422, r.text
    r = client.post(f"/v1/admin/reservations/{r1}/decline", headers=OP, json={"reason": "no capacity backs this price"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "declined"
    assert r.json()["data"]["closed_reason"] == "no capacity backs this price"
    assert client.post(f"/v1/reservations/{r1}/withdraw", headers=idem(h), json={}).status_code == 409
    # withdraw while offered = the customer declines the offer
    r2 = create(h).json()["data"]["request_id"]
    client.post(f"/v1/admin/reservations/{r2}/offer", headers=OP, json=offer_body())
    r = client.post(f"/v1/reservations/{r2}/withdraw", headers=idem(h), json={"reason": "found capacity elsewhere"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "withdrawn", r.text
    assert client.post(f"/v1/reservations/{r2}/accept", headers=idem(h), json={"offer_seq": 1}).status_code == 409
    # cancel after acceptance (order form never signed)
    r3 = create(h).json()["data"]["request_id"]
    client.post(f"/v1/admin/reservations/{r3}/offer", headers=OP, json=offer_body())
    assert client.post(f"/v1/reservations/{r3}/accept", headers=idem(h), json={"offer_seq": 1}).status_code == 200
    r = client.post(f"/v1/admin/reservations/{r3}/cancel", headers=OP, json={"reason": "order form not signed in 7 days"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled"
    # operator retracts an open offer
    r4 = create(h).json()["data"]["request_id"]
    client.post(f"/v1/admin/reservations/{r4}/offer", headers=OP, json=offer_body())
    r = client.post(f"/v1/admin/reservations/{r4}/decline", headers=OP, json={"reason": "provider withdrew capacity"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "declined"
    # list filters
    st = {x["request_id"]: x["status"] for x in client.get("/v1/reservations", headers=h).json()["data"]}
    assert st == {r1: "declined", r2: "withdrawn", r3: "cancelled", r4: "declined"}, st
    assert client.get("/v1/reservations", headers=h, params={"status": "open"}).json()["data"] == []
    assert client.get("/v1/reservations", headers=h, params={"status": "bogus"}).status_code == 422


def test_expiry():
    setup()
    h, _ = key()
    rid = create(h).json()["data"]["request_id"]
    client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=offer_body())
    with S_.begin() as s:   # the offer's validity has run out
        s.execute(text("UPDATE reservation_requests SET offer_valid_until = now() - interval '1 minute' "
                       "WHERE request_id = :i"), {"i": rid})
    r = client.post(f"/v1/reservations/{rid}/accept", headers=idem(h), json={"offer_seq": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "offer_expired", r.text
    d = client.get(f"/v1/reservations/{rid}", headers=h).json()["data"]
    assert d["status"] == "expired" and d["events"][-1]["actor"] == "system" and d["offer"]["valid"] is False
    # an un-offered request expires when its start window passes (the background job's function)
    r2 = create(h).json()["data"]["request_id"]
    assert R.expire_due() == 0
    assert R.expire_due(now=NOW + timedelta(days=15)) == 1
    assert q("SELECT status FROM reservation_requests WHERE request_id = :i", i=r2)[0].status == "expired"
    assert R.expire_due(now=NOW + timedelta(days=15)) == 0, "idempotent"
    import jobs
    assert "reservation_expiry" in jobs.JOBS


def test_open_request_cap():
    setup()
    h, aid = key()
    old = R.MAX_OPEN_PER_ACCOUNT
    R.MAX_OPEN_PER_ACCOUNT = 2
    try:
        assert create(h).status_code == 201 and create(h).status_code == 201
        r = create(h)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "too_many_open_requests", r.text
    finally:
        R.MAX_OPEN_PER_ACCOUNT = old


def test_suspended_read_only():
    setup()
    h, aid = key()
    rid = create(h).json()["data"]["request_id"]
    client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=offer_body())
    with S_.begin() as s:
        s.execute(text("UPDATE accounts SET status = 'suspended' WHERE id = :a"), {"a": aid})
    acc.reset_cache()
    keys.reset_cache()
    assert client.get("/v1/reservations", headers=h).status_code == 200
    assert client.get(f"/v1/reservations/{rid}", headers=h).status_code == 200
    assert create(h).status_code == 403
    assert client.post(f"/v1/reservations/{rid}/accept", headers=idem(h), json={"offer_seq": 1}).status_code == 403
    assert client.post(f"/v1/reservations/{rid}/withdraw", headers=idem(h), json={}).status_code == 403
    assert q("SELECT status FROM reservation_requests WHERE request_id = :i", i=rid)[0].status == "offered"
    # the service refuses too (defence in depth, e.g. a direct call)
    who = Principal(kind="api_key", account_id=aid, key_id=1, scopes=frozenset(CUSTOMER))
    try:
        R.create(body(), who)
        raise AssertionError("suspended account created a request")
    except R.Refused as e:
        assert e.code == "account_suspended" and e.status == 403


def test_rate_limit_class_and_pages():
    assert ratelimit.request_class("POST", "/v1/reservations/requests") == "execute"
    assert ratelimit.request_class("POST", "/v1/route/preview") == "execute"
    assert ratelimit.request_class("GET", "/v1/reservations") == "read"
    assert "reservations" in pages.PRIVATE_V1
    assert pages.is_page_path("/reserve") and pages.is_page_path("/reserve/rsv-0123456789abcdef")
    setup()
    r = client.get("/reserve")
    assert r.status_code == 200 and "/static/pages/reserve.js" in r.text and 'href="/reserve"' in r.text
    r = client.get("/methodology/reservations")
    assert r.status_code == 200 and "price-lock" in r.text.lower()
    # PUBLIC_PAGES on: the page shell is public, the data is not
    settings.public_pages = True
    settings.app_password = "secret-rsv"
    try:
        assert client.get("/v1/reservations").status_code == 401
        assert client.get("/v1/reservations/reference", params={"gpu": SLUG}).status_code == 401
    finally:
        settings.public_pages = False
        settings.app_password = None


def test_discord_feed():
    from alerts import channels
    setup()
    h, _ = key()
    rid = create(h).json()["data"]["request_id"]
    client.post(f"/v1/admin/reservations/{rid}/offer", headers=OP, json=offer_body())
    sql, build = channels._QUERIES["reservations"]
    with S_() as s:
        rows = [dict(r._mapping) for r in s.execute(text(sql), {"since": NOW - timedelta(hours=1), "n": 30,
                                                               "states": [], "minrel": 0})]
    assert [r["to_status"] for r in rows] == ["requested", "offered"], rows   # under_review is not posted
    msg = build(rows[1])
    e = msg["embeds"][0]
    assert "offer" in e["title"].lower() and "not a financial contract" in e["footer"]["text"]
    vals = {f["name"]: f["value"] for f in e["fields"]}
    assert vals["Offer"] == "$2.20/GPU-h" and vals["Customer limit"] == "$2.25/GPU-h" and vals["GPU-hours"] == "5,376"
    assert "NVLink" not in str(msg), "customer notes are never posted"
    assert channels.CHANNELS["reservations"] == "deployments_webhook_url"
    assert channels.run_channel("reservations")["skipped"] == "not configured"


def test_migration_roundtrip():
    """0019 applies on top of 0018 and downgrades cleanly (additive only)."""
    if not (Path(__file__).resolve().parent.parent / "migrations/versions/0018_settlement.py").exists():
        print("  (skipped: 0018_settlement not present yet)")
        return
    import scratchdb
    from alembic import command
    from sqlalchemy import create_engine

    import db as dbmod
    url = scratchdb.create(MIG_DB)
    old = settings.database_url
    settings.database_url = url
    try:
        cfg = dbmod._alembic_config()
        command.upgrade(cfg, "head")
        eng = create_engine(url)
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0019_reservations"
            for t in ("reservation_requests", "reservation_events"):
                assert c.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is not None, t
            assert c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_rsv_offer_has_supply_basis'")).scalar() == 1
        command.downgrade(cfg, "0018_settlement")
        with eng.connect() as c:
            assert c.execute(text("SELECT to_regclass('reservation_requests')")).scalar() is None
            assert c.execute(text("SELECT to_regclass('index_fixings')")).scalar() is not None, "0018 untouched"
        command.upgrade(cfg, "head")
        eng.dispose()
    finally:
        settings.database_url = old
        scratchdb.drop(MIG_DB)


# --------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"ok   {name}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    finally:
        bf.drop(S_, DB)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
