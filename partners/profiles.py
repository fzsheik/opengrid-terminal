"""Design-partner profiles: who the partner is, what they run, and what they pay today.

`normal_provider` / `normal_price_per_gpu_hour` are what the partner says they pay now. They
are the partner's statement (not observed by OpenGrid) and are used for one thing only: the
"savings vs your previous provider" figure in routing quality, shown only when given.

The operator onboards a partner in one step (admin_create_partner): an account (via
accounts.accounts.create_account), the profile, and optionally a first API key (via
accounts.keys.create_key; the secret is returned once). The default key scopes can preview
routes and manage the account but NOT execute: route:execute must be asked for explicitly,
and even then every supervised launch still needs operator approval.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

import normalize
from config import settings
from store.metrics import PartnerProfile

STATUSES = ("invited", "onboarding", "active")
TEXT_FIELDS = ("company", "technical_contact_name", "technical_contact_email", "workload_type", "normal_provider",
               "latency_requirements", "storage_requirements", "network_requirements", "status")
FIELDS = TEXT_FIELDS + ("preferred_gpus", "regions", "normal_price_per_gpu_hour", "max_price_per_gpu_hour",
                        "expected_gpu_count", "expected_duration_hours")
PARTNER_KEY_SCOPES = ("data:read", "route:preview", "deployments:read", "deployments:write", "account:manage")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def as_dict(p: PartnerProfile) -> dict:
    out = {"account_id": p.account_id}
    for f in FIELDS:
        v = getattr(p, f)
        out[f] = float(v) if isinstance(v, Decimal) else v
    out["created_at"] = p.created_at.isoformat() if p.created_at else None
    out["updated_at"] = p.updated_at.isoformat() if p.updated_at else None
    out["normal_price_kind"] = "partner-reported (not observed by OpenGrid)"
    return out


def clean(data: dict, *, partial: bool) -> dict:
    """Validate a create/patch body. Raises ValueError with a message for the caller."""
    out = {}
    for k, v in data.items():
        if k not in FIELDS:
            raise ValueError(f"unknown field {k!r}")
        if v is None:
            if k == "company":
                raise ValueError("company is required")
            out[k] = None if k not in ("preferred_gpus", "regions") else []
            continue
        if k in ("preferred_gpus", "regions"):
            if not isinstance(v, list) or len(v) > 50 or not all(isinstance(x, str) and 0 < len(x) <= 160 for x in v):
                raise ValueError(f"{k} must be a list of up to 50 short strings")
            out[k] = [x.strip() for x in v]
        elif k in ("normal_price_per_gpu_hour", "max_price_per_gpu_hour", "expected_duration_hours"):
            f = float(v)
            if not (0 < f < 100_000):
                raise ValueError(f"{k} must be > 0")
            out[k] = Decimal(str(f))
        elif k == "expected_gpu_count":
            if not isinstance(v, int) or not 0 < v <= 100_000:
                raise ValueError("expected_gpu_count must be a positive integer")
            out[k] = v
        else:
            if not isinstance(v, str):
                raise ValueError(f"{k} must be text")
            v = v.strip()
            limit = 4000 if k.endswith("_requirements") else 320
            if len(v) > limit:
                raise ValueError(f"{k} is too long (max {limit})")
            if k == "technical_contact_email" and v and not _EMAIL.match(v):
                raise ValueError("technical_contact_email is not an email address")
            if k == "status" and v not in STATUSES:
                raise ValueError(f"status must be one of {STATUSES}")
            if k == "company" and not v:
                raise ValueError("company is required")
            out[k] = v
    if not partial and not out.get("company"):
        raise ValueError("company is required")
    return out


def get(account_id: int) -> dict | None:
    with normalize.SessionLocal() as s:
        p = s.get(PartnerProfile, account_id)
        return as_dict(p) if p else None


def create(account_id: int, data: dict) -> dict:
    """Raises KeyError if a profile exists already (the caller PATCHes instead)."""
    data = clean(data, partial=False)
    with normalize.SessionLocal.begin() as s:
        if s.get(PartnerProfile, account_id) is not None:
            raise KeyError("profile exists")
        p = PartnerProfile(account_id=account_id, preferred_gpus=[], regions=[], status="invited",
                           created_at=datetime.now(timezone.utc))
        for k, v in data.items():
            setattr(p, k, v)
        s.add(p)
        s.flush()
        return as_dict(p)


def update(account_id: int, data: dict) -> dict:
    data = clean(data, partial=True)
    with normalize.SessionLocal.begin() as s:
        p = s.get(PartnerProfile, account_id, with_for_update=True)
        if p is None:
            raise LookupError("no partner profile")
        for k, v in data.items():
            setattr(p, k, v)
        p.updated_at = datetime.now(timezone.utc)
        s.flush()
        return as_dict(p)


def list_all() -> list[dict]:
    with normalize.SessionLocal() as s:
        return [as_dict(p) for p in s.scalars(select(PartnerProfile).order_by(PartnerProfile.created_at))]


def onboarding_instructions(account_id: int, key_created: bool) -> dict:
    base = settings.public_base_url.rstrip("/")
    return {
        "onboarding_status_url": f"{base}/v1/onboarding",
        "guide": f"{base}/methodology/first-live-route",
        "steps": [
            "1. Use the API key below (shown once) as `Authorization: Bearer <key>`." if key_created else
            "1. Create an API key: POST /v1/keys (scope account:manage) or ask the operator for one.",
            "2. Optional: connect your own provider credentials: POST /v1/credentials.",
            "3. Preview a route: POST /v1/route/preview with your GPU, count and region.",
            "4. Ask OpenGrid to run your first supervised deployment; the operator approves the quote.",
            "5. After it ends, tell us how it went: POST /v1/deployments/{id}/feedback.",
            f"Progress: GET {base}/v1/onboarding",
        ],
        "account_id": account_id,
    }


def admin_create_partner(profile: dict, *, create_key: bool = True, key_name: str = "onboarding",
                         grant_execute: bool = False) -> dict:
    """Account + profile (+ first key) in one step, via the accounts module's public functions."""
    from accounts import accounts, keys

    data = clean(profile, partial=False)
    email = data.get("technical_contact_email")
    acct = accounts.create_account(data["company"], email=email, plan="design_partner")
    try:
        prof = create(acct["id"], {k: (float(v) if isinstance(v, Decimal) else v) for k, v in data.items()})
    except Exception:
        # No half-onboarded partner: suspend the orphan account rather than leave it usable.
        accounts.set_status(acct["id"], "suspended")
        raise
    key = None
    if create_key:
        scopes = list(PARTNER_KEY_SCOPES) + (["route:execute"] if grant_execute else [])
        key = keys.create_key(acct["id"], key_name, scopes)
    return {"account": acct, "profile": prof, "api_key": key,
            "onboarding": onboarding_instructions(acct["id"], key is not None),
            "note": "the api_key secret is shown only in this response; OpenGrid stores a hash"
            if key else "no key created"}
