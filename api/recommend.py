"""Workload recommendations API (docs/OPENGRID2_CONTRACTS.md sections 2-3; methodology/recommendations.md).

    POST /v1/recommend/estimate   WorkloadRequirements -> Estimate (memory, GPU count, questions)      [data:read]
    POST /v1/recommend            WorkloadRequirements (+ "limit": 1-20) -> Recommendation              [data:read]

Both are read-only computations: the estimate touches no database; the recommendation reads market listings
through routing.scoring.rank_listings (never routing.engine.preview: no audit, quote or route rows). With
PUBLIC_PAGES on they are callable anonymously (api/pages.PUBLIC_POST), under the anonymous per-IP rate limit;
execution readiness for an anonymous caller is reported as "API key / sign-in required", never assumed.
Errors: 413 body over 32 KB, 415 not JSON, 422 {code, message, errors?} for validation.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from accounts.auth import Principal, require_scope
from api.common import ESTIMATED, envelope
from intelligence.estimate import estimate
from intelligence.recommend import RecommendError, recommend
from intelligence.requirements import WorkloadRequirements

router = APIRouter()
READ = require_scope("data:read")
METH = "recommendations"
MAX_BODY = 32 * 1024
MAX_LIMIT = 20


async def _body(request: Request) -> dict:
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        raise HTTPException(415, {"code": "json_required", "message": "send Content-Type: application/json"})
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY:
        raise HTTPException(413, {"code": "body_too_large", "message": f"request body over {MAX_BODY} bytes"})
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, {"code": "body_too_large", "message": f"request body over {MAX_BODY} bytes"})
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(422, {"code": "invalid_json", "message": "the body is not valid JSON"}) from None
    if not isinstance(body, dict):
        raise HTTPException(422, {"code": "invalid_request", "message": "the body must be a JSON object"})
    return body


def _requirements(body: dict) -> WorkloadRequirements:
    try:
        return WorkloadRequirements.model_validate(body)
    except ValidationError as e:
        errs = [{"field": ".".join(str(p) for p in x["loc"]), "message": x["msg"], "type": x["type"]}
                for x in e.errors()[:20]]
        raise HTTPException(422, {"code": "invalid_requirements",
                                  "message": "; ".join(f"{x['field']}: {x['message']}" for x in errs[:5]),
                                  "errors": errs}) from None


@router.post("/v1/recommend/estimate", tags=["recommend"],
             summary="Estimate GPU memory, GPU count and open questions for a workload (pure computation)")
async def recommend_estimate(request: Request, who: Principal = Depends(READ)):
    req = _requirements(await _body(request))
    return envelope(estimate(req), kind=ESTIMATED, methodology=METH)


@router.post("/v1/recommend", tags=["recommend"],
             summary="Recommend GPUs for a workload: estimate + ranked fresh market options (read-only)")
async def recommend_route(request: Request, who: Principal = Depends(READ)):
    body = await _body(request)
    limit = body.pop("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise HTTPException(422, {"code": "invalid_limit", "message": f"limit must be an integer 1-{MAX_LIMIT}"})
    req = _requirements(body)
    try:
        out = recommend(req, who, limit=limit)
    except RecommendError as e:
        raise HTTPException(422, {"code": e.code, "message": e.message}) from None
    return envelope(out, kind="mixed", methodology=METH,
                    kinds={"prices": "observed", "memory": ESTIMATED, "cost": ESTIMATED, "hardware": "theoretical",
                           "execution": "inferred"},
                    price_concept="observed_market_price")
