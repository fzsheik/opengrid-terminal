"""Execution (transaction) records, the billing hook, and cost reconciliation.

One execution_records row per deployment: quoted vs actual price, whether provisioning succeeded, how long
it took, attempts, uptime, interruptions, why it ended, and (when the caller says) whether the workload
completed. Kind: transaction. The only source reliability scoring may ever use.

Billing is INCREMENTAL (routing/tracker.py writes hour slices through billing.usage.record_usage_slice while
the deployment runs). bill(deployment_id), called by the core when a deployment is confirmed terminated
and retried by the tracker, writes the final slice, then reconciles cost. It never writes a second,
whole-lifetime usage record (that would double-charge the metered hours). Deployments billed before
incremental metering existed keep their single legacy usage record and are never re-metered.

Cost reconciliation (reconcile_cost), after confirmed termination, records SEPARATELY and never merges:
    quote                       per GPU-hour and total, from the quote the launch consumed
    expected_cost               quote x the actually metered (billable) time
    provider_reported_cost      adapter.reported_cost() where the provider exposes billing, else null + reason
    opengrid_transaction_cost   the sum of the deployment's usage records (provider cost passed through),
                                with OpenGrid's fee lines reported next to it, not inside it
    quote_error                 opengrid_transaction_cost - expected_cost (usd and pct)
    effective_hourly_rate       per GPU-hour, from the transaction cost and (separately) the provider's cost
    unexpected_fees             provider_reported_cost - opengrid_transaction_cost - billing_rounding, when > 0
    billing_rounding            what the provider's billing unit adds over exact metering (per minute, ...)
    billable_window             billable_start / billable_end and their basis (provider_running_at |
                                opengrid_observed_running | provider_created_at; end provider-reported or an
                                estimate), the provider lifecycle timestamps, and the OBSERVED runtime
                                (OpenGrid's own running time) kept separate from the billed runtime
stored in deployments.reconciliation (jsonb), provider_reported_cost and reconciled_at.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

import normalize
from store.routing import Deployment, ExecutionRecord

log = logging.getLogger(__name__)

RECONCILE_VERSION = "1"
# How long to keep asking a provider whose billing lags (RunPod hourly buckets, Vast daily charges).
PROVIDER_COST_RETRY_HOURS = 48


def _now() -> datetime:
    return datetime.now(timezone.utc)


def open_record(s, d: Deployment, *, ok: bool, attempts: int, latency_ms: int | None) -> None:
    now = _now()
    r = s.get(ExecutionRecord, d.deployment_id)
    if r is None:
        r = ExecutionRecord(deployment_id=d.deployment_id, created_at=now, uptime_seconds=0, interruptions=0)
        s.add(r)
    r.account_id, r.route_request_id, r.provider = d.account_id, d.route_request_id, d.provider
    r.gpu, r.gpu_count = d.gpu, d.gpu_count
    r.observed_market_price_per_gpu_hour = d.observed_market_price_per_gpu_hour
    r.quoted_price_per_gpu_hour = d.quoted_price_per_gpu_hour
    r.actual_price_per_gpu_hour = d.actual_price_per_gpu_hour
    r.provision_ok, r.attempts, r.provision_latency_ms = ok, attempts, latency_ms
    r.updated_at = now


def sync(s, d: Deployment) -> None:
    """Copy the deployment's running totals onto its record (cost is set by bill(), from metering)."""
    r = s.get(ExecutionRecord, d.deployment_id)
    if r is None:
        return
    r.actual_price_per_gpu_hour = d.actual_price_per_gpu_hour
    r.uptime_seconds, r.interruptions = d.uptime_seconds, d.interruptions
    r.termination_reason = d.termination_reason
    r.updated_at = _now()


