"""The probe agents' own routes (not Data API: the public view is /v1/reliability/*, api/reliability.py).

    GET  /v1/probes/targets          what is probed (public URLs, accepted statuses, provider region); agents read it
    POST /v1/probes/ingest           a remote agent posts raw results                       [probes:write]

Agents call the main host with their key (probes/agent.py). Methodology: /methodology/probes.
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
