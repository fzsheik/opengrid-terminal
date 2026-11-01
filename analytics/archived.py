"""Archived price history for one GPU (data kind 'archived'; methodology/archived-history.md).

    gpu_archived(gpu, segment, t0, t1) -> {points, providers, monthly, observed_from}
    extended(gpu, segment, days)       -> one continuous DAILY series: archived (held between captures) before
                                          the GPU's first observed day, observed from it on; kind per day

Read only from archived_prices. Nothing here feeds percentiles, indices, events or routing: those read observed
data only, and archived samples are too sparse (one capture every few days at best) to pass their coverage gates.

    points     one per (provider, source document): that provider's lowest priced listing for the GPU in the
               document, with the document's checkable URL. AWS price-list points carry valid_to (the version's
               period end): the price applied for the whole period, not only on the day.
    providers  per provider: samples, first / last date, lowest / highest
    monthly    per UTC month, one vote per provider (its lowest archived price that month): lowest and median
               across providers, and how many providers voted. A provider that was not captured that month does
               not vote, so the provider count is stated with every month.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import normalize

_POINTS = text("""
    SELECT provider, source, source_url, observed_at, max(valid_to) AS valid_to,
           min(price_per_gpu_hour) AS price, count(*) AS listings
    FROM archived_prices
    WHERE gpu = :gpu AND market_type = :seg AND visibility = 'public'
      AND (CAST(:t0 AS timestamptz) IS NULL OR observed_at >= :t0)
      AND (CAST(:t1 AS timestamptz) IS NULL OR observed_at <= :t1)
    GROUP BY provider, source, source_url, observed_at
    ORDER BY observed_at, provider
""")


def gpu_archived(gpu: str, segment: str = "on_demand", t0: datetime | None = None,
                 t1: datetime | None = None) -> dict:
    with normalize.SessionLocal() as s:
        rows = s.execute(_POINTS, {"gpu": gpu, "seg": segment, "t0": t0, "t1": t1}).all()
        observed_from = s.execute(text("SELECT min(hour) FROM market_hourly WHERE gpu = :gpu AND segment = :seg"),
                                  {"gpu": gpu, "seg": segment}).scalar()
    points = [{"t": r.observed_at.isoformat(), "provider": r.provider, "price_per_gpu_hour": float(r.price),
               "listings": r.listings, "source": r.source, "source_url": r.source_url,
               "valid_to": r.valid_to.isoformat() if r.valid_to else None} for r in rows]
    by_provider: dict[str, list] = {}
    months: dict[str, dict[str, float]] = {}
    for r in rows:
        by_provider.setdefault(r.provider, []).append(r)
        m = r.observed_at.strftime("%Y-%m")
        cur = months.setdefault(m, {})
        cur[r.provider] = min(cur.get(r.provider, float("inf")), float(r.price))
    providers = [{"provider": p, "samples": len(rs), "first": rs[0].observed_at.isoformat(),
                  "last": rs[-1].observed_at.isoformat(), "lowest": float(min(x.price for x in rs)),
                  "highest": float(max(x.price for x in rs)), "source": rs[0].source}
                 for p, rs in sorted(by_provider.items())]
    monthly = [{"month": m, "lowest": min(v.values()), "median": statistics.median(v.values()),
                "providers": len(v), "by_provider": dict(sorted(v.items()))} for m, v in sorted(months.items())]
    return {"gpu": gpu, "segment": segment, "kind": "archived", "points": points, "providers": providers,
            "monthly": monthly, "observed_from": observed_from.isoformat() if observed_from else None}


# ----------------------------------------------------------------------------------------------------------------
# One continuous daily series: archived before recording, observed from recording on
# ----------------------------------------------------------------------------------------------------------------

HOLD_DAYS = 45   # an archived Internet Archive price is held until the provider's next capture, at most this long

_OBS_DAILY = text("""
    SELECT provider, date_trunc('day', hour AT TIME ZONE 'UTC') AS day, min(min_price) AS price
    FROM market_hourly
    WHERE gpu = :gpu AND segment = :seg AND min_price IS NOT NULL
      AND (CAST(:t0 AS timestamptz) IS NULL OR hour >= :t0)
    GROUP BY 1, 2
