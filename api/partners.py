"""Design partners: profiles, onboarding, per-deployment feedback.

    POST  /v1/partners/me                    create the calling account's partner profile   [account:manage]
    GET   /v1/partners/me                    read it                                         [account:manage]
    PATCH /v1/partners/me                    edit it                                         [account:manage]
    GET   /v1/onboarding                     onboarding steps done / next, for the caller    [any authenticated]
    POST  /v1/deployments/{id}/feedback      create or edit feedback on one deployment       [deployments:write]
    GET   /v1/deployments/{id}/feedback      read it                                         [deployments:read]
    POST  /v1/admin/partners                 account + profile (+ first key) in one step     [admin]
    GET   /v1/admin/partners                 every partner with onboarding status            [admin]
    PATCH /v1/admin/partners/{account_id}    edit a partner's profile (e.g. status)          [admin]
    GET   /v1/admin/feedback                 all feedback with yes/no counts                 [admin]
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from accounts.accounts import account_for
from accounts.auth import Principal, principal, require_scope
from api.common import envelope
from partners import feedback, onboarding, profiles

router = APIRouter()
admin = require_scope("admin")


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str | None = Field(None, max_length=200)
    technical_contact_name: str | None = Field(None, max_length=200)
    technical_contact_email: str | None = Field(None, max_length=320)
    preferred_gpus: list[str] | None = Field(None, max_length=50)
    regions: list[str] | None = Field(None, max_length=50)
    workload_type: str | None = Field(None, max_length=64)
    normal_provider: str | None = Field(None, max_length=64)
    normal_price_per_gpu_hour: float | None = Field(None, gt=0, le=10_000)
    max_price_per_gpu_hour: float | None = Field(None, gt=0, le=10_000)
    expected_gpu_count: int | None = Field(None, ge=1, le=100_000)
    expected_duration_hours: float | None = Field(None, gt=0, le=100_000)
    latency_requirements: str | None = Field(None, max_length=4000)
    storage_requirements: str | None = Field(None, max_length=4000)
    network_requirements: str | None = Field(None, max_length=4000)
    status: Literal["invited", "onboarding", "active"] | None = None


class PartnerCreate(ProfileIn):
    company: str = Field(..., min_length=1, max_length=200)
    create_key: bool = True
    key_name: str = Field("onboarding", max_length=200)
    grant_execute: bool = Field(False, description="add route:execute to the first key (launches still need "
                                                   "operator approval in SUPERVISED mode)")


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    would_have_chosen_provider: StrictBool | None = None
    price_better: StrictBool | None = None
    setup_easier: StrictBool | None = None
    would_route_next: StrictBool | None = None
    what_broke: str | None = Field(None, max_length=4000)
    notes: str | None = Field(None, max_length=4000)


def _400(e: Exception):
    return HTTPException(400, str(e).strip("'\""))


def _set(body: BaseModel) -> dict:
    return body.model_dump(exclude_unset=True)


# ---------------------------------------------------------------- self-service

@router.post("/v1/partners/me", tags=["partners"], status_code=201, summary="Create your design-partner profile")
def create_my_profile(body: ProfileIn, who: Principal = Depends(require_scope("account:manage"))):
    acct = account_for(who)
    try:
        return envelope(profiles.create(acct, _set(body)))
    except KeyError:
        raise HTTPException(409, "a partner profile exists for this account; PATCH /v1/partners/me")
    except ValueError as e:
        raise _400(e)


@router.get("/v1/partners/me", tags=["partners"], summary="Your design-partner profile")
def get_my_profile(who: Principal = Depends(require_scope("account:manage"))):
    p = profiles.get(account_for(who))
    if p is None:
        raise HTTPException(404, "no partner profile for this account")
    return envelope(p)


@router.patch("/v1/partners/me", tags=["partners"], summary="Edit your design-partner profile")
def patch_my_profile(body: ProfileIn, who: Principal = Depends(require_scope("account:manage"))):
    try:
        return envelope(profiles.update(account_for(who), _set(body)))
    except LookupError:
        raise HTTPException(404, "no partner profile for this account; POST /v1/partners/me")
    except ValueError as e:
        raise _400(e)


@router.get("/v1/onboarding", tags=["partners"], summary="Onboarding: which steps are done and what is next")
def my_onboarding(who: Principal = Depends(principal)):
    if who.kind == "public":
        raise HTTPException(401, "OpenGrid API key required", headers={"WWW-Authenticate": "Bearer"})
    acct = account_for(who)
    return envelope({**onboarding.status(acct), "profile": profiles.get(acct)})


def _owned(deployment_id: str, who: Principal) -> int | None:
    exists, owner = feedback.deployment_owner(deployment_id)
    if not exists or (who.kind != "operator" and owner != who.account_id):
        raise HTTPException(404, "deployment not found")
    return owner


@router.post("/v1/deployments/{deployment_id}/feedback", tags=["partners"], summary="Feedback on one deployment")
def post_feedback(deployment_id: str, body: FeedbackIn, response: Response,
                  who: Principal = Depends(require_scope("deployments:write"))):
    owner = _owned(deployment_id, who)
    try:
        out, created = feedback.upsert(deployment_id, owner, _set(body))
    except ValueError as e:
        raise _400(e)
    response.status_code = 201 if created else 200
    return envelope(out, note="one feedback record per deployment; POST again to edit")


@router.get("/v1/deployments/{deployment_id}/feedback", tags=["partners"], summary="Read a deployment's feedback")
def get_feedback(deployment_id: str, who: Principal = Depends(require_scope("deployments:read"))):
    _owned(deployment_id, who)
    f = feedback.get(deployment_id)
    if f is None:
        raise HTTPException(404, "no feedback for this deployment yet")
    return envelope(f)


# ---------------------------------------------------------------- operator

@router.post("/v1/admin/partners", tags=["admin"], status_code=201,
             summary="Onboard a design partner: account + profile (+ first API key) in one step")
def admin_create_partner(body: PartnerCreate, who: Principal = Depends(admin)):
    data = body.model_dump(exclude_unset=True)
    create_key = data.pop("create_key", True)
    key_name = data.pop("key_name", "onboarding")
    grant_execute = data.pop("grant_execute", False)
    data["company"] = body.company
    try:
        out = profiles.admin_create_partner(data, create_key=create_key, key_name=key_name, grant_execute=grant_execute)
    except ValueError as e:
        raise _400(e)
    return envelope(out)


@router.get("/v1/admin/partners", tags=["admin"], summary="Every design partner with onboarding status")
def admin_list_partners(who: Principal = Depends(admin)):
    return envelope([{**p, "onboarding": onboarding.status(p["account_id"])} for p in profiles.list_all()])


@router.patch("/v1/admin/partners/{account_id}", tags=["admin"], summary="Edit a partner's profile")
def admin_patch_partner(account_id: int, body: ProfileIn, who: Principal = Depends(admin)):
    try:
        return envelope(profiles.update(account_id, _set(body)))
    except LookupError:
        raise HTTPException(404, "no partner profile for that account")
    except ValueError as e:
        raise _400(e)


@router.get("/v1/admin/feedback", tags=["admin"], summary="All deployment feedback")
def admin_feedback(account_id: int | None = None, who: Principal = Depends(admin)):
    return envelope(feedback.list_all(account_id))
