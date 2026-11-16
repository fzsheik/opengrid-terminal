"""Wholesale power markets (power/; methodology/power-markets.md).

    GET /v1/ops/power/sources        ADMIN ONLY. The registry: every source, its terms verdict, what it needs
    GET /v1/ops/power/status         ADMIN ONLY. Per dataset: series, series-days, depth, bytes; table sizes
    GET /v1/ops/power/series         ADMIN ONLY. The catalogue (every tier), or ?key=a,b: day rows (since, until,
                                     intervals=true for the per-interval values). ?source=eia_gas: natural gas
    GET /v1/ops/power/map            ADMIN ONLY. Provider regions with current listings -> ISO / price series
    GET /v1/ops/power/cross/regional ADMIN ONLY. Power cost per GPU-hour and the spread to the GPU price, by region
    GET /v1/ops/power/cross/leadlag  ADMIN ONLY. Lagged correlations of GPU price / capacity (and Henry Hub) moves
                                     with power price moves. Correlation, never cause
    GET /v1/ops/power/shifts         ADMIN ONLY. Power-price spikes in each major shift's window, joined at read
                                     time (market_shift_items untouched). Related, not causal
    GET /v1/ops/power/plants         ADMIN ONLY. Plants (EIA-860M generators summed) with capacity factor (EIA-923),
                                     idle MW and planned retirements; ?state ba technology status county year sort
                                     (idle | retirement | nameplate | cf) limit
    GET /v1/ops/power/queues         ADMIN ONLY. Interconnection-queue projects (LBNL Queued Up, ERCOT GIS), latest
                                     edition per source unless ?edition; plus MW queued per county, generation and
                                     load apart, active requests only
    GET /v1/ops/power/sites/status   ADMIN ONLY. Site tables: rows, bytes, editions, fetches; and the registry
    GET /v1/ops/power/curtailment    ADMIN ONLY. Wind / solar curtailment per region, reason and day (MWh), with the
                                     curtailed share of potential output where output is known (power/waste.py)
    GET /v1/ops/power/flaring        ADMIN ONLY. Flare sites ranked by flared volume with coordinates, or (?view=
                                     counties) regulator volumes by county, or (?lat=&lon=) the volume near a point
    GET /v1/ops/power/nodes/cheap    ADMIN ONLY. ERCOT / SPP nodes ranked by the share of their intervals priced
                                     below $20/MWh over a window (days=90, at most 365): where power is stranded.
                                     market=ercot|spp, series=rt|da, zone, node_type, min_days, until, limit
    GET /v1/ops/power/sites/ranking  ADMIN ONLY. US counties ranked for a small datacenter on spare, cheap power
                                     (power/siting.py): every part shown, the price grain on every row; ?state iso
                                     min_idle_mw days weights=name:number,... until limit
    GET /v1/ops/power/sites/near     ADMIN ONLY. The same parts for the area within km of lat / lon

Every power read is admin-only, with Cache-Control: private, no-store. There is no public route: power data is for
OpenGrid's own routing and research (the EIA-930 series are public domain and could be published later, through
the Data API's versioned contract). Context, not cause: nothing here says one series moved another.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from accounts.auth import Principal, require_scope
from api.common import OBSERVED, envelope
from power import cross, nodal, reads, registry, runner
from power.sites import reads as site_reads, registry as site_registry, runner as sites_runner

router = APIRouter()
ADMIN = require_scope("admin")
DOC = "methodology/power-markets.md"
MAX_KEYS = 20

runner.register()  # the power_forward job (off until POWER_ENABLED)
sites_runner.register()  # the power_sites job (off until POWER_SITES_ENABLED)
SITES_DOC = "methodology/power-markets.md#physical-sites"


def _ops(resp: Response, rows, **kw):
    resp.headers["Cache-Control"] = "private, no-store"
    return envelope(rows, kind=OBSERVED, doc=DOC, rule=reads.NOT_CAUSAL, **kw)


@router.get("/v1/ops/power/sources", tags=["ops"], include_in_schema=False,
            summary="Ops: power sources, terms verdicts and configuration")
def ops_power_sources(response: Response, who: Principal = Depends(ADMIN)):
    from power import flaring

    rows = registry.as_list()
    return _ops(response, rows, count=len(rows), selected=registry.selected(), flaring=flaring.verdicts(),
                declined=registry.declined_list())


@router.get("/v1/ops/power/status", tags=["ops"], include_in_schema=False, summary="Ops: power storage status")
def ops_power_status(response: Response, who: Principal = Depends(ADMIN)):
    st = runner.status()
    return _ops(response, st["datasets"], count=len(st["datasets"]), table_bytes=st["table_bytes"],
                total_bytes=st["total_bytes"])


@router.get("/v1/ops/power/series", tags=["ops"], include_in_schema=False,
            summary="Ops (internal tiers included): power series catalogue or day rows")
def ops_power_series(response: Response, key: str | None = Query(None, description="comma-separated series keys"),
                     source: str | None = None, since: date | None = None, until: date | None = None,
                     intervals: bool = False, who: Principal = Depends(ADMIN)):
    if not key:
        rows = reads.catalog(public_only=False, source=source)
        return _ops(response, rows, count=len(rows))
    keys = [k.strip() for k in key.split(",") if k.strip()]
    if len(keys) > MAX_KEYS:
        raise HTTPException(422, {"code": "too_many_series", "message": f"at most {MAX_KEYS} series per request"})
    rows = reads.days(keys, since, until, public_only=False, intervals=intervals)
    return _ops(response, rows, count=len(rows), keys=keys)


@router.get("/v1/ops/power/map", tags=["ops"], include_in_schema=False,
            summary="Ops: provider regions mapped to power markets (documented places only)")
def ops_power_map(response: Response, who: Principal = Depends(ADMIN)):
    from power import geo
    from sqlalchemy import text

    import normalize

    with normalize.SessionLocal() as s:
        regions = s.execute(text("""SELECT provider, region, country, count(*) AS listings FROM compute_listings
                                    WHERE canonical_gpu_name IS NOT NULL AND region IS NOT NULL
                                    GROUP BY 1, 2, 3 ORDER BY 1, 2""")).all()
    rows = [{"provider": r.provider, "region": r.region, "country": r.country, "listings": r.listings,
             "power": geo.locate(r.provider, r.region, r.country)} for r in regions]
    return _ops(response, rows, count=len(rows), mapped=sum(1 for r in rows if r["power"]))


@router.get("/v1/ops/power/cross/regional", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): power cost per GPU-hour and spread by region")
def ops_power_regional(response: Response, gpu: str | None = None, provider: str | None = None,
                       iso: str | None = None, since: date | None = None,
                       limit: int = Query(2000, ge=1, le=20000), who: Principal = Depends(ADMIN)):
    rows = cross.regional(gpu, provider, iso, since, limit)
    return _ops(response, rows, count=len(rows), visibility="internal", pue=cross.pue(), note=cross.RULE)


@router.get("/v1/ops/power/cross/leadlag", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): lagged correlations of GPU moves with power / gas moves")
def ops_power_leadlag(response: Response, gpu: str | None = None, iso: str | None = None,
                      metric: str | None = None, who: Principal = Depends(ADMIN)):
    rows = cross.leadlag(gpu, iso, metric)
    return _ops(response, rows, count=len(rows), visibility="internal", method=cross.METHOD, note=cross.RULE)


@router.get("/v1/ops/power/nodes/cheap", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): nodes ranked by the share of intervals priced below $20/MWh")
def ops_power_nodes_cheap(response: Response, days: int = Query(90, ge=1, le=nodal.MAX_WINDOW_DAYS),
                          market: str | None = Query(None, pattern="^(ercot|spp)$"),
                          series: str = Query("rt", pattern="^(rt|da)$"), zone: str | None = None,
                          node_type: str | None = None, min_days: int = Query(7, ge=1, le=nodal.MAX_WINDOW_DAYS),
                          until: date | None = None, limit: int = Query(100, ge=1, le=5000),
                          who: Principal = Depends(ADMIN)):
    rows = nodal.cheap_nodes(days, market, series=series, zone=zone, node_type=node_type, min_days=min_days,
                             limit=limit, until=until)
    return _ops(response, rows, count=len(rows), visibility="internal", window_days=days, series=series,
                note=nodal.READ_NOTE)


@router.get("/v1/ops/power/shifts", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): power-price spikes around major market shifts (related, not causal)")
def ops_power_shifts(response: Response, gpu: str | None = None, limit: int = Query(50, ge=1, le=200),
                     who: Principal = Depends(ADMIN)):
    rows = cross.shift_power(limit, gpu)
    return _ops(response, rows, count=len(rows), visibility="internal", note=cross.RULE)


# --------------------------------------------------------------------------- physical sites (power/sites/)

def _sites(resp: Response, rows, **kw):
    resp.headers["Cache-Control"] = "private, no-store"
    return envelope(rows, kind=OBSERVED, doc=SITES_DOC, **kw)


@router.get("/v1/ops/power/plants", tags=["ops"], include_in_schema=False,
            summary="Ops: power plants with capacity factor, idle MW and planned retirements")
def ops_power_plants(response: Response, state: str | None = None, ba: str | None = None,
                     technology: str | None = None, county: str | None = None,
                     status: str | None = Query(None, description="operating | standby | out_of_service | planned | "
                                                                  "retired | canceled | removed"),
                     sort: str = Query("idle", description="idle | retirement | nameplate | cf"),
                     year: int | None = Query(None, ge=2001, le=2100),
                     limit: int = Query(100, ge=1, le=5000), who: Principal = Depends(ADMIN)):
    if sort not in site_reads.SORTS:
        raise HTTPException(422, {"code": "bad_sort", "message": f"sort must be one of {site_reads.SORTS}"})
    rows = site_reads.plants(state=state, ba=ba, technology=technology, status=status, sort=sort, limit=limit,
                             year=year, county=county)
    return _sites(response, rows, count=len(rows), year=rows[0]["year"] if rows else None,
                  method=site_reads.METHOD,
                  attribution=site_registry.EIA_TERMS.attribution)


@router.get("/v1/ops/power/queues", tags=["ops"], include_in_schema=False,
            summary="Ops: interconnection-queue projects and MW queued per county")
def ops_power_queues(response: Response, source: str | None = Query(None, description="lbnl_queues | ercot_gis"),
                     edition: date | None = None, state: str | None = None, county: str | None = None,
                     iso: str | None = None, type: str | None = None,
                     status: str | None = Query(None, description="active | withdrawn | suspended | operational"),
                     kind: str | None = Query(None, description="generation | load"),
                     limit: int = Query(500, ge=1, le=50000), counties: int = Query(50, ge=0, le=5000),
                     who: Principal = Depends(ADMIN)):
    rows = site_reads.queue_projects(source, edition, state, county, iso, type, status, kind, limit)
    totals = site_reads.queued_mw_by_county(source, edition, state, iso, kind, "active", type, limit=counties or None)
    return _sites(response, rows, count=len(rows), editions=site_reads.latest_editions(),
                  county_totals={"generation": [t for t in totals if t["kind"] == "generation"],
                                 "load": [t for t in totals if t["kind"] == "load"]},
                  note="County totals: active requests only, per source and edition (LBNL includes ERCOT's queue; "
                       "never add the two). No public source lists large-load requests by project yet.")


@router.get("/v1/ops/power/sites/status", tags=["ops"], include_in_schema=False,
            summary="Ops: site tables, editions and fetches; the site registry")
def ops_power_sites_status(response: Response, who: Principal = Depends(ADMIN)):
    st = sites_runner.status()
    return _sites(response, st["tables"], sources=site_registry.as_list(), **{k: v for k, v in st.items()
                                                                               if k != "tables"})


WASTE_DOC = "methodology/power-waste.md"


@router.get("/v1/ops/power/curtailment", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): wind and solar curtailment by region, reason and day")
def ops_power_curtailment(response: Response, iso: str | None = None, fuel: str | None = None,
                          since: date | None = None, until: date | None = None,
                          who: Principal = Depends(ADMIN)):
    from power import waste

    if since and until and since > until:
        raise HTTPException(422, {"code": "bad_window", "message": "since is after until"})
    if since and until and (until - since).days > 3660:
        raise HTTPException(422, {"code": "window_too_long", "message": "at most ten years per request"})
    rows = waste.curtailment(since, until, iso, fuel)
    response.headers["Cache-Control"] = "private, no-store"
    return envelope(rows, kind=OBSERVED, doc=WASTE_DOC, count=len(rows), visibility="internal", note=waste.NOTE,
                    share_rule="share_curtailed = curtailed / (curtailed + output), all-reasons rows only",
                    proxy_rule=("measure 'proxy_hsl_minus_output' (ERCOT) is max(0, HSL - output), an estimate, "
                                "not reported curtailment"))


@router.get("/v1/ops/power/flaring", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): gas flare sites ranked by volume, county volumes, or volume near a point")
def ops_power_flaring(response: Response, view: str = Query("sites", pattern="^(sites|counties)$"),
                      country: str | None = None, state: str | None = None, year: int | None = None,
                      source: str | None = None, lat: float | None = Query(None, ge=-90, le=90),
                      lon: float | None = Query(None, ge=-180, le=180),
                      radius_km: float = Query(50.0, gt=0, le=500), period: date | None = None,
                      limit: int = Query(100, ge=1, le=5000), who: Principal = Depends(ADMIN)):
    from power import flaring, waste

    response.headers["Cache-Control"] = "private, no-store"
    meta = {"doc": WASTE_DOC, "visibility": "internal", "note": waste.NOTE, "energy_rule": flaring.ENERGY_RULE}
    if (lat is None) != (lon is None):
        raise HTTPException(422, {"code": "bad_point", "message": "give both lat and lon"})
    if lat is not None:
        near = waste.flared_near(lat, lon, radius_km, year, source)
        return envelope(near, kind=OBSERVED, count=len(near["nearest"]), **meta)
    if view == "counties":
        rows = waste.flared_by_county(state, period, "month", source, limit)
        return envelope(rows, kind=OBSERVED, count=len(rows), **meta)
    rows = waste.flare_sites(country, state, year, limit, source)
    return envelope(rows, kind=OBSERVED, count=len(rows), **meta)


# --------------------------------------------------------------------------- site ranking (power/siting.py)

SITING_DOC = "methodology/power-siting.md"


def _weights(s: str | None) -> dict | None:
    from power import siting

    try:
        return siting.parse_weights(s) or None
    except ValueError as exc:
        raise HTTPException(422, {"code": "bad_weights", "message": str(exc)}) from None


@router.get("/v1/ops/power/sites/ranking", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): US counties ranked for a small datacenter on spare, cheap power")
def ops_power_sites_ranking(response: Response, state: str | None = Query(None, pattern="^[A-Za-z]{2}$"),
                            iso: str | None = Query(None, description="ERCOT | SPP | CAISO | MISO | PJM | NYISO | "
                                                                      "ISO-NE"),
                            min_idle_mw: float | None = Query(None, ge=0),
                            days: int = Query(90, ge=1, le=365, description="price and curtailment window"),
                            weights: str | None = Query(None, description="name:number,... e.g. idle:0.4,queue:-0.2"),
                            until: date | None = None, limit: int = Query(100, ge=1, le=5000),
                            who: Principal = Depends(ADMIN)):
    from power import siting

    out = siting.ranking(state=state, iso=iso, min_idle_mw=min_idle_mw, window_days=days, weights=_weights(weights),
                         limit=limit, until=until)
    response.headers["Cache-Control"] = "private, no-store"
    return envelope(out["rows"], kind="derived", doc=SITING_DOC, count=len(out["rows"]), visibility="internal",
                    **out["meta"])


@router.get("/v1/ops/power/sites/near", tags=["ops"], include_in_schema=False,
            summary="Ops (internal): the site-ranking parts for the area around a point")
def ops_power_sites_near(response: Response, lat: float = Query(..., ge=-90, le=90),
                         lon: float = Query(..., ge=-180, le=180), km: float = Query(50.0, gt=0, le=500),
                         days: int = Query(90, ge=1, le=365), weights: str | None = None,
                         until: date | None = None, who: Principal = Depends(ADMIN)):
    from power import siting

    out = siting.near(lat, lon, km, window_days=days, weights=_weights(weights), until=until)
    meta = out.pop("meta")
    response.headers["Cache-Control"] = "private, no-store"
    return envelope(out, kind="derived", doc=SITING_DOC, visibility="internal", **meta)
