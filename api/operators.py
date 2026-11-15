"""Marketplace operators and cross-marketplace hardware matches (operators.py). ADMIN ONLY, INTERNAL: operator
keys never reach a public page, the public API, an index or a statistic (methodology/internal/operators.md).

    GET /v1/ops/operators            per marketplace (lium, vast): supply concentration and one row per operator
    GET /v1/ops/operators/overlap    Lium against Vast: machines with the same hardware fingerprint, the tier of
                                     each match, the price each side asks, and the operator pairs behind them

Computed on read from the stored raw copies; nothing is polled or written for it.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

import operators
from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from cache import ttl_cache

router = APIRouter()
admin = require_scope("admin")
DOC = "methodology/internal/operators.md (in the repository; never served as a page)"
NOTE = "INTERNAL data: operator keys are never republished"
CAVEAT = ("A fingerprint match is a candidate, not proof: 'strong' = same GPU model, GPU count, CPU model, country "
          "and RAM within 3%; 'build' = same GPU model, CPU model and country. See fingerprint_unique.")


@ttl_cache(300)
def _model():
    return operators.load()


def _as_of(model) -> dict:
    return {k: v.isoformat() if v else None for k, v in model["as_of"].items()}


@router.get("/v1/ops/operators", tags=["ops"], include_in_schema=False,
            summary="Ops: operators per marketplace and supply concentration (internal)")
def operators_read(marketplace: str | None = None, limit: int = Query(50, ge=1, le=1000),
                   who: Principal = Depends(admin)):
    if marketplace and marketplace not in operators.MARKETPLACES:
        raise HTTPException(404, f"no operator data for {marketplace!r}; one of {list(operators.MARKETPLACES)}")
    model = _model()
    out = {}
    for name in operators.MARKETPLACES:
        if marketplace and name != marketplace:
            continue
        summary = operators.operators(model["machines"][name])
        summary["rows"] = summary["rows"][:limit]
        out[name] = summary
    return envelope(out, kind=OBSERVED, visibility="internal", note=NOTE, doc=DOC, source_as_of=_as_of(model),
                    coverage=model["coverage"])


@router.get("/v1/ops/operators/overlap", tags=["ops"], include_in_schema=False,
            summary="Ops: Lium machines whose hardware fingerprint matches a Vast machine (internal)")
def operators_overlap(tier: str | None = Query(None, pattern="^(strong|build)$"), limit: int = Query(200, ge=1, le=5000),
                      who: Principal = Depends(admin)):
    model = _model()
    out = operators.overlap(model["machines"]["lium"], model["machines"]["vast"])
    matches = [m for m in out["matches"] if not tier or m["tier"] == tier]
    out["matches"] = matches[:limit]
    return envelope(out, kind=OBSERVED, visibility="internal", note=NOTE, caveat=CAVEAT, doc=DOC, a="lium", b="vast",
                    machines={k: len(v) for k, v in model["machines"].items()}, count=len(matches),
                    source_as_of=_as_of(model), coverage=model["coverage"])