""")


def _day(t: datetime):
    return t.astimezone(timezone.utc).date()


def extended(gpu: str, segment: str = "on_demand", days: int | None = 365) -> dict:
    """Daily, one value per provider per UTC day, then lowest / median / highest across providers.

    Before `archived_until` (the first UTC day OpenGrid recorded this GPU live) a provider's value is its archived
    price, held from one capture until its next (at most HOLD_DAYS; an AWS price-list version until its period
    end): the same "a posted price holds until it changes" rule the observed step lines use. From that day on
    it is the provider's lowest observed price that day (market_hourly), and archived data is not used.
    `kind` per day says which; `captures` lets a reader find the document behind any archived value.
    """
    with normalize.SessionLocal() as s:
        end = s.execute(text("SELECT max(hour) FROM market_hourly")).scalar()
        first_obs = s.execute(text("SELECT min(hour) FROM market_hourly WHERE gpu = :gpu AND segment = :seg"),
                              {"gpu": gpu, "seg": segment}).scalar()
        arch = s.execute(_POINTS, {"gpu": gpu, "seg": segment, "t0": None, "t1": None}).all()
        t0 = (end - timedelta(days=days)) if (end and days) else None
        obs = s.execute(_OBS_DAILY, {"gpu": gpu, "seg": segment, "t0": t0}).all()
    today = _day(end) if end else (_day(arch[-1].observed_at) if arch else None)
    if today is None:
        return {"gpu": gpu, "segment": segment, "days": [], "providers": [], "market": {}, "archived_until": None}
    seam = _day(first_obs) if first_obs else today + timedelta(days=1)
    start = _day(t0) if t0 else min([_day(r.observed_at) for r in arch] + [seam])
    day_list = [start + timedelta(days=i) for i in range((today - start).days + 1)]

    caps: dict[str, list] = {}
    for r in arch:
        caps.setdefault(r.provider, []).append(r)
    observed: dict[str, dict] = {}
    for r in obs:
        observed.setdefault(r.provider, {})[r.day.date() if hasattr(r.day, "date") else r.day] = float(r.price)

    providers = []
    for p in sorted(set(caps) | set(observed)):
        values, held = [], []
        pcaps, k = caps.get(p, []), -1
        for d in day_list:
            if d >= seam:
                values.append(observed.get(p, {}).get(d))
                held.append(None)
                continue
            while k + 1 < len(pcaps) and _day(pcaps[k + 1].observed_at) <= d:
                k += 1
            v = None
            if k >= 0:
                c = pcaps[k]
                limit = _day(c.valid_to) - timedelta(days=1) if c.valid_to else _day(c.observed_at) + timedelta(days=HOLD_DAYS)
                if d <= limit:
                    v = float(c.price)
            values.append(v)
            held.append(k if v is not None else None)
        if any(v is not None for v in values):
            providers.append({"provider": p, "values": values, "capture": held,
                              "captures": [{"t": c.observed_at.isoformat(), "source": c.source, "url": c.source_url}
                                           for c in pcaps]})
    lowest, median, highest, count = [], [], [], []
    for i in range(len(day_list)):
        vs = [x["values"][i] for x in providers if x["values"][i] is not None]
        lowest.append(min(vs) if vs else None)
        median.append(statistics.median(vs) if vs else None)
        highest.append(max(vs) if vs else None)
        count.append(len(vs))
    return {"gpu": gpu, "segment": segment, "resolution": "1d",
            "days": [datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat() for d in day_list],
            "kind": ["archived" if d < seam else "observed" for d in day_list],
            "archived_until": datetime(seam.year, seam.month, seam.day, tzinfo=timezone.utc).isoformat(),
            "market": {"lowest": lowest, "median": median, "highest": highest, "providers": count},
            "providers": providers, "hold_days": HOLD_DAYS}
