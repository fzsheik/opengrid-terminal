"""Archived prices: what a provider's price page or price list said on a past date, recovered from a public
archive (migration 0025_archived_prices; methodology/archived-history.md).

    archived_prices   one priced listing as it appeared in one archived source document: the Internet Archive's
                      capture of a provider's price page / public API, or one monthly version of AWS's EC2 price
                      list. Data kind 'archived': read by OpenGrid's own parsers, but NOT observed live by OpenGrid,
                      and sampled only on the days a capture exists.

Never mixed into listing_observations / market_hourly: percentiles, indices, events and routing read only
observed data. Every row names the exact document it came from (source_url, the human-checkable capture) and the
sha256 of the bytes that were parsed.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

SOURCES = ("wayback", "aws_price_list")


class ArchivedPrice(Base):
    __tablename__ = "archived_prices"
    __table_args__ = (
        UniqueConstraint("source", "source_url", "provider", "listing_id", name="uq_archived_doc_listing"),
        Index("ix_archived_gpu_time", "market_type", "gpu", "observed_at"),
        Index("ix_archived_provider_time", "provider", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(24))                 # wayback | aws_price_list
    source_url: Mapped[str] = mapped_column(String(600))            # the capture / price-list version, checkable
    source_sha256: Mapped[str] = mapped_column(String(64))          # of the bytes parsed
    parser: Mapped[str] = mapped_column(String(160))                # which code read it
    provider: Mapped[str] = mapped_column(String(64))
    listing_id: Mapped[str] = mapped_column(String(256))
    sku: Mapped[str | None] = mapped_column(String(256))
    raw_gpu_name: Mapped[str] = mapped_column(String(256))
    gpu: Mapped[str | None] = mapped_column(String(160))            # canonical name; null = unmapped (kept, not shown)
    gpu_count: Mapped[int] = mapped_column(Integer)
    region: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(8))
    price_per_gpu_hour: Mapped[float] = mapped_column(Numeric(14, 6))
    price_per_instance_hour: Mapped[float | None] = mapped_column(Numeric(14, 6))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    market_type: Mapped[str | None] = mapped_column(String(16))
    interruptible: Mapped[bool | None] = mapped_column(Boolean)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # capture time / version period start
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # aws_price_list period end
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    # data tier (0027_visibility): archived rows of a wholly internal provider are internal; public reads filter it
    visibility: Mapped[str] = mapped_column(String(8), default="public", server_default="public")
