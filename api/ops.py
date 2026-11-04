"""Data quality, trust/freshness, internal ops monitoring.

    /v1/ops/*      operator only (admin scope): health, incidents, quarantine decisions
    /v1/trust/*    public data reads (data:read): trust blocks, provider health without
                   internal error text
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import text

import normalize
from accounts.auth import Principal, require_scope
from api.common import INFERRED, OBSERVED, envelope, page, resolve_gpu
from quality import incidents, ops, quarantine, schema_watch, trust  # noqa: F401  (schema_watch registers its job)

router = APIRouter()
admin = require_scope("admin")
reader = require_scope("data:read")

M_QUALITY, M_TRUST = "data-quality", "data-trust"


@router.get("/v1/ops/summary", summary="Ops: everything that is broken or suspicious, in one view")
def ops_summary(who: Principal = Depends(admin)):
    return envelope(ops.summary(), kind=OBSERVED, methodology=M_QUALITY)


@router.get("/v1/ops/providers", summary="Ops: per-provider poll health, latency, errors")
def ops_providers(who: Principal = Depends(admin)):
    return envelope(trust.all_provider_health(), kind=OBSERVED, methodology=M_TRUST)


@router.get("/v1/ops/incidents", summary="Ops: quality incidents (schema changes, empty responses, flags)")
def ops_incidents(status: str | None = Query(None, pattern="^(open|resolved)$"), kind: str | None = None,
                  provider: str | None = None, limit: int = Query(200, ge=1, le=1000),
                  who: Principal = Depends(admin)):
    rows = incidents.recent(provider=provider, kinds=[kind] if kind else None, status=status, limit=limit)
    return envelope(rows, kind=OBSERVED, methodology=M_QUALITY)


@router.get("/v1/ops/quarantine", summary="Ops: held values awaiting a decision")
def ops_quarantine(status: str | None = Query("pending", pattern="^(pending|accepted|rejected|auto_accepted|superseded|all)$"),
                   provider: str | None = None, limit: int = Query(200, ge=1, le=1000),
                   who: Principal = Depends(admin)):
    rows = quarantine.queue(None if status == "all" else status, provider, limit)
    return envelope(rows, kind=OBSERVED, methodology=M_QUALITY)


def _resolve(qid: int, decision: str, who: Principal, note: str | None):
    by = "operator" if who.kind == "operator" else f"key:{who.key_id}"
    try:
        out = quarantine.resolve(qid, decision, by, note)
    except KeyError:
        raise HTTPException(404, f"no quarantine entry {qid}")
    except quarantine.NotPending as exc:
        raise HTTPException(409, f"quarantine entry {qid} is already {exc}")
    return envelope(out, kind=OBSERVED, methodology=M_QUALITY)


@router.post("/v1/ops/quarantine/{qid}/accept", summary="Ops: accept a held value and apply it now")
def ops_accept(qid: int, note: str | None = Body(None, embed=True), who: Principal = Depends(admin)):
    return _resolve(qid, "accept", who, note)


@router.post("/v1/ops/quarantine/{qid}/reject", summary="Ops: reject a held value (it stays held while sent)")
def ops_reject(qid: int, note: str | None = Body(None, embed=True), who: Principal = Depends(admin)):
    return _resolve(qid, "reject", who, note)


@router.get("/v1/ops/schema-changes", summary="Ops: source JSON shape changes per provider endpoint")
def ops_schema_changes(provider: str | None = None, limit: int = Query(200, ge=1, le=1000),
                       who: Principal = Depends(admin)):
    return envelope(schema_watch.changes(provider, limit), kind=OBSERVED, methodology=M_QUALITY)


@router.get("/v1/ops/stale", summary="Ops: listings not seen within their provider's polling window")
def ops_stale(provider: str | None = None, include_aging: bool = True,
              limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
              who: Principal = Depends(admin)):
    items, pg = page(ops.stale_listings(include_aging, provider), limit, offset)
    return envelope(items, kind=OBSERVED, methodology=M_TRUST, pagination=pg)


@router.get("/v1/ops/unmapped", summary="Ops: raw GPU names with no canonical mapping")
def ops_unmapped(who: Principal = Depends(admin)):
    return envelope(normalize.unmapped_gpus(), kind=OBSERVED, methodology=M_QUALITY)


@router.get("/v1/ops/internal-data", summary="Ops: INTERNAL-tier data (never public): registry, listings, signals")
def ops_internal_data(provider: str | None = None, who: Principal = Depends(admin)):
    """Everything OpenGrid reads but may not republish, each row labelled visibility='internal'
    (methodology/data-visibility.md). Admin only; never linked from a public page."""
    import provider_meta
    from routing import internal_data
    from sqlalchemy import select

    from store.capacity import CapacityCurrent

    with normalize.SessionLocal() as s:
        cap = [{c.name: getattr(r, c.name) for c in CapacityCurrent.__table__.columns}
               for r in s.scalars(select(CapacityCurrent).where(CapacityCurrent.visibility != "public"))]
    try:
        bands = internal_data.spot_interruption_bands()
    except Exception as exc:  # noqa: BLE001  (a missing / reshaped feed is reported, not raised)
        bands, bands_err = [], f"{type(exc).__name__}: {exc}"[:300]
    else:
        bands_err = None
    return envelope({"visibility": provider_meta.INTERNAL, "registry": provider_meta.visibility_registry(),
                     "listings": internal_data.internal_listings(provider), "capacity": cap,
                     "signals": {"aws_spot_interruption_bands": bands, "error": bands_err}},
                    kind=OBSERVED, methodology="data-visibility",
                    note="INTERNAL data: usable by routing, recommendations and deployment; never republish")


@router.get("/v1/ops/jobs", summary="Ops: background job health")
def ops_jobs(who: Principal = Depends(admin)):
    return envelope(ops.jobs_health(), kind=OBSERVED, methodology=M_QUALITY)


@router.get("/v1/ops/storage", summary="Ops: database sizes, deduplication and retention")
def ops_storage(who: Principal = Depends(admin)):
    import storage
    return envelope(storage.status(), kind=OBSERVED, methodology=M_QUALITY)


# --------------------------------------------------------------------------
# Public trust reads
# --------------------------------------------------------------------------

_LISTING_COLS = (
    "provider, listing_id, sku, raw_gpu_name, canonical_gpu_name, gpu_count, region, country, "
    "price_per_gpu_hour, price_per_instance_hour, currency, market_type, provider_tier, interruptible, "
    "available, capacity, capacity_unit, observed_at, first_seen_at"
)


@router.get("/internal/trust/providers", summary="Provider feed health (no internal error text)")
def trust_providers(who: Principal = Depends(reader)):
    import provider_meta  # a wholly internal provider is not named on public lists

    return envelope([trust.public_health(h) for h in trust.all_provider_health()
                     if provider_meta.is_public_provider(h["provider"])],
                    kind=OBSERVED, methodology=M_TRUST)
