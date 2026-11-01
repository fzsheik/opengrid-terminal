"""cost components: raw snapshots of machine-readable cost sources (cost/; methodology/full-cost.md).
Additive only: one new table. Components and estimates are computed on read; nothing statistical reads it.

    cost_snapshots   every fetch of an AWS offer file, Azure Retail Prices query, the Oracle price list or Verda
                     volume types, cut to the products cost/ reads; failures included

Revision ID: 0030_cost_components
Revises: 0028_changed_at
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0030_cost_components"
down_revision: Union[str, Sequence[str], None] = "0028_changed_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cost_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_cost_snapshots_source_time", "cost_snapshots", ["source", "fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_cost_snapshots_source_time", table_name="cost_snapshots")
    op.drop_table("cost_snapshots")
