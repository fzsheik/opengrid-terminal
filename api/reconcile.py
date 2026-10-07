"""Admin endpoints over reconciliation, orphan resources and provider validation (routing/reconcile.py,
routing/validation.py). Every route requires the admin scope.

    GET  /v1/admin/reconcile/runs            recent reconciliation runs and their findings
    POST /v1/admin/reconcile/run             run one pass now (optionally ?provider=)
    GET  /v1/admin/orphans                   orphan resources (?status=open|terminating|...)
    POST /v1/admin/orphans/{id}/resolve      {action: terminate|ignore|adopt, reason}   (*)
    POST /v1/admin/validation/start          {provider, gpu?}: creates a validation route pending approval (*)
    GET  /v1/admin/validation/{deployment_id}   the validation evidence checklist (read-only)
    POST /v1/admin/validation/{deployment_id}/mark   re-check and, when every step has evidence, mark validated (*)

(*) Idempotency-Key header required: a double click must never terminate or launch twice.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from accounts.auth import Principal, require_scope
from api.common import envelope
from routing import idempotency

router = APIRouter()
ADMIN = require_scope("admin")


def _by(who: Principal) -> str:
    return f"{who.kind}:{who.key_id}" if who.key_id else who.kind


def _idem(request: Request, who: Principal, scope: str, body, fn) -> JSONResponse:
    code, payload, replayed = idempotency.run(who=who, scope=scope, key=request.headers.get("Idempotency-Key"),
                                              body=body, fn=fn)
    return JSONResponse(payload, status_code=code, headers={"Idempotent-Replayed": "true"} if replayed else None)


def _validation_http(e: Exception) -> HTTPException:
    return HTTPException(404 if "no such deployment" in str(e) else 422,
                         {"code": "validation_refused", "message": str(e)})


class Resolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(..., pattern="^(terminate|ignore|adopt)$")
    reason: str = Field(..., min_length=3, max_length=500)


class StartValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(..., min_length=2, max_length=64)
    gpu: str | None = Field(None, max_length=160)


@router.get("/v1/admin/reconcile/runs", tags=["admin"], summary="Recent reconciliation runs")
def reconcile_runs(limit: int = Query(50, ge=1, le=500), who: Principal = Depends(ADMIN)):
    from routing import reconcile

    return envelope(reconcile.runs(limit), methodology="reconciliation", last_run=reconcile.last_run())


@router.post("/v1/admin/reconcile/run", tags=["admin"], summary="Run one reconciliation pass now")
def reconcile_now(provider: str | None = None, who: Principal = Depends(ADMIN)):
    from routing import reconcile

    # Safe to repeat: a pass only reads provider state and applies evidence-backed transitions.
    return envelope(reconcile.run_once(provider, trigger=f"manual:{_by(who)}"), methodology="reconciliation")


@router.get("/v1/admin/orphans", tags=["admin"], summary="Orphan resources: instances that may bill without a live deployment")
def list_orphans(status: str | None = Query(None, max_length=32), who: Principal = Depends(ADMIN)):
    from routing import reconcile

    items = reconcile.orphans(status)
    open_items = [o for o in items if o.get("status") in ("open", "terminating")]
    return envelope(items, methodology="reconciliation", open=len(open_items))


@router.post("/v1/admin/orphans/{orphan_id}/resolve", tags=["admin"], summary="Terminate, ignore or adopt an orphan")
def resolve_orphan(orphan_id: int, body: Resolve, request: Request, who: Principal = Depends(ADMIN)):
    from routing import reconcile

    def run():
        if not reconcile.orphans_by_id(orphan_id):
            raise HTTPException(404, "orphan not found")
        try:
            out = reconcile.resolve_orphan(orphan_id, body.action, _by(who), note=body.reason)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return 200, envelope(out, methodology="reconciliation")

    return _idem(request, who, f"orphan_resolve:{orphan_id}", body.model_dump(), run)


@router.post("/v1/admin/validation/start", tags=["admin"],
             summary="Create a provider validation route (1 GPU, capped cost and runtime; still needs approval)")
def start_validation(body: StartValidation, request: Request, who: Principal = Depends(ADMIN)):
    from routing import validation

    def run():
        try:
            out = validation.start_validation(body.provider.lower(), by=_by(who), gpu=body.gpu)
        except Exception as e:  # ValidationError / ControlError / guard refusals: definitive answers
            if isinstance(e, HTTPException):
                raise
            raise HTTPException(422, {"code": "validation_refused", "message": str(e)})
        return 202, envelope(out, methodology="provider-capabilities")

    return _idem(request, who, "validation_start", body.model_dump(), run)


@router.get("/v1/admin/validation/{deployment_id}", tags=["admin"], summary="Validation evidence for a deployment")
def validation_status(deployment_id: str, who: Principal = Depends(ADMIN)):
    from routing import validation

    # Read-only: never marks the provider validated from a GET.
    try:
        out = validation.validation_report(deployment_id, by=None, mark=False, live_check=False)
    except validation.ValidationError as e:
        raise _validation_http(e)
    return envelope(out, methodology="provider-capabilities")


@router.post("/v1/admin/validation/{deployment_id}/mark", tags=["admin"],
             summary="Re-check the full cycle and mark the provider validated if every step has evidence")
def validation_mark(deployment_id: str, request: Request, who: Principal = Depends(ADMIN)):
    from routing import validation

    def run():
        try:
            out = validation.validation_report(deployment_id, by=_by(who), mark=True, live_check=True)
        except validation.ValidationError as e:
            raise _validation_http(e)
        return 200, envelope(out, methodology="provider-capabilities")

    return _idem(request, who, f"validation_mark:{deployment_id}", {"deployment_id": deployment_id}, run)
