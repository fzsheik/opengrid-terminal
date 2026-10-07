"""market structure (store/structure.py)

Revision ID: 0003_structure
Revises: 0002_indices
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0003_structure"
down_revision: Union[str, Sequence[str], None] = "0002_indices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "structure_provider_daily",
        sa.Column("segment", sa.String(16), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("hours_tracked", sa.Integer(), nullable=False),
        sa.Column("hours_live", sa.Integer(), nullable=False),
        sa.Column("hours_priced", sa.Integer(), nullable=False),
        sa.Column("hours_available", sa.Integer(), nullable=False),
        sa.Column("hours_market", sa.Integer(), nullable=False),
        sa.Column("hours_compared", sa.Integer(), nullable=False),
        sa.Column("premium_sum", sa.Float(), nullable=False),
        sa.Column("hours_cheapest", sa.Integer(), nullable=False),
        sa.Column("hours_top3", sa.Integer(), nullable=False),
        sa.Column("rank_counts", postgresql.JSONB(), nullable=True),
        sa.Column("price_low", sa.Numeric(14, 6), nullable=True),
        sa.Column("price_high", sa.Numeric(14, 6), nullable=True),
        sa.Column("price_avg", sa.Numeric(14, 6), nullable=True),
        sa.Column("price_close", sa.Numeric(14, 6), nullable=True),
        sa.PrimaryKeyConstraint("segment", "gpu", "provider", "day"),
    )
    op.create_index("ix_spd_provider_day", "structure_provider_daily", ["provider", "day"])
    op.create_index("ix_spd_day", "structure_provider_daily", ["day"])

    op.create_table(
        "structure_gpu_daily",
        sa.Column("segment", sa.String(16), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("hours_market", sa.Integer(), nullable=False),
        sa.Column("hours_cv", sa.Integer(), nullable=False),
        sa.Column("cv_sum", sa.Float(), nullable=False),
        sa.Column("iqr_rel_sum", sa.Float(), nullable=False),
        sa.Column("hours_spread", sa.Integer(), nullable=False),
        sa.Column("spread_sum", sa.Float(), nullable=False),
        sa.Column("providers_max", sa.Integer(), nullable=False),
        sa.Column("provider_hours_tracked", sa.Integer(), nullable=False),
        sa.Column("provider_hours_priced", sa.Integer(), nullable=False),
        sa.Column("close_low", sa.Numeric(14, 6), nullable=True),
        sa.Column("close_median", sa.Numeric(14, 6), nullable=True),
        sa.Column("close_high", sa.Numeric(14, 6), nullable=True),
        sa.Column("close_providers", sa.Integer(), nullable=False),
        sa.Column("median_vol", sa.Float(), nullable=True),
        sa.Column("vol_returns", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("segment", "gpu", "day"),
    )
    op.create_index("ix_sgd_day", "structure_gpu_daily", ["day"])


def downgrade() -> None:
    op.drop_index("ix_sgd_day", table_name="structure_gpu_daily")
    op.drop_table("structure_gpu_daily")
    op.drop_index("ix_spd_day", table_name="structure_provider_daily")
    op.drop_index("ix_spd_provider_day", table_name="structure_provider_daily")
    op.drop_table("structure_provider_daily")
