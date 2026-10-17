"""OpenGrid Jobs orchestration: preview, submit, cancel, read, readiness, and the launch-time bootstrap render.

    preview(body, who)          workloads.select (the workload-aware selection: market_best vs execution_ready,
                                exclusions, blockers, cost estimate) + what a submission would do now
    submit(body, who)           validate -> select -> sandbox: a SIMULATED run (no provider, labelled simulated
                                everywhere) | real: requires an execution_ready candidate (refused with the exact
                                blockers otherwise; never faked) -> job row -> ephemeral SSH key if the customer
                                gave none -> a quote for exactly the selected listing (the existing preview ->
                                quote pattern) -> the EXISTING engine.route(quote_id=...) (customer purpose) so
                                the existing re-validation, cost guards, pending_approval, idempotency and
                                single-launch token apply -> job pending_approval with route_request_id /
                                deployment_id / quote_id. Approval is the existing POST /v1/route/{rr}/approve.
    cancel(job_id, who)         before launch: the existing engine.reject; after: cancel_requested -> the existing
                                deployments.terminate (actor system) -> job_sync follows to confirmed termination
    resume_from                 submit/preview pin {job_id, artifact, epoch, sha256, size} via
                                jobs/artifacts.resolve_resume (same account, stored, sha256 re-verified); the
                                bootstrap then downloads exactly that checkpoint (runner/resume) before training
    submit(..., chain=)         jobs/relaunch.py only: the SAME submit path for an automatic relaunch; the new job row
                                carries retry_of / root_job_id / attempt and UNIQUE(retry_of) refuses a second
                                successor (relaunch_exists) before anything is quoted or routed
    render_startup(d, spec)     called by routing/deployments.launch under the deployment row lock, right before
                                the provider call: replaces the placeholder startup_script "opengrid-job:<job_id>"
                                with runner.bootstrap.render(job_id, <derived token>, ...). The script and token
                                are never stored or logged: only sha256 + template version land on the job.
Nothing here calls a provider directly; every provider call is the existing engine / deployments code.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select

import normalize
from config import settings
from jobs import models as M
from jobs import states as S
from store.jobs import Job, JobArtifact, JobEvent, JobLog, JobMetric

log = logging.getLogger(__name__)

PLACEHOLDER = "opengrid-job:"
METHODOLOGY = "jobs"


class JobRefused(Exception):
    """A refusal the API returns as HTTP `status` with {code, message, ...}."""

    def __init__(self, code: str, message: str, status: int = 422, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return "job-" + secrets.token_hex(8)


def _actor(who) -> tuple[str, str]:
    aid = f"key:{who.key_id}" if who.key_id is not None else (who.kind or "operator")
    return ("admin" if who.has("admin") else "user"), aid


def _workload_error(exc) -> JobRefused:
    return JobRefused(exc.code, exc.message, getattr(exc, "status", 422), **({"field": exc.field} if exc.field else {}),
                      **(getattr(exc, "extra", None) or {}))


def _pin_resume(v, who) -> None:
    """resume_from -> the pinned, verified checkpoint {job_id, artifact, epoch, sha256, size} (same account only)."""
    ref = v.request.get("resume_from")
    if not ref:
        return
    from jobs import artifacts as A
    try:
        v.request["resume_from"] = A.resolve_resume(who.account_id, ref, epochs=int(v.request["config"]["epochs"]))
    except A.ArtifactError as exc:
        raise JobRefused(exc.code, exc.message, exc.status, field="resume_from") from None


# --------------------------------------------------------------------------
# Templates / readiness
# --------------------------------------------------------------------------

def templates() -> list[dict]:
    from workloads import templates as T
    out = []
    for t in T.list_templates():
        t["expected_runtime_minutes"] = list(T.expected_minutes(t, T.config_defaults(t)))
        try:
            from runner import bootstrap
            t["bootstrap_template_version"] = bootstrap.TEMPLATE_VERSION if t["id"] in bootstrap.TEMPLATES else None
        except Exception:  # noqa: BLE001
            t["bootstrap_template_version"] = None
        out.append(t)
    return out


def _callback_base() -> tuple[str, str | None]:
    base = (settings.job_callback_base_url or settings.public_base_url or "").rstrip("/")
    if not base:
        return base, "no JOB_CALLBACK_BASE_URL / PUBLIC_BASE_URL: the GPU could not call back"
    if not base.startswith("https://"):
        from accounts.security import deployed
        if deployed() or not base.startswith(("http://localhost", "http://127.0.0.1")):
            return base, f"callback base URL {base} is not https: the runner token would cross the network in clear"
    return base, None


def readiness(who) -> dict:
    """What blocks a REAL job run right now, item by item (never a provider call)."""
    from routing import adapters, control, guards
    from workloads import select as W

    items: list[dict] = []

    def add(code, ok, detail, *, required=True, **extra):
        items.append({"code": code, "ok": bool(ok), "required": required, "detail": detail, **extra})

    mode = control.effective_mode()
    add("env_live_provisioning", settings.routing_live_provisioning,
        "ROUTING_LIVE_PROVISIONING is on" if settings.routing_live_provisioning else
        "ROUTING_LIVE_PROVISIONING is off in this environment: the effective mode is capped at PREVIEW_ONLY")
    add("execution_mode", mode in (control.SUPERVISED, control.LIVE),
        f"effective execution mode {mode}" + ("" if mode in (control.SUPERVISED, control.LIVE)
                                              else ": an admin must set SUPERVISED (POST /v1/admin/execution/mode)"),
        mode=mode)
    provs = []
    for p in sorted(adapters.ADAPTERS) if hasattr(adapters, "ADAPTERS") else []:
        if adapters.level(p) < 2:
            continue
        try:
            r = W.provider_readiness(p, who, mode, None)
        except Exception as exc:  # noqa: BLE001
            r = {"provider": p, "reasons": [{"code": "readiness_error", "reason": type(exc).__name__}],
                 "approval_mode": None, "flags": None, "credential_source": None}
        provs.append({"provider": p, "ready": not r["reasons"] and mode in (control.SUPERVISED, control.LIVE),
                      "approval_mode": r.get("approval_mode"), "reasons": r["reasons"], "flags": r.get("flags"),
                      "credential_source": r.get("credential_source")})
    ready_provs = [p["provider"] for p in provs if p["ready"]]
    add("provider_ready", bool(ready_provs),
        ("execution-ready providers: " + ", ".join(ready_provs)) if ready_provs else
        "no provider is execution-ready (validated adapter + supervised enabled + credentials + startup script + "
        "per-deployment SSH key + not killed); see providers[]")
    for name, every in (("job_sync", settings.job_sync_interval_seconds),
                        ("routing_tracker", settings.tracker_interval_seconds),
                        ("reconcile", settings.reconcile_interval_seconds)):
        h = control.job_health(name, every)
        add(f"worker_{name}", h["ok"], h["reason"])
    try:
        ops_ok = control.ops_channel_configured()
    except Exception:  # noqa: BLE001
        ops_ok = False
    add("ops_alerts", ops_ok, "ops alert channel configured" if ops_ok else
        "OPS_ALERT_WEBHOOK_URL / secret not configured: failed terminations would page nobody")
    base, problem = _callback_base()
    add("callback_url", problem is None, problem or f"runner callbacks go to {base}/v1/jobs/<id>/runner/...")
    try:
        from runner import bootstrap
        add("bootstrap", True, f"runner bootstrap {bootstrap.TEMPLATE_VERSION} (bundle {bootstrap.BUNDLE_SHA256[:12]})",
            template_version=bootstrap.TEMPLATE_VERSION)
    except Exception as exc:  # noqa: BLE001
        add("bootstrap", False, f"runner.bootstrap unavailable ({type(exc).__name__}): no job can bootstrap")
    from jobs import artifacts as A
    h = A.health()
    add("artifact_store", h["ok"], h.get("reason") or f"artifact store {h['backend']} ok (max "
                                                      f"{settings.job_artifact_max_mb} MB per artifact)",
        backend=h.get("backend"))
    try:
        from jobs import ephemeral_ssh
        ephemeral_ssh.generate()
        add("ssh_key", True, "no customer key needed: an ephemeral key is generated per job and its private half "
                             "discarded (nobody can SSH in); or send ssh_public_key", required=False)
    except Exception as exc:  # noqa: BLE001
        add("ssh_key", False, f"ephemeral key generation failed ({type(exc).__name__}): send ssh_public_key")
    try:
        lim, use = guards.limits_for(who.account_id), guards.usage(who.account_id)
        act, cap = use.get("active_deployments"), lim.get("max_active_deployments")
        ok = cap is None or act is None or act < cap
        add("budget_active_deployments", ok, f"{act} active deployment(s) of {cap} allowed",
            limits={k: lim.get(k) for k in ("max_price_per_gpu_hour", "max_hourly_cost", "max_total_cost",
                                            "max_active_deployments", "monthly_spend_limit", "max_runtime_minutes")},
            usage={k: use.get(k) for k in ("active_deployments", "active_gpus", "hourly_burn", "month_spend")})
    except Exception as exc:  # noqa: BLE001
        add("budget_active_deployments", False, f"cost guards unreadable ({type(exc).__name__})")
    blockers = [i for i in items if i["required"] and not i["ok"]]
    return {"ready": not blockers, "execution_mode": mode,
            "live_provisioning_enabled": bool(settings.routing_live_provisioning),
            "blockers": [{"code": b["code"], "reason": b["detail"]} for b in blockers],
            "checks": items, "providers": provs, "operator_view": who.account_id is None,
            "note": "a real job needs every required check ok; a sandbox job (sandbox: true) needs none of them"}


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------

# Selection blocker codes -> the job refusal code (most actionable first).
_REFUSAL = (
    (("env_ceiling", "mode_disabled", "mode_preview_only"), "execution_disabled",
     "real execution is disabled in this environment"),
    (("provider_killed",), "provider_killed", "the provider's kill switch is on"),
    (("adapter_not_validated",), "provider_unvalidated", "no compatible provider has a validated adapter yet"),
    (("supervised_not_enabled", "provider_not_enabled", "launch_not_permitted"), "execution_disabled",
     "supervised launches are not enabled for the compatible providers"),
    (("no_credentials",), "credentials_unavailable", "no usable provider credentials"),
    (("no_compatible_gpu",), "no_suitable_gpu", "no GPU model meets the template's hardware requirements"),
    (("no_eligible_listing",), "no_eligible_provider", "no listing satisfies the workload right now"),
)


def refusal_for(sel: dict) -> JobRefused:
    blockers = sel.get("blockers") or []
    codes = {b.get("code") for b in blockers}
    by_code = sel.get("exclusions_by_code") or {}
    for keys, code, label in _REFUSAL:
        if codes & set(keys):
            break
    else:
        code, label = "no_eligible_provider", "no candidate OpenGrid can run now"
    if code == "no_eligible_provider" and by_code.get("over_budget") and sel.get("market_best") is None:
        code, label = "budget_exceeded", "every compatible listing costs more than max_total_cost_usd"
    msg = label + ": " + "; ".join(b.get("reason", "") for b in blockers[:6]) if blockers else label
    return JobRefused(code, msg, 422, blockers=blockers, market_best=_brief(sel.get("market_best")),
                      exclusions_by_code=by_code,
                      hint="sandbox: true runs a SIMULATED job with no provider (labelled simulated)")


def _brief(c: dict | None) -> dict | None:
    if not c:
        return None
    return {k: c.get(k) for k in ("provider", "gpu", "gpu_slug", "listing_id", "region", "price_per_gpu_hour",
                                  "price_kind", "executable", "why")} | {
        "est_max_total": (c.get("cost") or {}).get("est_max_total"),
        "readiness_reasons": (c.get("readiness") or {}).get("reasons")}


def _select(body, who) -> dict:
    from workloads.select import select
    from workloads.spec import WorkloadError
    try:
        return select(body, who)
    except WorkloadError as exc:
        raise _workload_error(exc) from None


def _public_request(req: dict, fp: str | None) -> dict:
    r = dict(req)
    r.pop("ssh_public_key", None)
    r["ssh_key_fingerprint"] = fp
    return r


def preview(body, who) -> dict:
    sel = _select(body, who)
    from workloads.spec import validate
    v = validate(body)
    _pin_resume(v, who)
    out = dict(sel)
    out["request"] = _public_request(sel["request"], v.ssh_key_fingerprint)
    if v.request.get("resume_from"):
        out["request"]["resume_from"] = v.request["resume_from"]
    sandbox = bool(sel["request"].get("sandbox"))
    if sandbox:
        sub = {"can_submit": True, "would": "simulated",
               "note": "SIMULATED: no provider is called; events, metrics and artifacts are labelled simulated"}
    elif sel.get("execution_ready"):
        er = sel["execution_ready"]
        sub = {"can_submit": True, "would": "pending_approval" if er["readiness"]["approval_mode"] != "LIVE"
               else "launch", "approval_mode": er["readiness"]["approval_mode"],
               "note": "POST /v1/jobs quotes this listing, re-validates it live and waits for admin approval "
                       "(POST /v1/route/{route_request_id}/approve) before anything is launched or billed"}
    else:
        r = refusal_for(sel)
        sub = {"can_submit": False, "would": "refused", "refusal": r.detail()}
    sub["ssh"] = ({"mode": "customer", "fingerprint": v.ssh_key_fingerprint} if v.ssh_key_fingerprint else
                  {"mode": "ephemeral", "note": "no ssh_public_key: an ephemeral key is generated at submission and "
                                                "its private half discarded; nobody (including OpenGrid) can SSH in"})
    if v.request.get("retry"):
        from jobs import relaunch
        sub["retry"] = relaunch.policy_preview(v.request)
    out["submission"] = sub
    return out


# --------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------

def _offer_for(provider: str, listing_id: str, want_region_group: str | None):
    from routing.adapters.base import Offer
    from tables import ComputeListingRow
    with normalize.SessionLocal() as s:
        row = s.get(ComputeListingRow, (provider, listing_id))
    if row is None or row.price_per_gpu_hour is None:
        raise JobRefused("gpu_unavailable", f"listing {provider}:{listing_id} is no longer listed; preview again", 409)
    price = float(row.price_per_gpu_hour)
    return Offer(provider=provider, listing_id=listing_id, sku=row.sku, raw_gpu_name=row.raw_gpu_name,
                 gpu=row.canonical_gpu_name, gpu_count=row.gpu_count, region=row.region, price_per_gpu_hour=price,
                 price_per_instance_hour=float(row.price_per_instance_hour or price * row.gpu_count),
                 provider_tier=row.provider_tier, want_region_group=want_region_group)


def _selection_record(sel: dict, chosen: dict | None) -> dict:
    mb = sel.get("market_best")
    c = chosen or mb or {}
    return {"provider": c.get("provider"), "gpu": c.get("gpu"), "gpu_slug": c.get("gpu_slug"),
            "listing_id": c.get("listing_id"), "region": c.get("region"),
            "price_per_gpu_hour": c.get("price_per_gpu_hour"), "price_kind": c.get("price_kind"),
            "score": c.get("score"), "why": c.get("why"), "cost": c.get("cost"), "readiness": c.get("readiness"),
            "factors": c.get("factors"), "explanation": c.get("explanation"),
            "market_best": _brief(mb), "alternatives": [_brief(a) for a in (sel.get("alternatives") or [])],
            "exclusions_total": sel.get("exclusions_total"), "exclusions_by_code": sel.get("exclusions_by_code"),
            "blockers": sel.get("blockers"), "estimate": sel.get("estimate"), "execution_mode": sel.get("execution_mode"),
            "strategy": sel.get("strategy"), "as_of": sel.get("as_of"),
            "market_data_freshness": sel.get("market_data_freshness"), "workload_market": sel.get("workload_market"),
            "template": sel.get("template"), "methodology": sel.get("methodology")}


def _create_job(who, v, sel, chosen, *, ssh: dict, sandbox: bool, chain: dict | None = None) -> str:
    from sqlalchemy.exc import IntegrityError

    from workloads import templates as T

    job_id = new_job_id()
    now = _now()
    req = v.request
    cost = (chosen or sel.get("market_best") or {}).get("cost") or {}
    actor, actor_id = _actor(who)
    ev = {"template": req["template"], "template_version": v.template.get("version"),
          "expected_runtime_minutes": list(T.expected_minutes(v.template, req["config"]))}
    reason = "SIMULATED sandbox job" if sandbox else "job submitted"
    if chain:
        actor, actor_id = "system", f"relaunch:{chain['retry_of']}"[:64]
        reason = chain.get("reason") or f"attempt {chain['attempt']}: automatic relaunch of {chain['retry_of']}"
        ev["relaunch"] = {k: chain.get(k) for k in ("retry_of", "root_job_id", "attempt", "max_attempts")}
        ev["relaunch_event"] = "relaunched_from"
    try:
        with normalize.SessionLocal.begin() as s:
            job = Job(job_id=job_id, account_id=who.account_id, key_id=who.key_id, principal_kind=who.kind,
                      name=req["name"], job_type=req["job_type"], template=req["template"],
                      template_version=v.template.get("version"), config=req["config"],
                      spec=_public_request(req, ssh.get("fingerprint")), status="created", sandbox=sandbox,
                      selection=M_json(_selection_record(sel, chosen)),
                      est_max_total_usd=_dec(cost.get("est_max_total")), est_typical_total_usd=_dec(cost.get("est_typical_total")),
                      runtime_ceiling_minutes=int((sel.get("runtime") or {}).get("effective_max_runtime_minutes")
                                                  or req["limits"]["max_runtime_minutes"]),
                      ssh_key_mode=ssh["mode"], ssh_key_fingerprint=ssh.get("fingerprint"), created_at=now, updated_at=now,
                      state_changed_at=now, evidence=M.blank_evidence())
            if chain:
                job.retry_of, job.root_job_id, job.attempt = chain["retry_of"], chain["root_job_id"], int(chain["attempt"])
            s.add(job)
            s.flush()     # UNIQUE(retry_of): a second successor of the same failed attempt stops here
            s.add(JobEvent(job_id=job_id, at=now, from_status=None, to_status="created", actor=actor, actor_id=actor_id,
                           reason=reason, evidence=ev))
            S.transition(s, job, "validating", actor="system", reason="request validated against the template",
                         evidence={"requirements": M_json(v.requirements), "notes": v.notes})
    except IntegrityError:
        if not chain:
            raise
        raise JobRefused("relaunch_exists", f"job {chain['retry_of']} already has a successor (one relaunch per "
                                            "failed attempt)", 409) from None
    return job_id


def M_json(v):
    return S._jsonable(v)


def _dec(v):
    return None if v is None else Decimal(str(round(float(v), 4)))


def submit(body, who, *, chain: dict | None = None) -> tuple[int, dict]:
    """chain: jobs/relaunch.py only ({retry_of, root_job_id, attempt, max_attempts, reason}); everything else is the
    one submit path every job takes."""
    from routing import engine
    from workloads.spec import WorkloadError, validate

    try:
        v = validate(body)
    except WorkloadError as exc:
        raise _workload_error(exc) from None
    _pin_resume(v, who)
    sel = _select(v, who)
    sandbox = bool(v.request["sandbox"])
    if chain and sandbox:
        raise JobRefused("relaunch_not_real", "a sandbox job is never relaunched", 422)
    if sandbox:
        ssh = {"mode": "none", "fingerprint": None}
        job_id = _create_job(who, v, sel, sel.get("execution_ready") or sel.get("market_best"), ssh=ssh, sandbox=True)
        run_simulated(job_id)
        return 201, get(job_id, who)
    ready = sel.get("execution_ready")
    if not ready:
        raise refusal_for(sel)
    if ready.get("limit_violations"):
        lv = ready["limit_violations"]
        if not all(x.get("overridable") for x in lv):
            raise JobRefused("budget_exceeded", "account cost guards refuse this job: " +
                             "; ".join(x.get("message", x.get("code", "")) for x in lv), 422, limit_violations=lv)
    engine._check_account_active(who)
    if v.request.get("ssh_public_key"):
        ssh = {"mode": "customer", "public_key": v.request["ssh_public_key"], "fingerprint": v.ssh_key_fingerprint}
    else:
        from jobs import ephemeral_ssh
        ssh = ephemeral_ssh.generate()
    job_id = _create_job(who, v, sel, ready, ssh=ssh, sandbox=False, chain=chain)
    try:
        return _route(job_id, who, v, sel, ready, ssh)
    except JobRefused as r:
        _fail_submission(job_id, r)
        raise JobRefused(r.code, r.message, r.status, job_id=job_id, **r.extra) from None
    except HTTPException as exc:
        r = _from_engine(exc)
        _fail_submission(job_id, r)
        raise JobRefused(r.code, r.message, r.status, job_id=job_id, **r.extra) from None


def _from_engine(exc: HTTPException) -> JobRefused:
    d = exc.detail if isinstance(exc.detail, dict) else {"code": "route_refused", "message": str(exc.detail)}
    code, msg = d.get("code") or "route_refused", d.get("message") or str(d)
    extra = {k: v for k, v in d.items() if k not in ("code", "message")}
    mapped = code
    if code == "quote_invalid" and "price moved" in msg:
        mapped = "price_changed"
    elif code == "quote_invalid" and "no longer available" in msg:
        mapped = "gpu_unavailable"
    elif code == "over_max_price":
        mapped = "price_changed"
    elif code in ("account_suspended", "account_unavailable"):
        mapped = code
    hint = {"price_changed": "the live price moved since the preview: preview again and resubmit",
            "gpu_unavailable": "the listing sold out on the live check: preview again for the next best candidate",
            "quote_expired": "the quote expired: preview again and resubmit"}.get(mapped)
    return JobRefused(mapped, msg, exc.status_code if exc.status_code >= 400 else 409, engine_code=code,
                      **({"hint": hint} if hint else {}), **{k: v for k, v in extra.items() if k in (
                          "new_quote", "failed", "violations")})


def _fail_submission(job_id: str, r: JobRefused) -> None:
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        if job is None or S.is_finished(job):
            return
        if job.deployment_id is not None:   # a deployment exists: job_sync owns the state from here
            S.note(s, job, f"submission error after the route created deployment {job.deployment_id}: {r.message}")
            return
        to = "validation_failed" if r.code in ("launch_spec_invalid",) else "no_eligible_provider"
        S.transition(s, job, to, actor="system", reason=f"{r.code}: {r.message}", outcome="failed",
                     failure_code=r.code, evidence={"code": r.code})


def _route(job_id: str, who, v, sel, ready: dict, ssh: dict) -> tuple[int, dict]:
    from routing import adapters, audit, engine, quotes

    req = v.request
    rs = dict(ready["route_spec"])
    listing_id = rs.pop("listing_id")
    cost = ready["cost"]
    billed_h = max(float(cost["billed_minutes"]) / 60.0, 1e-9)
    budget = float(req["limits"]["max_total_cost_usd"])
    user_max = float(req["limits"]["max_price_per_gpu_hour_usd"])
    # The total budget binds through the EXISTING max-price cap: approval re-validates the live price and refuses
    # above this, so quote x billed runtime can never exceed max_total_cost_usd.
    budget_cap = budget / (int(rs["count"]) * billed_h)
    max_price = round(min(user_max, budget_cap) - 5e-7, 6)
    if max_price < float(ready["price_per_gpu_hour"]) - 1e-9:
        raise JobRefused("budget_exceeded", f"${ready['price_per_gpu_hour']:.4f}/GPU-h x {rs['count']} GPU x "
                                            f"{billed_h:g} h billed exceeds max_total_cost_usd ${budget:.2f}", 422)
    offer = _offer_for(ready["provider"], listing_id, rs.get("region_group"))
    # The existing preview -> quote -> route(quote_id) pattern: a persisted preview route request + quote for
    # exactly the selected listing; the route re-validates it live before any deployment exists.
    ranking = {"mode": rs["mode"], "weights": sel.get("weights") or {}, "candidates": [
        {"rank": 1, "provider": offer.provider, "listing_id": offer.listing_id, "observed_at": ready.get("observed_at"),
         "price_per_gpu_hour": offer.price_per_gpu_hour, "gpu": offer.gpu, "job_id": job_id}],
        "multi_instance_alternatives": [], "exclusions": [], "market": sel.get("workload_market") or {},
        "as_of": sel.get("as_of")}
    preview_rr = audit.open_request(who, {"gpu": offer.gpu, "job_id": job_id, "purpose": "job_selection",
                                          "selection_listing": f"{offer.provider}:{offer.listing_id}"},
                                    ranking, preview=True)
    q = quotes.issue(route_request_id=preview_rr, account_id=who.account_id, offer=offer,
                     observed_price=offer.price_per_gpu_hour, quote_price=offer.price_per_gpu_hour,
                     price_source="observed", duration_hours=round(billed_h, 6), region_group=rs.get("region_group"),
                     adapter_cls=adapters.get(offer.provider), purpose="customer",
                     credential_source=(ready.get("readiness") or {}).get("credential_source"))
    audit.close_request(preview_rr, "previewed", {"job_id": job_id, "quote_id": q["quote_id"]})
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        job.quote_id = q["quote_id"]
        S.transition(s, job, "quoted", actor="system",
                     reason=f"quote {q['quote_id']} for {offer.provider}:{offer.listing_id} (observed price; the route "
                            "re-validates it live)",
                     evidence={"quote_id": q["quote_id"], "quote_price_per_gpu_hour": q["quote_price_per_gpu_hour"],
                               "est_total_cost": q["est_total_cost"], "expires_at": q["expires_at"],
                               "max_price_per_gpu_hour": max_price, "preview_route_request_id": preview_rr})
    spec = {"gpu": offer.gpu, "family": None, "variants": None, "count": int(rs["count"]),
            "region_group": rs.get("region_group"), "strict_region": bool(rs.get("strict_region")),
            "max_price_per_gpu_hour": max_price, "duration_hours": round(billed_h, 6), "deadline_hours": None,
            "mode": rs["mode"], "weights": rs.get("weights"), "preferences": rs.get("preferences") or {},
            "launch": {"ssh_public_key": ssh["public_key"], "startup_script": PLACEHOLDER + job_id},
            "quote_id": q["quote_id"], "max_runtime_minutes": int(rs["max_runtime_minutes"]), "job_id": job_id}
    code, out = engine.route(spec, who)
    dep = out.get("deployment") or {}
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        job.route_request_id = out.get("route_request_id")
        if dep.get("deployment_id"):
            job.deployment_id = job.deployment_id or dep["deployment_id"]
        S.note(s, job, f"routed: {out.get('status')}" + (f" ({out.get('reason')})" if out.get("reason") else ""),
               {"route_request_id": out.get("route_request_id"), "deployment_id": dep.get("deployment_id"),
                "route_status": out.get("status"), "quote_id": q["quote_id"]})
    if not dep.get("deployment_id"):
        raise JobRefused("execution_disabled", f"the route did not create a deployment: {out.get('reason') or out.get('status')}",
                         422, route_request_id=out.get("route_request_id"))
    from jobs import sync
    sync.sync_job(job_id, enforce=False)
    res = get(job_id, who)
    res["route"] = {"route_request_id": out.get("route_request_id"), "status": out.get("status"),
                    "reason": out.get("reason"), "http_code": code, "approval": out.get("approval"),
                    "runtime": out.get("runtime"), "ssh_access": out.get("ssh_access")}
    return 202, res


# --------------------------------------------------------------------------
# Sandbox: the SIMULATED run (same ingestion code, source='simulated')
# --------------------------------------------------------------------------

def simulated_report(job_id: str, info: dict, epochs: int, note: str, took_seconds: float) -> dict:
    """The sandbox artifact: what the job was, what the simulation stood in for, and what a real run would take.
    Every number from the run itself is a placeholder; the real-run figures are the template's limits and the
    measured typical MNIST timings (docs/FIRST_TRAINING_JOB.md), labelled as estimates."""
    limits = info.get("limits") or {}
    cfg = info.get("config") or {}
    return {
        "simulated": True,
        "summary": (f"Free practice run of '{info.get('name')}' ({info.get('template')}): {epochs} epoch(s) simulated in "
                    + ("under a second" if took_seconds < 1 else f"{took_seconds:.0f} seconds") + ". No GPU was rented, no provider was called and nothing was charged. "
                    "The accuracy numbers below are placeholders, not a trained model."),
        "job_id": job_id,
        "job": {"name": info.get("name"), "template": info.get("template"),
                "template_version": info.get("template_version"), "settings": cfg, "submitted_at": info.get("submitted_at")},
        "duration": {"this_run_seconds": round(took_seconds, 2),
                     "note": "a simulation finishes almost instantly because no machine is started"},
        "steps": [
            {"step": "rent a GPU", "real_run": "OpenGrid launches the cheapest approved GPU within your price cap",
             "this_run": "skipped (simulated)"},
            {"step": "set up the machine", "real_run": "installs PyTorch and checks the GPU works", "this_run": "skipped (simulated)"},
            {"step": "train", "real_run": f"trains a small digit-recognition network on MNIST for {epochs} epoch(s)",
             "this_run": "placeholder numbers written for each epoch"},
            {"step": "save the model", "real_run": "uploads model.pt, config.json, metrics.json, env.json and manifest.json",
             "this_run": "only this report is saved"},
            {"step": "shut down and settle", "real_run": "terminates the GPU, confirms it is gone and reconciles the actual cost",
             "this_run": "nothing to shut down; cost $0"},
        ],
        "placeholder_metrics": [{"epoch": e, "train_loss": round(1.0 / e, 4), "test_accuracy": round(min(0.5 + 0.1 * e, 0.9), 4)}
                                for e in range(1, epochs + 1)],
        "a_real_run_would": {
            "take": "about 6 to 15 minutes in total (mostly setup; training itself about 1 minute), estimate",
            "reach": "about 99% test accuracy after 3 epochs, estimate from a local run with the same code",
            "cost_at_most_usd": limits.get("max_total_cost_usd"),
            "price_cap_per_gpu_hour_usd": limits.get("max_price_per_gpu_hour_usd"),
            "stop_after_minutes": limits.get("max_runtime_minutes"),
            "produce": ["model.pt", "config.json", "metrics.json", "env.json", "manifest.json"],
            "needs": "an admin to approve the price quote before any GPU launches",
        },
        "note": note,
    }


def run_simulated(job_id: str) -> None:
    import json

    from jobs import artifacts as A
    from jobs import sync

    with normalize.SessionLocal() as s:
        job = s.get(Job, job_id)
        epochs = int((job.config or {}).get("epochs") or 1)
        every = int((job.config or {}).get("checkpoint_every_epochs") or 0)
        resume = (job.spec or {}).get("resume_from") or None
        info = {"name": job.name, "template": job.template, "template_version": job.template_version,
                "config": dict(job.config or {}), "limits": dict((job.spec or {}).get("limits") or {}),
                "submitted_at": job.created_at.isoformat() if job.created_at else None}
    t0 = _now()
    seq = [0]

    def ev(typ, **data):
        seq[0] += 1
        return {"seq": seq[0], "type": typ, "at": (t0 + timedelta(seconds=seq[0])).isoformat(),
                "data": {"simulated": True, **data}}

    note = "SIMULATED sandbox run: no provider was called and no GPU exists; values are placeholders"
    events = [ev("phase", phase="bootstrap_started", note=note),
              ev("log", stream="simulated", line="[SIMULATED] " + note),
              ev("phase", phase="torch_installed", torch="simulated", env_source="simulated"),
              ev("phase", phase="cuda_available", device="simulated", gpu_name=None)]
    start = 1
    if resume:   # the pinned checkpoint is real, but a simulation never downloads or loads it
        start = int(resume["epoch"]) + 1
        events.append(ev("phase", phase="resumed", from_epoch=start - 1, next_epoch=start, of=epochs,
                         from_job_id=resume["job_id"], artifact=resume["artifact"],
                         note="SIMULATED: the checkpoint was verified at submission but not loaded"))
    events.append(ev("phase", phase="training_started", device="simulated", start_epoch=start))
    for e in range(start, epochs + 1):
        events.append(ev("metric", epoch=e, train_loss=round(1.0 / e, 4), test_accuracy=round(min(0.5 + 0.1 * e, 0.9), 4),
                         batch_device="simulated"))
        events.append(ev("phase", phase="epoch_completed", epoch=e, of=epochs))
        if every and e < epochs and e % every == 0:
            # the event only: a sandbox stores no checkpoint bytes (only the runner may), so nothing is resumable
            events.append(ev("phase", phase="checkpoint_saved", epoch=e, name=f"ckpt-e{e:04d}.pt", stored=False))
    events += [ev("phase", phase="saving_artifacts"),
               ev("result", outcome="succeeded", params_changed=False, note=note)]
    sync.ingest(job_id, events, source="simulated")
    report = json.dumps(simulated_report(job_id, info, epochs, note, (_now() - t0).total_seconds()), indent=2).encode()
    meta = A.store().put(job_id, "simulated-report.json", report, hashlib.sha256(report).hexdigest(),
                         content_type="application/json", source="simulated")
    sync.on_artifact(job_id, meta)
    sync.complete(job_id, {"outcome": "succeeded", "summary": {"simulated": True, "note": note}}, source="simulated")
    with normalize.SessionLocal.begin() as s:
        job = s.get(Job, job_id, with_for_update=True)
        for k in ("instance_created", "gpu_terminated", "termination_verified", "runtime_initialized",
                  "gpu_detected", "params_updated", "real_metrics", "training_ran", "torch_installed",
                  "cuda_available"):
            M.set_check(job, k, "simulated", {"note": "sandbox: nothing real happened"}, "simulated", overwrite=True)
        if job.finished_at is None:
            job.finished_at = _now()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _load(job_id: str, who) -> Job:
    with normalize.SessionLocal() as s:
        job = s.get(Job, job_id)
    if job is None or (who is not None and who.account_id is not None and job.account_id != who.account_id):
        raise JobRefused("job_not_found", "job not found", 404)
    return job


def _dep(job):
    if not job.deployment_id:
        return None
    from store.routing import Deployment
    with normalize.SessionLocal() as s:
        return s.get(Deployment, job.deployment_id)


def get(job_id: str, who, *, sync_first: bool = False) -> dict:
    _load(job_id, who)
    if sync_first:
        from jobs import sync
        try:
            sync.sync_job(job_id, enforce=False)   # DB only: never a provider call on a read
        except Exception:  # noqa: BLE001
            log.exception("job %s: sync on read failed", job_id)
    job = _load(job_id, who)
    dep = _dep(job)
    quote = None
    if dep is not None and dep.quote_id:
        from routing import quotes
        quote = quotes.get(dep.quote_id)
    from jobs import artifacts as A
    with normalize.SessionLocal() as s:
        evs = [{"at": e.at.isoformat(), "from": e.from_status, "to": e.to_status, "actor": e.actor,
                "actor_id": e.actor_id, "reason": e.reason, "evidence": e.evidence}
               for e in s.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.at, JobEvent.id))]
    from jobs import relaunch
    return M.public(job, dep, quote=quote, detail=True, events=evs, artifacts=A.store().list(job_id),
                    metrics_latest=latest_metrics(job_id), runner_marks=checkpoint_marks(job_id),
                    chain=relaunch.chain_infos([job]).get(job_id))


def checkpoint_marks(job_id: str) -> dict:
    """What the runner (or the simulation) SAID about checkpoints / resume: {saved: [epochs], resumed: {...}|None}.
    The stored, verified checkpoints themselves are the artifacts (kind 'checkpoint')."""
    from store.jobs import JobRunnerEvent
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(select(JobRunnerEvent).where(
            JobRunnerEvent.job_id == job_id, JobRunnerEvent.type == "phase",
            JobRunnerEvent.data["phase"].astext.in_(("checkpoint_saved", "resumed"))).order_by(JobRunnerEvent.seq)))
    saved, resumed = [], None
    for r in rows:
        d = r.data or {}
        if d.get("phase") == "resumed":
            resumed = {"from_epoch": d.get("from_epoch"), "source": r.source, "at": r.at.isoformat() if r.at else None}
        elif isinstance(d.get("epoch"), int):
            saved.append(d["epoch"])
    return {"saved": sorted(set(saved)), "resumed": resumed}


def list_for(who, *, status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    q = select(Job).order_by(Job.created_at.desc())
    if who.account_id is not None:
        q = q.where(Job.account_id == who.account_id)
    if status == "open":
        q = q.where(Job.finished_at.is_(None))
    elif status:
        q = q.where(Job.status == status)
    with normalize.SessionLocal() as s:
        jobs = list(s.scalars(q.offset(offset).limit(limit)))
        from store.routing import Deployment
        ids = [j.deployment_id for j in jobs if j.deployment_id]
        deps = {d.deployment_id: d for d in s.scalars(select(Deployment).where(Deployment.deployment_id.in_(ids)))} \
            if ids else {}
    from jobs import relaunch
    chains = relaunch.chain_infos(jobs)
    return [M.public(j, deps.get(j.deployment_id), chain=chains.get(j.job_id)) for j in jobs]


def logs(job_id: str, who, *, after: int | None, limit: int) -> dict:
    job = _load(job_id, who)
    q = select(JobLog).where(JobLog.job_id == job_id)
    if after is not None:
        q = q.where(JobLog.id > after)
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(q.order_by(JobLog.id).limit(limit)))
    items = [{"id": r.id, "seq": r.seq, "at": r.at.isoformat(), "stream": r.stream, "line": r.line} for r in rows]
    return {"job_id": job_id, "items": items, "next_after": items[-1]["id"] if items else after,
            "more": len(items) == limit, "simulated": bool(job.sandbox),
            "cursor": "pass next_after as ?after= to read on"}


def latest_metrics(job_id: str) -> dict:
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(select(JobMetric).where(JobMetric.job_id == job_id).order_by(JobMetric.id)))
    out: dict = {}
    for r in rows:
        out[r.name] = {"value": r.value, "epoch": r.epoch, "step": r.step, "source": r.source}
    return out


def metrics(job_id: str, who) -> dict:
    job = _load(job_id, who)
    with normalize.SessionLocal() as s:
        rows = list(s.scalars(select(JobMetric).where(JobMetric.job_id == job_id)
                              .order_by(JobMetric.epoch.nulls_first(), JobMetric.step.nulls_first(), JobMetric.id)))
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r.name, []).append({"epoch": r.epoch, "step": r.step, "value": r.value,
                                              "at": r.at.isoformat(), "source": r.source})
    epochs: dict[int, dict] = {}
    for r in rows:
        if r.epoch is not None:
            epochs.setdefault(r.epoch, {"epoch": r.epoch})[r.name] = r.value
    return {"job_id": job_id, "simulated": bool(job.sandbox), "series": series,
            "epochs": [epochs[k] for k in sorted(epochs)], "latest": latest_metrics(job_id),
            "source": "simulated" if job.sandbox else "runner",
            "note": "SIMULATED placeholder values (sandbox)" if job.sandbox else
                    "reported by the runner on the GPU (train loss over all batches, held-out test accuracy)"}


def artifacts_list(job_id: str, who) -> list[dict]:
    _load(job_id, who)
    from jobs import artifacts as A
    return [{**a, "download": f"/v1/jobs/{job_id}/artifacts/{a['name']}"} for a in A.store().list(job_id)]


def artifact(job_id: str, name: str, who) -> tuple[dict, bytes]:
    _load(job_id, who)
    from jobs import artifacts as A
    try:
        got = A.store().get(job_id, name)
    except A.ArtifactError as exc:
        raise JobRefused(exc.code, exc.message, exc.status) from None
    if got is None:
        raise JobRefused("artifact_not_found", "artifact not found", 404)
    return got


# --------------------------------------------------------------------------
# Cancel
# --------------------------------------------------------------------------

def cancel(job_id: str, who) -> tuple[int, dict]:
    from routing import deployments as D
    from routing import engine
    from jobs import sync

    job = _load(job_id, who)
    actor, actor_id = _actor(who)
    if S.is_finished(job) or job.sandbox:
        return 200, {**get(job_id, who), "cancel": {"requested": False, "note": f"job is {job.status}: nothing to cancel"}}
    dep = _dep(job)
    now = _now()
    pre = dep is None or (dep.status in D.PRE_LAUNCH_STATES and dep.launch_token is None)
    if pre and dep is not None and job.route_request_id:
        try:
            engine.reject(job.route_request_id, who, reason=f"cancelled by the customer (job {job_id})")
        except HTTPException as exc:      # launched concurrently: fall through to terminate
            if not (isinstance(exc.detail, dict) and exc.detail.get("code") == "not_rejectable"):
                raise
            pre = False
    if pre:
        with normalize.SessionLocal.begin() as s:
            j = s.get(Job, job_id, with_for_update=True)
            j.cancel_requested_at = j.cancel_requested_at or now
            if not S.is_finished(j) and not S.can(j.status, "approval_rejected"):
                raise JobRefused("submission_in_progress", f"job {job_id} is still being submitted ({j.status}); "
                                                           "retry the cancel in a few seconds", 409)
            if not S.is_finished(j):
                S.transition(s, j, "approval_rejected", actor=actor, actor_id=actor_id,
                             reason="cancelled by the customer before launch (nothing was created or billed)",
                             outcome="cancelled", evidence={"deployment_id": j.deployment_id})
        sync.sync_job(job_id, enforce=False)
        return 200, {**get(job_id, who), "cancel": {"requested": True, "before_launch": True,
                                                    "note": "cancelled before launch: nothing was created"}}
    with normalize.SessionLocal.begin() as s:
        j = s.get(Job, job_id, with_for_update=True)
        j.cancel_requested_at = j.cancel_requested_at or now
        if j.outcome is None:
            j.outcome = "cancelled"
        if S.can(j.status, "cancel_requested"):
            S.transition(s, j, "cancel_requested", actor=actor, actor_id=actor_id,
                         reason="cancel requested by the customer: terminating the GPU",
                         evidence={"deployment_id": j.deployment_id})
        else:
            S.note(s, j, f"cancel requested while {j.status}", actor=actor, actor_id=actor_id)
    term = sync._terminate(job_id, dep.deployment_id, "cancelled by the customer")
    with normalize.SessionLocal.begin() as s:
        j = s.get(Job, job_id, with_for_update=True)
        if j.status == "cancel_requested":
            S.transition(s, j, "cancelling", actor="system", reason="terminate requested through the existing "
                                                                  "deployments.terminate; awaiting confirmation",
                         evidence={"terminate": term})
    sync.sync_job(job_id, enforce=False)
    return 202, {**get(job_id, who), "cancel": {"requested": True, "before_launch": False, "terminate": term,
                                                "note": "terminating: the job is cancelled once the provider confirms "
                                                        "the instance is gone (resource.disposition)"}}


# --------------------------------------------------------------------------
# The launch-time bootstrap render (called by routing/deployments.launch)
# --------------------------------------------------------------------------

def is_placeholder(script) -> bool:
    return isinstance(script, str) and script.strip().startswith(PLACEHOLDER)


def render_startup(d, launch_spec, *, now: datetime | None = None):
    """(provider_launch_spec, None) or (None, problem). `d` is the Deployment row the caller holds FOR UPDATE.
    Never raises; never logs or stores the token or the rendered script."""
    from jobs import tokens

    now = now or _now()
    job_id = launch_spec.startup_script.strip()[len(PLACEHOLDER):]
    if not tokens.JOB_ID_RE.match(job_id or ""):
        return None, "job_bootstrap_invalid: malformed job placeholder"
    try:
        from routing import guards
        eff = int(d.effective_max_runtime_minutes or d.max_runtime_minutes or guards.runtime_ceiling(
            d.account_id, None, purpose=d.purpose or "customer")["effective_max_runtime_minutes"])
        with normalize.SessionLocal.begin() as s:
            job = s.get(Job, job_id, with_for_update=True)
            if job is None:
                return None, "job_bootstrap_invalid: no such job"
            if job.sandbox or (d.purpose or "customer") != "customer":
                return None, "job_bootstrap_invalid: sandbox / non-customer deployment"
            if job.account_id != d.account_id:
                return None, "job_bootstrap_invalid: the job belongs to another account"
            if job.deployment_id not in (None, d.deployment_id) or job.route_request_id not in (None, d.route_request_id):
                return None, "job_bootstrap_invalid: the job is bound to another deployment"
            if S.is_finished(job) or job.cancel_requested_at is not None:
                return None, f"job_bootstrap_invalid: job is {job.status}"
            from runner import bootstrap
            expires = tokens.expiry_for(now + timedelta(minutes=eff))
            tokens.revoke_all(s, job_id, "superseded by a new launch render")
            nonce = secrets.token_hex(24)
            from store.jobs import JobToken
            trow = JobToken(job_id=job_id, nonce=nonce, created_at=now, expires_at=expires)
            s.add(trow)
            s.flush()
            base, problem = _callback_base()
            if problem:
                raise ValueError("callback base url")
            rf = (job.spec or {}).get("resume_from") or None
            script = bootstrap.render(job_id, tokens.derive(job_id, nonce), base_url=base, template=job.template,
                                      config=dict(job.config or {}), max_runtime_minutes=max(5, min(eff, 720)),
                                      allow_http=base.startswith("http://"),
                                      resume={"sha256": rf["sha256"], "epoch": int(rf["epoch"])} if rf else None)
            sha = hashlib.sha256(script.encode("utf-8")).hexdigest()
            job.deployment_id = job.deployment_id or d.deployment_id
            job.route_request_id = job.route_request_id or d.route_request_id
            job.bootstrap_sha256, job.bootstrap_rendered_at = sha, now
            job.bootstrap_template_version = getattr(bootstrap, "TEMPLATE_VERSION", None)
            S.note(s, job, "bootstrap rendered for the launch (the script and its token are never stored or logged)",
                   {"sha256": sha, "template_version": job.bootstrap_template_version,
                    "bundle_sha256": getattr(bootstrap, "BUNDLE_SHA256", None), "bytes": len(script.encode()),
                    "token_id": trow.id, "token_expires_at": expires.isoformat(), "deployment_id": d.deployment_id,
                    "resume_from": {k: rf.get(k) for k in ("job_id", "artifact", "epoch", "sha256")} if rf else None})
        out = dataclasses.replace(launch_spec, startup_script=script)
        for k in ("defaults_applied", "defaults_withheld"):
            if hasattr(launch_spec, k):
                setattr(out, k, getattr(launch_spec, k))
        return out, None
    except Exception as exc:  # noqa: BLE001 - never echo the exception (it could carry the script)
        log.error("job %s: bootstrap render failed (%s); the launch is refused", job_id, type(exc).__name__)
        return None, f"job_bootstrap_unavailable: the job bootstrap could not be rendered ({type(exc).__name__})"
