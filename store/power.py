"""Wholesale power-market tables (power/; methodology/power-markets.md). Their own tables, never GPU data.

    power_fetches   every request to a power source, failures and empty answers too: url (keys never stored),
                    status, bytes, sha256 of the exact bytes received and, where the source's per-day size was
                    measured small, the bytes themselves (compressed). Backfill fetches keep the hash only: the URL
                    re-fetches them and the hash verifies the copy (power/registry.py Dataset.keep_raw)
    power_series    the catalogue: one row per series (a hub's day-ahead LMP, a balancing authority's hourly
                    demand). Its visibility comes from the source's terms verdict (power/registry.py), refreshed on
                    every run; "internal" series are admin-only and never in a public read
    power_days      one row per (series, market day): the day's values as real[] (24 hourly, 288 five-minute;
                    23/25 or 276/300 on DST days, NULL where the source gave nothing), plus the day's mean/min/max
                    so day-level reads never unnest. Slot i starts at the market day's local midnight + i x interval.
                    Daily / weekly / monthly series (natural gas: interval_minutes 1440 / 10080 / 43200) hold one
                    value per row, `day` being the date the source labels the period with (week ending, month start)
    power_gpu_region_daily  DERIVED (power/cross.py): per day, provider region and GPU, the local power price beside
                    the GPU price: power cost per GPU-hour (TDP x PUE x price) and the spread. Internal unless every
                    input is public
    power_leadlag   DERIVED (power/cross.py): the correlation of daily GPU price / capacity changes with daily power
                    price changes at lags of -LAG..+LAG days. A correlation, never a cause
    power_generators, power_generator_history, power_plant_generation, power_queue_projects
                    PHYSICAL SITES (power/sites/; 0045): EIA generator inventory with its change-only history,
                    annual net generation per plant, and interconnection-queue projects per published edition
    power_flare_sites / power_flare_volumes / power_flare_areas   gas flared at oil and gas sites: sites with
                    coordinates, their volume per year, regulator volumes per county and month (power/flaring.py;
                    methodology/power-waste.md). Curtailment needs no table of its own: it is power_series /
                    power_days (kind "curtailment")

Nothing here is read by listings, rollups, indices, fixings, archived prices or any GPU statistic.
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer
from sqlalchemy import LargeBinary, REAL, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class PowerFetch(Base):
    __tablename__ = "power_fetches"
    __table_args__ = (Index("ix_power_fetches_dataset_period", "dataset", "period_start"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(1000))          # secrets redacted (power/http.py)
    period_start: Mapped[date | None] = mapped_column(Date)  # market days the request covered
    period_end: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    empty: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # answered, no data
    error: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    content_type: Mapped[str | None] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(8))            # "body" (bytes kept) | "hash" (url + sha256 only)
    encoding: Mapped[str | None] = mapped_column(String(8))  # body as stored: "zip" (as received) | "gzip" | None
    body = mapped_column(LargeBinary, nullable=True)   # (an annotation would read the `bytes` column above)
    days_stored: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class PowerSeries(Base):
    __tablename__ = "power_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120), unique=True)   # "caiso.da.lmp.th_np15"
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(64))
    iso: Mapped[str | None] = mapped_column(String(16))          # market operator / balancing authority group
    commodity: Mapped[str] = mapped_column(String(16), default="electricity", server_default="electricity")
    kind: Mapped[str] = mapped_column(String(32))                # "lmp" | "demand" | "spot_price" | "storage" | ...
    market: Mapped[str] = mapped_column(String(16))              # "da" | "rt" | "actual" | "forecast"
    location: Mapped[str] = mapped_column(String(64))            # the source's own node / zone / BA code
    location_type: Mapped[str] = mapped_column(String(16))       # "hub" | "zone" | "load_area" | "ba"
    unit: Mapped[str] = mapped_column(String(24))                # "usd_per_mwh" | "mwh"
    interval_minutes: Mapped[int] = mapped_column(Integer)      # 5, 15, 60; 1440 daily, 10080 weekly, 43200 monthly
    tz: Mapped[str] = mapped_column(String(40))                  # the market day's time zone
    name: Mapped[str] = mapped_column(String(200))
    visibility: Mapped[str] = mapped_column(String(16))          # "public" | "internal" (the source's terms)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PowerDay(Base):
    __tablename__ = "power_days"
    __table_args__ = (Index("ix_power_days_day", "day"),)

    series_id: Mapped[int] = mapped_column(Integer, ForeignKey("power_series.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)    # market (operating) day in power_series.tz
    vals: Mapped[list] = mapped_column(ARRAY(REAL))
    n: Mapped[int] = mapped_column(SmallInteger)                 # non-null values
    complete: Mapped[bool] = mapped_column(Boolean)              # every slot of the day has a value
    mean: Mapped[float | None] = mapped_column(REAL)
    min: Mapped[float | None] = mapped_column(REAL)
    max: Mapped[float | None] = mapped_column(REAL)
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revisions: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")


class PowerGpuRegionDaily(Base):
    __tablename__ = "power_gpu_region_daily"
    __table_args__ = (Index("ix_power_gpu_region_daily_gpu_day", "gpu", "day"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    region: Mapped[str] = mapped_column(String(128), primary_key=True)
    gpu: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(2))
    iso: Mapped[str] = mapped_column(String(16))
    price_series: Mapped[str] = mapped_column(String(120))       # power_series.key used
    match: Mapped[str] = mapped_column(String(24))               # "zone" (documented place) | "market_area_hub"
    power_price_usd_mwh: Mapped[float] = mapped_column(Float)    # the day's mean
    power_price_max_usd_mwh: Mapped[float | None] = mapped_column(Float)
    tdp_w: Mapped[int] = mapped_column(Integer)
    pue: Mapped[float] = mapped_column(Float)
    power_cost_usd_per_gpu_hour: Mapped[float] = mapped_column(Float)
    gpu_price_median: Mapped[float | None] = mapped_column(Float)   # that day's listings there, USD per GPU-hour
    gpu_price_low: Mapped[float | None] = mapped_column(Float)
    listings: Mapped[int] = mapped_column(Integer)
    spread_usd_per_gpu_hour: Mapped[float | None] = mapped_column(Float)   # median GPU price - power cost
    power_share: Mapped[float | None] = mapped_column(Float)              # power cost / median GPU price
    visibility: Mapped[str] = mapped_column(String(16))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PowerLeadLag(Base):
    __tablename__ = "power_leadlag"
    __table_args__ = (UniqueConstraint("gpu", "iso", "price_series", "metric", "lag_days", "window_end",
                                       name="uq_power_leadlag"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gpu: Mapped[str] = mapped_column(String(128))
    iso: Mapped[str] = mapped_column(String(16))
    price_series: Mapped[str] = mapped_column(String(120))
    metric: Mapped[str] = mapped_column(String(32))              # "gpu_price_median" | "capacity_available"
    lag_days: Mapped[int] = mapped_column(SmallInteger)          # > 0: the GPU series moves AFTER power
    window_start: Mapped[date] = mapped_column(Date)
    window_end: Mapped[date] = mapped_column(Date)
    n: Mapped[int] = mapped_column(Integer)
    corr: Mapped[float | None] = mapped_column(Float)
    spike_days: Mapped[int] = mapped_column(Integer, default=0)
    visibility: Mapped[str] = mapped_column(String(16))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    method: Mapped[str] = mapped_column(Text)


# --------------------------------------------------------------------------- physical sites (power/sites/; 0045)
# Where plants are, how much they run, and what is queued to connect. Snapshots, not series: their own tables.
# Every request is a power_fetches row (dataset "sites.*", url + sha256; bodies are never kept: each source keeps
# its files online). Tiers come from power/sites/registry.py.

class PowerGenerator(Base):
    """One row per EIA generator, the latest state seen: `period` is the inventory month it reflects and `via`
    which publication said so ("api": the operable inventory, "file": the monthly EIA-860M workbook, which adds
    planned, retired and canceled generators). A later month replaces a row; for the same month the file wins."""
    __tablename__ = "power_generators"
    __table_args__ = (Index("ix_power_generators_state_county", "state", "county"),
                      Index("ix_power_generators_ba", "ba_code"))

    plant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generator_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    plant_name: Mapped[str | None] = mapped_column(String(200))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    entity_name: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str | None] = mapped_column(String(2))
    county: Mapped[str | None] = mapped_column(String(64))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    ba_code: Mapped[str | None] = mapped_column(String(16))
    sector: Mapped[str | None] = mapped_column(String(64))
    technology: Mapped[str | None] = mapped_column(String(80))
    energy_source: Mapped[str | None] = mapped_column(String(8))
    prime_mover: Mapped[str | None] = mapped_column(String(8))
    nameplate_mw: Mapped[float | None] = mapped_column(Float)
    summer_mw: Mapped[float | None] = mapped_column(Float)
    winter_mw: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(8))              # EIA code: OP SB OA OS | P L T U V TS OT | RE | CN
    status_group: Mapped[str] = mapped_column(String(16))       # operating standby out_of_service planned retired
                                                                # canceled
    operating_ym: Mapped[str | None] = mapped_column(String(7))           # "YYYY-MM"
    planned_operation_ym: Mapped[str | None] = mapped_column(String(7))
    planned_retirement_ym: Mapped[str | None] = mapped_column(String(7))
    retirement_ym: Mapped[str | None] = mapped_column(String(7))
    period: Mapped[date] = mapped_column(Date)                   # inventory month (first day)
    via: Mapped[str] = mapped_column(String(8))                  # "api" | "file"
    first_period: Mapped[date] = mapped_column(Date)
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PowerGeneratorHistory(Base):
    """Change-only: a row when a generator first appears, when its status, capacity or (planned) retirement /
    operation month changes, and when it leaves the operable inventory ("left")."""
    __tablename__ = "power_generator_history"
    __table_args__ = (UniqueConstraint("plant_id", "generator_id", "period", name="uq_power_generator_history"),
                      Index("ix_power_generator_history_period", "period"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plant_id: Mapped[int] = mapped_column(Integer)
    generator_id: Mapped[str] = mapped_column(String(16))
    period: Mapped[date] = mapped_column(Date)
    change: Mapped[str] = mapped_column(String(8))               # "new" | "changed" | "left"
    status: Mapped[str] = mapped_column(String(8))
    status_group: Mapped[str] = mapped_column(String(16))
    nameplate_mw: Mapped[float | None] = mapped_column(Float)
    summer_mw: Mapped[float | None] = mapped_column(Float)
    planned_operation_ym: Mapped[str | None] = mapped_column(String(7))
    planned_retirement_ym: Mapped[str | None] = mapped_column(String(7))
    retirement_ym: Mapped[str | None] = mapped_column(String(7))
    via: Mapped[str] = mapped_column(String(8))
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))


class PowerPlantGeneration(Base):
    """Annual net generation per plant (EIA-923, all fuels and prime movers). Negative for storage-heavy plants."""
    __tablename__ = "power_plant_generation"

    plant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    net_mwh: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str | None] = mapped_column(String(2))
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revisions: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")


class PowerQueueProject(Base):
    """One interconnection request as one edition of one queue publication lists it. Editions are kept side by
    side (LBNL yearly, ERCOT GIS monthly), so a project's path through the queue is its rows across editions."""
    __tablename__ = "power_queue_projects"
    __table_args__ = (Index("ix_power_queue_projects_place", "state", "county"),
                      CheckConstraint("visibility IN ('public', 'internal')",
                                      name="ck_power_queue_projects_visibility"))

    source: Mapped[str] = mapped_column(String(24), primary_key=True)     # "lbnl_queues" | "ercot_gis"
    edition: Mapped[date] = mapped_column(Date, primary_key=True)         # data as of
    project_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    iso: Mapped[str | None] = mapped_column(String(24))          # LBNL region (an ISO, West, Southeast) | "ERCOT"
    entity: Mapped[str | None] = mapped_column(String(120))      # transmission provider
    state: Mapped[str | None] = mapped_column(String(2))
    county: Mapped[str | None] = mapped_column(String(64))
    fips: Mapped[str | None] = mapped_column(String(5))
    kind: Mapped[str] = mapped_column(String(12))                # "generation" | "load"
    type: Mapped[str | None] = mapped_column(String(48))         # "Solar", "Solar+Battery", "Gas", ...
    type_detail: Mapped[str | None] = mapped_column(String(120))
    mw: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))              # "active" | "withdrawn" | "suspended" | "operational"
    phase: Mapped[str | None] = mapped_column(String(80))
    queue_date: Mapped[date | None] = mapped_column(Date)
    proposed_cod: Mapped[date | None] = mapped_column(Date)
    on_date: Mapped[date | None] = mapped_column(Date)
    withdrawn_date: Mapped[date | None] = mapped_column(Date)
    ia_date: Mapped[date | None] = mapped_column(Date)
    poi: Mapped[str | None] = mapped_column(String(200))
    project_name: Mapped[str | None] = mapped_column(String(200))
    developer: Mapped[str | None] = mapped_column(String(200))
    visibility: Mapped[str] = mapped_column(String(8), server_default="internal")
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- flaring (0046_power_waste)
# Small: thousands of sites, one row per site and year. Raw first: every file read is a power_fetches row.

