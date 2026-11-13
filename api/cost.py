"""Full cost: cost components beside the hourly price, listing spec fields, and the reference-job estimator.

    GET  /v1/costs/components   egress, storage and other components per provider (filter provider, component)
    GET  /v1/costs/billing      billing increment and minimum per provider, with sources
    GET  /v1/costs/sources      the machine-readable cost sources: last fetch, last success, error
    GET  /v1/costs/presets      the published reference jobs R0-R3
    POST /v1/costs/estimate     {gpu, count, hours, egress_tb | egress_gb, storage_tb | storage_gb, storage_class,
                                min_fabric_gbps} or {"preset": "R1"} -> full cost per provider, unknowns named
    GET  /v1/costs/rl-presets   the RL reference jobs RL1-RL3
    POST /v1/costs/rl-estimate  {trainer, rollouts, sandboxes, weights_gb, syncs_per_hour, sync_fanout, hours, storage_gb,
                                ...} or {"preset": "RL1"} -> same-cluster vs split vs best RL plan, break-even,
                                sensitivity, caveats (cost/rl.py; methodology/rl-cost.md)

Public like /v1/reserved (api/pages.py PUBLIC_V1 "cost"; the POST is in PUBLIC_POST: a read-only computation).
Nothing here enters listings, indices, spreads, percentiles or routing. Methodology: /methodology/full-cost, /methodology/rl-cost.
Errors: 413 body over 16 KB, 415 not JSON, 422 {code, message}.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from accounts.auth import Principal, require_scope
from api.common import ESTIMATED, OBSERVED, envelope, resolve_gpu
from cache import ttl_cache
from cost import components as cc
from cost import estimate as est
from cost import manual, rl, sources

router = APIRouter()
READ = require_scope("data:read")
METHOD = "full-cost"
RULE = "Cost components and estimates are never part of listings, indices, spreads, market statistics or routing."
MAX_BODY = 16 * 1024
MAX_HOURS, MAX_GB, MAX_COUNT = 24 * 365, 10_000_000, 1024

sources.register()  # the cost_components background job (off when the poller is off)


@ttl_cache(300)
def _components():
    comps, notes, _ = est.load_components()
    return comps, notes


@router.get("/v1/costs/components", tags=["cost"], summary="Egress, storage and other cost components per provider")
def components(provider: str | None = None, component: str | None = None, limit: int = Query(100, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    if component and component not in cc.COMPONENTS:
        raise HTTPException(422, {"code": "invalid_component", "message": f"one of {list(cc.COMPONENTS)}"})
    comps, notes = _components()
    rows = [cc.to_json(c) for c in comps if (not provider or c["provider"] == provider)
            and (not component or c["component"] == component)]
    return envelope(page=(limit, cursor), data=rows, kind=OBSERVED, methodology=METHOD, count=len(rows), notes=notes, rule=RULE,
                    kinds={"api": "observed", "provider": "observed", "manual": "manual (source + verified_at)"},
                    providers=sorted({c["provider"] for c in comps}))


@router.get("/v1/costs/billing", tags=["cost"], summary="Billing increment and minimum per provider")
def billing(limit: int = Query(100, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    rows = [{"provider": p, **{k: b.get(k) for k in ("increment_s", "minimum_s", "status", "source", "notes")},
             "products": b.get("products"), "verified_at": manual.V} for p, b in sorted(manual.BILLING.items())]
    return envelope(page=(limit, cursor), data=rows, kind=OBSERVED, methodology=METHOD, count=len(rows))


@router.get("/v1/costs/sources", tags=["cost"], summary="Machine-readable cost sources and their last fetch")
def cost_sources(limit: int = Query(100, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    import normalize

    with normalize.SessionLocal() as s:
        snaps = sources.latest(s)
    rows = [{"source": k, "provider": src.provider, "url": src.url, "description": src.note,
             "last_ok_fetch_at": snaps[k]["fetched_at"], "last_attempt_at": snaps[k]["last_attempt_at"],
             "last_attempt_ok": snaps[k]["last_ok"], "last_error": snaps[k]["last_error"]}
            for k, src in sources.SOURCES.items()]
    return envelope(page=(limit, cursor), data=rows, kind=OBSERVED, methodology=METHOD, count=len(rows), poll_seconds=sources.POLL_SECONDS)


@router.get("/v1/costs/presets", tags=["cost"], summary="The published reference jobs")
def presets(limit: int = Query(100, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    return envelope(page=(limit, cursor), data=[{"preset": k, **v} for k, v in est.PRESETS.items()], kind=ESTIMATED, methodology=METHOD)


async def _body(request: Request) -> dict:
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        raise HTTPException(415, {"code": "json_required", "message": "send Content-Type: application/json"})
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


def _num(body: dict, key: str, lo: float, hi: float, default=None, integer=False):
    v = body.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise HTTPException(422, {"code": "invalid_" + key, "message": f"{key} must be a number"})
    if not lo <= v <= hi:
        raise HTTPException(422, {"code": "invalid_" + key, "message": f"{key} must be between {lo} and {hi}"})
    return v


def parse_job(body: dict) -> dict:
    """A request body -> the estimator's job. Terabytes are decimal (1 TB = 1,000 GB)."""
    unknown_keys = set(body) - {"preset", "gpu", "count", "hours", "egress_tb", "egress_gb", "storage_tb",
                                "storage_gb", "storage_class", "min_fabric_gbps"}
    if unknown_keys:
        raise HTTPException(422, {"code": "unknown_field", "message": f"unknown field(s): {sorted(unknown_keys)}"})
    job: dict = {}
    if body.get("preset") is not None:
        if body["preset"] not in est.PRESETS:
            raise HTTPException(422, {"code": "invalid_preset", "message": f"one of {list(est.PRESETS)}"})
        job = {k: v for k, v in est.PRESETS[body["preset"]].items() if k != "label"}
    if body.get("gpu") is not None or "gpu" not in job:
        gpu = resolve_gpu(body.get("gpu")) if isinstance(body.get("gpu"), str) and body.get("gpu") else None
        if not gpu:
            raise HTTPException(422, {"code": "invalid_gpu", "message": "gpu: a GPU slug or canonical name"})
        job["gpu"] = gpu
    for key, lo, hi, integer in (("count", 1, MAX_COUNT, True), ("hours", 1 / 60, MAX_HOURS, False)):
        v = _num(body, key, lo, hi, integer=integer)
        if v is not None:
            job[key] = v
        elif key not in job:
            raise HTTPException(422, {"code": "missing_" + key, "message": f"{key} is required"})
    for name in ("egress", "storage"):
        tb, gb = _num(body, f"{name}_tb", 0, MAX_GB / 1000), _num(body, f"{name}_gb", 0, MAX_GB)
        if tb is not None and gb is not None:
            raise HTTPException(422, {"code": f"invalid_{name}", "message": f"give {name}_tb or {name}_gb, not both"})
        if tb is not None:
            job[f"{name}_gb"] = tb * 1000
        elif gb is not None:
            job[f"{name}_gb"] = gb
        job.setdefault(f"{name}_gb", 0)
    sc = body.get("storage_class", "auto")
    if sc not in est.STORAGE_ORDER:
        raise HTTPException(422, {"code": "invalid_storage_class", "message": f"one of {list(est.STORAGE_ORDER)}"})
    job["storage_class"] = sc
    job["min_fabric_gbps"] = _num(body, "min_fabric_gbps", 1, 100_000)
    return job


