"""monthly reports: one row per calendar month (UTC) holding the internal monthly market report and its public-safe
summary (reports/monthly.py; methodology/monthly-report.md). Additive only: one new table, nothing else changes.

Revision ID: 0039_monthly_reports
Revises: 0038_measured_performance
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0039_monthly_reports"
down_revision: Union[str, Sequence[str], None] = "0038_measured_performance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "monthly_reports",
        sa.Column("month", sa.String(7), primary_key=True),
        sa.Column("generated_at", TS, nullable=False),
        sa.Column("regenerated_at", TS, nullable=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("public_summary", sa.Text(), nullable=False),
        sa.Column("posted_at", TS, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("monthly_reports")
