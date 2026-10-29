"""UptimeProject HTTP client and response parsing.

The parsers are pure (bytes/dicts in, plain dicts out) so they are tested on saved responses. Fetching
raises on failure; the poller catches per service so one bad service never loses the others.

Upstream shapes (v1, verified 2026-10-23):
    GET /v1/services                   [ {id, name, category, region?, provider, homepage_url, status_page_url,
                                          current_status, checks_count, availability_24h/7d/30d/90d} ]
    GET /v1/services/{id}              the above + checks [{id, check_type, target, interval_sec}]
                                       + probes [{probe_id, status, latency_ms}]
    GET /v1/services/{id}/uptime       {service_id, period, points:[{bucket, availability_pct, status,
                                          down_minutes, degraded_minutes, latency_p50_ms}], summary}
Data endpoints are cached 5 minutes upstream; there is no auth and no stated rate limit, so we poll hourly,
a few requests at a time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from config import settings
from uptime import sources

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
USER_AGENT = f"OpenGridTerminal-Uptime/1.0 (+{settings.public_base_url})"


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.uptime_base_url.rstrip("/"),
                             headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                             timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=True)


async def get_json(c: httpx.AsyncClient, path: str, params: dict | None = None):
    async with c.stream("GET", path, params=params) as resp:
        if not resp.is_success:
            raise RuntimeError(f"{path}: HTTP {resp.status_code}")
        size, chunks = 0, []
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError(f"{path}: body larger than {MAX_BYTES} bytes")
            chunks.append(chunk)
    return json.loads(b"".join(chunks))


# --------------------------------------------------------------------------- parsing

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v):
    n = _num(v)
    return None if n is None else int(round(n))


def _ts(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_services(body) -> list[dict]:
    """The services that belong to a tracked provider, as rows for uptime_services."""
    if not isinstance(body, list):
        raise ValueError("services: expected a JSON array")
    out = []
    for s in body:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str):
            continue
        slug = sources.provider_for(s.get("provider"))
        if not slug:
            continue
        out.append({
            "id": s["id"], "provider": slug, "name": str(s.get("name") or s["id"]),
            "upstream_provider": s.get("provider"), "category": str(s.get("category") or "other"),
            "region": s.get("region") or None, "homepage_url": s.get("homepage_url") or None,
            "status_page_url": s.get("status_page_url") or None, "current_status": s.get("current_status"),
            "checks_count": _int(s.get("checks_count")),
            "availability_24h": _num(s.get("availability_24h")), "availability_7d": _num(s.get("availability_7d")),
            "availability_30d": _num(s.get("availability_30d")), "availability_90d": _num(s.get("availability_90d")),
        })
    return out


def parse_detail(body) -> dict:
    """checks + the per-probe snapshot from GET /v1/services/{id}."""
    if not isinstance(body, dict):
        raise ValueError("service detail: expected a JSON object")
    checks = [{"id": c.get("id"), "check_type": c.get("check_type"), "target": c.get("target"),
               "interval_sec": _int(c.get("interval_sec"))}
              for c in body.get("checks") or [] if isinstance(c, dict)]
    probes = [{"probe_id": str(p["probe_id"]), "status": p.get("status"), "latency_ms": _int(p.get("latency_ms"))}
              for p in body.get("probes") or [] if isinstance(p, dict) and p.get("probe_id")]
    return {"checks": checks, "probes": probes}


def parse_points(body) -> list[dict]:
    """Hourly buckets from GET /v1/services/{id}/uptime. A point with no parseable bucket time is dropped."""
    if not isinstance(body, dict):
        raise ValueError("uptime: expected a JSON object")
    out = []
    for p in body.get("points") or []:
        if not isinstance(p, dict):
            continue
        bucket = _ts(p.get("bucket"))
        if bucket is None:
            continue
        out.append({"bucket": bucket, "availability_pct": _num(p.get("availability_pct")), "status": p.get("status"),
                    "down_minutes": _int(p.get("down_minutes")), "degraded_minutes": _int(p.get("degraded_minutes")),
                    "latency_p50_ms": _int(p.get("latency_p50_ms"))})
    return out
