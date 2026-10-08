"""core limits: runtime ceilings, ssh access, lifecycle timestamps (store/routing.py)

Revision ID: 0014_limits
Revises: 0013_security

Additive. Every deployment gets a finite runtime ceiling:
    deployments.effective_max_runtime_minutes  NOT NULL, CHECK > 0 (never unlimited)
    deployments.runtime_ceiling_source         request | account | system_default | system_hard_max |
                                               validation_cap | legacy_backfill
Legacy rows (written before this revision) are backfilled: effective = their max_runtime_minutes, else the
system default (60 min); terminate_deadline_at = created_at + effective where it was NULL; flagged with
runtime_ceiling_source = 'legacy_backfill'. NOTE: a legacy deployment still live past that deadline is
auto-terminated by the reconciliation job (deadline enforcement), by design: nothing may run unbounded.
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0014_limits"
down_revision: Union[str, Sequence[str], None] = "0013_security"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYSTEM_DEFAULT_MINUTES = 60   # settings.runtime_default_minutes at the time of this revision

TS = ("requested_termination_at", "provider_created_at", "provider_running_at", "provider_terminated_at",
      "billable_start", "billable_end")


def upgrade() -> None:
    op.add_column("account_limits", sa.Column("max_runtime_minutes", sa.Integer(), nullable=True))
    op.add_column("account_limits", sa.Column("default_runtime_minutes", sa.Integer(), nullable=True))

    op.add_column("deployments", sa.Column("effective_max_runtime_minutes", sa.Integer(), nullable=True))
    op.add_column("deployments", sa.Column("runtime_ceiling_source", sa.String(24), nullable=True))
    op.add_column("deployments", sa.Column("ssh_key_fingerprint", sa.String(80), nullable=True))
    op.add_column("deployments", sa.Column("operator_access", sa.String(128), nullable=True))
    for c in TS:
        op.add_column("deployments", sa.Column(c, sa.DateTime(timezone=True), nullable=True))
    op.add_column("deployments", sa.Column("billable_basis", sa.String(32), nullable=True))

    # legacy backfill: a finite ceiling and a deadline for every existing row, flagged
    op.execute(f"""
        UPDATE deployments SET
            effective_max_runtime_minutes = COALESCE(NULLIF(max_runtime_minutes, 0), {SYSTEM_DEFAULT_MINUTES}),
            runtime_ceiling_source = 'legacy_backfill'
        WHERE effective_max_runtime_minutes IS NULL
    """)
    op.execute("""
        UPDATE deployments SET
            terminate_deadline_at = created_at + make_interval(mins => effective_max_runtime_minutes)
        WHERE terminate_deadline_at IS NULL
    """)
    op.execute("""
        UPDATE deployments SET requested_termination_at = terminate_requested_at
        WHERE requested_termination_at IS NULL AND terminate_requested_at IS NOT NULL
    """)
    op.alter_column("deployments", "effective_max_runtime_minutes", nullable=False)
    op.create_check_constraint("ck_dep_effective_runtime_positive", "deployments",
                               "effective_max_runtime_minutes > 0")


def downgrade() -> None:
    op.drop_constraint("ck_dep_effective_runtime_positive", "deployments", type_="check")
    for c in ("billable_basis",) + TS + ("operator_access", "ssh_key_fingerprint", "runtime_ceiling_source",
                                         "effective_max_runtime_minutes"):
        op.drop_column("deployments", c)
    op.drop_column("account_limits", "default_runtime_minutes")
    op.drop_column("account_limits", "max_runtime_minutes")
