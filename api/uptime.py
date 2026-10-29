"""External control-plane uptime and latency, from UptimeProject (CC BY 4.0). A separate dataset on purpose.

    GET /v1/uptime/providers        one row per tracked provider; those UptimeProject measures carry their compute
                                    services rolled up, the rest are `measured: false` with no numbers
    GET /v1/uptime/services         every measured service (filters: provider, category)
    GET /v1/uptime/services/{id}    one service: checks, hourly + daily history, per-probe latency
    GET /v1/uptime/dips             every hour/day below 100% for a provider, with MEASURED scope (global / regional /
                                    partial), the probes failing, and provider-reported incidents that overlap it
    GET /v1/uptime/incidents        incidents providers REPORTED on their own status pages, with REPORTED scope
    GET /v1/uptime/health           when OpenGrid last polled UptimeProject, and whether it worked

These are third-party probes of a provider's public API endpoints, from five fixed locations. They say nothing
about GPU capacity or whether a launch would succeed, and they never feed OpenGrid's reliability score
(routing/reliability.py). Methodology: /methodology/uptime.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from provider_meta import _ALL
from uptime import incidents as incident_feeds
from uptime import poll, sources, store

poll.register()  # the `uptime` background job
incident_feeds.register()  # the `provider_incidents` background job

router = APIRouter()
READ = require_scope("data:read")
METHOD = "uptime"
_NAMES = {m.name: m.display_name for m in _ALL}
_SEVERITY = {"down": 3, "degraded": 2, "unknown": 1, "up": 0}


def _env(data, **meta):
    return envelope(data, kind=OBSERVED, methodology=METHOD, source="uptimeproject", source_url=sources.SOURCE_URL,
                    attribution=sources.ATTRIBUTION, **meta)


def _rollup(provider: str, svcs: list[dict]) -> dict:
    avail = {w: [s["availability"][w] for s in svcs if s["availability"][w] is not None] for w in ("24h", "7d", "30d", "90d")}
    lat = [s["last_24h"]["latency_p50_ms"] for s in svcs if s.get("last_24h") and s["last_24h"]["latency_p50_ms"] is not None]
    down = [s["last_24h"]["down_minutes"] for s in svcs if s.get("last_24h")]
    worst = max((s["status"] for s in svcs if s["status"]), key=lambda x: _SEVERITY.get(x, 1), default=None)
    return {
        "provider": provider, "display_name": _NAMES.get(provider, provider), "category": sources.COMPUTE_CATEGORY,
        "measured": True, "status": worst,
        # the lowest availability across the provider's compute services: one bad region is not hidden by an average
        "availability": {w: (min(v) if v else None) for w, v in avail.items()},
        "latency_p50_ms_24h": round(sum(lat) / len(lat)) if lat else None,
        "down_minutes_24h": sum(down) if down else None,
        "services": [{"id": s["id"], "name": s["name"], "region": s["region"], "status": s["status"]} for s in svcs],
    }


def _unmeasured(provider: str) -> dict:
    return {"provider": provider, "display_name": _NAMES.get(provider, provider), "category": sources.COMPUTE_CATEGORY,
            "measured": False, "status": None, "availability": {w: None for w in ("24h", "7d", "30d", "90d")},
            "latency_p50_ms_24h": None, "down_minutes_24h": None, "services": []}


@router.get("/v1/uptime/providers", tags=["uptime"], summary="Control-plane uptime per tracked provider")
def providers(who: Principal = Depends(READ)):
    by: dict[str, list[dict]] = {}
    for s in store.services(category=sources.COMPUTE_CATEGORY):
        by.setdefault(s["provider"], []).append(s)
    rows = [_rollup(p, v) for p, v in sorted(by.items())]
    covered = {r["provider"] for r in rows}
    # every provider OpenGrid tracks gets a row: the ones UptimeProject has no probe for say so, with no numbers
    rows += [_unmeasured(p) for p in sorted(set(_NAMES) - covered, key=lambda x: _NAMES[x].lower())]
    counts = store.incident_counts(30)
    for r in rows:  # None = this provider has no status feed OpenGrid reads; {total: 0} = a feed with nothing reported
        r["incidents_30d"] = counts.get(r["provider"])
    return _env(rows, tracked_providers=len(_NAMES), covered=len(covered),
                not_covered=sorted(set(_NAMES) - covered), health=store.health())


@router.get("/v1/uptime/services", tags=["uptime"], summary="Every measured service")
def list_services(provider: str | None = None, category: str | None = None, who: Principal = Depends(READ)):
    if provider and provider not in _NAMES:
        raise HTTPException(404, f"unknown provider {provider!r}")
    return _env(store.services(provider=provider, category=category))


@router.get("/v1/uptime/services/{service_id}", tags=["uptime"], summary="One service: history and per-probe latency")
def one_service(service_id: str, hours: int = Query(168, ge=1, le=2160), who: Principal = Depends(READ)):
    found = [s for s in store.services() if s["id"] == service_id]
    if not found:
        raise HTTPException(404, f"unknown service {service_id!r}")
    return _env({**found[0], "history": store.history(service_id, hours), "daily": store.daily(service_id, -(-hours // 24)),
                 "probes": store.probes(service_id, 24)}, hours=hours)


@router.get("/v1/uptime/health", tags=["uptime"], summary="When OpenGrid last polled UptimeProject")
def uptime_health(who: Principal = Depends(READ)):
    return _env(store.health())


def _known(provider: str | None):
    if provider and provider not in _NAMES:
        raise HTTPException(404, f"unknown provider {provider!r}")


@router.get("/v1/uptime/dips", tags=["uptime"], summary="Dips below 100% with measured scope and reported cause")
def list_dips(provider: str, days: int = Query(7, ge=1, le=90), service: str | None = None,
              who: Principal = Depends(READ)):
    _known(provider)
    rows = store.dips(provider, days, service)
    return _env(rows, provider=provider, days=days, resolution="hour" if days <= 7 else "day",
                scope_note=("scope is inferred from the probes: global = every probe failed (and every measured region "
                            "dipped, or the provider has one measured endpoint); regional = every probe failed for some "
                            "measured regions only; partial = only some probes failed"))


@router.get("/v1/uptime/incidents", tags=["uptime"], summary="Incidents providers reported on their status pages")
def list_incidents(provider: str | None = None, days: int = Query(90, ge=1, le=730), scope: str | None = Query(
        None, pattern="^(global|regional|unspecified)$"), who: Principal = Depends(READ)):
    _known(provider)
    return _env(store.incidents(provider, days, scope), days=days, reported_not_measured=True,
                feeds=sorted(incident_feeds.SOURCES))
