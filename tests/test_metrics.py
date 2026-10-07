"""Routing quality, provider reliability, economics, the first-route checklist and trace assembly,
on crafted deployment rows in a scratch database. No provider is ever called.

Run:  .venv/Scripts/python tests/test_metrics.py
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

from sqlalchemy import text  # noqa: E402

import fixtures  # noqa: E402
import jobs  # noqa: E402
import normalize  # noqa: E402
import observability as obs  # noqa: E402
import scratchdb  # noqa: E402
from config import settings  # noqa: E402
from routing import adapters, checklist, quality, reliability  # noqa: E402
from routing.adapters.base import Adapter  # noqa: E402

DB = "og_test_metrics_m"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
H100 = "NVIDIA H100 80GB SXM5"
_Session = None


# ---------------------------------------------------------------- crafted rows

_FILL = {"character varying": "x", "text": "x", "integer": 0, "bigint": 0, "smallint": 0, "numeric": 0,
         "boolean": False, "jsonb": {}, "json": {}, "timestamp with time zone": NOW, "ARRAY": []}


def put(table: str, **vals):
    """INSERT a row with the given values; NOT NULL columns without defaults get a neutral filler,
    so the test survives columns other agents add. jsonb values are passed as JSON."""
    with normalize.SessionLocal.begin() as s:
        meta = s.execute(text("SELECT column_name, data_type, is_nullable, column_default FROM "
                              "information_schema.columns WHERE table_schema='public' AND table_name=:t"),
                         {"t": table}).all()
        types = {c: t for c, t, _, _ in meta}
        for c, t, nullable, default in meta:
            if c not in vals and nullable == "NO" and default is None:
                vals[c] = _FILL.get(t, "x")
        missing = [k for k in vals if k not in types]
        assert not missing, f"{table} has no columns {missing}"
        cols = list(vals)
        ph = [f"CAST(:{c} AS jsonb)" if types[c] in ("jsonb", "json") else f":{c}" for c in cols]
        params = {c: json.dumps(v, default=str) if types[c] in ("jsonb", "json") else v for c, v in vals.items()}
        s.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(ph)})"), params)


def sql(q: str, **p):
    with normalize.SessionLocal.begin() as s:
        return s.execute(text(q), p)


def market(median=2.5, providers=4):
    return {"median": median, "low": 1.9, "low_provider": "C", "providers": providers, "as_of": NOW.isoformat(),
            "kind": "observed_market_price"}


def cands(*rows):
    return [{"rank": i + 1, "provider": p, "listing_id": f"{p}:h100", "price_per_gpu_hour": price, "score": 1 - i / 10}
            for i, (p, price) in enumerate(rows)]


def route(rr, *, preview=False, account=7, gpu=H100, mode="CHEAPEST", mkt=None, candidates=None, family=None,
          at=None):
    at = at or NOW - timedelta(hours=5)
    put("route_requests", id=rr, account_id=account, key_id=1, principal_kind="api_key", preview=preview, mode=mode,
        gpu=gpu, request={"gpu": gpu, "family": family}, status="previewed" if preview else "routing", created_at=at)
    c = candidates if candidates is not None else cands(("A", 2.0), ("B", 2.2), ("C", 1.9))
    put("routing_decisions", route_request_id=rr, mode=mode, weights={"price": 1}, candidates=c, exclusions=[],
        selected_provider=c[0]["provider"] if c else None, market_snapshot=mkt or market(), methodology_version="t",
        created_at=at)


def deployment(dep, rr, *, provider="A", account=7, status="running", purpose="customer", quote=2.0, actual=2.1,
               gpu_count=2, uptime=3600, ran=True, latency_s=90, attempt_kind=None, interruptions=0,
               termination_reason=None, terminated=False, approved=True, created=None, extra_events=()):
    created = created or NOW - timedelta(hours=4)
    put("deployments", deployment_id=dep, account_id=account, route_request_id=rr, provider=provider, gpu=H100,
        gpu_count=gpu_count, status=status, created_at=created, uptime_seconds=uptime, interruptions=interruptions,
        quoted_price_per_gpu_hour=quote, actual_price_per_gpu_hour=actual, purpose=purpose,
        approved_at=created + timedelta(seconds=30) if approved else None, approved_by="operator" if approved else None,
        termination_reason=termination_reason, terminated_at=created + timedelta(hours=2) if terminated else None)
    t0 = created + timedelta(seconds=60)
    put("provision_attempts", deployment_id=dep, route_request_id=rr, provider=provider, started_at=t0,
        finished_at=t0 + timedelta(seconds=3), latency_ms=3000, ok=attempt_kind is None, error_kind=attempt_kind)
    put("deployment_events", deployment_id=dep, at=created, from_status=None, to_status="created")
    put("deployment_events", deployment_id=dep, at=t0, from_status="approved", to_status="provisioning")
    if ran:
        put("deployment_events", deployment_id=dep, at=t0 + timedelta(seconds=latency_s), from_status="provisioning",
            to_status="running", evidence={"provider_state": "running"})
    for at, frm, to, ev in extra_events:
        put("deployment_events", deployment_id=dep, at=at, from_status=frm, to_status=to, evidence=ev)


def setup():
    global _Session
    if _Session is None:
        _Session = fixtures.session(scratchdb.create(DB))
    normalize.SessionLocal = _Session
    with _Session.begin() as s:
        for t in ("route_outcomes", "routing_decisions", "route_requests", "deployments", "deployment_events",
                  "provision_attempts", "partner_profiles", "deployment_feedback", "usage_records", "quotes",
                  "execution_control_log", "reconciliation_runs", "orphan_resources", "account_limits",
                  "provider_execution_flags", "usage_slices", "execution_records", "idempotency_keys"):
            if obs.has_table(s, t):
                s.execute(text(f"TRUNCATE {t} CASCADE"))
    return _Session


def seed_quality():
    setup()
    route("rr_prev", preview=True)
    route("rr_ok")
    deployment("dep-ok", "rr_ok")
    route("rr_thin", mkt=market(providers=2))
    deployment("dep-thin", "rr_thin", actual=2.4)
    route("rr_fam", family="h100", gpu="h100")
    route("rr_fail")
    deployment("dep-fail", "rr_fail", provider="B", status="provision_failed", ran=False, actual=None,
               attempt_kind="capacity", uptime=0)
    route("rr_val")
    deployment("dep-val", "rr_val", purpose="validation")
    put("partner_profiles", account_id=7, company="Acme", normal_price_per_gpu_hour=3.0, status="active",
        preferred_gpus=[], regions=[])


# ---------------------------------------------------------------- quality

def test_route_outcomes_compute():
    seed_quality()
    r = quality.recompute()
    assert r["computed"] == 6, r
    rows = {o["route_request_id"]: o for o in quality.outcomes(include_validation=True)}
    ok = rows["rr_ok"]
    assert ok["winner_provider"] == "A" and ok["runner_up_provider"] == "B" and ok["candidates_total"] == 3
    assert ok["cheapest_valid_provider"] == "C" and ok["cheapest_valid_price_per_gpu_hour"] == 1.9
    assert ok["market_median_per_gpu_hour"] == 2.5 and ok["comparison_valid"]
    assert ok["expected_savings_pct"] == 20.0, ok["expected_savings_pct"]       # (2.5 - 2.0) / 2.5
    assert abs(ok["realized_savings_pct"] - 16.0) < 1e-6                        # (2.5 - 2.1) / 2.5
    assert abs(ok["quote_error_pct"] - 5.0) < 1e-6                              # (2.1 - 2.0) / 2.0
    assert ok["provisioned"] and ok["launched"] and ok["provisioning_latency_ms"] == 90_000
    assert ok["gpu_hours"] == 2.0 and ok["deployment_provider"] == "A"
    thin = rows["rr_thin"]
    assert not thin["comparison_valid"] and thin["realized_savings_pct"] is None
    assert "median from 2 provider(s)" in thin["comparison_reason"]
    assert "family route" in rows["rr_fam"]["comparison_reason"] and rows["rr_fam"]["deployment_id"] is None
    fail = rows["rr_fail"]
    assert fail["launched"] and fail["provisioned"] is False and fail["provisioning_latency_ms"] is None
    assert rows["rr_prev"]["preview"] and rows["rr_prev"]["final"], "previews never change"
    assert "rr_val" not in {o["route_request_id"] for o in quality.outcomes()}, "validation hidden by default"


def test_quality_summary_math():
    seed_quality()
    quality.recompute()
    s = quality.summary()
    assert s["routes"] == 4 and s["previews"] == 1, s                  # validation excluded
    assert s["routing_success_rate"]["value"] == 0.5                  # ok, thin ran / ok, thin, fam, fail
    assert abs(s["provisioning_success_rate"]["value"] - 2 / 3) < 1e-3
    assert s["realized_savings_vs_median_pct"]["n"] == 1 and s["realized_savings_vs_median_pct"]["mean"] == 16.0
    prev = s["savings_vs_previous_provider_pct"]                     # partner pays 3.0: 30% and 20%
    assert prev["n"] == 2 and abs(prev["mean"] - 25.0) < 1e-6, prev
    assert s["provider_failure_rate"]["B"]["value"] == 1.0 and s["provider_failure_rate"]["A"]["value"] == 0.0
    assert s["quote_accuracy"]["mean_abs_error_pct"]["n"] == 2
    assert s["quote_accuracy"]["within_tolerance"]["value"] == 0.0, "5% and 20% errors are both past 2%"
    assert "median from 2 provider(s) at decision time" in json.dumps(s["no_valid_comparison"])
    full = quality.summary(include_validation=True)
    assert full["routes"] == 5


def test_recompute_idempotent_and_final():
    seed_quality()
    quality.recompute()
    quality.recompute()
    with normalize.SessionLocal() as s:
        assert s.execute(text("SELECT count(*) FROM route_outcomes")).scalar() == 6
    sql("UPDATE deployments SET actual_price_per_gpu_hour = 2.0 WHERE deployment_id = 'dep-ok'")
    quality.recompute()
    ok = next(o for o in quality.outcomes() if o["route_request_id"] == "rr_ok")
    assert ok["actual_price_per_gpu_hour"] == 2.0 and ok["realized_savings_pct"] == 20.0, "non-final rows refresh"
    # terminated > 1 day ago and reconciled: final, so a later change is not picked up without full=True
    sql("UPDATE deployments SET status='terminated', terminated_at=:t, reconciled_at=:t WHERE deployment_id='dep-ok'",
        t=NOW - timedelta(days=2))
    quality.recompute()
    sql("UPDATE deployments SET actual_price_per_gpu_hour = 1.0 WHERE deployment_id = 'dep-ok'")
    quality.recompute()
    ok = next(o for o in quality.outcomes() if o["route_request_id"] == "rr_ok")
    assert ok["final"] and ok["actual_price_per_gpu_hour"] == 2.0
    quality.recompute(full=True)
    ok = next(o for o in quality.outcomes() if o["route_request_id"] == "rr_ok")
    assert ok["actual_price_per_gpu_hour"] == 1.0


def test_usage_slices_drive_gpu_hours():
    seed_quality()
    with normalize.SessionLocal() as s:
        if not obs.has_table(s, "usage_slices"):
            print("   (usage_slices not present; skipped)")
            return
    put("usage_slices", deployment_id="dep-ok", provider="A", gpu=H100, gpu_count=2, period_start=NOW - timedelta(hours=3),
        period_end=NOW - timedelta(hours=2), running_seconds=1800, stopped_seconds=1800, cost_usd=2.1, kind="compute",
        created_at=NOW)
    quality.recompute()
    ok = next(o for o in quality.outcomes() if o["route_request_id"] == "rr_ok")
    assert ok["gpu_hours"] == 1.0, "only running seconds count: 1800 s x 2 GPUs"


# ---------------------------------------------------------------- economics

def test_economics():
    seed_quality()
    quality.recompute()
    e = quality.economics()
    t = e["totals"]
    assert t["with_valid_comparison"] == 1 and t["total_customer_savings_usd"] == 0.8, t   # (2.5-2.1) x 2 GPU-h
    assert t["median_savings_pct"] == 16.0
    assert e["by_provider"]["A"]["n"] == 1 and e["by_gpu"][H100]["total_savings_usd"] == 0.8
    assert e["by_strategy"]["CHEAPEST"]["mean_savings_pct"] == 16.0
    by = {d["deployment_id"]: d for d in e["deployments"]}
    assert by["dep-thin"]["savings_pct"] is None and "3 providers" in by["dep-thin"]["comparison"] or \
        "needs >= 3" in by["dep-thin"]["comparison"]
    assert "never ran" in by["dep-fail"]["comparison"]
    assert "dep-val" not in by, "validation deployments are not customer savings"
    assert quality.economics(account_id=999)["totals"]["status"].startswith("unavailable")


def test_economics_pure_cases():
    base = {"preview": False, "route_request_id": "rr", "gpu": H100, "strategy": "BALANCED", "account_id": 1,
            "market_median_per_gpu_hour": 2.0, "market_providers": 5, "comparison_valid": True,
            "comparison_reason": None, "provisioned": True, "deployment_purpose": "customer"}
    rows = [
        {**base, "deployment_id": "d1", "deployment_provider": "P", "actual_price_per_gpu_hour": None,
         "quoted_price_per_gpu_hour": 1.5, "gpu_hours": 10},                     # quote fallback, labelled
        {**base, "deployment_id": "d2", "deployment_provider": "Q", "actual_price_per_gpu_hour": 2.5,
         "quoted_price_per_gpu_hour": 2.4, "gpu_hours": 4},                       # paid above median: negative
        {**base, "deployment_id": "d3", "deployment_provider": "Q", "actual_price_per_gpu_hour": None,
         "quoted_price_per_gpu_hour": None, "gpu_hours": 1},                      # no price at all
        {**base, "deployment_id": "d4", "deployment_provider": "Q", "actual_price_per_gpu_hour": 1.0,
         "quoted_price_per_gpu_hour": 1.0, "gpu_hours": None},                    # ran, hours not metered yet
    ]
    e = quality.economics_from(rows)
    by = {d["deployment_id"]: d for d in e["deployments"]}
    assert by["d1"]["selected_price_basis"] == "quote" and by["d1"]["savings_usd"] == 5.0
    assert by["d2"]["savings_pct"] == -25.0 and by["d2"]["savings_usd"] == -2.0
    assert "no execution price or quote" in by["d3"]["comparison"]
    assert by["d4"]["savings_pct"] == 50.0 and by["d4"]["savings_usd"] is None and "pending" in by["d4"]["note"]
    assert e["totals"]["total_customer_savings_usd"] == 3.0 and e["totals"]["with_valid_comparison"] == 3


# ---------------------------------------------------------------- reliability

def _dep(provider, kind, i, *, unexpected=False, terminated=False, purpose="customer"):
    t = NOW - timedelta(hours=10) + timedelta(minutes=i)
    ev = [{"at": t, "to_status": "provisioning"}]
    att = [{"started_at": t, "ok": kind == "success", "error_kind": None if kind == "success" else
            {"rejected": "capacity", "timeout": "timeout", "unknown": "server"}[kind]}]
    status = {"success": "running", "rejected": "provision_failed", "timeout": "provider_timeout",
              "unknown": "launch_unknown"}[kind]
    if kind == "success":
        ev.append({"at": t + timedelta(seconds=60 + i), "to_status": "running"})
    if terminated:
        ev += [{"at": t + timedelta(hours=1), "to_status": "terminating"},
               {"at": t + timedelta(hours=1, minutes=1), "to_status": "terminated"}]
        status = "terminated"
    return {"deployment_id": f"d{provider}{i}", "provider": provider, "purpose": purpose, "status": status,
            "created_at": t - timedelta(seconds=30), "approved_at": t - timedelta(seconds=10),
            "termination_reason": "provider_terminated" if unexpected else ("user_requested" if terminated else None),
            "interruptions": 1 if unexpected else 0, "quoted_price_per_gpu_hour": 2.0,
            "actual_price_per_gpu_hour": 2.0 if kind == "success" else None, "events": ev, "attempts": att}


def test_reliability_sample_gating():
    settings.reliability_min_samples = 10
    deps = [_dep("Y", "success", i, terminated=i < 9) for i in range(10)]
    deps[9]["termination_reason"], deps[9]["interruptions"] = "provider_terminated", 1
    deps += [_dep("Y", "rejected", 10), _dep("Y", "timeout", 11)]
    deps += [_dep("X", "success", i) for i in range(3)]
    deps += [_dep("Z", "unknown", 0)]
    r = reliability.compute(deps)
    y, x, z = r["Y"], r["X"], r["Z"]
    assert y["launches"] == 12 and y["successful_launches"]["value"] == round(10 / 12, 4)
    assert y["failed_launches"]["rejected"] == 1 and y["failed_launches"]["timeout"] == 1
    assert y["unexpected_terminations"]["value"] == 0.1 and y["unexpected_terminations"]["n"] == 10
    assert y["termination_success"]["n"] == 9 and y["termination_success"]["value"] is None
    assert "insufficient sample (n=9" in y["termination_success"]["status"]
    assert y["score"]["value"] == round(100 * (10 / 12) * 0.9, 1), y["score"]
    assert y["score"]["factors_skipped_insufficient_sample"] == ["termination_success"]
    assert y["provisioning_latency_ms"]["value"]["p50"] is not None and y["api_error_rate"]["n"] == 12
    assert x["score"]["value"] is None and x["score"]["status"] == "insufficient sample (n=3, need 10)"
    assert x["successful_launches"]["value"] is None, "three successes are not a reliability verdict"
    assert z["launches"] == 1 and z["failed_launches"]["unknown"] == 1
    assert z["successful_launches"]["value"] is None and "insufficient sample (n=1" in z["successful_launches"]["status"]
    assert z["score"]["value"] is None, "one failure is never a verdict"


def test_reliability_classify_and_in_flight():
    d = _dep("Q", "success", 0)
    assert reliability.classify(d) == "success"
    d = {"status": "provisioning", "events": [], "attempts": [{"started_at": NOW, "ok": None}]}
    assert reliability.classify(d) is None, "in flight: no verdict"
    d = {"status": "failed", "events": [], "attempts": [{"started_at": NOW, "ok": False, "error_kind": "auth"}]}
    assert reliability.classify(d) == "rejected"
    d = {"status": "failed", "events": [], "attempts": [{"started_at": NOW, "ok": False, "error_kind": "server"}]}
    assert reliability.classify(d) == "unknown", "a 5xx is ambiguous, not a rejection"


def test_reliability_from_db_excludes_validation():
    seed_quality()
    settings.reliability_min_samples = 1
    try:
        cust = reliability.report()
        assert set(cust["providers"]) == {"A", "B"} and cust["providers"]["A"]["launches"] == 2  # ok + thin
        assert cust["kind"] == "transaction"
        adm = reliability.admin_report()
        assert adm["validation"]["A"]["launches"] == 1 and adm["customer"]["A"]["launches"] == 2
        assert cust["providers"]["B"]["failed_launches"]["rejected"] == 1
    finally:
        settings.reliability_min_samples = 10


# ---------------------------------------------------------------- trace

def test_trace_assembly():
    seed_quality()
    with normalize.SessionLocal() as s:
        has_quotes = obs.has_table(s, "quotes")
    if has_quotes:
        put("quotes", id="q_1", route_request_id="rr_ok", provider="A", listing_id="A:h100", gpu=H100, gpu_count=2,
            quote_price_per_gpu_hour=2.0, est_hourly_cost=4.0, price_source="live_check", status="consumed",
            created_at=NOW - timedelta(hours=4, minutes=1), expires_at=NOW, consumed_by_deployment_id="dep-ok")
    put("usage_records", account_id=7, deployment_id="dep-ok", kind="compute", provider="A", gpu=H100, gpu_count=2,
        period_start=NOW - timedelta(hours=3), period_end=NOW - timedelta(hours=2), gpu_hours=2, provider_cost_usd=4.2,
        created_at=NOW - timedelta(hours=1))
    put("deployment_events", deployment_id="dep-ok", at=NOW - timedelta(hours=2), from_status="running",
        to_status="terminated", evidence={"list": "absent"})
    put("deployment_feedback", deployment_id="dep-ok", account_id=7, price_better=True, created_at=NOW)
    t = obs.trace("rr_ok")
    steps = [x["step"] for x in t["steps"]]
    assert steps[0] == "route_request" and "decision" in steps and "approval" in steps
    order = ["route_request", "decision", "deployment_created", "approval", "provision_attempt",
             "termination", "billing_usage", "feedback"]
    pos = [steps.index(x) for x in order]
    assert pos == sorted(pos), steps
    if has_quotes:
        assert steps.index("quote") < steps.index("deployment_created")
    ats = [x["at"] for x in t["steps"] if x["at"]]
    assert ats == sorted(ats, key=lambda a: datetime.fromisoformat(a)), "time ordered"
    by_dep = obs.trace("dep-ok")
    assert by_dep["focus_deployment_id"] == "dep-ok" and by_dep["route_request_id"] == "rr_ok"
    assert [x["step"] for x in by_dep["steps"]] == steps
    dec = next(x for x in t["steps"] if x["step"] == "decision")
    assert dec["candidates_total"] == 3 and dec["weights"] == {"price": 1}
    assert obs.trace("rr_nope") is None


# ---------------------------------------------------------------- checklist

class FakeAdapter(Adapter):
    LEVEL = 3
    CREDENTIALS = ()
    REQUIRED_LAUNCH = ("ssh_key", "image")
    FAIL = False

    def list_instances(self):
        if FakeAdapter.FAIL:
            raise RuntimeError("401 unauthorized api_key=abcdef1234567890")
        return []


class FakeControl:
    mode = "PREVIEW_ONLY"
    flags = {"adapter_status": "simulated", "supervised_enabled": False, "killed": False}

    @staticmethod
    def effective_mode():
        return FakeControl.mode

    @staticmethod
    def provider_flags(p):
        return dict(FakeControl.flags)


def item(provider="syn_ck", **kw):
    out = checklist.checklist(provider, **kw)
    return out, {i["id"]: i for i in out["items"]}


def test_checklist_transitions():
    setup()
    adapters.register("syn_ck", FakeAdapter)
    saved = {k: getattr(settings, k) for k in ("api_key_pepper", "credentials_encryption_key", "routing_launch_defaults",
                                               "routing_live_provisioning")}
    saved_ctl = checklist._control
    saved_jobs = {k: v for k, v in jobs.JOBS.items() if "reconcil" in k}
    for k in saved_jobs:
        jobs.JOBS.pop(k)
    saved_inst = dict(obs.INSTALLED)
    try:
        settings.api_key_pepper, settings.credentials_encryption_key = None, None
        settings.routing_launch_defaults, settings.routing_live_provisioning = {}, False
        checklist._control = lambda: FakeControl
        FakeControl.mode, FakeControl.flags = "PREVIEW_ONLY", {"adapter_status": "simulated", "supervised_enabled": False}
        obs.INSTALLED.update(middleware=True, json=False)
        out, it = item()
        assert out["overall"] == "red"
        for k in ("api_key_pepper", "credentials_encryption_key", "migrations_at_head", "ssh_key", "launch_params",
                  "execution_mode", "provider_validated", "kill_switch_tested", "termination_tested", "logging_active",
                  "quote_approved"):
            assert it[k]["status"] in ("red", "unknown"), (k, it[k])
        assert it["provider_credential"]["status"] == "green", it["provider_credential"]

        # secrets
        settings.api_key_pepper = "short"
        assert item()[1]["api_key_pepper"]["status"] == "red"
        settings.api_key_pepper = "p" * 48
        assert item()[1]["api_key_pepper"]["status"] == "green"
        settings.credentials_encryption_key = "a passphrase"
        assert item()[1]["credentials_encryption_key"]["status"] == "red"
        from cryptography.fernet import Fernet

        settings.credentials_encryption_key = Fernet.generate_key().decode()
        assert item()[1]["credentials_encryption_key"]["status"] == "green"

        # migrations: the scratch DB came from create_all (no alembic_version) -> red; stamp head -> green
        import db

        sql("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(64) PRIMARY KEY)")
        sql("DELETE FROM alembic_version")
        sql("INSERT INTO alembic_version VALUES (:v)", v=db.head_revision())
        assert item()[1]["migrations_at_head"]["status"] == "green"

        # provider credential probe: a failing read-only call is red, and its error is redacted
        FakeAdapter.FAIL = True
        c = item()[1]["provider_credential"]
        assert c["status"] == "red" and "abcdef1234567890" not in c["evidence"], c
        FakeAdapter.FAIL = False
        assert item(probe=False)[1]["provider_credential"]["status"] == "unknown"

        # launch defaults
        settings.routing_launch_defaults = {"syn_ck": {"ssh_public_key": "ssh-ed25519 AAAA test"}}
        _, it = item()
        assert it["ssh_key"]["status"] == "green" and it["launch_params"]["status"] == "red"
        settings.routing_launch_defaults = {"syn_ck": {"ssh_public_key": "ssh-ed25519 AAAA", "image": "ubuntu-22.04",
                                                       "region": "us-east-1"}}
        assert item()[1]["launch_params"]["status"] == "green"

        # spend limits: an account row is green
        put("account_limits", account_id=7, max_hourly_cost=20, max_gpus=4)
        assert item(account_id=7)[1]["spend_limits"]["status"] == "green"

        # mode + flags
        assert "ROUTING_LIVE_PROVISIONING is false" in item()[1]["execution_mode"]["evidence"]
        settings.routing_live_provisioning = True
        assert item()[1]["execution_mode"]["status"] == "red"
        FakeControl.mode = "LIVE"
        assert "must be SUPERVISED" in item()[1]["execution_mode"]["evidence"]
        FakeControl.mode = "SUPERVISED"
        assert item()[1]["execution_mode"]["status"] == "red", "provider flag still off"
        FakeControl.flags = {"adapter_status": "validated", "supervised_enabled": True, "killed": False,
                             "validated_at": NOW.isoformat(), "validation_deployment_id": "dep-v"}
        _, it = item()
        assert it["execution_mode"]["status"] == "green" and it["provider_validated"]["status"] == "green"
        FakeControl.flags = {**FakeControl.flags, "killed": True}
        assert item()[1]["execution_mode"]["status"] == "red"
        FakeControl.flags = {**FakeControl.flags, "killed": False}

        # kill switch: rows as routing/control.py writes them
        put("execution_control_log", at=NOW - timedelta(days=9), action="kill_all", target="mode",
            after={"mode": "DISABLED"}, reason="old drill", actor="op")
        put("execution_control_log", at=NOW - timedelta(days=9) + timedelta(minutes=5), action="set_mode",
            target="mode", before={"mode": "DISABLED"}, after={"mode": "SUPERVISED"}, reason="old", actor="op")
        assert item()[1]["kill_switch_tested"]["status"] == "red", "older than 7 days"
        put("execution_control_log", at=NOW - timedelta(hours=2), action="set_mode", target="mode",
            before={"mode": "SUPERVISED"}, after={"mode": "DISABLED"}, reason="drill", actor="op")
        assert item()[1]["kill_switch_tested"]["status"] == "red", "kill without un-kill"
        put("execution_control_log", at=NOW - timedelta(hours=1), action="set_mode", target="mode",
            before={"mode": "DISABLED"}, after={"mode": "SUPERVISED"}, reason="drill done", actor="op")
        assert item()[1]["kill_switch_tested"]["status"] == "green"

        # reconciliation worker
        assert item()[1]["reconciliation_running"]["status"] in ("red", "unknown")
        j = jobs.Job("reconcile", lambda: None, 120)
        jobs.JOBS["reconcile"] = j
        j.last_finished, j.last_error, j.runs = NOW - timedelta(seconds=30), None, 5
        put("reconciliation_runs", started_at=NOW - timedelta(seconds=40), finished_at=NOW - timedelta(seconds=30),
            trigger="job", status="ok")
        assert item()[1]["reconciliation_running"]["status"] == "green", item()[1]["reconciliation_running"]
        j.last_error = "RuntimeError: boom"
        assert item()[1]["reconciliation_running"]["status"] == "red"
        j.last_error = None
        j.last_finished = NOW - timedelta(minutes=30)
        put("reconciliation_runs", started_at=NOW - timedelta(seconds=20), finished_at=NOW - timedelta(seconds=10),
            trigger="job", status="failed", error="list failed")
        assert item()[1]["reconciliation_running"]["status"] == "red"
        j.last_finished = NOW
        sql("DELETE FROM reconciliation_runs WHERE status = 'failed'")

        # termination tested
        route("rr_v")
        deployment("dep-v", "rr_v", provider="syn_ck", purpose="validation", status="terminated", terminated=True,
                   extra_events=[(NOW - timedelta(hours=1), "terminating", "terminated", None)])
        assert item()[1]["termination_tested"]["status"] == "red", "no provider confirmation evidence"
        put("deployment_events", deployment_id="dep-v", at=NOW - timedelta(minutes=30), from_status="terminating",
            to_status="terminated", evidence={"list_instances": "absent", "status": "not_found x2"})
        assert item()[1]["termination_tested"]["status"] == "green", item()[1]["termination_tested"]

        # logging
        obs.INSTALLED.update(json=True)
        assert item()[1]["logging_active"]["status"] == "green"

        # quote approval for a route
        route("rr_q")
        assert item(route_request_id="rr_q")[1]["quote_approved"]["status"] == "red"
        put("quotes", id="q_ok", route_request_id="rr_q", provider="syn_ck", listing_id="l", gpu=H100, gpu_count=1,
            quote_price_per_gpu_hour=2.0, est_hourly_cost=2.0, price_source="live_check", status="active",
            created_at=NOW, expires_at=NOW + timedelta(minutes=4))
        assert item(route_request_id="rr_q")[1]["quote_approved"]["status"] == "red", "not approved yet"
        deployment("dep-q", "rr_q", provider="syn_ck", status="approved", ran=False)
        sql("UPDATE deployments SET quote_id = 'q_ok' WHERE deployment_id = 'dep-q'")
        assert item(route_request_id="rr_q")[1]["quote_approved"]["status"] == "green"
        sql("UPDATE quotes SET expires_at = :t, status = 'expired' WHERE id = 'q_ok'", t=NOW - timedelta(minutes=1))
        assert item(route_request_id="rr_q")[1]["quote_approved"]["status"] == "red"
        sql("UPDATE quotes SET expires_at = :t, status = 'active' WHERE id = 'q_ok'", t=NOW + timedelta(minutes=4))

        # alerts: unknown until an ops channel exists -> overall cannot be green
        out, it = item(route_request_id="rr_q", account_id=7)
        assert it["alerts_active"]["status"] == "unknown" and out["overall"] != "green", out
        import alerts

        alerts.ops_channel_configured = lambda: True
        try:
            out, it = item(route_request_id="rr_q", account_id=7)
        finally:
            del alerts.ops_channel_configured
        not_green = [i["id"] for i in out["items"] if i["status"] != "green"]
        assert out["overall"] == "green" and not not_green, not_green
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)
        checklist._control = saved_ctl
        jobs.JOBS.pop("reconcile", None)
        jobs.JOBS.update(saved_jobs)
        obs.INSTALLED.update(saved_inst)
        adapters.unregister("syn_ck")


def test_checklist_kill_parsing():
    assert checklist._is_kill({"action": "kill_provider", "target": "lambda"}, "lambda") is True
    assert checklist._is_kill({"action": "kill_provider", "target": "vast"}, "lambda") is None, "another provider"
    assert checklist._is_kill({"action": "unkill_provider", "target": "lambda"}, "lambda") is False
    assert checklist._is_kill({"action": "set_mode", "target": "mode", "after": {"mode": "DISABLED"}}, "x") is True
    assert checklist._is_kill({"action": "set_mode", "before": {"mode": "DISABLED"}, "after": {"mode": "SUPERVISED"}},
                              "x") is False
    assert checklist._is_kill({"action": "set_provider_flags", "target": "x", "after": {"supervised_enabled": True}},
                              "x") is None


def test_admin_endpoints():
    seed_quality()
    quality.recompute()
    from fastapi.testclient import TestClient

    import main
    from accounts.auth import Principal

    settings.app_password = None
    c = TestClient(main.app)
    c.get("/v1/admin/metrics")  # counters update after a response completes
    m = c.get("/v1/admin/metrics").json()["data"]
    db_ = m["database"]
    assert db_["route_previews"] == 1 and db_["route_launches"] == 4 and db_["launch_failures"] == 1
    assert db_["approvals"] == 4 and db_["deployment_lifecycle_seconds"]["provisioning_to_running"]["n"] == 3
    assert "api_requests" in {x["name"] for x in m["in_process"]["counters"]}
    assert c.get("/v1/admin/trace/rr_ok").json()["data"]["route_request_id"] == "rr_ok"
    assert c.get("/v1/admin/trace/nope").status_code == 404
    assert c.get("/v1/admin/checklist", params={"provider": "lambda", "probe": False}).json()["data"]["overall"] != "green"
    ov = c.get("/v1/admin/execution/overview").json()["data"]
    assert {d["deployment_id"] for d in ov["live_deployments"]} >= {"dep-ok", "dep-val"}
    q = c.get("/v1/admin/quality").json()["data"]
    assert q["summary"]["routes"] == 4
    assert c.get("/v1/economics").json()["data"]["totals"]["total_customer_savings_usd"] == 0.8
    assert c.get("/v1/reliability").json()["meta"]["kind"] == "transaction"
    # a key sees only its own account, and never admin views
    from accounts import auth

    key = Principal(kind="api_key", account_id=8, key_id=9, scopes=frozenset({"deployments:read", "data:read"}))
    main.app.dependency_overrides[auth.principal] = lambda: key
    try:
        assert c.get("/v1/admin/metrics").status_code == 403
        assert c.get("/v1/economics").json()["data"]["totals"]["deployments"] == 0
        assert c.get("/v1/economics", params={"account_id": 7}).status_code == 403
        assert c.get("/v1/quality").json()["data"]["summary"]["routes"] == 0
    finally:
        main.app.dependency_overrides.clear()


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
