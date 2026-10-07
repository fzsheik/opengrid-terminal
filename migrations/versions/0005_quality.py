"""data quality and quarantine (store/quality.py)

Revision ID: 0005_quality
Revises: 0004_events
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0005_quality"
down_revision: Union[str, Sequence[str], None] = "0004_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "quality_quarantine",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("listing_id", sa.String(256), nullable=False),
        sa.Column("field", sa.String(48), nullable=False),
        sa.Column("rule", sa.String(48), nullable=False),
        sa.Column("previous_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("auto_acceptable", sa.Boolean(), nullable=False),
        sa.Column("first_seen", TZ, nullable=False),
        sa.Column("last_seen", TZ, nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("resolved_at", TZ, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
    )
    op.create_index("uq_quarantine_pending", "quality_quarantine", ["provider", "listing_id", "field"],
                    unique=True, postgresql_where=sa.text("status = 'pending'"))
    op.create_index("ix_quarantine_lookup", "quality_quarantine", ["provider", "listing_id", "field", "resolved_at"])
    op.create_index("ix_quarantine_status", "quality_quarantine", ["status", "first_seen"])

    op.create_table(
        "quality_incidents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("listing_id", sa.String(256), nullable=True),
        sa.Column("endpoint", sa.String(256), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("first_seen", TZ, nullable=False),
        sa.Column("last_seen", TZ, nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("resolved_at", TZ, nullable=True),
        sa.Column("dedupe_key", sa.String(512), nullable=False, unique=True),
    )
    op.create_index("ix_incident_open", "quality_incidents", ["status", "last_seen"])
    op.create_index("ix_incident_provider_kind", "quality_incidents", ["provider", "kind", "last_seen"])

    op.create_table(
        "quality_polls",
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("polled_at", TZ, primary_key=True),
        sa.Column("listings", sa.Integer(), nullable=False),
        sa.Column("held", sa.Integer(), nullable=False),
        sa.Column("withheld", sa.Integer(), nullable=False),
        sa.Column("recorded_at", TZ, nullable=False),
    )

    op.create_table(
        "source_schemas",
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("endpoint", sa.String(256), primary_key=True),
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("paths", postgresql.JSONB(), nullable=False),
        sa.Column("path_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("first_seen", TZ, nullable=False),
        sa.Column("last_seen", TZ, nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.Column("first_raw_id", sa.BigInteger(), nullable=False),
        sa.Column("last_raw_id", sa.BigInteger(), nullable=False),
    )

    op.create_table(
        "quality_cursor",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", TZ, nullable=True),
    )

    # Hot-path indexes on the core tables (see methodology/data-quality.md, "Scale notes").
    # Provider health: last ok / failed fetch, 24h failure rate and latency; _PRICE_THEN's
    # first-ok-fetch per provider becomes an index-only scan.
    op.create_index("ix_raw_provider_ok_time", "raw_snapshots", ["provider", "ok", "fetched_at"])
    # Stale-listing scans and per-provider live counts.
    op.create_index("ix_listing_provider_seen", "compute_listings", ["provider", "observed_at"])


def downgrade() -> None:
    op.drop_index("ix_listing_provider_seen", table_name="compute_listings")
    op.drop_index("ix_raw_provider_ok_time", table_name="raw_snapshots")
    op.drop_table("quality_cursor")
    op.drop_table("source_schemas")
    op.drop_table("quality_polls")
    op.drop_index("ix_incident_provider_kind", table_name="quality_incidents")
    op.drop_index("ix_incident_open", table_name="quality_incidents")
    op.drop_table("quality_incidents")
    op.drop_index("ix_quarantine_status", table_name="quality_quarantine")
    op.drop_index("ix_quarantine_lookup", table_name="quality_quarantine")
    op.drop_index("uq_quarantine_pending", table_name="quality_quarantine")
    op.drop_table("quality_quarantine")
