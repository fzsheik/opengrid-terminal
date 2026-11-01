"""market context: raw snapshots of context sources and parsed series (context/; methodology/market-context.md).
Additive only: two new tables. Nothing statistical reads them.

    context_snapshots   every EIA / SEC EDGAR fetch, cut to what is read; failures included
    context_series      (series_id, period_start, period_end) -> value, unit, published_at, first_seen_at, basis

Revision ID: 0032_context_series
Revises: 0026_fx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0032_context_series"
down_revision: Union[str, Sequence[str], None] = "0026_fx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_context_snapshots_source_key_time", "context_snapshots", ["source", "key", "fetched_at"])
    op.create_table(
        "context_series",
        sa.Column("series_id", sa.String(120), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(24, 6), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("published_at", TS, nullable=True),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("revisions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_id", sa.BigInteger(), sa.ForeignKey("context_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("series_id", "period_start", "period_end"),
    )
    op.create_index("ix_context_series_series_end", "context_series", ["series_id", "period_end"])


def downgrade() -> None:
    op.drop_index("ix_context_series_series_end", table_name="context_series")
    op.drop_table("context_series")
    op.drop_index("ix_context_snapshots_source_key_time", table_name="context_snapshots")
    op.drop_table("context_snapshots")
