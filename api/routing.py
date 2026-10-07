"""Capability registry, best execution, route preview, routing, approval, deployments, execution admin.

    GET  /v1/capabilities                      every provider: API supports vs OpenGrid implements   [data:read]
    GET  /v1/capabilities/{provider}           one provider                                         [data:read]
    GET  /v1/best/{gpu}                        "Best Available Now": the transparent ranking         [data:read]
    POST /v1/route/preview                     dry-run decision + persisted quote (observed price)   [route:preview]
    POST /v1/route                     (*)     check, quote, guards -> pending_approval (SUPERVISED) or
                                               launch (LIVE, validated + live-enabled providers)    [route:execute]
    GET  /v1/route/{route_request_id}          the audit record (+ deployment, quote)               [route:preview]
    POST /v1/route/{id}/approve        (*)     admin: {quote_id, override_limits?, reason?} -> re-validate, launch [admin]
    POST /v1/route/{id}/reject                 admin: {reason}                                       [admin]
    GET  /v1/quotes/{quote_id}                 a quote: prices, fees, expiry, status                 [route:preview]
    GET  /v1/deployments                       your deployments (?status=live|uncertain|<state>)    [deployments:read]
    GET  /v1/deployments/{id}?refresh=false    one deployment; refresh=true asks the provider       [deployments:read]
    POST /v1/deployments/{id}/terminate (*)    idempotent; works for suspended accounts, any mode   [deployments:write]
    POST /v1/deployments/{id}/stop      (*)    where the provider API supports stop                 [deployments:write]
    POST /v1/deployments/{id}/outcome          the caller's report: did the workload complete?      [deployments:write]
  admin (scope admin; every change needs a reason and is written to execution_control_log)
    GET/POST /v1/admin/execution/mode                   DISABLED | PREVIEW_ONLY | SUPERVISED | LIVE
    GET      /v1/admin/execution/providers              every provider's flags
    GET/POST /v1/admin/execution/providers/{p}          supervised_enabled / live_enabled (demote adapter_status)
    POST     /v1/admin/execution/kill                   global kill switch (mode DISABLED)
    POST     /v1/admin/execution/providers/{p}/kill     provider kill switch;  .../unkill lifts it
    GET      /v1/admin/execution/log                    the control log
    POST     /v1/admin/execution/validation             a validation route for one provider (pending approval)
    GET/POST /v1/admin/execution/limits/{account_id}    cost guards
    GET      /v1/admin/deployments?state=live           every account's deployments with accrued cost estimate
    POST     /v1/admin/deployments/{id}/terminate       force-terminate (idempotent)
(*) Idempotency-Key header REQUIRED (routing/idempotency.py): replay on same body, 422 on a different body,
409 + Retry-After while the first is in flight.

Prices are labelled by concept: candidates carry observed market prices, a route carries a
quote, a deployment carries the execution price once the provider reports one.
"""

from __future__ import annotations

from typing import Literal

from config import settings
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import routing.tracker  # noqa: F401  registers the routing_tracker job
from accounts.auth import Principal, require_scope
from api.common import INFERRED, TRANSACTION, envelope, gpu_slug, resolve_gpu_or_family
from routing import (audit, capabilities, control, deployments, engine, guards, idempotency, quotes, scoring,
                     transactions)

router = APIRouter()
READ = require_scope("data:read")



def _region(value: str | None) -> str | None:
    if not value:
        return None
    try:
        from regions import REGION_GROUPS
    except ImportError:
        raise HTTPException(422, "region filtering is unavailable (regions module missing)")
    for g in REGION_GROUPS:
        if g.lower() == value.strip().lower():
            return g
    raise HTTPException(422, f"unknown region {value!r}; one of {', '.join(REGION_GROUPS)}")


def _mode(value: str) -> str:
    m = (value or "BALANCED").strip().upper()
    if m not in scoring.MODES:
        raise HTTPException(422, f"unknown mode {value!r}; one of {', '.join(scoring.MODES)}")
    return m


# --------------------------------------------------------------------------
# Capabilities and best execution
# --------------------------------------------------------------------------

