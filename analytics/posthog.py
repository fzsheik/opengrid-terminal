"""PostHog numbers for the admin Value page: page views and visitors (7 and 30 days), top pages (7 days).

PostHog is an optional processor for the same anonymous events the first-party tracker records (web/core.js sends
them to both; see analytics/product.py for the privacy rules, which PostHog follows too: no cookie, no person
profiles, no session recording, query strings stripped, Do Not Track and Global Privacy Control respected, operator
sessions not sent).

The numbers are read server-side with HogQL (POST {app host}/api/projects/{id}/query/) using a personal API key with
the Query: Read scope. That key never leaves the server, and errors name the status, never the key. Results are
cached in memory for 10 minutes so the admin page cannot spend the PostHog query budget.

"Visitors" are distinct PostHog ids. Persistence is sessionStorage, so one id is one browser tab session: the count is
of visits, not of people (PostHog cannot recognise a returning visitor, by design).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import httpx

from config import settings

TTL_SECONDS = 600
TIMEOUT_SECONDS = 10.0
_cache: dict = {}
_lock = threading.Lock()

# Exactly these queries run; nothing from the request goes into them.
TOTALS = ("SELECT countIf(timestamp > now() - INTERVAL 7 DAY), uniqIf(distinct_id, timestamp > now() - INTERVAL 7 DAY), "
          "count(), uniq(distinct_id) FROM events WHERE event = '$pageview' AND timestamp > now() - INTERVAL 30 DAY")
TOP_PAGES = ("SELECT properties.$pathname AS path, count() AS views, uniq(distinct_id) AS visitors FROM events "
             "WHERE event = '$pageview' AND timestamp > now() - INTERVAL 7 DAY "
             "GROUP BY path ORDER BY views DESC LIMIT 10")


def missing() -> list[str]:
    """The settings the admin numbers still need (empty: all set)."""
    out = []
    if not settings.posthog_key:
        out.append("POSTHOG_KEY")
    if not settings.posthog_project_id:
        out.append("POSTHOG_PROJECT_ID")
    if not settings.posthog_personal_api_key or not settings.posthog_personal_api_key.get_secret_value():
        out.append("POSTHOG_PERSONAL_API_KEY")
    return out


def project_url() -> str | None:
    pid = settings.posthog_project_id
    return f"{settings.posthog_app_host}/project/{pid}" if pid else None


def _query(client: httpx.Client, hogql: str) -> list:
    url = f"{settings.posthog_app_host}/api/projects/{settings.posthog_project_id}/query/"
    r = client.post(url, json={"query": {"kind": "HogQLQuery", "query": hogql}},
                    headers={"Authorization": f"Bearer {settings.posthog_personal_api_key.get_secret_value()}"})
    if r.status_code != 200:
        hint = {401: "check POSTHOG_PERSONAL_API_KEY", 403: "the personal API key needs the Query: Read scope on this project",
                404: "check POSTHOG_PROJECT_ID and POSTHOG_HOST (US or EU)"}.get(r.status_code, "")
        raise PostHogError(f"PostHog answered HTTP {r.status_code}" + (f": {hint}" if hint else ""))
    return r.json().get("results") or []


class PostHogError(Exception):
    pass


def new_client() -> httpx.Client:  # tests swap this for an httpx.MockTransport client
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _fetch(client: httpx.Client) -> dict:
    tot = _query(client, TOTALS)
    pv7, v7, pv30, v30 = (tot[0] if tot else [0, 0, 0, 0])[:4]
    pages = [{"path": p or "(unknown)", "views": int(n or 0), "visitors": int(u or 0)} for p, n, u in _query(client, TOP_PAGES)]
    return {"last_7_days": {"pageviews": int(pv7 or 0), "visitors": int(v7 or 0)},
            "last_30_days": {"pageviews": int(pv30 or 0), "visitors": int(v30 or 0)},
            "top_pages_7_days": pages}


def summary(*, refresh: bool = False, client: httpx.Client | None = None, now=time.monotonic) -> dict:
    need = missing()
    base = {"configured": not need, "tracking": bool(settings.posthog_key), "missing": need,
            "project_url": project_url(), "host": settings.posthog_host}
    if need:
        return base
    with _lock:
        hit = _cache.get("v")
        if hit and not refresh and now() - hit[0] < TTL_SECONDS:
            return {**base, **hit[1], "cached": True}
    try:
        own = client is None
        client = client or new_client()
        try:
            data = _fetch(client)
        finally:
            if own:
                client.close()
    except PostHogError as e:
        return {**base, "error": str(e)}
    except (httpx.HTTPError, ValueError) as e:
        return {**base, "error": f"PostHog could not be reached ({type(e).__name__})"}
    data["as_of"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        _cache["v"] = (now(), data)
    return {**base, **data, "cached": False}


def reset() -> None:
    with _lock:
        _cache.clear()
