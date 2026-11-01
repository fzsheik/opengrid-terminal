"""INTERNAL-tier data that routing may read (methodology/data-visibility.md; provider_meta.INTERNAL_SOURCES).

Nothing here may reach a public page, public API answer, statistic, index or fixing. Callers are OpenGrid's own
execution paths (routing, recommendations for a key, job placement) and the admin view (GET /v1/ops/internal-data).

    internal_listings(provider=None)      current internal compute_listings rows (e.g. DeepInfra's container-API prices)
    spot_interruption_bands(region=None)  AWS Spot Instance Advisor bands per GPU instance type and region, read from
                                          the stored raw feed (providers/aws.spot_advice). Spot is not ranked yet,
                                          so no route uses them today; they are ready for the spot release.
"""

from __future__ import annotations

from sqlalchemy import text

import normalize
import provider_meta
from cache import ttl_cache

_INTERNAL = text("""
    SELECT provider, listing_id, sku, canonical_gpu_name AS gpu, gpu_count, region, price_per_gpu_hour,
           price_per_instance_hour, market_type, provider_tier, available, observed_at, visibility
    FROM compute_listings
    WHERE visibility <> 'public' AND (CAST(:p AS text) IS NULL OR provider = :p)
    ORDER BY provider, listing_id
""")


def internal_listings(provider: str | None = None) -> list[dict]:
    with normalize.SessionLocal() as s:
        rows = [dict(r._mapping) for r in s.execute(_INTERNAL, {"p": provider})]
    for r in rows:
        for k in ("price_per_gpu_hour", "price_per_instance_hour"):
            r[k] = None if r[k] is None else float(r[k])
    return rows


@ttl_cache(600)
def spot_interruption_bands(region: str | None = None) -> list[dict]:
    """[{instance_type, region, gpu, gpu_count, interruption_band, interruption_label, savings_pct, last_modified,
    visibility: 'internal'}] from the newest stored Spot Advisor feed; [] when none is stored."""
    from providers import aws

    tier = provider_meta.visibility("aws", aws.SPOT_ADVISOR, "signals")
    with normalize.SessionLocal() as s:
        advice = aws.spot_advice(normalize._latest_raw(s, "aws"))
    return [{"instance_type": a.instance_type, "region": a.region, "gpu": a.canonical_gpu_name,
             "raw_gpu_name": a.raw_gpu_name, "gpu_count": a.gpu_count, "interruption_band": a.interruption_band,
             "interruption_label": a.interruption_label, "savings_pct": a.savings_pct,
             "last_modified": a.last_modified, "visibility": tier}
            for a in advice if region is None or a.region == region]