def cost_of(d: Deployment) -> tuple[Decimal | None, str | None]:
    """Legacy wall-clock estimate (provisioned_at -> terminated_at). Kept for callers; billing uses slices."""
    if d.provisioned_at is None or d.terminated_at is None:
        return None, None
    price, basis = d.actual_price_per_gpu_hour, "actual"
    if price is None:
        price, basis = d.quoted_price_per_gpu_hour, "quote"
    if price is None:
        return None, None
    hours = Decimal(str(max(0.0, (d.terminated_at - d.provisioned_at).total_seconds()))) / 3600
    return (Decimal(str(price)) * d.gpu_count * hours).quantize(Decimal("0.0001")), basis


def metered_totals(deployment_id: str) -> dict:
    """Sums over the deployment's usage slices (billable seconds, cost) and its usage records / charges."""
    from store.accounts import Charge, UsageRecord
    from store.reconcile import UsageSlice

    with normalize.SessionLocal() as s:
        sl = s.execute(select(func.count(UsageSlice.id), func.coalesce(func.sum(UsageSlice.billable_seconds), 0),
                              func.coalesce(func.sum(UsageSlice.running_seconds), 0),
                              func.coalesce(func.sum(UsageSlice.stopped_seconds), 0),
                              func.coalesce(func.sum(UsageSlice.cost_usd), 0),
                              func.count(UsageSlice.id).filter(UsageSlice.usage_record_id.is_(None)
                                                               & (UsageSlice.billable_seconds > 0)),
                              func.bool_or(UsageSlice.end_estimated))
                       .where(UsageSlice.deployment_id == deployment_id)).one()
        ur = s.execute(select(func.count(UsageRecord.id), func.coalesce(func.sum(UsageRecord.provider_cost_usd), 0),
                              func.coalesce(func.sum(UsageRecord.gpu_hours), 0), func.max(UsageRecord.id))
                       .where(UsageRecord.deployment_id == deployment_id)).one()
        fees = s.scalar(select(func.coalesce(func.sum(Charge.amount_usd), 0))
                        .join(UsageRecord, UsageRecord.id == Charge.usage_record_id)
                        .where(UsageRecord.deployment_id == deployment_id, Charge.kind == "fee"))
    return {"slices": sl[0], "billable_seconds": int(sl[1]), "running_seconds": int(sl[2]),
            "stopped_seconds": int(sl[3]), "slice_cost_usd": Decimal(str(sl[4])), "slices_unrecorded": sl[5],
            "end_estimated": bool(sl[6]), "usage_records": ur[0], "usage_cost_usd": Decimal(str(ur[1])),
            "usage_gpu_hours": Decimal(str(ur[2])), "last_usage_record_id": ur[3], "fees_usd": Decimal(str(fees))}


def bill(deployment_id: str) -> int | None:
    """Finish metering a terminated deployment and reconcile its cost. Returns the last usage record id (or
    None when nothing was billable / not finished yet). Idempotent; the tracker retries."""
    from routing import tracker

    with normalize.SessionLocal() as s:
        d = s.get(Deployment, deployment_id)
        r = s.get(ExecutionRecord, deployment_id)
    if d is None or d.status != "terminated":
        return None
    legacy = r is not None and r.usage_record_id is not None and not metered_totals(deployment_id)["slices"]
    if not legacy:
        tracker.meter(deployment_id)
    t = metered_totals(deployment_id)
    if not legacy and t["slices_unrecorded"]:
        return None      # a usage record failed to write; the tracker retries
    with normalize.SessionLocal.begin() as s:
        rec = s.get(ExecutionRecord, deployment_id)
        if rec is not None and not legacy:
            rec.provider_cost_usd = t["usage_cost_usd"].quantize(Decimal("0.0001"))
            rec.cost_basis = "metered"
            rec.usage_record_id = t["last_usage_record_id"]
            rec.updated_at = _now()
    try:
        reconcile_cost(deployment_id)
    except Exception:  # noqa: BLE001 - reconciliation is retried; billing is already recorded
        log.exception("cost reconciliation of %s failed; retried", deployment_id)
    return t["last_usage_record_id"]


