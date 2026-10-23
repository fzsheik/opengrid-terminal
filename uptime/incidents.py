"""Incidents each provider reports on its own status page, loaded into provider_incidents.

Two source kinds, both public JSON:
    statuspage  Atlassian Statuspage: /api/v2/incidents.json (the 50 most recent incidents, each with its update
                text and affected components) and /api/v2/components.json (the page's component count, used to
                recognise an incident that touched most of the page)
    gcp         status.cloud.google.com/incidents.json (recent incidents with explicit affected locations)

Reported, not measured: a provider's own words about what happened. They under-report, and nothing here feeds a score.
A page that stops answering is simply not refreshed; its stored incidents stay. Disabled by settings.uptime_enabled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from config import settings
from jobs import job
from uptime import fetch, scope, store

log = logging.getLogger(__name__)

# OpenGrid provider slug -> where its incidents come from. Verified 2026-10-23; a provider is added only after its
# endpoint has been seen returning incident JSON.
SOURCES: dict[str, dict] = {
    "hyperstack": {"kind": "statuspage", "base": "https://status.hyperstack.cloud", "page": "https://status.hyperstack.cloud"},
    "nebius": {"kind": "statuspage", "base": "https://status.nebius.com", "page": "https://status.nebius.com"},
    "crusoe": {"kind": "statuspage", "base": "https://status.crusoecloud.com", "page": "https://status.crusoecloud.com"},
    "digitalocean": {"kind": "statuspage", "base": "https://status.digitalocean.com", "page": "https://status.digitalocean.com"},
    "lambda": {"kind": "statuspage", "base": "https://status.lambda.ai", "page": "https://status.lambda.ai"},
    "gcp": {"kind": "gcp", "base": "https://status.cloud.google.com", "page": "https://status.cloud.google.com"},
}
MAX_UPDATES = 20
MAX_TEXT = 4000
CONCURRENCY = 3


def _ts(v) -> datetime | None:
    return fetch._ts(v)


def _clip(s, n=MAX_TEXT):
    return None if s is None else str(s)[:n]


# --------------------------------------------------------------------------- parsing (pure)

def parse_statuspage(provider: str, body, total_components: int | None, page_url: str) -> list[dict]:
    if not isinstance(body, dict) or not isinstance(body.get("incidents"), list):
        raise ValueError("statuspage: expected {incidents: [...]}")
    out = []
    for i in body["incidents"]:
        if not isinstance(i, dict) or not i.get("id"):
            continue
        started = _ts(i.get("started_at")) or _ts(i.get("created_at"))
        if started is None:
            continue
        ups = sorted((u for u in i.get("incident_updates") or [] if isinstance(u, dict)),
                     key=lambda u: u.get("display_at") or u.get("created_at") or "")
        names = [c.get("name") for c in i.get("components") or [] if isinstance(c, dict)]
        for u in ups:  # update-level names are the specific ones ("EU-NORTH1 - Identity and Access Management")
            names += [c.get("name") for c in u.get("affected_components") or [] if isinstance(c, dict)]
        names = sorted({n for n in names if n})
        title = str(i.get("name") or i["id"])
        sc, regions, basis = scope.classify_incident(title, names, total_components)
        out.append({
            "provider": provider, "external_id": str(i["id"]), "source": "statuspage", "title": title,
            "impact": i.get("impact"), "status": i.get("status"), "started_at": started,
            "resolved_at": _ts(i.get("resolved_at")), "url": i.get("shortlink") or page_url,
            "scope": sc, "scope_basis": basis, "regions": regions, "components": names[:50],
            "updates": [{"at": (_ts(u.get("display_at")) or _ts(u.get("created_at"))).isoformat() if (u.get("display_at") or u.get("created_at")) else None,
                         "status": u.get("status"), "body": _clip(u.get("body"))} for u in ups[:MAX_UPDATES]],
        })
    return out


def parse_gcp(provider: str, body, page_url: str) -> list[dict]:
    if not isinstance(body, list):
        raise ValueError("gcp: expected a JSON array")
    out = []
    for i in body:
        if not isinstance(i, dict) or not i.get("id"):
            continue
        started = _ts(i.get("begin")) or _ts(i.get("created"))
        if started is None:
            continue
        locs = [x.get("id") for k in ("currently_affected_locations", "previously_affected_locations")
                for x in i.get(k) or [] if isinstance(x, dict)]
        sc, regions, basis = scope.classify_gcp_incident(locs)
        ups = sorted((u for u in i.get("updates") or [] if isinstance(u, dict)), key=lambda u: u.get("when") or u.get("created") or "")
        products = sorted({p.get("title") for p in i.get("affected_products") or [] if isinstance(p, dict) and p.get("title")})
        uri = i.get("uri")
        out.append({
            "provider": provider, "external_id": str(i["id"]), "source": "gcp", "title": _clip(i.get("external_desc"), 500) or str(i["id"]),
            "impact": (i.get("status_impact") or "").lower() or None, "status": "resolved" if i.get("end") else "open",
            "started_at": started, "resolved_at": _ts(i.get("end")),
            "url": ("https://status.cloud.google.com/" + uri.lstrip("/")) if isinstance(uri, str) and uri else page_url,
            "scope": sc, "scope_basis": basis, "regions": regions, "components": products[:50],
            "updates": [{"at": u.get("when") or u.get("created"), "status": u.get("status"), "body": _clip(u.get("text"))}
                        for u in ups[:MAX_UPDATES]],
        })
    return out


# --------------------------------------------------------------------------- polling

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": fetch.USER_AGENT, "Accept": "application/json"},
                             timeout=httpx.Timeout(fetch.TIMEOUT_SECONDS), follow_redirects=True)


async def _get(c: httpx.AsyncClient, url: str):
    async with c.stream("GET", url) as resp:
        if not resp.is_success:
            raise RuntimeError(f"{url}: HTTP {resp.status_code}")
        size, chunks = 0, []
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                raise ValueError(f"{url}: body too large")
            chunks.append(chunk)
    import json
    return json.loads(b"".join(chunks))


async def _one(c, provider: str, src: dict) -> tuple[str, list[dict] | None, str | None]:
    try:
        if src["kind"] == "statuspage":
            body = await _get(c, src["base"] + "/api/v2/incidents.json")
            total = None
            try:  # the component count only sharpens the global call; an incident list without it is still good
                comps = await _get(c, src["base"] + "/api/v2/components.json")
                total = len([x for x in comps.get("components", []) if not x.get("group")]) or None
            except Exception:  # noqa: BLE001
                pass
            return provider, parse_statuspage(provider, body, total, src["page"]), None
        return provider, parse_gcp(provider, await _get(c, src["base"] + "/incidents.json"), src["page"]), None
    except Exception as exc:  # noqa: BLE001 - one page failing never loses another
        return provider, None, f"{type(exc).__name__}: {exc}"[:300]


async def _run() -> list[tuple[str, list[dict] | None, str | None]]:
    sem = asyncio.Semaphore(CONCURRENCY)
    async with _client() as c:
        async def guarded(p, s):
            async with sem:
                return await _one(c, p, s)
        return await asyncio.gather(*(guarded(p, s) for p, s in SOURCES.items()))


def run() -> dict:
    started = time.perf_counter()
    now = datetime.now(timezone.utc)
    results = asyncio.run(_run())
    incidents, failed = 0, {}
    for provider, rows, err in results:
        if err:
            failed[provider] = err
            log.warning("incident poll %s failed: %s", provider, err)
            continue
        incidents += store.upsert_incidents(rows, now)
    return {"ok": True, "providers": len(results), "incidents": incidents, "failed": failed,
            "duration_ms": int((time.perf_counter() - started) * 1000)}


_registered = False


def register() -> None:
    global _registered
    if _registered or not settings.uptime_enabled:
        return
    _registered = True
    job("provider_incidents", every_seconds=600, initial_delay_seconds=120)(run)
