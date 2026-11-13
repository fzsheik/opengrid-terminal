"""Batch fan-out tables (migration 0036_batches; methodology/batches.md, jobs/batches.py).

    batches          one per submitted batch: the validated request, the placement plan, the budget
                     (max_total_usd across ALL shards and ALL relaunch attempts), the concurrency limit, the failure
                     policy, the ONE batch approval (who, when) and the batch status / outcome
    batch_shards     one row per shard: UNIQUE(batch_id, shard_index), its parameters, the planned placement, the
                     terms its approval covers, and job_id = the shard's CURRENT job (an ordinary job; a relaunch
                     successor replaces it by compare-and-set, so a shard never has two live jobs)
    batch_attempts   the budget ledger: one row per reservation (an attempt of a shard), UNIQUE(batch_id, ref) with
                     ref = "s<i>:submit:<n>" | "s<i>:relaunch:<failed job id>", job_id UNIQUE once attached (in the
                     same transaction that creates the job: orchestrator._create_job -> jobs.batches.attach)
    batch_events     append-only batch log: submitted, approved, shard submitted / refused / re-approval needed,
                     cancel, stop, finished

A batch never provisions anything itself: every shard is an ordinary job (jobs.orchestrator.submit); account_id is a
plain integer (None = the site operator), like jobs.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class Batch(Base):
    __tablename__ = "batches"
    __table_args__ = (
        Index("ix_batches_account_time", "account_id", "created_at"),
        Index("ix_batches_open", "finished_at", postgresql_where=text("finished_at IS NULL")),
        CheckConstraint("max_total_usd > 0", name="ck_batches_budget_positive"),
        CheckConstraint("max_running >= 1", name="ck_batches_max_running"),
    )

    batch_id: Mapped[str] = mapped_column(String(40), primary_key=True)        # bat-<hex>
    account_id: Mapped[int | None] = mapped_column(Integer)
    key_id: Mapped[int | None] = mapped_column(Integer)
    principal_kind: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    template: Mapped[str] = mapped_column(String(64))
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    spec: Mapped[dict] = mapped_column(JSONB)                                   # the validated batch request (no key)
    plan: Mapped[dict | None] = mapped_column(JSONB)                           # the placement plan at submission
    status: Mapped[str] = mapped_column(String(16))                            # active | stopping | cancelling | finished
    outcome: Mapped[str | None] = mapped_column(String(16))                    # succeeded|partial|failed|cancelled|stopped|simulated
    shards_total: Mapped[int] = mapped_column(Integer)
    max_total_usd: Mapped[float] = mapped_column(Numeric(14, 4))
    max_running: Mapped[int] = mapped_column(Integer)
    on_failure: Mapped[str] = mapped_column(String(32))                        # continue | stop_on_first_failure
    approval_status: Mapped[str] = mapped_column(String(16))                   # none | approved | not_required
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approver_kind: Mapped[str | None] = mapped_column(String(16))
    approver_key_id: Mapped[int | None] = mapped_column(Integer)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchShard(Base):
    __tablename__ = "batch_shards"
    __table_args__ = (
        Index("ux_batch_shards_index", "batch_id", "shard_index", unique=True),
        Index("ux_batch_shards_job", "job_id", unique=True, postgresql_where=text("job_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(40))
    shard_index: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSONB)                                # this shard's config overrides
    status: Mapped[str] = mapped_column(String(24))
    planned: Mapped[dict | None] = mapped_column(JSONB)                        # {provider, listing_id, price, ...}
    approved: Mapped[dict | None] = mapped_column(JSONB)                       # the terms the batch approval covers
    job_id: Mapped[str | None] = mapped_column(String(40))                     # the CURRENT job (latest attempt)
    root_job_id: Mapped[str | None] = mapped_column(String(40))                # the first job
    attempts: Mapped[int] = mapped_column(Integer, default=0)                  # jobs attached so far
    tries: Mapped[int] = mapped_column(Integer, default=0)                     # submissions tried (refusals retried)
    reason_code: Mapped[str | None] = mapped_column(String(48))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchAttempt(Base):
    """One budget reservation. Counted at reserved_usd until its job's deployment is reconciled (then the actual
    cost) or the attempt provably launched nothing (then 0); released (0) when no job was ever created."""
    __tablename__ = "batch_attempts"
    __table_args__ = (
        Index("ux_batch_attempts_ref", "batch_id", "ref", unique=True),
        Index("ux_batch_attempts_job", "job_id", unique=True, postgresql_where=text("job_id IS NOT NULL")),
        Index("ix_batch_attempts_shard", "batch_id", "shard_index"),
        CheckConstraint("reserved_usd >= 0", name="ck_batch_attempts_reserved"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(40))
    shard_index: Mapped[int] = mapped_column(Integer)
    ref: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16))                              # submit | relaunch
    prev_job_id: Mapped[str | None] = mapped_column(String(40))                # the shard's job at reservation time
    job_id: Mapped[str | None] = mapped_column(String(40))
    reserved_usd: Mapped[float] = mapped_column(Numeric(14, 4))
    status: Mapped[str] = mapped_column(String(16))                            # reserved | attached | released
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BatchEvent(Base):
    __tablename__ = "batch_events"
    __table_args__ = (Index("ix_batch_events_batch", "batch_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(32))
    shard_index: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(16))                             # user | admin | system
    actor_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