def unbilled() -> list[str]:
    """Terminated deployments whose metering or cost reconciliation is not finished."""
    from store.reconcile import DeploymentWatch

    with normalize.SessionLocal() as s:
        q = (select(Deployment.deployment_id)
             .outerjoin(DeploymentWatch, DeploymentWatch.deployment_id == Deployment.deployment_id)
             .where(Deployment.status == "terminated", Deployment.provider.is_not(None),
                    (DeploymentWatch.metering_complete.is_not(True)) | Deployment.reconciled_at.is_(None))
             .limit(500))
        return list(s.scalars(q))


# --------------------------------------------------------------------------
# Cost reconciliation
# --------------------------------------------------------------------------

UNIT_SECONDS = {"per second": 1, "per minute": 60, "per hour": 3600}


def billing_unit(provider: str | None) -> tuple[str, int | None, str]:
    """(label, seconds per unit or None when unknown, evidence) from the adapter's capability matrix."""
    from routing import adapters

    cls = adapters.get(provider or "")
    if cls is None:
        return "unknown", None, "no adapter"
    label, ev = cls.CAPABILITIES.billing_unit
    if label in UNIT_SECONDS:
        return label, UNIT_SECONDS[label], ev
    if "refund" in (label or ""):
        return label, 1, ev          # prepaid increments with the unused part refunded: effectively pro-rata
    return label or "unknown", None, ev


def _f(v) -> float | None:
    return None if v is None else round(float(v), 6)


def _quote(d: Deployment) -> dict:
    from store.routing import QuoteRow

    q = None
    if d.quote_id:
        with normalize.SessionLocal() as s:
            q = s.get(QuoteRow, d.quote_id)
    return {"quote_id": d.quote_id,
            "price_per_gpu_hour": _f(q.quote_price_per_gpu_hour if q else d.quoted_price_per_gpu_hour),
            "est_hourly_cost": _f(q.est_hourly_cost) if q else None,
            "est_total_cost": _f(q.est_total_cost) if q else None,
            "duration_hours": _f(q.duration_hours) if q else None,
            "billing_unit": q.billing_unit if q else None, "fees": q.fees if q else None,
            "basis": d.quote_basis, "kind": "quote"}


def _provider_cost(d: Deployment, start: datetime | None, end: datetime | None) -> dict:
    from routing import deployments
    from routing.adapters.results import CostReport

    if not d.provider_instance_id:
        return {"amount_usd": None, "reason": "no provider instance id", "kind": "provider_reported"}
    try:
        a = deployments.adapter_for(d)
    except Exception as exc:  # noqa: BLE001 - CredentialsUnavailable / no adapter
        return {"amount_usd": None, "reason": f"pinned credentials unavailable: {getattr(exc, 'message', exc)}",
                "kind": "provider_reported"}
    try:
        rep = a.reported_cost(d.provider_instance_id, start, end)
    except Exception as exc:  # noqa: BLE001
        rep = CostReport(None, start, end, reason=f"reported_cost failed: {type(exc).__name__}")
    finally:
        a.close()
    return {"amount_usd": _f(rep.amount_usd), "reason": rep.reason, "basis": rep.basis,
            "period_start": start.isoformat() if start else None, "period_end": end.isoformat() if end else None,
            "kind": "provider_reported"}


