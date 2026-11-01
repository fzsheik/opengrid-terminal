"""FX tables (fx.py; methodology/fx.md). Raw first, like provider prices.

    fx_snapshots  every fetch of the ECB euro reference-rate feed, the body verbatim (XML text), failures included
    fx_rates      one row per (rate_date, base, quote) parsed from those bodies. Only EUR->USD is parsed; every
                  other quote stays in the raw body. Rebuildable from fx_snapshots by re-parsing.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class FxSnapshot(Base):
    __tablename__ = "fx_snapshots"
    __table_args__ = (Index("ix_fx_snapshots_source_time", "source", "fetched_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(16))           # "ecb"
    url: Mapped[str] = mapped_column(String(300))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    body: Mapped[str | None] = mapped_column(Text)


class FxRate(Base):
    """`rate` units of `quote` buy one unit of `base` (ECB convention: 1 EUR = 1.1172 USD)."""

    __tablename__ = "fx_rates"

    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    base: Mapped[str] = mapped_column(String(3), primary_key=True)
    quote: Mapped[str] = mapped_column(String(3), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    source: Mapped[str] = mapped_column(String(16))
    snapshot_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("fx_snapshots.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
