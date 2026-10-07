"""Data-quality tables: what the quality layer held back, noticed, and fingerprinted.

    quality_quarantine   a suspicious new value for one field of one listing, held
                         back from compute_listings / listing_observations until it is
                         accepted (by an operator, or automatically once it persists)
                         or rejected. One pending row per (provider, listing_id, field).
    quality_incidents    things that are not a held value: a normalizer that raised, an
                         empty response, a source schema change, a static mapping flag.
                         One row per dedupe_key, re-opened and counted when it recurs.
    quality_polls        one row per provider poll the screen saw: how many listings the
                         normalizer produced, so "listing count jumped 3x" has a norm.
    source_schemas       one row per distinct JSON shape seen per (provider, endpoint).
    quality_cursor       how far the schema watcher has read raw_snapshots (by id).

Raw snapshots are never modified by anything here.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base, ComputeListingRow, RawSnapshot


class Quarantine(Base):
    __tablename__ = "quality_quarantine"
    __table_args__ = (
        # At most one open hold per field of a listing.
        Index("uq_quarantine_pending", "provider", "listing_id", "field", unique=True,
              postgresql_where=text("status = 'pending'")),
        Index("ix_quarantine_lookup", "provider", "listing_id", "field", "resolved_at"),
        Index("ix_quarantine_status", "status", "first_seen"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    listing_id: Mapped[str] = mapped_column(String(256))  # '*' for provider-level holds
    field: Mapped[str] = mapped_column(String(48))
    rule: Mapped[str] = mapped_column(String(48))
    previous_value: Mapped[object] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[object] = mapped_column(JSONB, nullable=True)
    # The latest listing as the provider reported it (so accept can apply it), plus rule context.
    detail: Mapped[dict | None] = mapped_column(JSONB)
    auto_acceptable: Mapped[bool] = mapped_column(default=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|accepted|rejected|auto_accepted
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class Incident(Base):
    __tablename__ = "quality_incidents"
    __table_args__ = (
        Index("ix_incident_open", "status", "last_seen"),
        Index("ix_incident_provider_kind", "provider", "kind", "last_seen"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(48))
    provider: Mapped[str | None] = mapped_column(String(64))
    listing_id: Mapped[str | None] = mapped_column(String(256))
    endpoint: Mapped[str | None] = mapped_column(String(256))
    severity: Mapped[str] = mapped_column(String(16), default="notable")  # info|notable|major
    detail: Mapped[dict | None] = mapped_column(JSONB)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str] = mapped_column(String(512), unique=True)


class QualityPoll(Base):
    __tablename__ = "quality_polls"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    polled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    listings: Mapped[int] = mapped_column(Integer)   # what the normalizer produced
    held: Mapped[int] = mapped_column(Integer, default=0)      # listings with a field held back
    withheld: Mapped[int] = mapped_column(Integer, default=0)  # listings not saved at all this poll
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceSchema(Base):
    __tablename__ = "source_schemas"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(256), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    paths: Mapped[list] = mapped_column(JSONB)  # sorted "path:type", capped
    path_count: Mapped[int] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(default=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    first_raw_id: Mapped[int] = mapped_column(BigInteger)
    last_raw_id: Mapped[int] = mapped_column(BigInteger)


class QualityCursor(Base):
    __tablename__ = "quality_cursor"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Indexes on the core tables that the quality / ops / trust reads need. Declared here so
# create_all (tests) and migration 0005 agree; tables.py is left alone.
Index("ix_raw_provider_ok_time", RawSnapshot.provider, RawSnapshot.ok, RawSnapshot.fetched_at)
Index("ix_listing_provider_seen", ComputeListingRow.provider, ComputeListingRow.observed_at)