def compute_reconciliation(d: Deployment, totals: dict, provider_cost: dict, quote: dict) -> dict:
    """The reconciliation numbers (pure: no I/O). Every figure is kept separate."""
    gpus = d.gpu_count or 1
    billable_h = totals["billable_seconds"] / 3600
    gpu_h = billable_h * gpus
    qp = quote.get("price_per_gpu_hour")
    expected = None if qp is None else round(qp * gpu_h, 6)
    txn = round(float(totals["usage_cost_usd"]), 6)
    label, unit_s, unit_ev = billing_unit(d.provider)
    price = d.actual_price_per_gpu_hour if d.actual_price_per_gpu_hour is not None else d.quoted_price_per_gpu_hour
    rounding = None
    if unit_s and price is not None and totals["billable_seconds"] > 0:
        billed_s = math.ceil(totals["billable_seconds"] / unit_s) * unit_s
        rounding = round(float(price) * gpus * (billed_s - totals["billable_seconds"]) / 3600, 6)
    prov = provider_cost.get("amount_usd")
    unexpected = None
    if prov is not None:
        unexpected = round(prov - txn - (rounding or 0), 6)
        unexpected = unexpected if unexpected > 0.005 else 0.0
    return {
        "version": RECONCILE_VERSION, "currency": "USD", "kind": "transaction",
        "quote": quote,
        "runtime": {"billable_seconds": totals["billable_seconds"], "running_seconds": totals["running_seconds"],
                    "stopped_seconds": totals["stopped_seconds"], "gpu_count": gpus, "gpu_hours": round(gpu_h, 6),
                    "end_estimated": totals["end_estimated"], "uptime_seconds_observed": d.uptime_seconds},
        "expected_cost": {"amount_usd": expected, "basis": "quote price x metered billable GPU-hours",
                          "kind": "estimated"},
        "provider_reported_cost": provider_cost,
        "opengrid_transaction_cost": {"amount_usd": txn, "usage_records": totals["usage_records"],
                                      "fees_usd": round(float(totals["fees_usd"]), 6),
                                      "basis": "sum of usage records (provider cost passed through); OpenGrid "
                                               "fees reported separately", "kind": "transaction"},
        "quote_error": {"amount_usd": None if expected is None else round(txn - expected, 6),
                        "pct": None if not expected else round((txn - expected) / expected * 100, 4),
                        "basis": "transaction cost - expected cost (execution price vs quote)"},
        "effective_hourly_rate": {
            "per_gpu_hour_transaction": None if gpu_h <= 0 else round(txn / gpu_h, 6),
            "per_gpu_hour_provider": None if prov is None or gpu_h <= 0 else round(prov / gpu_h, 6)},
        "unexpected_fees": {"amount_usd": unexpected,
                            "reason": None if prov is not None else "no provider-reported cost to compare",
                            "basis": "provider-reported - transaction cost - billing rounding (> $0.005)"},
        "billing_rounding": {"amount_usd": rounding, "billing_unit": label, "unit_seconds": unit_s,
                             "evidence": unit_ev,
                             "reason": None if rounding is not None else "billing unit unknown or nothing billed"},
    }


def reconcile_cost(deployment_id: str, *, force: bool = False) -> dict | None:
    """Reconcile one confirmed-terminated deployment's cost. Re-asks the provider for its cost while it may
    still be lagging (PROVIDER_COST_RETRY_HOURS); otherwise idempotent."""
    with normalize.SessionLocal() as s:
        d = s.get(Deployment, deployment_id)
    if d is None or d.status != "terminated":
        return None
    prev = d.reconciliation or {}
    prev_amount = ((prev.get("provider_reported_cost") or {}).get("amount_usd"))
    if d.reconciled_at is not None and not force:
        lagging = (prev_amount is None and d.terminated_at is not None
                   and _now() - d.terminated_at < timedelta(hours=PROVIDER_COST_RETRY_HOURS)
                   and _supports_reported_cost(d.provider)
                   and _now() - d.reconciled_at >= timedelta(hours=1))
        if not lagging:
            return prev
    totals = metered_totals(deployment_id)
    start = getattr(d, "billable_start", None) or d.provisioned_at or d.created_at
    end = getattr(d, "billable_end", None) or d.terminated_at
    pc = _provider_cost(d, start, end)
    rec = compute_reconciliation(d, totals, pc, _quote(d))
    rec["billable_window"] = billable_window(d, totals)
    with normalize.SessionLocal.begin() as s:
        row = s.get(Deployment, deployment_id, with_for_update=True)
        row.reconciliation = rec
        row.reconciled_at = _now()
        row.provider_reported_cost = None if pc.get("amount_usd") is None else Decimal(str(pc["amount_usd"]))
    return rec


