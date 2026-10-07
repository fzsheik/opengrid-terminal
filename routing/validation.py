"""Provider validation harness: the only path from adapter_status 'simulated' to 'validated'.

Every adapter ships VALIDATION_STATUS = 'SIMULATED' (tested only against mocked HTTP). A provider becomes
'validated' (control.provider_flags) only after ONE real, supervised, capped launch completes the full cycle
and this module finds evidence for every step. Nothing here bypasses the execution core: the launch goes
through engine.create_validation_route() (control.launch_permission(purpose='validation'), guards'
validation caps, quote, pending_approval) and an admin still approves it through the normal approve call.

    start_validation(provider, *, by, gpu=None)
        picks the cheapest 1-GPU on-demand listing whose INSTANCE price is within
        settings.validation_max_price_per_hour, and asks the core for a validation route (max runtime
        settings.validation_max_runtime_minutes, auto-terminate deadline, operator ssh key from launch
        defaults). Returns the route request + deployment, which waits for admin approval.

    validation_report(deployment_id, *, by=None, mark=True)
        checks the evidence, step by step:
          1 launch_accepted        a provision attempt accepted (or adopted) with a provider instance id
          2 observed_running       a status read reported 'running' (state machine event with evidence)
          3 find_instance          find_instance(og-name) returned exactly this instance while it ran
          4 list_instances         list_instances() contained it while it ran
          5 terminate_accepted     a terminate call to the provider was accepted
          6 termination_confirmed  terminated with two provider signals (status + list, or two not_found
                                   reads >= 60 s apart), re-checked now against list_instances()
          7 cost_reconciled        cost reconciliation ran (transaction cost recorded; provider cost or
                                   the reason it is unavailable)
        and only when every step passes calls control.mark_validated(provider, deployment_id, evidence, by).
        Any missing step -> {"validated": false, "missing": [...]}; nothing is marked.

The manual procedure is in methodology/provider-capabilities.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

import normalize
from config import settings
from routing import adapters, deployments
from store.reconcile import DeploymentWatch
from store.routing import Deployment, DeploymentEvent, ProvisionAttempt

log = logging.getLogger(__name__)

STEPS = ("launch_accepted", "observed_running", "find_instance", "list_instances", "terminate_accepted",
         "termination_confirmed", "cost_reconciled")


class ValidationError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc)


def pick_listing(provider: str, gpu: str | None = None) -> dict | None:
    """The cheapest eligible 1-GPU on-demand listing within the validation price cap, or None."""
    from tables import ComputeListingRow as L

    cap = float(settings.validation_max_price_per_hour)
    with normalize.SessionLocal() as s:
        q = (select(L).where(L.provider == provider, L.gpu_count == 1, L.canonical_gpu_name.is_not(None),
                             L.price_per_instance_hour.is_not(None), L.price_per_instance_hour <= cap,
                             L.market_type == "on_demand", L.available.is_not(False))
             .order_by(L.price_per_instance_hour, L.listing_id))
        if gpu:
            q = q.where(L.canonical_gpu_name == gpu)
        rows = [r for r in s.scalars(q.limit(50))
                if not getattr(r, "interruptible", False) and (r.provider_tier or "") not in ("cheapest", "from_price")]
    if not rows:
        return None
    r = rows[0]
    return {"listing_id": r.listing_id, "gpu": r.canonical_gpu_name, "price_per_instance_hour": float(r.price_per_instance_hour),
            "region": r.region}


def start_validation(provider: str, *, by: str, gpu: str | None = None) -> dict:
    """Create a validation deployment through the core (pending admin approval). Raises ValidationError."""
    from routing import control, engine

    provider = (provider or "").lower()
    if adapters.get(provider) is None or adapters.level(provider) < 2:
        raise ValidationError(f"OpenGrid has no provisioning adapter for {provider}")
    allowed, _, why = control.launch_permission(provider, purpose="validation")
    if not allowed:
        raise ValidationError(why)
    pick = pick_listing(provider, gpu)
    if pick is None:
        raise ValidationError(f"no 1-GPU on-demand {provider} listing at or under "
                              f"${settings.validation_max_price_per_hour:.2f}/h" + (f" for {gpu}" if gpu else ""))
    try:
        rr_id = engine.create_validation_route(provider, pick["listing_id"], by=by,
                                               max_runtime_minutes=settings.validation_max_runtime_minutes)
    except Exception as exc:  # noqa: BLE001 - RouteRefused and friends
        raise ValidationError(getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)) from exc
    d = deployments.for_request(rr_id)
    return {"route_request_id": rr_id, "deployment_id": d.deployment_id if d else None,
            "status": d.status if d else None, "quote_id": d.quote_id if d else None, "listing": pick,
            "max_runtime_minutes": settings.validation_max_runtime_minutes,
            "next": "an admin approves the route (POST /v1/route/{id}/approve with the quote id); then watch the "
                    "deployment run, terminate it (or let the deadline do it), and call validation_report()"}


def _events(dep_id: str) -> list[DeploymentEvent]:
    with normalize.SessionLocal() as s:
        return list(s.scalars(select(DeploymentEvent).where(DeploymentEvent.deployment_id == dep_id)
                              .order_by(DeploymentEvent.at, DeploymentEvent.id)))


def evidence(deployment_id: str, *, live_check: bool = True) -> dict:
    """{step: {ok, ...detail}} for every step. live_check re-lists the provider account (read-only)."""
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, deployment_id)
        atts = list(s.scalars(select(ProvisionAttempt).where(ProvisionAttempt.deployment_id == deployment_id)))
        w = s.get(DeploymentWatch, deployment_id)
    if d is None:
        raise ValidationError("no such deployment")
    ev = _events(deployment_id)
    out: dict = {}
    acc = [a for a in atts if a.instance_id and (a.outcome == "accepted" or "adopted" in (a.error or ""))]
    out["launch_accepted"] = {"ok": bool(acc and d.provider_instance_id
                                         and str(acc[0].instance_id) == str(d.provider_instance_id)),
                              "instance_id": d.provider_instance_id,
                              "attempts": [{"outcome": a.outcome, "instance_id": a.instance_id} for a in atts]}
    run = [e for e in ev if e.to_status == "running" and e.from_status != "running"
           and (e.evidence or {}).get("state") == "running"]
    out["observed_running"] = {"ok": bool(run), "at": run[0].at.isoformat() if run else None}
    v = (w.validation if w else None) or {}
    out["find_instance"] = {"ok": bool((v.get("find_instance") or {}).get("ok")), **(v.get("find_instance") or {})}
    out["list_instances"] = {"ok": bool((v.get("list_instances") or {}).get("ok")), **(v.get("list_instances") or {})}
    term = [e for e in ev if (e.evidence or {}).get("outcome") == "accepted"
            and "terminate" in (e.reason or "").lower()]
    out["terminate_accepted"] = {"ok": bool(term), "at": term[0].at.isoformat() if term else None}
    ended = [e for e in ev if e.to_status == "terminated"]
    basis = ((ended[-1].evidence or {}) if ended else {})
    two = bool(ended) and ("two signals" in str(basis.get("basis", "")) or "not_found reads" in str(basis.get("basis", ""))
                           or basis.get("state") == "terminated")
    confirm = {"ok": False, "status": d.status, "basis": basis.get("basis") or basis.get("state")}
    if d.status == "terminated" and two:
        if basis.get("state") == "terminated" and "two signals" not in str(basis.get("basis", "")):
            # one provider signal (status 'terminated'): the second is a fresh list without the instance.
            confirm["needs_list_check"] = True
        else:
            confirm["ok"] = True
        if live_check:
            listed = _listed_now(d)
            confirm["list_now"] = listed
            if listed.get("ok") is False:
                confirm["ok"] = False
            elif confirm.get("needs_list_check") and listed.get("absent"):
                confirm["ok"] = True
    out["termination_confirmed"] = confirm
    rec = d.reconciliation or {}
    txn = (rec.get("opengrid_transaction_cost") or {}).get("amount_usd")
    prov = rec.get("provider_reported_cost") or {}
    out["cost_reconciled"] = {"ok": d.reconciled_at is not None and txn is not None
                              and (prov.get("amount_usd") is not None or bool(prov.get("reason"))),
                              "opengrid_transaction_cost": txn, "provider_reported_cost": prov.get("amount_usd"),
                              "provider_cost_reason": prov.get("reason"),
                              "reconciled_at": d.reconciled_at.isoformat() if d.reconciled_at else None}
    return out


def _listed_now(d: Deployment) -> dict:
    """Read-only list with the pinned credential: is the instance absent (or listed as ended)?"""
    try:
        a = deployments.adapter_for(d)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"credentials: {getattr(exc, 'message', exc)}"}
    try:
        rows = a.list_instances()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(getattr(exc, "message", exc))[:300]}
    finally:
        a.close()
    alive = [r for r in rows if str(r.instance_id) == str(d.provider_instance_id) and r.alive]
    return {"ok": not alive, "absent": not alive, "listed": len(rows), "at": _now().isoformat()}


def validation_report(deployment_id: str, *, by: str | None = None, mark: bool = True,
                      live_check: bool = True) -> dict:
    """Check the full cycle; mark the provider validated only when every step has evidence."""
    d = deployments.load_row(deployment_id)
    if d is None:
        raise ValidationError("no such deployment")
    if d.purpose != "validation":
        raise ValidationError(f"{deployment_id} is a {d.purpose} deployment, not a validation deployment")
    ev = evidence(deployment_id, live_check=live_check)
    missing = [k for k in STEPS if not ev.get(k, {}).get("ok")]
    report = {"deployment_id": deployment_id, "provider": d.provider, "validated": False, "steps": ev,
              "missing": missing, "checked_at": _now().isoformat()}
    if missing or not mark:
        return report
    if not by:
        raise ValidationError("by (the operator) is required to mark a provider validated")
    from routing import control

    flags = control.mark_validated(d.provider, deployment_id, {"steps": ev, "checked_at": report["checked_at"]}, by)
    report["validated"] = True
    report["provider_flags"] = flags
    return report