class PowerFlareSite(Base):
    """A flare site: a satellite detection cell (World Bank / VIIRS: 0.01 degree) or a regulator's location (North
    Dakota: the PLSS section holding the flaring wells). Coordinates are the source's own (WGS84)."""
    __tablename__ = "power_flare_sites"
    __table_args__ = (UniqueConstraint("source", "site_key", name="uq_power_flare_sites_source_key"),
                      Index("ix_power_flare_sites_lat_lon", "lat", "lon"),
                      Index("ix_power_flare_sites_country_state", "country", "state"),
                      CheckConstraint("visibility IN ('public', 'internal')", name="ck_power_flare_sites_visibility"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))
    site_key: Mapped[str] = mapped_column(String(64))           # "c+3103+04728" | "t151r101s11"
    name: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str | None] = mapped_column(String(32))         # "upstream oil" | "downstream" | "wells" ...
    country: Mapped[str | None] = mapped_column(String(3))       # ISO 3166 alpha-3
    country_name: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(8))
    county: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    visibility: Mapped[str] = mapped_column(String(8), default="internal", server_default="internal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PowerFlareVolume(Base):
    """A site's flared gas in a year, in m3 and mcf (1 mcf = 28.316846592 m3; the source's own unit is exact, the
    other converted). monthly_mcf holds the months where the source is monthly (North Dakota): slot 1 = January,
    NULL = not reported (yet). energy_mwh_derived is DERIVED (power/flaring.py: heat content x heat rate)."""
    __tablename__ = "power_flare_volumes"
    __table_args__ = (Index("ix_power_flare_volumes_year", "year"),)

    site_id: Mapped[int] = mapped_column(Integer, ForeignKey("power_flare_sites.id", ondelete="CASCADE"),
                                         primary_key=True)
    year: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    volume_m3: Mapped[float | None] = mapped_column(Float)
    volume_mcf: Mapped[float | None] = mapped_column(Float)
    monthly_mcf: Mapped[list | None] = mapped_column(ARRAY(REAL))
    months_reported: Mapped[int | None] = mapped_column(SmallInteger)
    detections: Mapped[int | None] = mapped_column(Integer)      # source rows merged into the site that year
    energy_mwh_derived: Mapped[float | None] = mapped_column(Float)
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PowerFlareArea(Base):
    """Flared gas by area and period from a regulator (North Dakota: county x month), with the gas produced there
    where the source gives it, so the flared share is known."""
    __tablename__ = "power_flare_areas"
    __table_args__ = (CheckConstraint("visibility IN ('public', 'internal')", name="ck_power_flare_areas_visibility"),
                      Index("ix_power_flare_areas_period", "period"))

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    area_type: Mapped[str] = mapped_column(String(16), primary_key=True)   # "county" | "state"
    country: Mapped[str] = mapped_column(String(3), primary_key=True)
    state: Mapped[str] = mapped_column(String(8), primary_key=True)
    area: Mapped[str] = mapped_column(String(64), primary_key=True)
    period: Mapped[date] = mapped_column(Date, primary_key=True)          # first day of the month / year
    period_type: Mapped[str] = mapped_column(String(8), primary_key=True)  # "month" | "year"
    volume_mcf: Mapped[float | None] = mapped_column(Float)
    gas_produced_mcf: Mapped[float | None] = mapped_column(Float)
    wells: Mapped[int | None] = mapped_column(Integer)                     # wells reporting gas flared > 0
    energy_mwh_derived: Mapped[float | None] = mapped_column(Float)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))
    visibility: Mapped[str] = mapped_column(String(8), default="internal", server_default="internal")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
