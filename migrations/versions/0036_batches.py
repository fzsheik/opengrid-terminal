"""batches: batch fan-out of ordinary jobs (jobs/batches.py; methodology/batches.md). Additive only: no existing
table is touched.

    batches          the batch: request, placement plan, budget (max_total_usd over every shard and relaunch), the
                     concurrency limit, the failure policy, the one batch approval, status / outcome
    batch_shards     UNIQUE(batch_id, shard_index): a shard exists once; job_id (UNIQUE when set) is the shard's
                     current job, replaced only by compare-and-set
    batch_attempts   the budget ledger: UNIQUE(batch_id, ref) (one reservation per submission try / relaunch of a
                     failed job), job_id UNIQUE once attached in the job-creating transaction
    batch_events     the append-only batch log

Revision ID: 0036_batches
Revises: 0035_power_markets
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0036_batches"
down_revision: Union[str, Sequence[str], None] = "0035_power_markets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("batch_id", sa.String(40), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("principal_kind", sa.String(16), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("template", sa.String(64), nullable=False),
        sa.Column("sandbox", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("spec", J, nullable=False),
        sa.Column("plan", J, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("shards_total", sa.Integer(), nullable=False),
        sa.Column("max_total_usd", sa.Numeric(14, 4), nullable=False),
        sa.Column("max_running", sa.Integer(), nullable=False),
        sa.Column("on_failure", sa.String(32), nullable=False),
        sa.Column("approval_status", sa.String(16), nullable=False),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", TS, nullable=True),
        sa.Column("approver_kind", sa.String(16), nullable=True),
        sa.Column("approver_key_id", sa.Integer(), nullable=True),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("cancel_requested_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint("max_total_usd > 0", name="ck_batches_budget_positive"),
        sa.CheckConstraint("max_running >= 1", name="ck_batches_max_running"),
    )
    op.create_index("ix_batches_account_time", "batches", ["account_id", "created_at"])
    op.create_index("ix_batches_open", "batches", ["finished_at"], postgresql_where=sa.text("finished_at IS NULL"))
    op.create_table(
        "batch_shards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(40), nullable=False),
        sa.Column("shard_index", sa.Integer(), nullable=False),
        sa.Column("params", J, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("planned", J, nullable=True),
        sa.Column("approved", J, nullable=True),
        sa.Column("job_id", sa.String(40), nullable=True),
        sa.Column("root_job_id", sa.String(40), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason_code", sa.String(48), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
    )
    op.create_index("ux_batch_shards_index", "batch_shards", ["batch_id", "shard_index"], unique=True)
    op.create_index("ux_batch_shards_job", "batch_shards", ["job_id"], unique=True,
                    postgresql_where=sa.text("job_id IS NOT NULL"))
    op.create_table(
        "batch_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(40), nullable=False),
        sa.Column("shard_index", sa.Integer(), nullable=False),
        sa.Column("ref", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("prev_job_id", sa.String(40), nullable=True),
        sa.Column("job_id", sa.String(40), nullable=True),
        sa.Column("reserved_usd", sa.Numeric(14, 4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("reserved_usd >= 0", name="ck_batch_attempts_reserved"),
    )
    op.create_index("ux_batch_attempts_ref", "batch_attempts", ["batch_id", "ref"], unique=True)
    op.create_index("ux_batch_attempts_job", "batch_attempts", ["job_id"], unique=True,
                    postgresql_where=sa.text("job_id IS NOT NULL"))
    op.create_index("ix_batch_attempts_shard", "batch_attempts", ["batch_id", "shard_index"])
    op.create_table(
        "batch_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(40), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("shard_index", sa.Integer(), nullable=True),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence", J, nullable=True),
    )
    op.create_index("ix_batch_events_batch", "batch_events", ["batch_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_batch_events_batch", table_name="batch_events")
    op.drop_table("batch_events")
    op.drop_index("ix_batch_attempts_shard", table_name="batch_attempts")
    op.drop_index("ux_batch_attempts_job", table_name="batch_attempts")
    op.drop_index("ux_batch_attempts_ref", table_name="batch_attempts")
    op.drop_table("batch_attempts")
    op.drop_index("ux_batch_shards_job", table_name="batch_shards")
    op.drop_index("ux_batch_shards_index", table_name="batch_shards")
    op.drop_table("batch_shards")
    op.drop_index("ix_batches_open", table_name="batches")
    op.drop_index("ix_batches_account_time", table_name="batches")
    op.drop_table("batches")
