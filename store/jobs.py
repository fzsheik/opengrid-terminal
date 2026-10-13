"""OpenGrid Jobs tables (migration 0017_jobs; methodology/jobs.md).

    jobs                one per submitted workload: the request, the selection evidence, the job state (separate
                        from the deployment's), the outcome, the 12 verification checks (evidence jsonb)
    job_events          every job state transition: from, to, actor, reason, evidence (append-only)
    job_runner_events   the raw runner callback ledger, one row per (job, seq): duplicate seq is ignored,
                        so delivery is idempotent; job_sync rebuilds runner-derived state from it
    job_logs            runner log lines (id is the read cursor), truncated per line
    job_metrics         per-epoch / per-step metrics the runner reported
    job_artifacts       artifacts: name, size, sha256 (verified on upload), backend, bytes (postgres backend)
                        or object key (s3 backend)
    job_tokens          runner callback tokens: ONLY the nonce is stored; the token is HMAC(pepper, job+nonce),
                        derived when the bootstrap is rendered and never written anywhere

A job's account_id is a plain integer (no cross-domain foreign key); None is the site operator. A job
references route_request_id / deployment_id / quote_id of the EXISTING execution layer; it never duplicates
the deployment's state.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, LargeBinary, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_account_time", "account_id", "created_at"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_deployment", "deployment_id"),
        Index("ix_jobs_route_request", "route_request_id"),
        Index("ix_jobs_open", "finished_at", postgresql_where=text("finished_at IS NULL")),
    )

    job_id: Mapped[str] = mapped_column(String(40), primary_key=True)          # job-<hex>
    account_id: Mapped[int | None] = mapped_column(Integer)
    key_id: Mapped[int | None] = mapped_column(Integer)
    principal_kind: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    job_type: Mapped[str] = mapped_column(String(32))
    template: Mapped[str] = mapped_column(String(64))
    template_version: Mapped[str | None] = mapped_column(String(32))
    config: Mapped[dict] = mapped_column(JSONB)
    spec: Mapped[dict] = mapped_column(JSONB)                                   # the validated request (no key material)
    status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str | None] = mapped_column(String(16))                    # succeeded | failed | cancelled | simulated
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    route_request_id: Mapped[str | None] = mapped_column(String(32))
    deployment_id: Mapped[str | None] = mapped_column(String(40))
    quote_id: Mapped[str | None] = mapped_column(String(40))
    selection: Mapped[dict | None] = mapped_column(JSONB)                      # the preview/selection evidence used
    est_max_total_usd: Mapped[float | None] = mapped_column(Numeric(14, 4))
    est_typical_total_usd: Mapped[float | None] = mapped_column(Numeric(14, 4))
    runtime_ceiling_minutes: Mapped[int | None] = mapped_column(Integer)
    bootstrap_sha256: Mapped[str | None] = mapped_column(String(64))
    bootstrap_template_version: Mapped[str | None] = mapped_column(String(32))
    bootstrap_rendered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ssh_key_mode: Mapped[str | None] = mapped_column(String(16))               # customer | ephemeral
    ssh_key_fingerprint: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminate_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_runner_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runner_phase: Mapped[str | None] = mapped_column(String(32))
    runner_result: Mapped[dict | None] = mapped_column(JSONB)                  # the runner's complete{} body
    failure_code: Mapped[str | None] = mapped_column(String(48))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)                       # the 12 verification checks


class JobEvent(Base):
    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job", "job_id", "at"), Index("ix_job_events_at", "at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16))                             # user | admin | system | runner | reconciler
    actor_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)


class JobRunnerEvent(Base):
    __tablename__ = "job_runner_events"
    __table_args__ = (Index("ix_job_runner_events_job", "job_id", "received_at"),)

    job_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_id: Mapped[int | None] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))        # the runner's own clock
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="runner")          # runner | simulated
    data: Mapped[dict | None] = mapped_column(JSONB)


class JobLog(Base):
    __tablename__ = "job_logs"
    __table_args__ = (Index("ix_job_logs_cursor", "job_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)   # the read cursor
    job_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int | None] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stream: Mapped[str] = mapped_column(String(16))                            # stdout | stderr | system | simulated
    line: Mapped[str] = mapped_column(Text)


class JobMetric(Base):
    __tablename__ = "job_metrics"
    __table_args__ = (Index("ix_job_metrics_job", "job_id", "name", "epoch", "step"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int | None] = mapped_column(BigInteger)
    epoch: Mapped[int | None] = mapped_column(Integer)
    step: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), default="runner")          # runner | simulated


class JobArtifact(Base):
    __tablename__ = "job_artifacts"
    __table_args__ = (Index("ux_job_artifacts_name", "job_id", "name", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str | None] = mapped_column(String(128))
    backend: Mapped[str] = mapped_column(String(16))                           # postgres | s3
    bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)    # postgres backend only
    object_key: Mapped[str | None] = mapped_column(String(512))                # s3 backend only
    source: Mapped[str] = mapped_column(String(16), default="runner")          # runner | simulated
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class JobToken(Base):
    __tablename__ = "job_tokens"
    __table_args__ = (Index("ix_job_tokens_job", "job_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(40))
    nonce: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(200))
    last_seq: Mapped[int | None] = mapped_column(BigInteger)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
