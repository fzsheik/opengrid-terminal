"""External control-plane probe data from UptimeProject (api.uptimeproject.org, CC BY 4.0).

    uptime_services       one row per UptimeProject service that belongs to a provider OpenGrid tracks, with its
                          headline availability and current status as of the last poll
    uptime_hourly         hourly buckets (availability, down/degraded minutes, p50 latency) per service; the newest
                          bucket is rewritten while it is still filling
    uptime_daily          daily buckets (same columns); UptimeProject only serves daily buckets beyond 7 days, so this is
                          the only way to backfill history (up to its first measurement) and what 30/90-day views read
    uptime_probe_samples  the per-probe status + latency snapshot at each poll (the history endpoint only has the
                          cross-probe p50, so per-probe latency exists only from the day we start polling)
    uptime_fetch_log      one row per poll: ok, services, points, error, duration
    probe_results         OpenGrid's own probes, RAW, one row per (target, location, time) (see the end of this file)
    provider_incidents    incidents the PROVIDER reported on its own status page (Statuspage / Google Cloud), with
                          the update text, the affected components, and an inferred scope (global / regional /
                          unspecified) with the regions it names. Reported, not measured: kept apart from the probes.

Kept apart from OpenGrid's own deployment evidence (routing/reliability.py): nothing here feeds a score.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class UptimeService(Base):
    __tablename__ = "uptime_services"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)      # UptimeProject service id
    provider: Mapped[str] = mapped_column(String(64))                  # OpenGrid provider slug
    name: Mapped[str] = mapped_column(String(200))
    upstream_provider: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(32))
    region: Mapped[str | None] = mapped_column(String(64))
    homepage_url: Mapped[str | None] = mapped_column(Text)
    status_page_url: Mapped[str | None] = mapped_column(Text)
    current_status: Mapped[str | None] = mapped_column(String(16))
    checks_count: Mapped[int | None] = mapped_column(Integer)
    checks: Mapped[list | None] = mapped_column(JSONB)                 # [{id, check_type, target, interval_sec}]
    availability_24h: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    availability_7d: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    availability_30d: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    availability_90d: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UptimeHourly(Base):
    __tablename__ = "uptime_hourly"

    service_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    availability_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    status: Mapped[str | None] = mapped_column(String(16))
    down_minutes: Mapped[int | None] = mapped_column(Integer)
    degraded_minutes: Mapped[int | None] = mapped_column(Integer)
    latency_p50_ms: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_uptime_hourly_bucket", "bucket"),)


class UptimeDaily(Base):
    __tablename__ = "uptime_daily"

    service_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)   # 00:00 UTC of the day
    availability_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    status: Mapped[str | None] = mapped_column(String(16))
    down_minutes: Mapped[int | None] = mapped_column(Integer)
    degraded_minutes: Mapped[int | None] = mapped_column(Integer)
    latency_p50_ms: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UptimeProbeSample(Base):
    __tablename__ = "uptime_probe_samples"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    service_id: Mapped[str] = mapped_column(String(96))
    probe_id: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(16))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_uptime_probe_service_time", "service_id", "observed_at"),)


class UptimeFetchLog(Base):
    __tablename__ = "uptime_fetch_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ok: Mapped[bool] = mapped_column(Boolean)
    services: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class ProviderIncident(Base):
    __tablename__ = "provider_incidents"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)        # OpenGrid provider slug
    external_id: Mapped[str] = mapped_column(String(96), primary_key=True)     # the status page's incident id
    source: Mapped[str] = mapped_column(String(32))                            # statuspage | gcp
    title: Mapped[str] = mapped_column(Text)
    impact: Mapped[str | None] = mapped_column(String(32))                    # none/minor/major/critical | service_outage ...
    status: Mapped[str | None] = mapped_column(String(32))                    # investigating ... resolved
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    url: Mapped[str | None] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(16))                             # global | regional | unspecified
    scope_basis: Mapped[str | None] = mapped_column(Text)
    regions: Mapped[list] = mapped_column(JSONB, default=list)
    components: Mapped[list] = mapped_column(JSONB, default=list)             # affected component / product names
    updates: Mapped[list] = mapped_column(JSONB, default=list)                 # [{at, status, body}] oldest first
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_provider_incidents_started", "provider", "started_at"),)


# --------------------------------------------------------------------------- OpenGrid's own probes (probes/)
# RAW first: probe_results holds every result untouched; the two hourly tables are derived from it and can be rebuilt.

class ProbeResult(Base):
    __tablename__ = "probe_results"

    target_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    location: Mapped[str] = mapped_column(String(32), primary_key=True)          # the vantage point that ran the probe
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(48))
    ok: Mapped[bool] = mapped_column(Boolean)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error_class: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    dns_ms: Mapped[int | None] = mapped_column(Integer)
    connect_ms: Mapped[int | None] = mapped_column(Integer)
    tls_ms: Mapped[int | None] = mapped_column(Integer)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int | None] = mapped_column(Integer)
    resolved_ip: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_probe_results_time", "observed_at"),)


class ProbeAgent(Base):
    __tablename__ = "probe_agents"

    agent_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    location: Mapped[str] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    results_total: Mapped[int] = mapped_column(BigInteger, default=0)


class ProbeLocationHourly(Base):
    __tablename__ = "probe_location_hourly"

    target_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    location: Mapped[str] = mapped_column(String(32), primary_key=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    checks: Mapped[int] = mapped_column(Integer)
    ok_checks: Mapped[int] = mapped_column(Integer)
    latency_p50_ms: Mapped[int | None] = mapped_column(Integer)
    latency_p95_ms: Mapped[int | None] = mapped_column(Integer)
    latency_max_ms: Mapped[int | None] = mapped_column(Integer)
    error_classes: Mapped[dict] = mapped_column(JSONB, default=dict)


class ProbeTargetHourly(Base):
    __tablename__ = "probe_target_hourly"

    target_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    checks: Mapped[int] = mapped_column(Integer)
    ok_checks: Mapped[int] = mapped_column(Integer)
    availability: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    slots_ok: Mapped[int] = mapped_column(Integer)
    slots_regional: Mapped[int] = mapped_column(Integer)
    slots_global: Mapped[int] = mapped_column(Integer)
    slots_single: Mapped[int] = mapped_column(Integer)
    slot_minutes: Mapped[int] = mapped_column(Integer)
    failing_locations: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_classes: Mapped[dict] = mapped_column(JSONB, default=dict)
    locations: Mapped[list] = mapped_column(JSONB, default=list)
    latency_p50_ms: Mapped[int | None] = mapped_column(Integer)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_probe_target_hourly_bucket", "bucket"),)
