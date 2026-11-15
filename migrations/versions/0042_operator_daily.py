"""operator daily: marketplace operators and cross-marketplace hardware matches, one row per day, internal
(operators.py; store/operators.py; methodology/internal/operators.md). Additive only: two new tables.

Revision ID: 0042_operator_daily
Revises: 0041_lium_grafana
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0042_operator_daily"
down_revision: Union[str, Sequence[str], None] = "0041_lium_grafana"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
VIS = "visibility IN ('public', 'internal')"


def _vis(table: str):
    return (sa.Column("visibility", sa.String(8), nullable=False, server_default="internal"),
            sa.CheckConstraint(VIS, name=f"ck_{table}_visibility"))


def upgrade() -> None:
    op.create_table(
        "operator_daily",
        sa.Column("marketplace", sa.String(32), primary_key=True),
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("as_of", TS, nullable=False),
        sa.Column("machines", sa.Integer, nullable=False),
        sa.Column("identified_machines", sa.Integer, nullable=False),
        sa.Column("operators", sa.Integer, nullable=False),
        sa.Column("gpus", sa.Integer, nullable=False),
        sa.Column("top1_gpu_share", sa.Numeric(7, 4)),
        sa.Column("top5_gpu_share", sa.Numeric(7, 4)),
        sa.Column("hhi", sa.Integer),
        sa.Column("coverage", sa.String(48), nullable=False),
        sa.Column("computed_at", TS, nullable=False),
        *_vis("operator_daily"),
    )
    op.create_table(
        "operator_overlap_daily",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("marketplace_a", sa.String(32), primary_key=True),
        sa.Column("marketplace_b", sa.String(32), primary_key=True),
        sa.Column("comparable_a", sa.Integer, nullable=False),
        sa.Column("comparable_b", sa.Integer, nullable=False),
        sa.Column("strong_matches", sa.Integer, nullable=False),
        sa.Column("build_matches", sa.Integer, nullable=False),
        sa.Column("machines_a_matched", sa.Integer, nullable=False),
        sa.Column("machines_b_matched", sa.Integer, nullable=False),
        sa.Column("operator_pairs", sa.Integer, nullable=False),
        sa.Column("median_strong_gap_pct", sa.Numeric(9, 2)),
        sa.Column("note", sa.Text),
        sa.Column("computed_at", TS, nullable=False),
        *_vis("operator_overlap_daily"),
    )


def downgrade() -> None:
    op.drop_table("operator_overlap_daily")
    op.drop_table("operator_daily")
