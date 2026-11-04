"""Provider reliability in the Data API (docs/API_CONTRACT.md section 7). OpenGrid's own measurements and the
providers' own words; never third-party uptime data (UptimeProject stays internal).

    GET /v1/reliability/providers                    one row per provider: our probes (status, availability,
                                                     latency, reasons), data freshness per stream, feed health
    GET /v1/reliability/providers/{provider}         the same, plus each probe target's series per vantage point
    GET /v1/reliability/providers/{provider}/dips    hours with a failed check, with measured scope
    GET /v1/reliability/incidents                    incidents providers reported on their own status pages
    GET /v1/reliability/targets                      what OpenGrid probes
    GET /v1/reliability/agents                       the vantage points that have reported
    GET /v1/reliability/probes                       raw probe results of one target, newest first

Every block says its `source`: opengrid_probes (measured), opengrid_feeds (our polling), provider_status_page
(reported, not measured). Nothing here feeds routing or any score.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

import freshness
import provider_meta
from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from probes import store as probes
from probes import targets
from quality import trust
from uptime import incidents as incident_feeds
from uptime import store as status_pages

router = APIRouter()
READ = require_scope("data:read")
METHOD = "probes"
SCOPE_NOTE = ("scope is by vantage point: global = every reporting location failed (at least two reported); "
              "regional = some failed while others succeeded; unconfirmed = only one location reported. "
              "region_scope compares the provider's own regional endpoints in the same hour.")


def _rows(hours: int) -> dict[str, dict]:
    out: dict[str, dict] = {}

    def row(p: str) -> dict:
        return out.setdefault(p, {"provider": p, "probes": None, "freshness": [], "feed_health": None})

    for r in probes.summary(hours):
        row(r["provider"])["probes"] = {**{k: v for k, v in r.items() if k != "provider"}, "source": "opengrid_probes"}
    for r in freshness.report():
        row(r["provider"])["freshness"].append({**{k: v for k, v in r.items() if k != "provider"},
                                                "source": "opengrid_feeds"})
    for h in trust.all_provider_health():
        row(h["provider"])["feed_health"] = {**{k: v for k, v in trust.public_health(h).items() if k != "provider"},
                                             "source": "opengrid_feeds"}
    return {p: r for p, r in out.items() if provider_meta.is_public_provider(p)}


@router.get("/v1/reliability/providers", tags=["reliability"],
            summary="Per provider: probe status, availability, latency; data freshness; feed health")
def providers(hours: int = Query(24, ge=1, le=720), who: Principal = Depends(READ)):
    rows = sorted(_rows(hours).values(), key=lambda r: r["provider"])
    return envelope(rows, kind=OBSERVED, methodology=METHOD, hours=hours, agents=len(probes.agents()),
                    sla=f"freshness: p95 age over 24 h <= {freshness.SLA_FACTOR} x the stream's target interval")


@router.get("/v1/reliability/providers/{provider}", tags=["reliability"],
            summary="One provider: probes with each target's series, data freshness, feed health")
def provider(provider: str, hours: int = Query(24, ge=1, le=720), who: Principal = Depends(READ)):
    r = _rows(hours).get(provider)
    ts = targets.for_provider(provider)
    if r is None and not ts:
        raise HTTPException(404, f"no reliability data for provider {provider!r}; see /v1/reliability/providers")
    r = r or {"provider": provider, "probes": None, "freshness": [], "feed_health": None}
    r["targets"] = [{**t.public(), **probes.target_series(t.id, hours)} for t in ts]
    return envelope(r, kind=OBSERVED, methodology=METHOD, hours=hours)


@router.get("/v1/reliability/providers/{provider}/dips", tags=["reliability"],
            summary="Hours with a failed check, with measured scope and reason")
def dips(provider: str, days: int = Query(7, ge=1, le=30), who: Principal = Depends(READ)):
    if not targets.for_provider(provider):
        raise HTTPException(404, f"no probe targets for provider {provider!r}; see /v1/reliability/targets")
    return envelope(probes.dips(provider, days), kind=OBSERVED, methodology=METHOD, source="opengrid_probes",
                    days=days, agents=len(probes.agents()), scope_note=SCOPE_NOTE)


@router.get("/v1/reliability/incidents", tags=["reliability"],
            summary="Incidents providers reported on their own status pages")
def incidents(provider: str | None = None, days: int = Query(90, ge=1, le=730),
              scope: str | None = Query(None, pattern="^(global|regional|unspecified)$"), who: Principal = Depends(READ)):
    rows = [r for r in status_pages.incidents(provider, days, scope) if provider_meta.is_public_provider(r["provider"])]
    return envelope(rows, kind=OBSERVED, methodology="uptime", source="provider_status_page", days=days,
                    reported_not_measured=True, feeds=sorted(incident_feeds.SOURCES))


@router.get("/v1/reliability/targets", tags=["reliability"], summary="What OpenGrid probes")
def list_targets(who: Principal = Depends(READ)):
    return envelope([t.public() for t in targets.TARGETS], kind=OBSERVED, methodology=METHOD,
                    source="opengrid_probes", count=len(targets.TARGETS))


@router.get("/v1/reliability/agents", tags=["reliability"], summary="Vantage points that have reported")
def agents(who: Principal = Depends(READ)):
    return envelope(probes.agents(), kind=OBSERVED, methodology=METHOD, source="opengrid_probes")


@router.get("/v1/reliability/probes", tags=["reliability"], summary="Raw probe results of one target, newest first")
def raw(target: str, location: str | None = None, limit: int = Query(100, ge=1, le=500), failures_only: bool = False,
        who: Principal = Depends(READ)):
    if target not in targets.BY_ID:
        raise HTTPException(404, f"unknown target {target!r}; see /v1/reliability/targets")
    return envelope(probes.raw(target, location, limit, failures_only), kind=OBSERVED, methodology=METHOD,
                    source="opengrid_probes", target=target)
