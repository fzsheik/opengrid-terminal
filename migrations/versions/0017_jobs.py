"""jobs: OpenGrid Jobs (store/jobs.py): jobs, job_events, job_runner_events, job_logs, job_metrics,
job_artifacts, job_tokens. Additive only.

Revision ID: 0017_jobs
Revises: 0016_raw_storage
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0017_jobs"
down_revision: Union[str, Sequence[str], None] = "0016_raw_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(40), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("principal_kind", sa.String(16), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("template", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(32), nullable=True),
        sa.Column("config", J, nullable=False),
        sa.Column("spec", J, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("route_request_id", sa.String(32), nullable=True),
        sa.Column("deployment_id", sa.String(40), nullable=True),
        sa.Column("quote_id", sa.String(40), nullable=True),
        sa.Column("selection", J, nullable=True),
        sa.Column("est_max_total_usd", sa.Numeric(14, 4), nullable=True),
        sa.Column("est_typical_total_usd", sa.Numeric(14, 4), nullable=True),
        sa.Column("runtime_ceiling_minutes", sa.Integer(), nullable=True),
        sa.Column("bootstrap_sha256", sa.String(64), nullable=True),
        sa.Column("bootstrap_template_version", sa.String(32), nullable=True),
        sa.Column("bootstrap_rendered_at", TS, nullable=True),
        sa.Column("ssh_key_mode", sa.String(16), nullable=True),
        sa.Column("ssh_key_fingerprint", sa.String(80), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("state_changed_at", TS, nullable=True),
        sa.Column("cancel_requested_at", TS, nullable=True),
        sa.Column("terminate_requested_at", TS, nullable=True),
        sa.Column("last_runner_event_at", TS, nullable=True),
        sa.Column("runner_phase", sa.String(32), nullable=True),
        sa.Column("runner_result", J, nullable=True),
        sa.Column("failure_code", sa.String(48), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("evidence", J, nullable=True),
    )
    op.create_index("ix_jobs_account_time", "jobs", ["account_id", "created_at"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_deployment", "jobs", ["deployment_id"])
    op.create_index("ix_jobs_route_request", "jobs", ["route_request_id"])
    op.create_index("ix_jobs_open", "jobs", ["finished_at"], postgresql_where=sa.text("finished_at IS NULL"))

    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("job_id", sa.String(40), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence", J, nullable=True),
    )
    op.create_index("ix_job_events_job", "job_events", ["job_id", "at"])
    op.create_index("ix_job_events_at", "job_events", ["at"])

    op.create_table(
        "job_runner_events",
        sa.Column("job_id", sa.String(40), primary_key=True),
        sa.Column("seq", sa.BigInteger(), primary_key=True),
        sa.Column("token_id", sa.BigInteger(), nullable=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("at", TS, nullable=True),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("data", J, nullable=True),
    )
    op.create_index("ix_job_runner_events_job", "job_runner_events", ["job_id", "received_at"])

    op.create_table(
        "job_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("job_id", sa.String(40), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
    )
    op.create_index("ix_job_logs_cursor", "job_logs", ["job_id", "id"])

    op.create_table(
        "job_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("job_id", sa.String(40), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=True),
        sa.Column("step", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
    )
    op.create_index("ix_job_metrics_job", "job_metrics", ["job_id", "name", "epoch", "step"])

    op.create_table(
        "job_artifacts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("job_id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("backend", sa.String(16), nullable=False),
        sa.Column("bytes", sa.LargeBinary(), nullable=True),
        sa.Column("object_key", sa.String(512), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ux_job_artifacts_name", "job_artifacts", ["job_id", "name"], unique=True)

    op.create_table(
        "job_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("job_id", sa.String(40), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("revoke_reason", sa.String(200), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
    )
    op.create_index("ix_job_tokens_job", "job_tokens", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_job_tokens_job", table_name="job_tokens")
    op.drop_table("job_tokens")
    op.drop_index("ux_job_artifacts_name", table_name="job_artifacts")
    op.drop_table("job_artifacts")
    op.drop_index("ix_job_metrics_job", table_name="job_metrics")
    op.drop_table("job_metrics")
    op.drop_index("ix_job_logs_cursor", table_name="job_logs")
    op.drop_table("job_logs")
    op.drop_index("ix_job_runner_events_job", table_name="job_runner_events")
    op.drop_table("job_runner_events")
    op.drop_index("ix_job_events_at", table_name="job_events")
    op.drop_index("ix_job_events_job", table_name="job_events")
    op.drop_table("job_events")
    for ix in ("ix_jobs_open", "ix_jobs_route_request", "ix_jobs_deployment", "ix_jobs_status", "ix_jobs_account_time"):
        op.drop_index(ix, table_name="jobs")
    op.drop_table("jobs")
