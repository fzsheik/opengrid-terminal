"""Marketplace operators and cross-marketplace hardware matches (operators.py). ADMIN ONLY, INTERNAL: operator
keys never reach a public page, the public API, an index or a statistic (methodology/internal/operators.md).

    GET /v1/ops/operators            per marketplace (lium, vast): supply concentration and one row per operator
    GET /v1/ops/operators/overlap    Lium against Vast: machines with the same hardware fingerprint, the tier of
                                     each match, the price each side asks, and the operator pairs behind them

    GET /v1/ops/operators/history    the stored daily series: concentration per marketplace and the matches per day

The two reads above are computed on read from the stored raw copies. The daily series is written by the hourly
`operators_daily` job (operators.run_daily) and rebuilt for past days by `python -m operators backfill`.
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


operators.register()  # operators_daily


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


@router.get("/v1/ops/operators/history", tags=["ops"], include_in_schema=False,
            summary="Ops: operators and hardware matches per day (internal)")
def operators_history(days: int = Query(90, ge=1, le=1095), who: Principal = Depends(admin)):
    out = operators.history(days)
    return envelope(out, kind=OBSERVED, visibility="internal", note=NOTE, doc=DOC, caveat=CAVEAT, window_days=days,
                    coverage_values="a row's `coverage` says what that day's stored data covered; days with "
                                    "different coverage are not comparable (operators.COVERAGE_*)")
