"""Product analytics: event intake, the activation funnel, top pages and searches.

    POST /v1/events/track     a batch of client events (anonymous allowed; rate-limited)   [none / any principal]
    GET  /v1/admin/funnel     visitor -> market user -> account -> key -> preview -> deployment -> 2nd, by week [admin]
    GET  /v1/admin/product    top pages, searches, GPUs, repeat visitors                  [admin]
    POST /v1/admin/product/sync   derive server events (api calls, previews, approvals, completions) now [admin]

/v1/events/track must be reachable without the site password for anonymous public pages: the lead
adds it to api/pages.py (is_public: POST of exactly this path). Until then, only signed-in
operators and API keys can send events. No IP address is stored; see analytics/product.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from accounts.auth import Principal, principal, require_scope
from analytics import product
from api.common import envelope

router = APIRouter()
admin = require_scope("admin")


def _who(request: Request) -> Principal | None:
    """The caller if authenticated; None for an anonymous visitor (no auth header at all)."""
    if not request.headers.get("authorization"):
        try:
            return principal(request)  # open local dev / site session -> operator
        except HTTPException:
            return None
    return principal(request)  # a bad key is a 401, not silently anonymous


@router.post("/v1/events/track", tags=["product"], status_code=202, summary="Record product events (batch)")
def track(request: Request, body: dict = Body(..., examples=[{"events": [
        {"event": "page_view", "anon_id": "a1b2c3d4e5f6", "props": {"page": "/gpus/h100-80gb-sxm5"}}]}])):
    who = _who(request)
    events = body.get("events") if isinstance(body, dict) else None
    anon = next((e.get("anon_id") for e in events or [] if isinstance(e, dict) and e.get("anon_id")), None)
    ip = request.client.host if request.client else None
    if not product.allow(product.client_key(ip, anon)):
        raise HTTPException(429, "too many event batches; slow down", headers={"Retry-After": "60"})
    account_id = who.account_id if who is not None and who.kind == "api_key" else None
    try:
        out = product.track(events, account_id=account_id, internal=bool(who and who.kind == "operator"))
    except product.Rejected as e:
        raise HTTPException(422, str(e))
    return envelope(out, note="no IP address or personal data is stored; props are sanitized")


@router.get("/v1/admin/funnel", tags=["admin"], summary="Activation funnel by week")
def admin_funnel(weeks: int = Query(8, ge=1, le=104), who: Principal = Depends(admin)):
    return envelope(product.funnel(weeks), methodology="economics")


@router.get("/v1/admin/product", tags=["admin"], summary="Top pages, searches and repeat visitors")
def admin_product(days: int = Query(30, ge=1, le=365), limit: int = Query(20, ge=1, le=200),
                  who: Principal = Depends(admin)):
    return envelope(product.summary(days, limit))


@router.post("/v1/admin/product/sync", tags=["admin"], summary="Derive server-side product events now")
def admin_product_sync(full: bool = False, who: Principal = Depends(admin)):
    return envelope(product.sync_server_events(None if full else 3))
