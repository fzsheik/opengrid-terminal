"""Lium market-data feeds beyond the price poll (lium_data/; methodology/lium-data.md; migration 0040_lium_data).

Raw first, change-only, every row INTERNAL by default (provider_meta.INTERNAL_SOURCES names each feed):

    lium_feed_snapshots    every distinct answer of every feed, the body verbatim (CSV or JSON text). A repeat of the
                           latest answer for the same (feed, params) with the same status and content is not stored
                           again: last_seen_at / seen_count move instead (so a 403 or a 404 is one row, not one per
                           poll). Volatile fields (`generated_at`) are left out of content_sha256, kept in the body.
    lium_trades            completed rentals from the data-partner trades CSV (kind transaction, basis trade_record),
                           one row per stable row key (the feed's trade id when it has one, else a hash of the row)
    lium_hourly_gpu        data-partner hourly rentals / GPU-hours / mean paid price per closed UTC hour and GPU model
    lium_daily_aggregate   data-partner daily VWAP per end day, GPU model and provider country (Lium's own grouping)
    lium_feed_values       public JSON feeds in long form, CHANGE-ONLY per (subject, gpu_count, metric): a row is
                           written when a value changes, appears, or leaves the feed (present false). Launch success
                           and time to SSH, open renter requests, rented/total per GPU type, spot idle pay, idle-pay
                           capacity buckets, reference prices
    lium_throughput_daily  Lium's published daily GMV, GPU-hours, rentals and fleet utilisation (public stats feed,
                           parsed from the raw_snapshots rows providers/lium.py already stores)
    lium_model_stats_daily per UTC day of reading and GPU model: Lium's published 30-day median PAID price,
                           utilisation, cheapest ask and GPUs listed / rented
    lium_grafana_rentals   one row per rental at its start, from Lium's public Grafana (lium_data/grafana.py;
                           migration 0041): start time, GPU model, price per GPU-hour paid, tier (spot / secure)
    lium_grafana_hourly    hourly series from the same Grafana, one row per UTC hour and GPU model: renter spend,
                           GPUs listed, GPUs rented; a revised hour is updated in place
    lium_grafana_network_hourly  whole-network counts per UTC hour from the "Rental check" dashboard (migration
                           0043): rentals created, deploys that failed on the node's side
    lium_grafana_price_daily     per UTC day and GPU model: Lium's median asking price of unrented nodes and median
                           price paid on rented nodes
    lium_grafana_daily_counts    per UTC day, series and label: failed deploys by cause, penalties by reason code
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric,
                        String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

_VIS = "visibility IN ('public', 'internal')"


def _vis():
    return mapped_column(String(8), default="internal", server_default="internal")


class LiumFeedSnapshot(Base):
    __tablename__ = "lium_feed_snapshots"
    __table_args__ = (Index("ix_lium_feed_snapshots_feed_time", "feed", "fetched_at"),
                      CheckConstraint(_VIS, name="ck_lium_feed_snapshots_visibility"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feed: Mapped[str] = mapped_column(String(48))                 # lium_data.feeds name, e.g. "launch_stats"
    url: Mapped[str] = mapped_column(String(400))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)     # query / path parameters, {} for none
    params_key: Mapped[str] = mapped_column(String(200), default="")  # canonical params, the change-only key
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    # ok | empty | forbidden | unauthorized | not_found | unavailable | rate_limited | invalid | error
    state: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[str | None] = mapped_column(Text)
    rows: Mapped[int | None] = mapped_column(Integer)             # data rows parsed from the body
    rejected: Mapped[int | None] = mapped_column(Integer)         # rows the parser could not read (kept raw)
    visibility: Mapped[str] = _vis()


class LiumTrade(Base):
    __tablename__ = "lium_trades"
    __table_args__ = (Index("ix_lium_trades_end", "end_at"), Index("ix_lium_trades_gpu_end", "gpu_model", "end_at"),
                      CheckConstraint(_VIS, name="ck_lium_trades_visibility"))

    row_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_id: Mapped[str | None] = mapped_column(String(128))
    renter_hash: Mapped[str | None] = mapped_column(String(128))    # Lium's salted hash, never a real id
    provider_hash: Mapped[str | None] = mapped_column(String(128))
    gpu_model: Mapped[str] = mapped_column(String(128))             # as Lium writes it
    gpu_count: Mapped[int] = mapped_column(Integer)
    price_per_gpu_hour: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gpu_hours: Mapped[Decimal] = mapped_column(Numeric(16, 6))      # gpu_count x (end - start), derived
    provider_country: Mapped[str | None] = mapped_column(String(64))
    extra: Mapped[dict | None] = mapped_column(JSONB)               # columns this parser does not know, verbatim
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("lium_feed_snapshots.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()


class LiumHourlyGpu(Base):
    __tablename__ = "lium_hourly_gpu"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_hourly_gpu_visibility"),)

    hour_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    gpu_model: Mapped[str] = mapped_column(String(128), primary_key=True)
    rentals: Mapped[int | None] = mapped_column(Integer)
    gpu_hours: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    mean_price_per_gpu_hour_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("lium_feed_snapshots.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()


class LiumDailyAggregate(Base):
    __tablename__ = "lium_daily_aggregate"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_daily_aggregate_visibility"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)                  # UTC end day
    gpu_model: Mapped[str] = mapped_column(String(128), primary_key=True)
    country: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    deals: Mapped[int | None] = mapped_column(Integer)
    gpu_hours: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    low: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    high: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    extra: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("lium_feed_snapshots.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()


class LiumFeedValue(Base):
    __tablename__ = "lium_feed_values"
    __table_args__ = (UniqueConstraint("snapshot_id", "subject", "gpu_count", "metric", name="uq_lium_feed_values"),
                      Index("ix_lium_feed_values_feed_time", "feed", "observed_at"),
                      Index("ix_lium_feed_values_key", "feed", "subject", "gpu_count", "metric", "observed_at"),
                      CheckConstraint(_VIS, name="ck_lium_feed_values_visibility"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("lium_feed_snapshots.id", ondelete="CASCADE"))
    feed: Mapped[str] = mapped_column(String(48))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))     # when OpenGrid first read this answer
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the feed's own time, if any
    subject: Mapped[str] = mapped_column(String(160))      # GPU type / class / model as Lium writes it; "*" = feed-wide
    gpu_count: Mapped[int] = mapped_column(Integer, default=0)                 # GPUs per pod / bucket; 0 = n/a
    metric: Mapped[str] = mapped_column(String(48))
    value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    present: Mapped[bool] = mapped_column(Boolean, default=True)   # False: the key left the feed at observed_at
    visibility: Mapped[str] = _vis()


class LiumThroughputDaily(Base):
    __tablename__ = "lium_throughput_daily"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_throughput_daily_visibility"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    gmv_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    gpu_hours: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))
    rentals: Mapped[int | None] = mapped_column(Integer)
    utilisation: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    utilisation_partial: Mapped[bool | None] = mapped_column(Boolean)          # the day was still running when read
    raw_snapshot_id: Mapped[int | None] = mapped_column(BigInteger)            # raw_snapshots.id (lium, STATS)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revisions: Mapped[int] = mapped_column(Integer, default=0)
    visibility: Mapped[str] = _vis()


class LiumModelStatsDaily(Base):
    __tablename__ = "lium_model_stats_daily"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_model_stats_daily_visibility"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)                  # UTC day OpenGrid read it
    gpu_model: Mapped[str] = mapped_column(String(128), primary_key=True)
    median_price_30d: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    utilisation_30d: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    from_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    gpus_listed: Mapped[int | None] = mapped_column(Integer)
    gpus_rented: Mapped[int | None] = mapped_column(Integer)
    raw_snapshot_id: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))     # newest read of the day
    visibility: Mapped[str] = _vis()


class LiumGrafanaRental(Base):
    __tablename__ = "lium_grafana_rentals"
    __table_args__ = (Index("ix_lium_grafana_rentals_start", "start_at"),
                      CheckConstraint(_VIS, name="ck_lium_grafana_rentals_visibility"))

    # 128 bits of sha256(start to the nanosecond | GPU model | price | ordinal among identical points of one answer)
    row_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gpu_model: Mapped[str] = mapped_column(String(128))             # as Lium writes it
    price_per_gpu_hour: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    tier: Mapped[str | None] = mapped_column(String(16))            # spot | secure; NULL: neither tier read named it
    # the answer that first held it (its fetched_at is when OpenGrid first saw the rental)
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("lium_feed_snapshots.id", ondelete="SET NULL"))
    visibility: Mapped[str] = _vis()


class LiumGrafanaHourly(Base):
    __tablename__ = "lium_grafana_hourly"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_grafana_hourly_visibility"),)

    hour_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    gpu_model: Mapped[str] = mapped_column(String(128), primary_key=True)
    # NULL: the panel had no value for this hour and model. A zero spend is stored only on a row the fleet panels
    # made (a model with no fleet and no spend has no row at all).
    spend_usd_per_hour: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    gpus_listed: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))     # an hourly mean, so not whole
    gpus_rented: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()


class LiumGrafanaNetworkHourly(Base):
    __tablename__ = "lium_grafana_network_hourly"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_grafana_network_hourly_visibility"),)

    hour_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    # NULL: the panel had no row for the hour. Its queries count rows, so an hour with nothing has no row; Lium's
    # failure log begins 2026-08-12, and before it a NULL means "not recorded", not zero.
    rentals_created: Mapped[int | None] = mapped_column(Integer)
    node_deploy_failures: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()


class LiumGrafanaPriceDaily(Base):
    __tablename__ = "lium_grafana_price_daily"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_grafana_price_daily_visibility"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)                  # UTC day of Lium's samples
    gpu_model: Mapped[str] = mapped_column(String(128), primary_key=True)      # as Lium writes it
    # NULL: Lium gave no median for the day (fewer than three nodes, or before its samples begin)
    median_asking: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))      # unrented nodes, per GPU-hour
    median_paid: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))        # rented nodes, per GPU-hour
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()


class LiumGrafanaDailyCount(Base):
    __tablename__ = "lium_grafana_daily_counts"
    __table_args__ = (CheckConstraint(_VIS, name="ck_lium_grafana_daily_counts_visibility"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    series: Mapped[str] = mapped_column(String(32), primary_key=True)          # deploy_failures | penalties
    label: Mapped[str] = mapped_column(String(128), primary_key=True)          # Lium's cause / reason code
    count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()
