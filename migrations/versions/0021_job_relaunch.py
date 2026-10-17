"""job relaunch: the attempt chain of an automatically relaunched job (jobs/relaunch.py; methodology/jobs.md
"Automatic relaunch"). Additive only.

    jobs.retry_of / root_job_id / attempt   the chain links (NULL on every existing / non-relaunched job = attempt 1)
    ux_jobs_retry_of                        UNIQUE(retry_of): at most ONE successor per failed attempt, whatever
                                            number of workers / passes race
    job_relaunches                          one decision row per failed attempt (PRIMARY KEY failed_job_id): the
                                            claim a worker takes before it may submit, then relaunched | stopped
                                            (with the reason) | blocked (internal error, retried, ops-alerted)

Revision ID: 0021_job_relaunch
Revises: 0020_intelligence
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0021_job_relaunch"
down_revision: Union[str, Sequence[str], None] = "0020_intelligence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("jobs", sa.Column("retry_of", sa.String(40), nullable=True))
    op.add_column("jobs", sa.Column("root_job_id", sa.String(40), nullable=True))
    op.add_column("jobs", sa.Column("attempt", sa.Integer(), nullable=True))
    op.create_index("ux_jobs_retry_of", "jobs", ["retry_of"], unique=True,
                    postgresql_where=sa.text("retry_of IS NOT NULL"))
    op.create_index("ix_jobs_root", "jobs", ["root_job_id"], postgresql_where=sa.text("root_job_id IS NOT NULL"))
    op.create_table(
        "job_relaunches",
        sa.Column("failed_job_id", sa.String(40), primary_key=True),
        sa.Column("root_job_id", sa.String(40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(48), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("successor_job_id", sa.String(40), nullable=True),
        sa.Column("excluded_providers", J, nullable=True),
        sa.Column("resume_from", J, nullable=True),
        sa.Column("spend_so_far_usd", sa.Numeric(14, 4), nullable=True),
        sa.Column("max_total_usd", sa.Numeric(14, 4), nullable=True),
        sa.Column("est_next_usd", sa.Numeric(14, 4), nullable=True),
        sa.Column("detail", J, nullable=True),
        sa.Column("tries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=True),
    )
    op.create_index("ix_job_relaunches_root", "job_relaunches", ["root_job_id"])
    op.create_index("ix_job_relaunches_successor", "job_relaunches", ["successor_job_id"])


def downgrade() -> None:
    op.drop_index("ix_job_relaunches_successor", table_name="job_relaunches")
    op.drop_index("ix_job_relaunches_root", table_name="job_relaunches")
    op.drop_table("job_relaunches")
    op.drop_index("ix_jobs_root", table_name="jobs")
    op.drop_index("ux_jobs_retry_of", table_name="jobs")
    op.drop_column("jobs", "attempt")
    op.drop_column("jobs", "root_job_id")
    op.drop_column("jobs", "retry_of")
