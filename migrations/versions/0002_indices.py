"""analytics rollups and index levels (store/analytics.py)

Revision ID: 0002_indices
Revises: 337d80df1c26
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0002_indices"
down_revision: Union[str, Sequence[str], None] = "337d80df1c26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_hourly",
        sa.Column("segment", sa.String(16), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("country", sa.String(8), nullable=True),
        sa.Column("min_price", sa.Numeric(14, 6), nullable=True),
        sa.Column("max_price", sa.Numeric(14, 6), nullable=True),
        sa.Column("avg_price", sa.Numeric(14, 6), nullable=True),
        sa.Column("live_listings", sa.Integer(), nullable=False),
        sa.Column("priced_listings", sa.Integer(), nullable=False),
        sa.Column("available_listings", sa.Integer(), nullable=False),
        sa.Column("unknown_listings", sa.Integer(), nullable=False),
        sa.Column("sold_out_listings", sa.Integer(), nullable=False),
        sa.Column("capacity_gpus", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("segment", "gpu", "provider", "region", "hour"),
    )
    op.create_index("ix_mh_gpu_hour", "market_hourly", ["segment", "gpu", "hour"])
    op.create_index("ix_mh_provider_hour", "market_hourly", ["provider", "hour"])
    op.create_index("ix_mh_hour", "market_hourly", ["hour"])

    op.create_table(
        "index_levels",
        sa.Column("index_id", sa.String(96), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("level", sa.Double(), nullable=True),
        sa.Column("raw_level", sa.Double(), nullable=True),
        sa.Column("constituents", sa.Integer(), nullable=False),
        sa.Column("link_constituents", sa.Integer(), nullable=True),
        sa.Column("method", sa.String(32), nullable=True),
        sa.Column("segment_no", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("methodology_version", sa.String(16), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("index_id", "hour"),
    )
    op.create_index("ix_il_hour", "index_levels", ["hour"])
    op.create_index("ix_il_extremes", "index_levels", ["index_id", "segment_no", "level"],
                    postgresql_where=sa.text("published"))


def downgrade() -> None:
    op.drop_index("ix_il_extremes", table_name="index_levels")
    op.drop_index("ix_il_hour", table_name="index_levels")
    op.drop_table("index_levels")
    op.drop_table("market_hourly")
