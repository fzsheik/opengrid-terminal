"""job resume / OOM escalation / move (jobs/continuation.py; methodology/jobs.md "Resume", "OOM escalation", "Move").
Additive only.

    jobs.succession_kind        how an attempt continues its predecessor: relaunch | resume | oom_escalation | move
                                (NULL on a first attempt; retry_of / root_job_id / attempt keep linking the chain)
    job_relaunches.kind         relaunch | oom_escalation (the same one-decision-per-failed-attempt claim row)
    job_moves                   a user-requested move; at most one OPEN per job (partial unique index)
    job_runner_instructions     instructions for the runner (checkpoint_now) on the existing runner channel

Revision ID: 0037_job_resume_move
Revises: 0036_batches
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0037_job_resume_move"
down_revision: Union[str, Sequence[str], None] = "0036_batches"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())
OPEN = sa.text("status IN ('requested', 'checkpointing', 'terminating', 'submitting')")


def upgrade() -> None:
    op.add_column("jobs", sa.Column("succession_kind", sa.String(16), nullable=True))
    op.add_column("job_relaunches", sa.Column("kind", sa.String(16), nullable=False, server_default="relaunch"))
    op.create_table(
        "job_moves",
        sa.Column("move_id", sa.String(40), primary_key=True),
        sa.Column("job_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("principal_kind", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("max_total_cost_usd", sa.Numeric(14, 4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("requested_at", TS, nullable=False),
        sa.Column("checkpoint_deadline_at", TS, nullable=False),
        sa.Column("checkpoint", J, nullable=True),
        sa.Column("terminate_requested_at", TS, nullable=True),
        sa.Column("successor_job_id", sa.String(40), nullable=True),
        sa.Column("outcome_code", sa.String(48), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("detail", J, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
    )
    op.create_index("ux_job_moves_open", "job_moves", ["job_id"], unique=True, postgresql_where=OPEN)
    op.create_index("ix_job_moves_job", "job_moves", ["job_id", "created_at"])
    op.create_table(
        "job_runner_instructions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("ref", sa.String(40), nullable=True),
        sa.Column("issued_at", TS, nullable=False),
        sa.Column("delivered_at", TS, nullable=True),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("cancelled_at", TS, nullable=True),
    )
    op.create_index("ix_job_runner_instructions_job", "job_runner_instructions", ["job_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_job_runner_instructions_job", table_name="job_runner_instructions")
    op.drop_table("job_runner_instructions")
    op.drop_index("ix_job_moves_job", table_name="job_moves")
    op.drop_index("ux_job_moves_open", table_name="job_moves")
    op.drop_table("job_moves")
    op.drop_column("job_relaunches", "kind")
    op.drop_column("jobs", "succession_kind")