@router.get("/v1/capabilities", summary="What each provider's API allows vs what OpenGrid implements")
def get_capabilities(who: Principal = Depends(READ)):
    return envelope(capabilities.all_capabilities(), methodology="routing", levels=capabilities.LEVELS,
                    live_provisioning_enabled=bool(settings.routing_live_provisioning),
                    note="verified_live is false everywhere: no adapter has been run against a real provider "
                         "account yet; adapters are tested against mocked HTTP from documented API shapes")


@router.get("/v1/capabilities/{provider}", summary="One provider's integration capabilities")
def get_capability(provider: str, who: Principal = Depends(READ)):
    return envelope(capabilities.capability(provider.lower()), methodology="routing", levels=capabilities.LEVELS)


@router.get("/v1/best/{gpu}", summary="Best Available Now: transparent best-execution ranking for one GPU")
def best(gpu: str, count: int = Query(1, ge=1, le=64), region: str | None = None,
         mode: str = "BALANCED", max_price: float | None = Query(None, gt=0),
         limit: int = Query(10, ge=1, le=100), strict_region: bool = False,
         per_variant: int = Query(3, ge=1, le=25, description="families only: top candidates per variant"),
         who: Principal = Depends(READ)):
    kind, name = resolve_gpu_or_family(gpu)
    m = _mode(mode)
    if m == "USER_DEFINED":
        raise HTTPException(422, "USER_DEFINED needs weights: use POST /v1/route/preview")
    group = _region(region)
    if strict_region and not group:
        raise HTTPException(422, "strict_region needs a region")
    note = "ranking is inferred from observed market prices; candidate prices are observed market prices, not quotes"
    try:
        if kind == "family":
            import families
            out = []
            for v in families.variants(name):
                r = scoring.rank_listings(v, count=count, region_group=group, max_price=max_price, mode=m,
                                          limit=per_variant, strict_region=strict_region)
                for c in r["candidates"]:
                    c.pop("raw_gpu_name", None)
                out.append({"gpu": v, "slug": gpu_slug(v), "market": r["market"],
                            "candidates_total": r["candidates_total"], "top": r["candidates"],
                            "exclusions_total": r["exclusions_total"], "exclusions_by_code": r["exclusions_by_code"],
                            "link": f"/v1/best/{gpu_slug(v)}"})
            fam = families.family(name)
            data = {"family": name, "slug": fam["slug"], "kind": fam["kind"], "note": families.NOTE,
                    "mode": m, "count": count, "region_group": group, "strict_region": strict_region,
                    "by_variant": out}
            return envelope(data, kind="family", resolved_as="family", requested=gpu, methodology="best-execution",
                            also=["/methodology/families"],
                            note=note + "; each variant is ranked on its own market, separately")
        r = scoring.rank_listings(name, count=count, region_group=group, max_price=max_price, mode=m,
                                  limit=limit, strict_region=strict_region)
    except scoring.ScoringError as e:
        raise HTTPException(422, str(e))
    r["exclusions"] = r["exclusions"][:100]
    for c in r["candidates"] + r["multi_instance_alternatives"]:
        c.pop("raw_gpu_name", None)
    return envelope(r, kind=INFERRED, methodology="best-execution", note=note)


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------

def _idem(request: Request, who: Principal, scope: str, body, fn, *, resource_of=None,
          reclaim_check=None) -> JSONResponse:
    code, payload, replayed = idempotency.run(who=who, scope=scope, key=request.headers.get("Idempotency-Key"),
                                              body=body, fn=fn, resource_of=resource_of, reclaim_check=reclaim_check)
    return JSONResponse(payload, status_code=code, headers={"Idempotent-Replayed": "true"} if replayed else None)


def _refused(fn):
    """Control-plane / guard errors (ValueError subclasses) -> 422, never a 500."""
    try:
        return fn()
    except control.ControlError as e:
        raise HTTPException(422, {"code": "invalid_control_change", "message": str(e)})


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclude_providers: list[str] = Field(default_factory=list, max_length=100)
    include_providers: list[str] | None = Field(None, max_length=100)
    require_level: int = Field(0, ge=0, le=3, description="minimum OpenGrid integration level")
    require_available: bool = Field(False, description="only listings with explicit availability")


SSH_PUBLIC_KEY = r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]{40,8192}( [^\r\n]{0,200})?$"


