"""capacity: provider-reported capacity, current state + change-only history (capacity.py; methodology/capacity.md).
Additive only.

    capacity_current  one row per (provider, sku, region_key): in stock, tier, counts, the provider's own word
    capacity_changes  one row per state change; rebuildable from raw_snapshots

Revision ID: 0025_capacity
Revises: 0024_probes
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0025_capacity"
down_revision: Union[str, Sequence[str], None] = "0024_probes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "capacity_current",
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("sku", sa.String(200), nullable=False),
        sa.Column("region_key", sa.String(96), nullable=False),
        sa.Column("region", sa.String(96), nullable=True),
        sa.Column("country", sa.String(8), nullable=True),
        sa.Column("raw_gpu_name", sa.String(200), nullable=False),
        sa.Column("canonical_gpu_name", sa.String(200), nullable=True),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("in_stock", sa.Boolean(), nullable=True),
        sa.Column("tier", sa.String(8), nullable=True),
        sa.Column("available_count", sa.Integer(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("count_unit", sa.String(16), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("changed_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("provider", "sku", "region_key"),
    )
    op.create_index("ix_capacity_current_canonical_gpu_name", "capacity_current", ["canonical_gpu_name"])
    op.create_table(
        "capacity_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("sku", sa.String(200), nullable=False),
        sa.Column("region_key", sa.String(96), nullable=False),
        sa.Column("canonical_gpu_name", sa.String(200), nullable=True),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("in_stock", sa.Boolean(), nullable=True),
        sa.Column("tier", sa.String(8), nullable=True),
        sa.Column("available_count", sa.Integer(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=False),
    )
    op.create_index("ix_capacity_changes_key_time", "capacity_changes", ["provider", "sku", "region_key", "observed_at"])
    op.create_index("ix_capacity_changes_gpu_time", "capacity_changes", ["canonical_gpu_name", "observed_at"])


def downgrade() -> None:
    op.drop_table("capacity_changes")
    op.drop_table("capacity_current")