@router.post("/v1/costs/estimate", tags=["cost"],
             summary="Full cost of a job per provider: compute + egress + storage, unknown components named")
async def cost_estimate(request: Request, who: Principal = Depends(READ)):
    job = parse_job(await _body(request))
    out = est.load(job)
    return envelope(out["rows"], kind=ESTIMATED, methodology=METHOD, count=len(out["rows"]), job=job,
                    unranked=out["unranked"], notes=out["notes"], rule=RULE, price_concept="list_price",
                    lower_bound=sum(1 for r in out["rows"] if r["total_is_lower_bound"]))


# --------------------------------------------------------------------------- RL post-training cost (cost/rl.py)

@router.get("/v1/costs/rl-presets", tags=["cost"], summary="The RL reference jobs")
def rl_presets(limit: int = Query(100, ge=1, le=1000), cursor: str | None = None, who: Principal = Depends(READ)):
    return envelope(page=(limit, cursor), data=[{"preset": k, **v} for k, v in rl.PRESETS.items()], kind=ESTIMATED,
                    methodology="rl-cost")


@router.post("/v1/costs/rl-estimate", tags=["cost"],
             summary="RL post-training cost: trainer + rollouts + sandboxes + weight syncs, same cluster vs split")
async def rl_estimate(request: Request, who: Principal = Depends(READ)):
    try:
        job = rl.parse(await _body(request), lambda v: resolve_gpu(v, strict=False))
    except rl.RLInputError as e:
        raise HTTPException(422, {"code": e.code, "message": e.message}) from None
    out = rl.load(job)
    notes = out.pop("notes")
    return envelope(out, kind=ESTIMATED, methodology="rl-cost", job=job, notes=notes, rule=RULE,
                    price_concept="list_price")