def billable_window(d: Deployment, totals: dict) -> dict:
    """The window OpenGrid billed and where each edge came from (never more precise than the evidence)."""
    from store.reconcile import DeploymentWatch

    iso = lambda t: None if t is None else t.isoformat()  # noqa: E731
    with normalize.SessionLocal() as s:
        w = s.get(DeploymentWatch, d.deployment_id)
    md = d.provider_metadata or {}
    label, unit_s, _ = billing_unit(d.provider)
    return {"start": iso(getattr(d, "billable_start", None)), "end": iso(getattr(d, "billable_end", None) or d.terminated_at),
            "basis": getattr(d, "billable_basis", None),
            "end_basis": (w.ended_basis if w else None) or ("provider_reported" if getattr(d, "provider_terminated_at", None)
                                                           else "first_observed"),
            "end_estimated": bool(totals.get("end_estimated")),
            "provider_created_at": iso(getattr(d, "provider_created_at", None)),
            "provider_running_at": iso(getattr(d, "provider_running_at", None)),
            "provider_terminated_at": iso(getattr(d, "provider_terminated_at", None)),
            "provider_fields": md.get("lifecycle_time_fields") or {},
            "requested_termination_at": iso(getattr(d, "requested_termination_at", None) or d.terminate_requested_at),
            "observed_running_seconds": d.uptime_seconds or 0,
            "billed_seconds": totals.get("billable_seconds"),
            "provider_billing_unit": label, "provider_billing_unit_seconds": unit_s,
            "kind": "transaction"}


def _supports_reported_cost(provider: str | None) -> bool:
    from routing import adapters

    cls = adapters.get(provider or "")
    return cls is not None and cls.CAPABILITIES.reported_cost[0] in ("YES", "PARTIAL")


def reconcile_pending() -> int:
    """Re-run cost reconciliation where the provider's billing may have caught up."""
    since = _now() - timedelta(hours=PROVIDER_COST_RETRY_HOURS)
    with normalize.SessionLocal() as s:
        ids = list(s.scalars(select(Deployment.deployment_id).where(
            Deployment.status == "terminated", Deployment.reconciled_at.is_not(None),
            Deployment.provider_reported_cost.is_(None), Deployment.terminated_at >= since).limit(200)))
    n = 0
    for dep_id in ids:
        try:
            n += reconcile_cost(dep_id) is not None
        except Exception:  # noqa: BLE001
            log.exception("cost reconciliation retry for %s failed", dep_id)
    return n


def record_outcome(deployment_id: str, workload_completed: bool) -> None:
    """The caller's own report of whether the workload finished (OpenGrid cannot observe it)."""
    with normalize.SessionLocal.begin() as s:
        r = s.get(ExecutionRecord, deployment_id)
        if r is not None:
            r.workload_completed = workload_completed
            r.updated_at = _now()


def as_dict(r: ExecutionRecord) -> dict:
    f = lambda v: None if v is None else float(v)  # noqa: E731
    return {
        "kind": "transaction",
        "observed_market_price_per_gpu_hour": f(r.observed_market_price_per_gpu_hour),
        "quoted_price_per_gpu_hour": f(r.quoted_price_per_gpu_hour),
        "execution_price_per_gpu_hour": f(r.actual_price_per_gpu_hour),
        "provision_ok": r.provision_ok, "provision_latency_ms": r.provision_latency_ms, "attempts": r.attempts,
        "uptime_seconds": r.uptime_seconds, "interruptions": r.interruptions,
        "termination_reason": r.termination_reason, "workload_completed": r.workload_completed,
        "provider_cost_usd": f(r.provider_cost_usd), "cost_basis": r.cost_basis,
        "usage_recorded": r.usage_record_id is not None,
    }
