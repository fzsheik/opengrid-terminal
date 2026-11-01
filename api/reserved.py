"""Reserved and committed GPU prices: their own section, never part of on-demand listings, indices or statistics.

    GET /v1/reserved          one row per (provider, GPU, GPUs per machine, region, term); filters gpu, provider, term
    GET /v1/reserved/summary  per GPU: the cheapest row in each term bucket; filter gpu

Computed on read from stored raw snapshots (reserved.py); nothing is polled or written for it. Public like
/v1/capacity (api/pages.py PUBLIC_V1, same data:read scope for keys). Methodology: /methodology/reserved-prices.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

import reserved
from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope, resolve_gpu
from cache import ttl_cache

router = APIRouter()
READ = require_scope("data:read")
METHOD = "reserved-prices"
RULE = "Reserved and committed prices are never part of on-demand listings, indices, spreads or market statistics."


@ttl_cache(60)
def _model():
    return reserved.load()


def _filters(provider: str | None, term: str | None):
    if provider and provider not in reserved.PROVIDERS:
        raise HTTPException(404, f"no reserved prices for provider {provider!r}; supported: {list(reserved.PROVIDERS)}")
    if term and term not in reserved.BUCKET_KEYS:
        raise HTTPException(422, f"unknown term {term!r}; one of {list(reserved.BUCKET_KEYS)}")


def _meta(notes, newest) -> dict:
    return {"providers": list(reserved.PROVIDERS), "sources": reserved.SOURCES, "notes": notes, "rule": RULE,
            "terms": [{"key": k, "months": m, "label": label} for k, m, label in reserved.TERM_BUCKETS],
            "source_as_of": newest.isoformat() if newest else None}


@router.get("/v1/reserved", tags=["reserved"], summary="Reserved and committed GPU prices, with the on-demand saving")
def rows(gpu: str | None = Query(None, description="GPU slug or canonical name"), provider: str | None = None,
         term: str | None = Query(None, description="term bucket: " + ", ".join(reserved.BUCKET_KEYS)),
         who: Principal = Depends(READ)):
    _filters(provider, term)
    want = resolve_gpu(gpu) if gpu else None
    data, notes, newest = _model()
    out = [r for r in data if (not want or r["gpu"] == want) and (not provider or r["provider"] == provider)
           and (not term or r["term_bucket"] == term)]
    return envelope(out, kind=OBSERVED, methodology=METHOD, count=len(out),
                    derived=sum(1 for r in out if r["derived"]), **_meta(notes, newest))


@router.get("/v1/reserved/summary", tags=["reserved"], summary="Per GPU: the cheapest reserved price in each term")
def summary(gpu: str | None = Query(None, description="GPU slug or canonical name"), who: Principal = Depends(READ)):
    want = resolve_gpu(gpu) if gpu else None
    data, notes, newest = _model()
    out = [g for g in reserved.summary(data) if not want or g["gpu"] == want]
    return envelope(out, kind=OBSERVED, methodology=METHOD, count=len(out), **_meta(notes, newest))
