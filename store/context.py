"""Market-context tables (context/; methodology/market-context.md). Raw first, like provider prices.

    context_snapshots  every fetch of a context source (EIA, SEC EDGAR), failures included; the body cut to what
                       context/ reads (EDGAR company facts are 4 MB; a filing's XBRL instance 1.4 MB)
    context_series     one row per (series, period): the value, its unit, when the source PUBLISHED it (EDGAR: the
                       filing date; EIA: not given, so null) and when OpenGrid first saw it. Rebuildable from
                       context_snapshots by re-parsing. A later different value for the same period is a revision:
                       the row takes the new value and counts it.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (Index("ix_context_snapshots_source_key_time", "source", "key", "fetched_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))        # "eia" | "sec_edgar"
    key: Mapped[str] = mapped_column(String(200))          # what was fetched, e.g. "companyfacts:1045810"
    url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | list | None] = mapped_column(JSONB(none_as_null=True))


class ContextSeries(Base):
    __tablename__ = "context_series"
    __table_args__ = (Index("ix_context_series_series_end", "series_id", "period_end"),)

    series_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    unit: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    basis: Mapped[str] = mapped_column(String(16))          # "reported" | "derived" (EDGAR quarter from year-to-date)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revisions: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("context_snapshots.id", ondelete="SET NULL"))
    meta: Mapped[dict | None] = mapped_column(JSONB)        # accession, form, concept, how derived
