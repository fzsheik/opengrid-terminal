"""Workload performance observations, for future learned recommendations (migration 0020_intelligence;
methodology/recommendations.md).

    performance_observations   one observed (or user-supplied, or theoretical) run of a workload class on a GPU:
                               what ran (gpu, workload class, model size, method, precision), what it measured
                               (runtime, peak VRAM, utilization, provisioning delay, total cost), the outcome, where
                               the row came from (source_kind) and whether the owner consented to its use

Written by intelligence/measured.py from COMPLETED REAL OpenGrid jobs only (source_kind 'completed_job'; never a
sandbox / simulated job, never a failed one). The recommender prefers a consented measured observation over the
theoretical estimate for the same GPU and workload signature, and labels which one it used. Only source_kind
'measured_benchmark' or 'completed_job' rows with consent=true ever count as measured. account_id is a plain
integer (no cross-domain foreign key).

Migration 0037_measured_performance added the measurement columns (provider, workload signature, throughput and its
unit, the work measured, overhead, observed_at, data_kind) and a partial unique index so one job is recorded once.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

SOURCE_KINDS = ("theoretical", "user_supplied", "measured_benchmark", "completed_job")
MEASURED_KINDS = ("measured_benchmark", "completed_job")
OUTCOMES = ("succeeded", "failed", "oom", "cancelled", "unknown")
THROUGHPUT_UNITS = ("samples_per_second", "tokens_per_second")


class PerformanceObservation(Base):
    __tablename__ = "performance_observations"
    __table_args__ = (
        Index("ix_perfobs_gpu_class", "gpu", "workload_class"),
        Index("ix_perfobs_created", "created_at"),
        Index("ix_perfobs_signature", "workload_signature", "gpu"),
        Index("ux_perfobs_completed_job", "job_id", unique=True,
              postgresql_where=text("source_kind = 'completed_job' AND job_id IS NOT NULL")),
        CheckConstraint("source_kind IN ('theoretical', 'user_supplied', 'measured_benchmark', 'completed_job')",
                        name="ck_perfobs_source_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gpu: Mapped[str] = mapped_column(String(128))                          # canonical GPU name
    gpu_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    workload_class: Mapped[str] = mapped_column(String(32))                # training_full / training_lora / inference ...
    model_name: Mapped[str | None] = mapped_column(String(200))
    model_parameters_b: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(String(16))                 # full / lora / qlora / inference
    precision: Mapped[str | None] = mapped_column(String(16))
    runtime_s: Mapped[float | None] = mapped_column(Float)
    peak_vram_gb: Mapped[float | None] = mapped_column(Float)
    utilization: Mapped[float | None] = mapped_column(Float)               # 0..1 mean GPU utilization
    provisioning_delay_s: Mapped[float | None] = mapped_column(Float)
    total_cost_usd: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str | None] = mapped_column(String(16))
    source_kind: Mapped[str] = mapped_column(String(24))
    consent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    account_id: Mapped[int | None] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 0037_measured_performance
    provider: Mapped[str | None] = mapped_column(String(64))
    workload_signature: Mapped[str | None] = mapped_column(String(200))  # intelligence.measured.signature(...)
    micro_batch_size: Mapped[int | None] = mapped_column(Integer)
    throughput: Mapped[float | None] = mapped_column(Float)              # training-loop throughput (no setup)
    throughput_unit: Mapped[str | None] = mapped_column(String(24))      # samples_per_second | tokens_per_second
    work_measured: Mapped[float | None] = mapped_column(Float)           # samples (or tokens) the timed epochs did
    train_seconds: Mapped[float | None] = mapped_column(Float)           # sum of the timed epochs' wall-clock
    overhead_s: Mapped[float | None] = mapped_column(Float)              # job wall-clock minus train_seconds
    epochs_measured: Mapped[int | None] = mapped_column(Integer)
    gpu_detected: Mapped[str | None] = mapped_column(String(128))        # nvidia-smi name the runner reported
    data_kind: Mapped[str | None] = mapped_column(String(16))            # "measured" for completed_job rows
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # when the job finished
