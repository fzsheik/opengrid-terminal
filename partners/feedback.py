"""Per-deployment feedback from the design partner: one row per deployment, editable.

The questions are deliberately few and binary (plus free text) so answers can be counted:
would you have chosen this provider yourself, was the price better than what you pay, was
setup easier, would you route your next workload through OpenGrid, and what broke.
Feedback is the partner's opinion (kind: reported), never mixed into reliability scores.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text

import normalize
from store.metrics import DeploymentFeedback

BOOLS = ("would_have_chosen_provider", "price_better", "setup_easier", "would_route_next")
TEXTS = ("what_broke", "notes")


def as_dict(f: DeploymentFeedback) -> dict:
    out = {"deployment_id": f.deployment_id, "account_id": f.account_id}
    for k in BOOLS + TEXTS:
        out[k] = getattr(f, k)
    out["created_at"] = f.created_at.isoformat() if f.created_at else None
    out["updated_at"] = f.updated_at.isoformat() if f.updated_at else None
    return out


def deployment_owner(deployment_id: str) -> tuple[bool, int | None]:
    """(exists, account_id) from the deployments table."""
    with normalize.SessionLocal() as s:
        row = s.execute(text("SELECT account_id FROM deployments WHERE deployment_id = :d"),
                        {"d": deployment_id}).first()
    return (row is not None, row[0] if row else None)


def clean(data: dict) -> dict:
    out = {}
    for k, v in data.items():
        if k in BOOLS:
            if v is not None and not isinstance(v, bool):
                raise ValueError(f"{k} must be true, false or null")
        elif k in TEXTS:
            if v is not None and (not isinstance(v, str) or len(v) > 4000):
                raise ValueError(f"{k} must be text up to 4000 characters")
            v = v.strip() if isinstance(v, str) else v
        else:
            raise ValueError(f"unknown field {k!r}")
        out[k] = v
    return out


def upsert(deployment_id: str, account_id: int | None, data: dict) -> tuple[dict, bool]:
    """Create or edit; returns (feedback, created). Only fields present in `data` change on edit."""
    data = clean(data)
    now = datetime.now(timezone.utc)
    with normalize.SessionLocal.begin() as s:
        f = s.get(DeploymentFeedback, deployment_id, with_for_update=True)
        created = f is None
        if created:
            f = DeploymentFeedback(deployment_id=deployment_id, account_id=account_id, created_at=now)
            s.add(f)
        else:
            f.updated_at = now
        for k, v in data.items():
            setattr(f, k, v)
        s.flush()
        return as_dict(f), created


def get(deployment_id: str) -> dict | None:
    with normalize.SessionLocal() as s:
        f = s.get(DeploymentFeedback, deployment_id)
        return as_dict(f) if f else None


def list_all(account_id: int | None = None, limit: int = 500) -> dict:
    q = select(DeploymentFeedback).order_by(DeploymentFeedback.created_at.desc())
    if account_id is not None:
        q = q.where(DeploymentFeedback.account_id == account_id)
    with normalize.SessionLocal() as s:
        items = [as_dict(f) for f in s.scalars(q.limit(limit))]
    summary = {}
    for k in BOOLS:
        answered = [i[k] for i in items if i[k] is not None]
        summary[k] = {"yes": sum(1 for a in answered if a), "no": sum(1 for a in answered if not a),
                      "n": len(answered)}
    return {"items": items, "summary": summary, "kind": "reported",
            "note": "partner opinions; never used in reliability or savings figures"}
