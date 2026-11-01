"""How far back archived history reaches, per provider and GPU (`python -m backfill coverage`).

    coverage(market, today)  -> {"rows": [...], "providers_without": [...], "bars": {...}}
    render(report)           -> text table

Per (provider, canonical GPU) in archived_prices for one market (default on_demand; "all" pools every market,
including reserved_* and transacted, labelled per row): earliest and latest archived date, whole months from the
earliest date to `today`, the sources and bases that contributed, and flags against the 24 / 36 / 60-month bars.
Unmapped GPUs (gpu null) are counted per provider, not listed. Providers with no archived row at all (every live
provider, plus EXTRA_PROVIDERS not yet on main) are listed so gaps are visible.

Months: a point dated 2021-11-01 read on 2026-11-01 is 60 months back. An "effective since" point counts like any
other point here, because it is evidence of the price on its date; the basis column says when that is the only kind
of evidence for the pair.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text

import normalize

BARS = (24, 36, 60)
# Providers added on branch providers/batch-nokey (not on main yet), so the report shows them before merge.
EXTRA_PROVIDERS = ("paperspace", "akash", "hydrahost", "cudo", "gpuai", "exoscale", "huggingface")

_Q = text("""
    SELECT provider, gpu, min(observed_at) AS first, max(observed_at) AS last, count(*) AS points,
           count(DISTINCT date_trunc('month', observed_at)) AS months_with_data,
           array_agg(DISTINCT source) AS sources, array_agg(DISTINCT coalesce(basis, '')) AS bases,
           array_agg(DISTINCT market_type) AS markets
    FROM archived_prices
    WHERE gpu IS NOT NULL AND (CAST(:market AS text) = 'all' OR market_type = :market)
    GROUP BY provider, gpu
    ORDER BY provider, min(observed_at)
""")
_UNMAPPED = text("""
    SELECT provider, count(*) FROM archived_prices
    WHERE gpu IS NULL AND (CAST(:market AS text) = 'all' OR market_type = :market) GROUP BY provider
""")


def months_between(first: date, today: date) -> int:
    m = (today.year - first.year) * 12 + (today.month - first.month)
    return m - (1 if today.day < first.day else 0)


def coverage(market: str = "on_demand", today: date | None = None, known_providers=None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    with normalize.SessionLocal() as s:
        rows = s.execute(_Q, {"market": market}).all()
        unmapped = dict(s.execute(_UNMAPPED, {"market": market}).all())
    out = []
    for r in rows:
        first = r.first.astimezone(timezone.utc).date()
        months = months_between(first, today)
        out.append({"provider": r.provider, "gpu": r.gpu, "first": first.isoformat(),
                    "last": r.last.astimezone(timezone.utc).date().isoformat(), "months": months,
                    "points": r.points, "months_with_data": r.months_with_data,
                    "sources": sorted(r.sources), "bases": sorted(b for b in r.bases if b),
                    "markets": sorted(m for m in r.markets if m),
                    **{f"ge_{b}": months >= b for b in BARS}})
    if known_providers is None:
        from providers import PROVIDERS
        known_providers = set(PROVIDERS) | set(EXTRA_PROVIDERS)
    have = {r["provider"] for r in out}
    return {"market": market, "today": today.isoformat(), "bars": BARS, "rows": out,
            "unmapped_points": unmapped,
            "providers_without": sorted(set(known_providers) - have),
            "summary": {f"ge_{b}": sorted({r["provider"] for r in out if r[f"ge_{b}"]}) for b in BARS}}


def render(rep: dict) -> str:
    lines = [f"Archived coverage, market {rep['market']}, as of {rep['today']} (months back from the earliest point)",
             "", f"{'provider':15} {'gpu':30} {'first':10} {'months':>6} {'pts':>5} {'mo w/ data':>10}  "
             f"{'24':>3} {'36':>3} {'60':>3}  sources"]
    for r in rep["rows"]:
        flag = lambda b: "yes" if r[f"ge_{b}"] else "-"  # noqa: E731
        lines.append(f"{r['provider']:15} {r['gpu'][:30]:30} {r['first']:10} {r['months']:>6} {r['points']:>5} "
                     f"{r['months_with_data']:>10}  {flag(24):>3} {flag(36):>3} {flag(60):>3}  "
                     f"{','.join(r['sources'])}" + (f" [{','.join(r['bases'])}]" if r["bases"] else ""))
    lines += ["", "providers reaching each bar (any GPU): "
              + "; ".join(f"{b} months: {len(rep['summary'][f'ge_{b}'])} ({', '.join(rep['summary'][f'ge_{b}'])})"
                          for b in rep["bars"])]
    if rep["unmapped_points"]:
        lines.append("unmapped-GPU points (stored, not charted): "
                     + ", ".join(f"{p} {n}" for p, n in sorted(rep["unmapped_points"].items())))
    lines.append("providers with no archived point in this market: " + ", ".join(rep["providers_without"]))
    return "\n".join(lines)
