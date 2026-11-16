"""Lium market data beyond prices (lium_data/; methodology/lium-data.md). ADMIN ONLY: every value is INTERNAL
(provider_meta.INTERNAL_SOURCES), so nothing here is on a public page, the public API or a statistic.

    GET /v1/ops/lium/status                 every feed: newest answer, state (403 / 404 shown plainly), row counts
    GET /v1/ops/lium/trades/stats           per UTC end day and GPU model from the partner trade records: rentals,
                                            GPU-hours, VWAP, low, high (kind transaction, basis derived_from_trades)
    GET /v1/ops/lium/trades/check           that VWAP beside Lium's own daily VWAP (daily-aggregate feed)
    GET /v1/ops/lium/hourly                 partner hourly rentals / GPU-hours / mean paid price per GPU model
    GET /v1/ops/lium/daily-aggregate        partner daily VWAP per GPU model and provider country
    GET /v1/ops/lium/throughput             Lium's published daily GMV / GPU-hours / rentals / utilisation, and the
                                            30-day median paid price per model (kind transaction, published_aggregate)
    GET /v1/ops/lium/feeds/{feed}           the newest answer of a keyless feed, one row per GPU (launch_stats,
                                            requests, executor_stats, spot_idle_rates, idle_capacity, shared_config)
    GET /v1/ops/lium/feeds/{feed}/history   one metric's changes over time
    GET /v1/ops/lium/providers              per provider (miner hotkey): rentals started and failed, renters, uptime,
                                            and how many machines and GPUs are rented now and what they earn per hour
    GET /v1/ops/lium/grafana/rentals        per UTC day or hour of rental start, GPU model and tier, from Lium's
                                            public Grafana: rentals, median / mean / low / high price paid
    GET /v1/ops/lium/grafana/hourly         renter spend, GPUs listed or GPUs rented per UTC hour and GPU model
    GET /v1/ops/lium/grafana/prices-daily   Lium's median asking price and median price paid per UTC day and GPU model
    GET /v1/ops/lium/grafana/launches       rentals created and node-side deploy failures on the whole network, per
                                            UTC hour or day
    GET /v1/ops/lium/grafana/daily-counts   failed deploys by cause, or penalties by reason code, per UTC day
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, TRANSACTION, envelope
from lium_data import feeds as F
from lium_data import partner, reads, schedule

router = APIRouter()
admin = require_scope("admin")
METHOD = "lium-data"
NOTE = "INTERNAL data (provider_meta.INTERNAL_SOURCES): never republish"

schedule.register()  # lium_public, lium_providers, lium_grafana (and lium_partner when LIUM_PARTNER_API_KEY is set)


def _env(data, kind: str, basis: str | None = None, **meta):
    return envelope(data, kind=kind, methodology=METHOD, visibility="internal", basis=basis, note=NOTE, **meta)


@router.get("/v1/ops/lium/status", tags=["ops"], summary="Ops: Lium market-data feeds, state and row counts (internal)")
def lium_status(who: Principal = Depends(admin)):
    return _env(reads.status(), OBSERVED)


@router.get("/v1/ops/lium/trades/stats", tags=["ops"], summary="Ops: VWAP, GPU-hours and rentals per GPU per day from Lium trades (internal)")
def lium_trade_stats(days: int = Query(30, ge=1, le=730), gpu: str | None = None, country: str | None = None,
                     who: Principal = Depends(admin)):
    rows = partner.trade_stats(days, gpu, country)
    return _env(rows, TRANSACTION, F.DERIVED_FROM_TRADES, count=len(rows), window_days=days,
                day_axis="UTC day the rental ended; a rental counts whole on its end day")


@router.get("/v1/ops/lium/trades/check", tags=["ops"], summary="Ops: VWAP from Lium trades vs Lium's published daily VWAP (internal)")
def lium_trade_check(days: int = Query(30, ge=1, le=365), who: Principal = Depends(admin)):
    return _env(reads.trade_stats_vs_published(days), TRANSACTION, F.DERIVED_FROM_TRADES, window_days=days)


@router.get("/v1/ops/lium/hourly", tags=["ops"], summary="Ops: Lium hourly rentals, GPU-hours, mean paid price (internal)")
def lium_hourly(hours: int = Query(48, ge=1, le=24 * 90), gpu: str | None = None, who: Principal = Depends(admin)):
    rows = reads.hourly(hours, gpu)
    return _env(rows, TRANSACTION, F.PUBLISHED_AGGREGATE, count=len(rows))


@router.get("/v1/ops/lium/daily-aggregate", tags=["ops"], summary="Ops: Lium's daily VWAP per GPU and country (internal)")
def lium_daily_aggregate(days: int = Query(30, ge=1, le=730), gpu: str | None = None, who: Principal = Depends(admin)):
    rows = reads.daily_aggregate(days, gpu)
    return _env(rows, TRANSACTION, F.PUBLISHED_AGGREGATE, count=len(rows))


@router.get("/v1/ops/lium/throughput", tags=["ops"], summary="Ops: Lium's published GMV, GPU-hours, rentals, median paid price (internal)")
def lium_throughput(days: int = Query(60, ge=1, le=730), who: Principal = Depends(admin)):
    return _env(reads.throughput(days), TRANSACTION, F.PUBLISHED_AGGREGATE,
                caveat="Lium's own figures from its own rentals; not checked against trade records")


@router.get("/v1/ops/lium/feeds/{feed}", tags=["ops"], summary="Ops: the newest answer of a keyless Lium feed (internal)")
def lium_feed(feed: str, who: Principal = Depends(admin)):
    if feed not in F.FEEDS or F.FEEDS[feed].partner:
        raise HTTPException(404, f"unknown feed; one of {[f.name for f in F.PUBLIC_FEEDS]}")
    out = reads.current(feed)
    if out is None:
        raise HTTPException(404, "not fetched yet")
    return _env(out, F.FEEDS[feed].kind, F.FEEDS[feed].basis)


@router.get("/v1/ops/lium/feeds/{feed}/history", tags=["ops"], summary="Ops: one Lium feed metric over time (internal)")
def lium_feed_history(feed: str, subject: str, metric: str, gpu_count: int = 0, days: int = Query(30, ge=1, le=365),
                      who: Principal = Depends(admin)):
    if feed not in F.FEEDS or F.FEEDS[feed].partner:
        raise HTTPException(404, "unknown feed")
    rows = reads.history(feed, subject, metric, gpu_count, days)
    return _env(rows, F.FEEDS[feed].kind, F.FEEDS[feed].basis, count=len(rows))


@router.get("/v1/ops/lium/providers", tags=["ops"], summary="Ops: Lium providers, their rental record and what is rented now (internal)")
def lium_providers(limit: int = Query(100, ge=1, le=1000), who: Principal = Depends(admin)):
    out = reads.providers()
    out["rows"] = out["rows"][:limit]
    return _env(out, OBSERVED, "provider_reported", count=out["providers"],
                source="lium.io/api/provider-stats/{hotkey} and its /executors, read hourly")


_METRICS = {p.metric: p for p in F.SERIES_PANELS}


@router.get("/v1/ops/lium/grafana/rentals", tags=["ops"], summary="Ops: prices paid per GPU on Lium, from its public Grafana (internal)")
def lium_grafana_rentals(days: int = Query(30, ge=1, le=730), gpu: str | None = None,
                         tier: str | None = Query(None, pattern="^(spot|secure)$"),
                         by: str = Query("day", pattern="^(day|hour)$"), who: Principal = Depends(admin)):
    rows = reads.grafana_rental_stats(days, gpu, tier, by)
    return _env(rows, TRANSACTION, F.DERIVED_FROM_TRADES, count=len(rows), window_days=days,
                source="grafana.lium.io, Marketplace prices: one point per rental start",
                caveat="one vote per rental, not volume-weighted: the panel gives no GPU count or end time; "
                       "tier is null until the spot or secure read names it")


@router.get("/v1/ops/lium/grafana/hourly", tags=["ops"], summary="Ops: Lium renter spend and fleet per hour and GPU, from its public Grafana (internal)")
def lium_grafana_hourly(metric: str = "spend_usd_per_hour", hours: int = Query(48, ge=1, le=24 * 400),
                        gpu: str | None = None, who: Principal = Depends(admin)):
    if metric not in _METRICS:
        raise HTTPException(404, f"unknown metric; one of {sorted(_METRICS)}")
    rows = reads.grafana_hourly(metric, hours, gpu)
    panel = _METRICS[metric]
    return _env(rows, panel.kind, panel.basis, count=len(rows), source=f"grafana.lium.io, {panel.title}",
                caveat="Lium's hourly mean of its own samples; the running hour is partial until it closes")


@router.get("/v1/ops/lium/grafana/prices-daily", tags=["ops"], summary="Ops: Lium's daily median asking and paid price per GPU, from its public Grafana (internal)")
def lium_grafana_prices_daily(days: int = Query(30, ge=1, le=730), gpu: str | None = None,
                              who: Principal = Depends(admin)):
    rows = reads.grafana_price_daily(days, gpu)
    panel = F.GRAFANA_PANELS["grafana_price_daily"]
    return _env(rows, panel.kind, panel.basis, count=len(rows), window_days=days,
                source=f"grafana.lium.io, {panel.title}",
                columns={"median_asking": "observed: unrented nodes' asking price per GPU-hour",
                         "median_paid": "transaction, published aggregate: price per GPU-hour on rented nodes"},
                caveat="Lium's medians over its six-hourly samples, one vote per node, models with three nodes or "
                       "more; paid from 2025-12-03, asking from 2026-09-16; the running day is partial")


@router.get("/v1/ops/lium/grafana/launches", tags=["ops"], summary="Ops: rentals created and node-side deploy failures on Lium, from its public Grafana (internal)")
def lium_grafana_launches(hours: int = Query(48, ge=1, le=24 * 730), by: str = Query("hour", pattern="^(hour|day)$"),
                          who: Principal = Depends(admin)):
    rows = reads.grafana_launches(hours, by)
    panel = F.GRAFANA_PANELS["grafana_launch_created"]
    return _env(rows, panel.kind, panel.basis, count=len(rows), source=f"grafana.lium.io, {panel.title}",
                caveat="whole network, not per GPU model; a null count is an hour the panel had no row for, and "
                       "Lium's failure log begins 2026-08-12; failures Lium puts down to the renter are left out")


@router.get("/v1/ops/lium/grafana/daily-counts", tags=["ops"], summary="Ops: Lium's failed deploys by cause or penalties by reason per day, from its public Grafana (internal)")
def lium_grafana_daily_counts(series: str = "deploy_failures", days: int = Query(30, ge=1, le=730),
                              label: str | None = None, who: Principal = Depends(admin)):
    if series not in F.COUNT_SERIES:
        raise HTTPException(404, f"unknown series; one of {sorted(F.COUNT_SERIES)}")
    rows = reads.grafana_daily_counts(series, days, label)
    panel = F.COUNT_SERIES[series]
    return _env(rows, panel.kind, panel.basis, count=len(rows), window_days=days,
                source=f"grafana.lium.io, {panel.title}",
                caveat="Lium's own classification; the running day is partial until it closes")
