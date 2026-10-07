"""Trust labels for listings and health for providers, from observable facts only.

listing_trust(row) -> {
    source            the host OpenGrid reads (provider_meta.source)
    source_type       direct_api | public_page | aggregator | pricing_file | unknown
    source_detail     provider_meta.source_type (authenticated_api, public_api, ...)
    last_fetched      the provider's last successful raw fetch (any endpoint)
    last_seen         when this listing was last present in a fetch (compute_listings.observed_at)
    last_changed      this listing's last recorded change (newest listing_observations row)
    age_seconds       now - last_seen
    freshness         fresh   age <= FRESH_POLLS x the provider's polling interval
                      aging   older, but within market.stale_after() (2.5 polls, min 10 min)
                      stale   beyond it: the market view already treats it as gone
    availability_basis explicit (mapping.py: `available` is RAW), inferred (DERIVED or
                      CONSTANT: a stated rule), unknown (ABSENT, unmapped, or available is None)
    held              pending quarantine holds on this listing (field, rule, since)
    flags             open listing-level incidents (e.g. instance_price_mismatch)
    confidence        high | medium | low, with the reasons (see CONFIDENCE below)
}

No reliability score is invented: confidence is a label over the facts above and the
provider's health status, and every reason is returned with it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import mapping
import market
import normalize
import provider_meta
from cache import ttl_cache
from providers import PROVIDERS
from quality import checks

FRESH_POLLS = 1.5
WINDOW = timedelta(hours=24)

SOURCE_TYPES = {
    "authenticated_api": "direct_api",
    "public_api": "direct_api",
    "public_page": "public_page",
    "aggregator": "aggregator",
    "public_pricing_file": "pricing_file",
}

CONFIDENCE = """
low     any of: freshness stale; a pending quarantine hold on the listing; an open
        listing-level quality flag; the provider's health status is down
high    all of: freshness fresh; source_type direct_api; availability_basis explicit or
        inferred; provider status healthy; no holds, no flags
medium  everything else (e.g. a public page, aggregator or pricing file source; aging;
        availability unknown; provider degraded)
