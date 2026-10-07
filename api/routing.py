"""Capability registry, best execution, route preview, routing, deployments.

    GET  /v1/capabilities                      every provider: API supports vs OpenGrid implements   [data:read]
    GET  /v1/capabilities/{provider}           one provider                                         [data:read]
    GET  /v1/best/{gpu}                        "Best Available Now": the transparent ranking         [data:read]
                                               (a family: each variant ranked separately; strict_region)
    POST /v1/route/preview                     dry-run decision + quote; never calls a provider     [route:preview]
    POST /v1/route                             check, quote, and (only if live provisioning is
                                               enabled here) provision with failover                [route:execute]
    GET  /v1/route/{route_request_id}          the audit record: request + every candidate/exclusion [route:preview]
    GET  /v1/deployments                       your deployments                                     [deployments:read]
    GET  /v1/deployments/{id}?refresh=true     one deployment; refresh asks the provider            [deployments:read]
    POST /v1/deployments/{id}/terminate                                                             [deployments:write]
    POST /v1/deployments/{id}/stop             where the provider API supports stop                 [deployments:write]
    POST /v1/deployments/{id}/outcome          the caller's report: did the workload complete?      [deployments:write]

Prices are labelled by concept: candidates carry observed market prices, a route carries a
quote, a deployment carries the execution price once the provider reports one.
"""

from __future__ import annotations

from typing import Literal

from config import settings
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

import routing.tracker  # noqa: F401  registers the routing_tracker job
from accounts.auth import Principal, require_scope
from api.common import INFERRED, TRANSACTION, envelope, gpu_slug, resolve_gpu_or_family
from routing import audit, capabilities, deployments, engine, scoring, transactions

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

class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exclude_providers: list[str] = Field(default_factory=list, max_length=100)
    include_providers: list[str] | None = Field(None, max_length=100)
    require_level: int = Field(0, ge=0, le=3, description="minimum OpenGrid integration level")
    require_available: bool = Field(False, description="only listings with explicit availability")


class Launch(BaseModel):
    """Canonical launch parameters; each adapter translates them (see routing/adapters/base.py)."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    ssh_key: str | None = Field(None, max_length=256, description="name/id of a key registered with the provider")
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


@router.post("/v1/route", summary="Route a workload: check, quote and (if enabled here) provision")
def route_execute(body: RouteBody, who: Principal = Depends(require_scope("route:execute"))):
    spec = _spec(body)
    try:
        out = engine.route(spec, who)
    except scoring.ScoringError as e:
        raise HTTPException(422, str(e))
    return envelope(out, kind=TRANSACTION if out.get("deployment") else INFERRED, methodology="routing")


@router.get("/v1/route/{route_request_id}", summary="The audit record of one preview or route")
def route_record(route_request_id: str, who: Principal = Depends(require_scope("route:preview"))):
    r = audit.get(route_request_id, who)
    if r is None:
        raise HTTPException(404, "route request not found")
    return envelope(r, methodology="routing")


# --------------------------------------------------------------------------
# Deployments
# --------------------------------------------------------------------------

@router.get("/v1/deployments", summary="Your deployments")
def list_deployments(status: str | None = Query(None, pattern="^(" + "|".join(deployments.STATUSES) + ")$"),
                     who: Principal = Depends(require_scope("deployments:read"))):
    return envelope(deployments.list_for(who, status), kind=TRANSACTION, methodology="routing")


@router.get("/v1/deployments/{deployment_id}", summary="One deployment; refresh=true asks the provider")
def get_deployment(deployment_id: str, refresh: bool = True,
                   who: Principal = Depends(require_scope("deployments:read"))):
    return envelope(deployments.get(deployment_id, who, refresh_now=refresh), kind=TRANSACTION,
                    methodology="routing")


@router.post("/v1/deployments/{deployment_id}/terminate", summary="Terminate a deployment")
def terminate_deployment(deployment_id: str, who: Principal = Depends(require_scope("deployments:write"))):
    return envelope(deployments.terminate(deployment_id, who), kind=TRANSACTION, methodology="routing")


@router.post("/v1/deployments/{deployment_id}/stop", summary="Stop a deployment (where the provider supports it)")
def stop_deployment(deployment_id: str, who: Principal = Depends(require_scope("deployments:write"))):
    return envelope(deployments.stop(deployment_id, who), kind=TRANSACTION, methodology="routing")


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workload_completed: bool


@router.post("/v1/deployments/{deployment_id}/outcome", summary="Report whether the workload completed")
def deployment_outcome(deployment_id: str, body: Outcome,
                       who: Principal = Depends(require_scope("deployments:write"))):
    deployments.get(deployment_id, who)  # 404s for someone else's deployment
    transactions.record_outcome(deployment_id, body.workload_completed)
    return envelope(deployments.public(deployment_id), kind=TRANSACTION, methodology="routing")