class Launch(BaseModel):
    """Canonical launch parameters; each adapter translates them (see routing/adapters/base.py)."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$",
                             description="ignored for the provider: OpenGrid names every instance og-<deployment_id>")
    ssh_key: str | None = Field(None, max_length=256, description="name/id of a key registered with the provider: "
                                                                  "ONLY with your own (BYO) provider credentials")
    ssh_public_key: str | None = Field(None, max_length=9000, pattern=SSH_PUBLIC_KEY,
                                       description="your SSH public key; registered for this deployment only")
    image: str | None = Field(None, max_length=256, description="OS image (VMs) or container image (RunPod, Vast)")
    disk_gb: int | None = Field(None, ge=10, le=20000)
    env: dict[str, str] = Field(default_factory=dict, max_length=50)


class RouteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gpu: str = Field(..., description="GPU slug or canonical name, e.g. h100-80gb-sxm5; a family "
                                      "(e.g. h100) only with allow_variants")
    allow_variants: bool = Field(False, description="gpu may be a family: route across its listed variants, "
                                                    "each candidate tagged with its variant")
    count: int = Field(1, ge=1, le=64, description="GPUs per instance; one instance per route")
    region: str | None = Field(None, description="region group: US, Canada, Europe, UK, APAC, ...")
    strict_region: bool = Field(False, description="exclude listings whose region is unknown, instead of "
                                                   "scoring them 0.5 on region match")
    max_price_per_gpu_hour: float | None = Field(None, gt=0, le=1000)
    duration_hours: float | None = Field(None, gt=0, le=24 * 366)
    deadline_hours: float | None = Field(None, gt=0, le=24 * 366)
    mode: Literal["CHEAPEST", "FASTEST_AVAILABLE", "BALANCED", "MOST_STABLE", "USER_DEFINED"] = "BALANCED"
    weights: dict[str, float] | None = None
    preferences: Preferences | None = None
    launch: Launch | None = None
    quote_id: str | None = Field(None, max_length=40, pattern=r"^q_[0-9a-f]{8,32}$",
                                 description="launch exactly this quote (from preview), after a live re-validation")
    max_runtime_minutes: int | None = Field(None, ge=1, le=60 * 24 * 31,
                                            description="auto-terminate deadline after launch")

    @field_validator("mode", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.upper() if isinstance(v, str) else v


def _spec(body: RouteBody) -> dict:
    kind, name = resolve_gpu_or_family(body.gpu)
    family, variants = None, None
    if kind == "family":
        import families
        variants = families.variants(name)
        if not body.allow_variants:
            raise HTTPException(422, {
                "code": "family_needs_variant", "family": name,
                "message": f"{body.gpu!r} is a GPU family; its variants are different products. Choose one: "
                           f"{', '.join(gpu_slug(v) for v in variants)} -- or set allow_variants: true to route "
                           f"across all of them",
                "variants": [{"gpu": v, "slug": gpu_slug(v)} for v in variants]})
        family = name
    region = _region(body.region)
    if body.strict_region and not region:
        raise HTTPException(422, "strict_region needs a region")
    spec = {
        "gpu": name, "family": family, "variants": variants, "strict_region": body.strict_region,
        "count": body.count, "region_group": region,
        "max_price_per_gpu_hour": body.max_price_per_gpu_hour, "duration_hours": body.duration_hours,
        "deadline_hours": body.deadline_hours, "mode": body.mode, "weights": body.weights,
        "preferences": body.preferences.model_dump() if body.preferences else {},
        "launch": body.launch.model_dump(exclude_none=True) if body.launch else None,
        "quote_id": body.quote_id, "max_runtime_minutes": body.max_runtime_minutes,
    }
    try:  # validate mode/weights before anything is written
        scoring.effective_weights(spec["mode"], spec["weights"], bool(spec["region_group"]))
    except scoring.ScoringError as e:
        raise HTTPException(422, str(e))
    return spec


@router.post("/v1/route/preview", summary="Dry-run a routing decision: selected, alternatives, quote")
def route_preview(body: RouteBody, who: Principal = Depends(require_scope("route:preview"))):
    spec = _spec(body)
    try:
        out = engine.preview(spec, who)
    except scoring.ScoringError as e:
        raise HTTPException(422, str(e))
    return envelope(out, kind=INFERRED, methodology="routing")


@router.post("/v1/route", summary="Route a workload: check, quote, then approval (SUPERVISED) or launch (LIVE)")
def route_execute(body: RouteBody, request: Request, who: Principal = Depends(require_scope("route:execute"))):
    spec = _spec(body)  # validation errors (422) need no key: nothing has happened yet

    def run():
        try:
            code, out = engine.route(spec, who)
        except scoring.ScoringError as e:
            raise HTTPException(422, str(e))
        return code, envelope(out, kind=TRANSACTION if out.get("deployment") else INFERRED, methodology="routing",
                              also=["/methodology/execution-safety"])

    return _idem(request, who, "route", body.model_dump(mode="json"), run,
                 resource_of=lambda p: (p.get("data") or {}).get("route_request_id"),
                 # a crashed / stale attempt that already created a deployment is never re-run (second instance)
                 reclaim_check=idempotency.deployments_since(who))


@router.get("/v1/route/{route_request_id}", summary="The audit record of one preview or route")
def route_record(route_request_id: str, who: Principal = Depends(require_scope("route:preview"))):
    r = audit.get(route_request_id, who)
    if r is None:
        raise HTTPException(404, "route request not found")
    d = deployments.for_request(route_request_id)
    if d is not None:
        r["deployment"] = deployments.public(d.deployment_id, detail=False)
        r["quote"] = quotes.get(d.quote_id) if d.quote_id else None
    return envelope(r, methodology="routing")


class Approve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(..., max_length=40)
    override_limits: bool = False
    reason: str | None = Field(None, max_length=2000)


@router.post("/v1/route/{route_request_id}/approve", summary="Admin: approve a pending launch (re-validates the quote)")
def route_approve(route_request_id: str, body: Approve, request: Request,
                  who: Principal = Depends(require_scope("admin"))):
    def run():
        code, out = engine.approve(route_request_id, who, quote_id=body.quote_id,
                                   override_limits=body.override_limits, reason=body.reason)
        return code, envelope(out, kind=TRANSACTION, methodology="execution-safety")

    return _idem(request, who, f"approve:{route_request_id}", body.model_dump(mode="json"), run)


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(..., min_length=3, max_length=2000)


@router.post("/v1/route/{route_request_id}/reject", summary="Admin: reject a pending launch")
def route_reject(route_request_id: str, body: Reason, who: Principal = Depends(require_scope("admin"))):
    return envelope(engine.reject(route_request_id, who, reason=body.reason), kind=TRANSACTION,
                    methodology="execution-safety")


@router.get("/v1/quotes/{quote_id}", summary="One quote: price, fees, expiry, status")
def get_quote(quote_id: str, who: Principal = Depends(require_scope("route:preview"))):
    q = quotes.get(quote_id, who)
    if q is None:
        raise HTTPException(404, "quote not found")
    return envelope(q, kind=TRANSACTION, methodology="execution-safety")


# --------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------

_STATUS_FILTER = "^(" + "|".join(deployments.STATUSES + ("live", "uncertain")) + ")$"


@router.get("/v1/deployments", summary="Your deployments")
def list_deployments(status: str | None = Query(None, pattern=_STATUS_FILTER),
                     limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                     who: Principal = Depends(require_scope("deployments:read"))):
    return envelope(deployments.list_for(who, status, limit=limit, offset=offset), kind=TRANSACTION,
                    methodology="routing", limit=limit, offset=offset)


@router.get("/v1/deployments/{deployment_id}", summary="One deployment; refresh=true asks the provider")
def get_deployment(deployment_id: str, refresh: bool = False,
                   who: Principal = Depends(require_scope("deployments:read"))):
    return envelope(deployments.get(deployment_id, who, refresh_now=refresh), kind=TRANSACTION,
                    methodology="routing")


@router.post("/v1/deployments/{deployment_id}/terminate", summary="Terminate a deployment (idempotent)")
def terminate_deployment(deployment_id: str, request: Request,
                         who: Principal = Depends(require_scope("deployments:write"))):
    def run():
        return 202, envelope(deployments.terminate(deployment_id, who), kind=TRANSACTION, methodology="execution-safety")

    deployments._load(deployment_id, who)  # 404 before claiming a key for someone else's deployment
    return _idem(request, who, f"terminate:{deployment_id}", {}, run)


@router.post("/v1/deployments/{deployment_id}/stop", summary="Stop a deployment (where the provider supports it)")
def stop_deployment(deployment_id: str, request: Request, who: Principal = Depends(require_scope("deployments:write"))):
    def run():
        return 202, envelope(deployments.stop(deployment_id, who), kind=TRANSACTION, methodology="execution-safety")

    deployments._load(deployment_id, who)
    return _idem(request, who, f"stop:{deployment_id}", {}, run)


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workload_completed: bool


@router.post("/v1/deployments/{deployment_id}/outcome", summary="Report whether the workload completed")
def deployment_outcome(deployment_id: str, body: Outcome,
                       who: Principal = Depends(require_scope("deployments:write"))):
    deployments.get(deployment_id, who)  # 404s for someone else's deployment
    transactions.record_outcome(deployment_id, body.workload_completed)
    return envelope(deployments.public(deployment_id), kind=TRANSACTION, methodology="routing")


# --------------------------------------------------------------------------
# Admin: execution control plane (scope admin; every change needs a reason and is logged)
# --------------------------------------------------------------------------

ADMIN = require_scope("admin")


def _by(who: Principal) -> str:
    return f"key:{who.key_id}" if who.key_id is not None else (who.kind or "operator")


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["DISABLED", "PREVIEW_ONLY", "SUPERVISED", "LIVE"]
    reason: str = Field(..., min_length=3, max_length=2000)

    @field_validator("mode", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.upper() if isinstance(v, str) else v


@router.get("/v1/admin/execution/mode", tags=["admin"], summary="Execution mode: stored, effective, env ceiling")
def get_mode(who: Principal = Depends(ADMIN)):
    return envelope(control.mode_status(), methodology="execution-safety")


@router.post("/v1/admin/execution/mode", tags=["admin"], summary="Set the execution mode (reason required)")
def post_mode(body: ModeBody, who: Principal = Depends(ADMIN)):
    return envelope(_refused(lambda: control.set_mode(body.mode, reason=body.reason, by=_by(who))),
                    methodology="execution-safety")


@router.get("/v1/admin/execution/providers", tags=["admin"], summary="Every provider's execution flags")
def get_all_flags(who: Principal = Depends(ADMIN)):
    return envelope(control.all_provider_flags(), methodology="execution-safety", effective_mode=control.effective_mode())


@router.get("/v1/admin/execution/providers/{provider}", tags=["admin"], summary="One provider's execution flags")
def get_flags(provider: str, who: Principal = Depends(ADMIN)):
    f = control.provider_flags(provider)
    perm = {p: dict(zip(("allowed", "mode_used", "reason"), control.launch_permission(provider, purpose=p)))
            for p in control.PURPOSES}
    return envelope({**f, "launch_permission": perm}, methodology="execution-safety")


class FlagsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supervised_enabled: bool | None = None
    live_enabled: bool | None = None
    adapter_status: Literal["simulated"] | None = Field(None, description="demotion only; 'validated' is recorded "
                                                                          "by a completed validation cycle")
    reason: str = Field(..., min_length=3, max_length=2000)


@router.post("/v1/admin/execution/providers/{provider}", tags=["admin"], summary="Set a provider's execution flags")
def post_flags(provider: str, body: FlagsBody, who: Principal = Depends(ADMIN)):
    return envelope(_refused(lambda: control.set_provider_flags(
        provider, reason=body.reason, by=_by(who), supervised_enabled=body.supervised_enabled,
        live_enabled=body.live_enabled, adapter_status=body.adapter_status)), methodology="execution-safety")


@router.post("/v1/admin/execution/kill", tags=["admin"], summary="Global kill switch: mode DISABLED for new launches")
def post_kill(body: Reason, who: Principal = Depends(ADMIN)):
    return envelope(_refused(lambda: control.kill_all(body.reason, _by(who))), methodology="execution-safety",
                    note="new launches stop; monitoring, reconciliation, stop and terminate keep working")


@router.post("/v1/admin/execution/providers/{provider}/kill", tags=["admin"], summary="Kill switch for one provider")
def post_kill_provider(provider: str, body: Reason, who: Principal = Depends(ADMIN)):
    return envelope(_refused(lambda: control.kill_provider(provider, body.reason, _by(who))),
                    methodology="execution-safety")


@router.post("/v1/admin/execution/providers/{provider}/unkill", tags=["admin"], summary="Lift a provider kill switch")
def post_unkill_provider(provider: str, body: Reason, who: Principal = Depends(ADMIN)):
    return envelope(_refused(lambda: control.unkill_provider(provider, body.reason, _by(who))),
                    methodology="execution-safety")


@router.get("/v1/admin/execution/log", tags=["admin"], summary="Execution control / admin action log")
def get_control_log(limit: int = Query(200, ge=1, le=2000), target: str | None = None,
                    who: Principal = Depends(ADMIN)):
    return envelope(control.recent_log(limit, target), methodology="execution-safety")


class ValidationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(..., max_length=64)
    listing_id: str | None = Field(None, max_length=256)
    max_runtime_minutes: int | None = Field(None, ge=1, le=60)
    launch: Launch | None = None
    reason: str = Field(..., min_length=3, max_length=2000)


@router.post("/v1/admin/execution/validation", tags=["admin"], status_code=202,
             summary="Create a validation route for one provider (pending approval; validation caps)")
def post_validation(body: ValidationBody, who: Principal = Depends(ADMIN)):
    rr_id = engine.create_validation_route(body.provider, body.listing_id, by=_by(who),
                                           max_runtime_minutes=body.max_runtime_minutes,
                                           launch=body.launch.model_dump(exclude_none=True) if body.launch else None)
    d = deployments.for_request(rr_id)
    return envelope({"route_request_id": rr_id, "deployment": deployments.public(d.deployment_id, operator=True),
                     "quote": quotes.get(d.quote_id),
                     "approve": f"POST /v1/route/{rr_id}/approve"}, kind=TRANSACTION, methodology="execution-safety")


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_price_per_gpu_hour: float | None = Field(None, gt=0, le=1000)
    max_hourly_cost: float | None = Field(None, ge=0, le=100000)
    max_total_cost: float | None = Field(None, ge=0, le=10000000)
    max_gpus: int | None = Field(None, ge=0, le=4096)
    max_active_deployments: int | None = Field(None, ge=0, le=1000)
    provider_allowlist: list[str] | None = Field(None, max_length=100)
    region_allowlist: list[str] | None = Field(None, max_length=100)
    monthly_spend_limit: float | None = Field(None, ge=0, le=100000000)
    reason: str = Field(..., min_length=3, max_length=2000)


@router.get("/v1/admin/execution/limits/{account_id}", tags=["admin"], summary="An account's cost guards and usage")
def get_limits(account_id: int, who: Principal = Depends(ADMIN)):
    return envelope({"limits": guards.limits_for(account_id), "usage": guards.usage(account_id)},
                    methodology="execution-safety")


@router.post("/v1/admin/execution/limits/{account_id}", tags=["admin"],
             summary="Set an account's cost guards (null = settings default)")
def post_limits(account_id: int, body: LimitsBody, who: Principal = Depends(ADMIN)):
    values = body.model_dump(exclude={"reason"}, exclude_unset=True)
    return envelope(_refused(lambda: guards.set_limits(account_id, values, by=_by(who), reason=body.reason)),
                    methodology="execution-safety")


@router.get("/v1/admin/deployments", tags=["admin"], summary="Every account's deployments (state=live|uncertain|<status>)")
def admin_deployments(state: str | None = Query(None, pattern=_STATUS_FILTER),
                      limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0),
                      who: Principal = Depends(ADMIN)):
    return envelope(deployments.admin_list(state, limit=limit, offset=offset), kind=TRANSACTION,
                    methodology="execution-safety", limit=limit, offset=offset)


@router.post("/v1/admin/deployments/{deployment_id}/terminate", tags=["admin"], status_code=202,
             summary="Admin force-terminate (idempotent; any account; any execution mode)")
def admin_terminate(deployment_id: str, body: Reason, request: Request, who: Principal = Depends(ADMIN)):
    def run():
        control.record("force_terminate", f"deployment:{deployment_id}", reason=body.reason, actor=_by(who))
        return 202, envelope(deployments.terminate(deployment_id, who, force=True, reason=body.reason),
                             kind=TRANSACTION, methodology="execution-safety")

    deployments._load(deployment_id, None)
    if request.headers.get("Idempotency-Key"):
        return _idem(request, who, f"force_terminate:{deployment_id}", body.model_dump(), run)
    code, payload = run()
    return JSONResponse(jsonable_encoder(payload), status_code=code)
