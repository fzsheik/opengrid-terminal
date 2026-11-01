"""Major market shifts and the items stored around each (analytics/shifts.py; methodology/shifts.md).

    market_shifts        one row per major shift: a coverage-matched GPU median or index move beyond its own
                         volatility, a provider-wide repricing, a capacity collapse, or (data_kind 'archived') a
                         list-price change between two archived captures. `shift_key` makes detection idempotent.
    market_shift_items   the related items ranked around one shift (news, provider announcements, filings,
                         OpenGrid's own market events), with the score components and a snapshot of what was
                         shown, so the explanation is stable; recomputing replaces them under a new items_version.

Related means near in time and about the same GPU / provider / vendor. Nothing here says an item caused a shift.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class MarketShift(Base):
    __tablename__ = "market_shifts"
    __table_args__ = (
        Index("ix_ms_at", "at"),
        Index("ix_ms_gpu_at", "gpu", "at"),
        Index("ix_ms_provider_at", "provider", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    shift_key: Mapped[str] = mapped_column(String(300), unique=True)
    data_kind: Mapped[str] = mapped_column(String(16))          # observed | archived
    type: Mapped[str] = mapped_column(String(40))
    segment: Mapped[str | None] = mapped_column(String(16))
    gpu: Mapped[str | None] = mapped_column(String(160))
    provider: Mapped[str | None] = mapped_column(String(64))
    index_id: Mapped[str | None] = mapped_column(String(96))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))            # when the shift was measured
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # related items are read in this window
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    direction: Mapped[str] = mapped_column(String(8))                        # up | down | mixed
    magnitude: Mapped[float | None] = mapped_column(Float)                   # fraction: -0.18 = -18%
    severity: Mapped[str] = mapped_column(String(10))                        # notable | major
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)                       # the numbers and the rule that fired
    definition_version: Mapped[str] = mapped_column(String(16))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    items_version: Mapped[str | None] = mapped_column(String(16))
    items_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketShiftItem(Base):
    __tablename__ = "market_shift_items"
    __table_args__ = (UniqueConstraint("shift_id", "item_type", "ref_id", name="uq_msi_shift_item"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    shift_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("market_shifts.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(String(24))       # news | provider_announcement | filing | market_event
    ref_id: Mapped[int] = mapped_column(BigInteger)          # news_items.id or market_events.id
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hours_from_shift: Mapped[float] = mapped_column(Float)
    position: Mapped[str] = mapped_column(String(8))         # before | after
    score: Mapped[float] = mapped_column(Float)
    score_components: Mapped[dict] = mapped_column(JSONB)
    snapshot: Mapped[dict] = mapped_column(JSONB)            # title, url, source, kind, backfilled: as shown
    match: Mapped[str | None] = mapped_column(Text)          # why it is related: gpu | provider | vendor | market-wide
