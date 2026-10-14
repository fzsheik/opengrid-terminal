"""intelligence: performance_observations (store/intelligence.py), the data model for future learned
recommendations. No data, no learning; the recommender only reads it. Additive only.

Revision ID: 0020_intelligence
Revises: 0019_reservations
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020_intelligence"
down_revision: Union[str, Sequence[str], None] = "0019_reservations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "performance_observations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("gpu", sa.String(128), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("workload_class", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=True),
        sa.Column("model_parameters_b", sa.Float(), nullable=True),
        sa.Column("method", sa.String(16), nullable=True),
        sa.Column("precision", sa.String(16), nullable=True),
        sa.Column("runtime_s", sa.Float(), nullable=True),
        sa.Column("peak_vram_gb", sa.Float(), nullable=True),
        sa.Column("utilization", sa.Float(), nullable=True),
        sa.Column("provisioning_delay_s", sa.Float(), nullable=True),
        sa.Column("total_cost_usd", sa.Float(), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("consent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.String(64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.CheckConstraint("source_kind IN ('theoretical', 'user_supplied', 'measured_benchmark', 'completed_job')",
                           name="ck_perfobs_source_kind"),
    )
    op.create_index("ix_perfobs_gpu_class", "performance_observations", ["gpu", "workload_class"])
    op.create_index("ix_perfobs_created", "performance_observations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_perfobs_created", table_name="performance_observations")
    op.drop_index("ix_perfobs_gpu_class", table_name="performance_observations")
    op.drop_table("performance_observations")
