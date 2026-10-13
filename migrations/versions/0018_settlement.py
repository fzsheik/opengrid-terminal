"""settlement: official index fixings (store/settlement.py): index_fixings (append-only, hash-chained),
fixing_evidence (raw snapshots a fixing was computed from; exempt from retention) and
methodology_notices (recorded methodology change notices). Additive only.

index_fixings and fixing_evidence are append-only ledgers: a trigger rejects every UPDATE,
DELETE and TRUNCATE, so a published fixing can only be corrected by appending a restatement.

Revision ID: 0018_settlement
Revises: 0017_jobs
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0018_settlement"
down_revision: Union[str, Sequence[str], None] = "0017_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())

GUARD_FN = """
CREATE OR REPLACE FUNCTION og_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only ledger %: % is not allowed (corrections are new restated rows)',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$
"""


def _guard(table: str) -> None:
    op.execute(f"CREATE TRIGGER {table}_no_update BEFORE UPDATE OR DELETE ON {table} "
               f"FOR EACH ROW EXECUTE FUNCTION og_append_only()")
    op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
               f"FOR EACH STATEMENT EXECUTE FUNCTION og_append_only()")


def upgrade() -> None:
    op.create_table(
        "index_fixings",
        # Ledger position; the hash chain runs in seq order.
        sa.Column("seq", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("fixing_id", sa.String(48), nullable=False, unique=True),
        sa.Column("index_id", sa.String(96), nullable=False),
        sa.Column("fixing_type", sa.String(8), nullable=False),
        sa.Column("fixing_time", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("methodology_version", sa.String(16), nullable=False),
        sa.Column("settlement_version", sa.String(16), nullable=False),
        sa.Column("inputs", J, nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("flags", J, nullable=False),
        sa.Column("restates", sa.String(48), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_fixings_index_time", "index_fixings", ["index_id", "fixing_type", "fixing_time"])
    # Exactly one original fixing per (index, type, time); restatements are extra rows.
    op.create_index("ux_fixings_original", "index_fixings", ["index_id", "fixing_type", "fixing_time"],
                    unique=True, postgresql_where=sa.text("restates IS NULL"))
    op.create_index("ix_fixings_restates", "index_fixings", ["restates"],
                    postgresql_where=sa.text("restates IS NOT NULL"))

    op.create_table(
        "fixing_evidence",
        sa.Column("fixing_seq", sa.BigInteger(), primary_key=True),
        sa.Column("snapshot_id", sa.BigInteger(), primary_key=True),
        sa.Column("payload_sha256", sa.String(64), nullable=True),
    )
    op.create_index("ix_fixing_evidence_snapshot", "fixing_evidence", ["snapshot_id"])
    op.create_index("ix_fixing_evidence_payload", "fixing_evidence", ["payload_sha256"])

    op.create_table(
        "methodology_notices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),          # indices | settlement
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("effective_from", TS, nullable=False),
        sa.Column("notice_days", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("published_at", TS, nullable=False),
    )
    op.create_index("ux_methodology_notices", "methodology_notices", ["scope", "version"], unique=True)

    op.execute(GUARD_FN)
    _guard("index_fixings")
    _guard("fixing_evidence")


def downgrade() -> None:
    for table in ("fixing_evidence", "index_fixings"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update ON {table}")
    op.execute("DROP FUNCTION IF EXISTS og_append_only()")
    op.drop_index("ux_methodology_notices", table_name="methodology_notices")
    op.drop_table("methodology_notices")
    op.drop_index("ix_fixing_evidence_payload", table_name="fixing_evidence")
    op.drop_index("ix_fixing_evidence_snapshot", table_name="fixing_evidence")
    op.drop_table("fixing_evidence")
    op.drop_index("ix_fixings_restates", table_name="index_fixings")
    op.drop_index("ux_fixings_original", table_name="index_fixings")
    op.drop_index("ix_fixings_index_time", table_name="index_fixings")
    op.drop_table("index_fixings")
