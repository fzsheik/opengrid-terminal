"""Archived prices: what a provider's price page or price list said on a past date, recovered from a public
archive (migration 0025_archived_prices; methodology/archived-history.md).

    archived_prices   one priced listing as it appeared in one archived source document: the Internet Archive's
                      capture of a provider's price page / public API, or one monthly version of AWS's EC2 price
                      list. Data kind 'archived': read by OpenGrid's own parsers, but NOT observed live by OpenGrid,
                      and sampled only on the days a capture exists.

    catalog_snapshots third-party catalogue files kept verbatim (0031_backfill_sources), e.g. dstack's gpuhunt
                      catalogues, whose public versions expire after about 90 days.

Never mixed into listing_observations / market_hourly: percentiles, indices, events and routing read only
observed data. Every row names the exact document it came from (source_url, the human-checkable capture) and the
sha256 of the bytes that were parsed.
"""

from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, Index, Integer, LargeBinary, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

# Where an archived point came from (archived_prices.source). The first two are read from the publisher's own
# documents; the datasets are third parties' compilations, recorded with their licence on every point.
SOURCES = ("wayback", "aws_price_list", "azure_retail", "fsdl_cloud_gpus", "huang_vast_monthly", "skypilot_catalog")
# Primary sources win over compilations: a dataset point that repeats one of these for the same provider / listing /
# date is stored as corroboration on the primary row, never as a second point (backfill/datasets/dedupe.py).
PRIMARY_SOURCES = ("aws_price_list", "azure_retail", "wayback")

# What one point means (archived_prices.basis). NULL on rows from before 0031: capture (wayback) or
# price_list_version (aws_price_list).
BASES = {
    "capture": "what an archived copy of the provider's page / API showed on the capture day",
    "price_list_version": "the provider's published price list for a stated effective period [observed_at, valid_to)",
    "effective_since": "a CURRENT price whose publisher states it has applied since observed_at; one point, not a "
                       "series: the price before observed_at is unknown and nothing between then and now is implied",
    "dataset_snapshot": "a row of a third-party dataset as of one revision (e.g. a git commit date)",
    "monthly_median": "a third party's monthly median of transacted rates (not a posted price)",
    "catalog_commit": "a row of a third-party catalogue as of one git commit",
}

# Reserved terms live in their own market_type and never in on_demand / spot (reserved.py founders' rule).
RESERVED_MARKETS = ("reserved_1y", "reserved_3y")


class ArchivedPrice(Base):
    __tablename__ = "archived_prices"
    __table_args__ = (
        UniqueConstraint("source", "source_url", "provider", "listing_id", name="uq_archived_doc_listing"),
        Index("ix_archived_gpu_time", "market_type", "gpu", "observed_at"),
        Index("ix_archived_provider_time", "provider", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(24))                 # SOURCES
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
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # capture time / period start / "since"
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # aws_price_list period end
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    # data tier (0027_visibility): archived rows of a wholly internal provider are internal; public reads filter it
    visibility: Mapped[str] = mapped_column(String(8), default="public", server_default="public")
    # 0031_backfill_sources
    basis: Mapped[str | None] = mapped_column(String(24))           # BASES
    licence: Mapped[str | None] = mapped_column(String(64))
    attribution: Mapped[str | None] = mapped_column(Text)
    corroboration: Mapped[list | None] = mapped_column(JSONB)       # [{source, source_url, observed_at, price...}]


class CatalogSnapshot(Base):
    """One third-party catalogue file exactly as downloaded (raw-first; e.g. gpuhunt's catalog.zip per provider).
    Kept because the publisher expires old versions; nothing reads it into prices automatically."""

    __tablename__ = "catalog_snapshots"
    __table_args__ = (
        UniqueConstraint("source", "name", "version", name="uq_catalog_snapshot_version"),
        Index("ix_catalog_snapshots_source_time", "source", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))                 # gpuhunt
    name: Mapped[str] = mapped_column(String(64))                   # the publisher's provider key, e.g. aws
    version: Mapped[str] = mapped_column(String(64))                # the publisher's version id
    url: Mapped[str] = mapped_column(String(400))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # Last-Modified
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(64))
    licence: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    body: Mapped[bytes] = mapped_column(LargeBinary)
