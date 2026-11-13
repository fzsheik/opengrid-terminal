"""measured performance: completed REAL jobs become measured observations in performance_observations
(intelligence/measured.py; methodology/recommendations.md "Measured performance"). Additive only: new nullable
columns, one index, one partial unique index (one observation per job). No existing row or column changes.

Revision ID: 0037_measured_performance
Revises: 0035_power_markets
NOTE: 0036 is reserved by a parallel branch. At merge, re-chain this revision's down_revision onto that 0036
revision id so the history stays linear (single head).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0037_measured_performance"
down_revision: Union[str, Sequence[str], None] = "0035_power_markets"  # re-chain onto 0036 at merge
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
T = "performance_observations"
COLUMNS = (
    ("provider", sa.String(64)),
    ("workload_signature", sa.String(200)),
    ("micro_batch_size", sa.Integer()),
    ("throughput", sa.Float()),
    ("throughput_unit", sa.String(24)),
    ("work_measured", sa.Float()),
    ("train_seconds", sa.Float()),
    ("overhead_s", sa.Float()),
    ("epochs_measured", sa.Integer()),
    ("gpu_detected", sa.String(128)),
    ("data_kind", sa.String(16)),
    ("observed_at", TS),
)


def upgrade() -> None:
    for name, typ in COLUMNS:
        op.add_column(T, sa.Column(name, typ, nullable=True))
    op.create_index("ix_perfobs_signature", T, ["workload_signature", "gpu"])
    op.create_index("ux_perfobs_completed_job", T, ["job_id"], unique=True,
                    postgresql_where=sa.text("source_kind = 'completed_job' AND job_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ux_perfobs_completed_job", table_name=T)
    op.drop_index("ix_perfobs_signature", table_name=T)
    for name, _ in reversed(COLUMNS):
        op.drop_column(T, name)
