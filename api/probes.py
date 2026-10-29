"""OpenGrid's own provider probes: raw results in, rollups that tell regional from global out.

    GET  /v1/probes/targets          what is probed (public URLs, accepted statuses, provider region)
    POST /v1/probes/ingest           a remote agent posts raw results                       [probes:write]
    GET  /v1/probes/summary          per provider: status now, availability, latency, who reported, failure reasons
    GET  /v1/probes/targets/{id}     one target: hourly series, per-vantage-point latency, recent raw failures
    GET  /v1/probes/dips             every hour with a failed check: vantage-point scope, provider-region context,
                                     failure reasons, overlapping provider-reported incidents
    GET  /v1/probes/raw              raw results, newest first (audit)
    GET  /v1/probes/agents           the vantage points that have reported

Measured by OpenGrid itself, from the vantage points listed in /agents. A single vantage point cannot tell a provider
outage from a path problem; the scope fields say so. Methodology: /methodology/probes.
"""

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from probes import run as probe_run
from probes import store, targets

probe_run.register()  # the probe_local + probe_rollup background jobs

router = APIRouter()
READ = require_scope("data:read")
WRITE = require_scope("probes:write")
METHOD = "probes"
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")
_LOC = re.compile(r"^[a-z0-9-]{2,32}$")


def _env(data, **meta):
    return envelope(data, kind=OBSERVED, methodology=METHOD, source="opengrid_probes", **meta)


class ProbeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(max_length=96)
    observed_at: datetime
    ok: bool
    status_code: int | None = Field(None, ge=100, le=599)
    error_class: str | None = Field(None, max_length=24)
    error: str | None = Field(None, max_length=400)
    dns_ms: int | None = Field(None, ge=0, le=600_000)
    connect_ms: int | None = Field(None, ge=0, le=600_000)
    tls_ms: int | None = Field(None, ge=0, le=600_000)
    ttfb_ms: int | None = Field(None, ge=0, le=600_000)
    total_ms: int | None = Field(None, ge=0, le=600_000)
    resolved_ip: str | None = Field(None, max_length=64)
    location: str | None = Field(None, max_length=32)  # accepted from the agent; the batch's location is what is stored


class IngestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    location: str
    results: list[ProbeIn] = Field(max_length=500)


@router.get("/v1/probes/targets", tags=["probes"], summary="What OpenGrid probes")
def list_targets(who: Principal = Depends(READ)):
    return _env([t.public() for t in targets.TARGETS], count=len(targets.TARGETS))


@router.post("/v1/probes/ingest", tags=["probes"], summary="Post raw probe results from a remote agent")
def ingest(body: IngestIn, who: Principal = Depends(WRITE)):
    if not _ID.match(body.agent_id) or not _LOC.match(body.location):
        raise HTTPException(422, "agent_id must be 1-48 of A-Za-z0-9_.:- and location 2-32 lowercase letters, digits or dashes")
    out = store.record([r.model_dump() for r in body.results], body.agent_id, body.location)
    return _env(out)


@router.get("/v1/probes/summary", tags=["probes"], summary="Per provider: status, availability, latency, reasons")
def summary(hours: int = Query(24, ge=1, le=720), who: Principal = Depends(READ)):
    rows = store.summary(hours)
    return _env(rows, hours=hours, agents=store.agents(), providers_with_targets=len(rows))


@router.get("/v1/probes/targets/{target_id:path}", tags=["probes"], summary="One target: series, per vantage point, failures")
def one_target(target_id: str, hours: int = Query(24, ge=1, le=720), who: Principal = Depends(READ)):
    t = targets.BY_ID.get(target_id)
    if t is None:
        raise HTTPException(404, f"unknown target {target_id!r}")
    return _env({**t.public(), **store.target_series(target_id, hours), "recent_failures": store.raw(target_id, limit=20, failures_only=True)},
                hours=hours)


@router.get("/v1/probes/dips", tags=["probes"], summary="Hours with a failed check, with scope and reason")
def dips(provider: str, days: int = Query(7, ge=1, le=30), who: Principal = Depends(READ)):
    if not targets.for_provider(provider):
        raise HTTPException(404, f"no probe targets for provider {provider!r}")
    return _env(store.dips(provider, days), provider=provider, days=days, agents=len(store.agents()),
                scope_note=("scope is by vantage point: global = every reporting location failed (at least two reported); "
                            "regional = some failed while others succeeded; unconfirmed = only one location reported. "
                            "region_scope compares the provider's own regional endpoints in the same hour."))


@router.get("/v1/probes/raw", tags=["probes"], summary="Raw probe results, newest first")
def raw(target: str, location: str | None = None, limit: int = Query(100, ge=1, le=500), failures_only: bool = False,
        who: Principal = Depends(READ)):
    if target not in targets.BY_ID:
        raise HTTPException(404, f"unknown target {target!r}")
    return _env(store.raw(target, location, limit, failures_only), target=target)


@router.get("/v1/probes/agents", tags=["probes"], summary="Vantage points that have reported")
def agents(who: Principal = Depends(READ)):
    return _env(store.agents())
