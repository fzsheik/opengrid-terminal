"""Billing models: record_usage under different fee policies, versioning, per-account overrides,
credits on draft invoices, and that no fee is hardcoded.

Run:  .venv/Scripts/python tests/test_billing.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

os.environ["OPENGRID_NO_JOBS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import main  # noqa: E402
import normalize  # noqa: E402
import scratchdb  # noqa: E402
from accounts import accounts, keys, ratelimit, usage  # noqa: E402
from billing import invoices, policy  # noqa: E402
from billing.usage import record_usage  # noqa: E402
from config import settings  # noqa: E402

DB = "og_test_accounts_billing"
client = TestClient(main.app)
GPU = "NVIDIA H100 80GB SXM5"
T0 = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
_Session = None


def setup():
    """A fresh database per test: policies are global state."""
    global _Session
    if _Session is not None:
        _Session.kw["bind"].dispose()
    _Session = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = _Session
    accounts.reset_cache()
    keys.reset_cache()
    ratelimit.reset()
    settings.app_password = None
    settings.api_key_pepper = "test-pepper"
    return _Session


def lines(S, usage_id):
    with S() as s:
        return [(r.kind, r.amount_usd, r.policy_id) for r in
                s.execute(text("SELECT kind, amount_usd, policy_id FROM charges WHERE usage_record_id = :u ORDER BY id"), {"u": usage_id})]


def use(acct, dep, hours=2, gpus=8, cost="40.00", start=T0, kind="compute"):
    return record_usage(account_id=acct, deployment_id=dep, provider="lambda", gpu=GPU, gpu_count=gpus,
                        period_start=start, period_end=start + timedelta(hours=hours),
                        provider_cost_usd=Decimal(cost), kind=kind)


def test_no_policy_means_no_fee():
    S = setup()
    a = accounts.create_account("nofee")["id"]
    u = use(a, "dep_nofee")
    assert lines(S, u) == [("compute", Decimal("40.000000"), None)], "nothing invented when no policy exists"
    assert policy.active(a) is None


def test_pct_vs_flat_policies():
    S = setup()
    a = accounts.create_account("pct")["id"]
    b = accounts.create_account("flat")["id"]
    g = policy.create("global pct", [{"kind": "buyer_fee_pct", "pct": 5}], effective_from=T0 - timedelta(days=30))
    f = policy.create("flat override", [{"kind": "flat_per_gpu_hour", "usd": "0.05"}], account_id=b,
                      effective_from=T0 - timedelta(days=30))
    ua, ub = use(a, "dep_a"), use(b, "dep_b")
    assert lines(S, ua) == [("compute", Decimal("40"), None), ("fee", Decimal("2.000000"), g["id"])], lines(S, ua)
    assert lines(S, ub) == [("compute", Decimal("40"), None), ("fee", Decimal("0.800000"), f["id"])], "16 GPU-h x $0.05"
    assert use(a, "dep_a") == ua, "idempotent per deployment period"
    # BYO: the provider bills the account; only the fee line exists.
    ubyo = use(a, "dep_byo", kind="byo")
    assert lines(S, ubyo) == [("fee", Decimal("2.000000"), g["id"])]


def test_versioning_and_composition():
    S = setup()
    a = accounts.create_account("ver")["id"]
    v1 = policy.create("v1", [{"kind": "buyer_fee_pct", "pct": 5}], effective_from=T0 - timedelta(days=10))
    v2 = policy.create("v2", [{"kind": "buyer_fee_pct", "pct": 3}, {"kind": "spread", "usd_per_gpu_hour": 0.1},
                              {"kind": "flat_per_gpu_hour", "usd": 0.01, "applies_to": ["compute"]}], effective_from=T0)
    assert v2["version"] == 2
    hist = policy.history()
    assert hist[0]["effective_to"] is not None and hist[1]["effective_to"] is None, "v1 closed by v2"
    old = use(a, "dep_old", start=T0 - timedelta(days=1))
    new = use(a, "dep_new", start=T0 + timedelta(hours=1))
    assert [x[2] for x in lines(S, old) if x[0] == "fee"] == [v1["id"]], "usage priced by the terms in force then"
    fees = [x[1] for x in lines(S, new) if x[0] == "fee"]
    assert fees == [Decimal("1.200000"), Decimal("1.600000"), Decimal("0.160000")], fees
    for bad in ([{"kind": "made_up"}], [{"kind": "buyer_fee_pct"}], [{"kind": "spread", "pct": 1, "usd_per_gpu_hour": 1}],
                [{"kind": "buyer_fee_pct", "pct": -1}]):
        try:
            policy.create("bad", bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass


def test_credits_on_draft_invoice():
    S = setup()
    a = accounts.create_account("credit")["id"]
    policy.create("pct + sub", [{"kind": "buyer_fee_pct", "pct": 10}, {"kind": "subscription", "usd_per_month": 20}],
                  effective_from=T0 - timedelta(days=60))
    use(a, "dep_1")                                            # 40 + 4
    use(a, "dep_2", cost="10", start=T0 + timedelta(days=2))   # 10 + 1
    use(a, "dep_oct", start=datetime(2026, 10, 2, tzinfo=timezone.utc))  # next month: not on this invoice
    c1 = invoices.add_credit(a, "30", "launch credit", expires_at=datetime(2026, 12, 1, tzinfo=timezone.utc))
    c2 = invoices.add_credit(a, "100", "goodwill")
    invoices.add_credit(a, "50", "expired", expires_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    r = invoices.draft("2026-09", a)
    assert r[0]["subtotal_usd"] == 75.0 and r[0]["credits_usd"] == 75.0 and r[0]["total_usd"] == 0.0, r
    inv = invoices.invoices_for(a)[0]
    assert inv["status"] == "draft" and inv["totals"] == {"compute": 50.0, "fee": 5.0, "subscription": 20.0, "credit": -75.0}, inv
    bal = {c["id"]: c["remaining_usd"] for c in invoices.credits_for(a)}
    assert bal[c1["id"]] == 0.0 and bal[c2["id"]] == 55.0, "soonest-expiring first; expired untouched"
    # Rebuilding the draft is idempotent (credits restored, then re-applied).
    invoices.draft("2026-09", a)
    assert {c["id"]: c["remaining_usd"] for c in invoices.credits_for(a)} == bal
    inv2 = invoices.invoices_for(a)[0]
    assert inv2["total_usd"] == 0.0 and len(inv2["lines"]) == len(inv["lines"])
    # Issued invoices are never rebuilt.
    with S.begin() as s:
        s.execute(text("UPDATE invoices SET status = 'issued'"))
    assert "skipped" in invoices.draft("2026-09", a)[0]


def test_billing_endpoints():
    setup()
    a = accounts.create_account("api")["id"]
    k = keys.create_key(a, "billing", ["billing:read"])
    h = {"Authorization": f"Bearer {k['secret']}"}
    assert client.get("/v1/billing/policy", headers=h).json()["data"] is None
    r = client.post("/v1/admin/fee-policies", json={"name": "data api", "account_id": a, "effective_from": "2026-01-01T00:00:00Z",
                                                    "components": [{"kind": "data_api", "usd_per_1k_requests": 1, "free_requests": 1}]})
    assert r.status_code == 201, r.text
    assert client.get("/v1/billing/policy", headers=h).json()["data"]["components"][0]["kind"] == "data_api"
    use(a, "dep_api", start=datetime.now(timezone.utc) - timedelta(hours=3))
    r = client.get("/v1/billing/usage", headers=h)
    assert r.status_code == 200 and r.json()["meta"]["kind"] == "transaction"
    assert r.json()["data"]["totals_by_kind"] == {"compute": 40.0}
    assert client.post("/v1/admin/credits", json={"account_id": a, "amount_usd": "5", "reason": "x"}).status_code == 201
    for _ in range(3):
        client.get("/v1/billing/usage", headers=h)  # 2 billable requests after the 1 free (+ earlier ones)
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    r = client.post(f"/v1/admin/invoices/draft?period={period}&account_id={a}")
    assert r.status_code == 200, r.text
    inv = client.get("/v1/billing/invoices", headers=h).json()["data"]["invoices"][0]
    kinds = {line["kind"] for line in inv["lines"]}
    assert {"compute", "data_api", "credit"} <= kinds, inv
    assert client.post("/v1/admin/invoices/draft?period=2026-13").status_code in (400, 422)
    assert client.get("/v1/billing/invoices", headers={"Authorization": "Bearer " + keys.create_key(a, "ro")["secret"]}).status_code == 403
    usage.flush()


def test_no_hardcoded_fee():
    """Grep the billing code: no literal fee percentage outside docstrings/examples."""
    import re

    src = (Path(__file__).resolve().parent.parent / "billing")
    for f in src.glob("*.py"):
        code = re.sub(r'"""[\s\S]*?"""', "", f.read_text(encoding="utf-8"))
        assert not re.search(r"\b0?\.05\b|\b5\s*%|pct\"?\s*[:=]\s*5\b", code), f"hardcoded fee in {f.name}"


TESTS = (test_no_policy_means_no_fee, test_pct_vs_flat_policies, test_versioning_and_composition,
         test_credits_on_draft_invoice, test_billing_endpoints, test_no_hardcoded_fee)

if __name__ == "__main__":
    try:
        for t in TESTS:
            t(); print(t.__name__, "ok")
    finally:
        if _Session:
            _Session.kw["bind"].dispose()
        scratchdb.drop(DB)