"""

LISTING_FLAG_KINDS = tuple(r for r, v in checks.RULES.items() if v[1] == "flag" and r not in ("listing_collapse", "empty_response"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def interval(provider: str) -> float:
    cls = PROVIDERS.get(provider)
    return cls.polling.interval_seconds if cls else 900.0


def freshness(provider: str, last_seen: datetime | None, now: datetime | None = None) -> tuple[str, float | None]:
    if last_seen is None:
        return "stale", None
    age = ((now or _now()) - last_seen).total_seconds()
    if age <= FRESH_POLLS * interval(provider):
        return "fresh", age
    if age <= market.stale_after(provider).total_seconds():
        return "aging", age
    return "stale", age


def availability_basis(provider: str, available) -> str:
    fm = mapping.PROVIDER_MAPPINGS.get(provider)
    fm = fm and fm.by_field().get("available")
    if available is None or fm is None or fm.kind == mapping.ABSENT:
        return "unknown"
    if fm.kind == mapping.RAW:
        return "explicit"
    return "inferred"  # DERIVED / CONSTANT / LOOKUP: a stated rule, see mapping.py


def context(providers: list[str] | None = None, listing_keys: list[tuple[str, str]] | None = None) -> dict:
    """Everything listing_trust needs, loaded in a few queries for many rows."""
    from quality import quarantine

    with normalize.SessionLocal() as s:
        if providers is None:
            providers = list(s.execute(text("SELECT DISTINCT provider FROM compute_listings")).scalars())
        last_ok = {
            r.provider: r.last_ok for r in s.execute(text(
                """SELECT p AS provider, (SELECT max(fetched_at) FROM raw_snapshots r
                                         WHERE r.provider = p AND r.ok) AS last_ok
                   FROM unnest(CAST(:ps AS text[])) AS p"""), {"ps": list(providers)})
        }
        q = """SELECT c.provider, c.listing_id, o.observed_at
               FROM compute_listings c
               LEFT JOIN LATERAL (SELECT observed_at FROM listing_observations o
                                  WHERE o.provider = c.provider AND o.listing_id = c.listing_id
                                  ORDER BY observed_at DESC LIMIT 1) o ON true
               WHERE c.provider = ANY(:ps)"""
        params: dict = {"ps": list(providers)}
        if listing_keys is not None:
            q += " AND c.listing_id = ANY(:ids)"
            params["ids"] = [k[1] for k in listing_keys]
        last_changed = {(r.provider, r.listing_id): r.observed_at for r in s.execute(text(q), params)}
        flags: dict = {}
        for r in s.execute(text(
                "SELECT provider, listing_id, kind, last_seen FROM quality_incidents "
                "WHERE status = 'open' AND kind = ANY(:k) AND provider = ANY(:ps)"),
                {"k": list(LISTING_FLAG_KINDS), "ps": list(providers)}):
            flags.setdefault((r.provider, r.listing_id), []).append({"kind": r.kind, "last_seen": r.last_seen})
    status = {h["provider"]: h["status"] for h in all_provider_health() if h["provider"] in providers}
    return {"now": _now(), "last_ok": last_ok, "last_changed": last_changed, "flags": flags,
            "holds": quarantine.open_holds(list(providers)), "status": status}


def listing_trust(row: dict, ctx: dict | None = None) -> dict:
    """The trust block for one compute_listings row (a dict with at least provider,
    listing_id, observed_at, available)."""
    provider, lid = row["provider"], row["listing_id"]
    if ctx is None:
        ctx = context([provider], [(provider, lid)])
    meta = provider_meta.meta(provider)
    fresh, age = freshness(provider, row.get("observed_at"), ctx["now"])
    basis = availability_basis(provider, row.get("available"))
    source_type = SOURCE_TYPES.get(meta.source_type, "unknown")
    held = ctx["holds"].get((provider, lid), [])
    flags = ctx["flags"].get((provider, lid), [])
    status = ctx["status"].get(provider, "unknown")

    low, not_high = [], []
    if fresh == "stale":
        low.append("listing not seen within the provider's stale window")
    if held:
        low.append("a value for this listing is held in quarantine: " + ", ".join(h["rule"] for h in held))
    if flags:
        low.append("open quality flag: " + ", ".join(f["kind"] for f in flags))
    if status == "down":
        low.append("provider feed is down")
    if fresh == "aging":
        not_high.append("aging: last seen more than one polling interval ago")
    if source_type != "direct_api":
        not_high.append(f"source is {source_type.replace('_', ' ')}, not the provider's own API")
    if basis == "unknown":
        not_high.append("availability unknown")
    if status not in ("healthy", "down"):
        not_high.append(f"provider status {status}")
    confidence = "low" if low else ("medium" if not_high else "high")

    return {
        "source": meta.source or None,
        "source_type": source_type,
        "source_detail": meta.source_type,
        "last_fetched": ctx["last_ok"].get(provider),
        "last_seen": row.get("observed_at"),
        "last_changed": ctx["last_changed"].get((provider, lid)),
        "freshness": fresh,
        "age_seconds": None if age is None else round(age, 1),
        "polling_interval_seconds": interval(provider),
        "availability_basis": basis,
        "held": held,
        "flags": flags,
        "provider_status": status,
        "confidence": confidence,
        "confidence_reasons": low or not_high,
    }


# --------------------------------------------------------------------------
# Provider health
# --------------------------------------------------------------------------

_HEALTH = text(
    """
    WITH w AS (
        SELECT ok, duration_ms FROM raw_snapshots
        WHERE provider = :p AND fetched_at >= :since
    )
    SELECT
        (SELECT max(fetched_at) FROM raw_snapshots WHERE provider = :p AND ok) AS last_ok,
        (SELECT count(*) FROM w) AS fetches_24h,
        (SELECT count(*) FROM w WHERE NOT ok) AS failures_24h,
        (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) FROM w WHERE duration_ms IS NOT NULL) AS p50,
        (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) FROM w WHERE duration_ms IS NOT NULL) AS p95,
        (SELECT count(*) FROM compute_listings WHERE provider = :p) AS listings_total,
        (SELECT count(*) FROM compute_listings WHERE provider = :p AND observed_at >= :live_since) AS listings_now,
        (SELECT count(*) FROM quality_quarantine WHERE provider = :p AND status = 'pending') AS quarantined,
        (SELECT count(*) FROM quality_incidents WHERE provider = :p AND kind = 'schema_change'
                                                  AND last_seen >= :since) AS schema_changes_24h,
        (SELECT count(*) FROM quality_incidents WHERE provider = :p AND kind = 'schema_change'
                                                  AND last_seen >= :since7) AS schema_changes_7d
    """
)

_LAST_FAIL = text(
    """SELECT fetched_at, endpoint, status_code, error FROM raw_snapshots
       WHERE provider = :p AND NOT ok ORDER BY fetched_at DESC LIMIT 1"""
)
_CONSECUTIVE = text(
    """SELECT count(*) FROM raw_snapshots
       WHERE provider = :p AND NOT ok AND fetched_at > coalesce(CAST(:last_ok AS timestamptz), '-infinity')"""
)
_THEN = text(
    """SELECT polled_at, listings FROM quality_polls
       WHERE provider = :p AND polled_at <= :t AND polled_at >= :t - CAST(:slack AS interval)
       ORDER BY polled_at DESC LIMIT 1"""
)
_OPEN = text(
    """SELECT kind, count(*) AS n FROM quality_incidents
       WHERE provider = :p AND status = 'open' GROUP BY kind"""
)
FEED_INCIDENTS = ("normalizer_error", "empty_response", "quality_layer_error")


def provider_health(provider: str) -> dict:
    now = _now()
    stale = market.stale_after(provider)
    with normalize.SessionLocal() as s:
        h = s.execute(_HEALTH, {"p": provider, "since": now - WINDOW, "since7": now - timedelta(days=7),
                                "live_since": now - stale}).one()
        fail = s.execute(_LAST_FAIL, {"p": provider}).first()
        consecutive = s.execute(_CONSECUTIVE, {"p": provider, "last_ok": h.last_ok}).scalar()
        then = s.execute(_THEN, {"p": provider, "t": now - WINDOW, "slack": f"{int(stale.total_seconds())} seconds"}).first()
        open_incidents = {r.kind: r.n for r in s.execute(_OPEN, {"p": provider})}

    rate = (h.failures_24h / h.fetches_24h) if h.fetches_24h else None
    reasons = []
    if h.last_ok is None:
        status = "down"
        reasons.append("no successful fetch recorded")
    elif now - h.last_ok > stale:
        status = "down"
        reasons.append(f"no successful fetch for {int((now - h.last_ok).total_seconds())}s (stale window {int(stale.total_seconds())}s)")
    else:
        if consecutive >= 2:
            reasons.append(f"{consecutive} failed responses since the last good one")
        if rate is not None and rate > 0.2:
            reasons.append(f"{rate:.0%} of responses failed in 24h")
        for k in FEED_INCIDENTS:
            if open_incidents.get(k):
                reasons.append(f"open {k} incident")
        status = "degraded" if reasons else "healthy"

    return {
        "provider": provider,
        "display_name": provider_meta.meta(provider).display_name,
        "source": provider_meta.meta(provider).source or None,
        "source_type": SOURCE_TYPES.get(provider_meta.meta(provider).source_type, "unknown"),
        "polling_interval_seconds": interval(provider),
        "status": status,
        "status_reasons": reasons,
        "last_ok_fetch": h.last_ok,
        "last_failure": None if fail is None else {
            "at": fail.fetched_at, "endpoint": fail.endpoint, "status_code": fail.status_code,
            "error": (fail.error or "")[:300] or None,
        },
        "consecutive_failures": consecutive,
        "fetches_24h": h.fetches_24h,
        "failures_24h": h.failures_24h,
        "failure_rate_24h": None if rate is None else round(rate, 4),
        "latency_ms_p50_24h": None if h.p50 is None else round(float(h.p50), 1),
        "latency_ms_p95_24h": None if h.p95 is None else round(float(h.p95), 1),
        "listings_total": h.listings_total,
        "listings_now": h.listings_now,
        "listings_24h_ago": None if then is None else then.listings,
        "listings_24h_ago_at": None if then is None else then.polled_at,
        "listings_24h_ago_note": None if then is not None else "insufficient history (no screened poll near 24h ago)",
        "quarantined": h.quarantined,
        "schema_changes_24h": h.schema_changes_24h,
        "schema_changes_7d": h.schema_changes_7d,
        "open_incidents": open_incidents,
    }


def known_providers() -> list[str]:
    """Providers with any raw data or listings (skip-scan over raw_snapshots' provider prefix)."""
    with normalize.SessionLocal() as s:
        raw = s.execute(text(
            """WITH RECURSIVE ps AS (
                   (SELECT provider FROM raw_snapshots ORDER BY provider LIMIT 1)
                   UNION ALL
                   SELECT (SELECT r.provider FROM raw_snapshots r WHERE r.provider > ps.provider
                           ORDER BY r.provider LIMIT 1) FROM ps WHERE ps.provider IS NOT NULL)
               SELECT provider FROM ps WHERE provider IS NOT NULL""")).scalars().all()
        listed = s.execute(text("SELECT DISTINCT provider FROM compute_listings")).scalars().all()
    return sorted(set(raw) | set(listed))


@ttl_cache(30)
def all_provider_health() -> list[dict]:
    return [provider_health(p) for p in known_providers()]


def public_health(h: dict) -> dict:
    """Health without internal error text or endpoint paths."""
    out = {k: v for k, v in h.items() if k not in ("last_failure", "open_incidents")}
    out["last_failure_at"] = h["last_failure"]["at"] if h.get("last_failure") else None
    return out
