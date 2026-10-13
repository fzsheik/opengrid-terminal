"""Settlement tables (0018_settlement): the official fixings ledger (analytics/settlement.py,
methodology/settlement.md).

    index_fixings        append-only, hash-chained: one row per official fixing (hourly | daily) and
                         one more per restatement. Never updated, never deleted (DB trigger).
    fixing_evidence      the raw_snapshots rows each fixing was computed from; storage.py retention
                         never deletes a referenced snapshot or its body. Append-only (DB trigger).
    methodology_notices  recorded methodology change notices; a fixing is published only under a
                         version that has one.

The append-only trigger is installed both by the migration and, for create_all (tests), by the DDL
listeners below, so the guarantee does not depend on which path built the schema.
"""

from datetime import datetime

from sqlalchemy import DDL, BigInteger, DateTime, Double, Index, Integer, String, Text, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class IndexFixing(Base):
    __tablename__ = "index_fixings"
    __table_args__ = (
        Index("ix_fixings_index_time", "index_id", "fixing_type", "fixing_time"),
        Index("ux_fixings_original", "index_id", "fixing_type", "fixing_time", unique=True,
              postgresql_where=text("restates IS NULL")),
        Index("ix_fixings_restates", "restates", postgresql_where=text("restates IS NOT NULL")),
    )

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)  # chain order
    fixing_id: Mapped[str] = mapped_column(String(48), unique=True)
    index_id: Mapped[str] = mapped_column(String(96))
    fixing_type: Mapped[str] = mapped_column(String(8))            # hourly | daily
    fixing_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))                # final | not_published | restated
    value: Mapped[float | None] = mapped_column(Double)            # null unless publishable
    reason: Mapped[str | None] = mapped_column(Text)
    methodology_version: Mapped[str] = mapped_column(String(16))   # index methodology, frozen per fixing
    settlement_version: Mapped[str] = mapped_column(String(16))    # settlement rules, frozen per fixing
    inputs: Mapped[dict] = mapped_column(JSONB)                    # everything the value is recomputed from
    input_hash: Mapped[str] = mapped_column(String(64))            # sha256 of canonical JSON of inputs
    flags: Mapped[list] = mapped_column(JSONB)                     # surveillance / sufficiency flags
    restates: Mapped[str | None] = mapped_column(String(48))       # the ORIGINAL fixing_id this corrects
    actor: Mapped[str] = mapped_column(String(64))                 # "system" or the admin who restated
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FixingEvidence(Base):
    __tablename__ = "fixing_evidence"
    __table_args__ = (
        Index("ix_fixing_evidence_snapshot", "snapshot_id"),
        Index("ix_fixing_evidence_payload", "payload_sha256"),
    )

    fixing_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64))


class MethodologyNotice(Base):
    __tablename__ = "methodology_notices"
    __table_args__ = (Index("ux_methodology_notices", "scope", "version", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16))          # indices | settlement
    version: Mapped[str] = mapped_column(String(16))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    notice_days: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Same SQL as migrations/versions/0018_settlement.py (DDL() %-formats its text: %% is a literal %).
_GUARD_FN = DDL("""
CREATE OR REPLACE FUNCTION og_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only ledger %%: %% is not allowed (corrections are new restated rows)',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation';
END;
$$
""")


def _install_guard(table):
    event.listen(table, "after_create", _GUARD_FN.execute_if(dialect="postgresql"))
    for sql in (f"CREATE TRIGGER {table.name}_no_update BEFORE UPDATE OR DELETE ON {table.name} "
                f"FOR EACH ROW EXECUTE FUNCTION og_append_only()",
                f"CREATE TRIGGER {table.name}_no_truncate BEFORE TRUNCATE ON {table.name} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION og_append_only()"):
        event.listen(table, "after_create", DDL(sql).execute_if(dialect="postgresql"))


_install_guard(IndexFixing.__table__)
_install_guard(FixingEvidence.__table__)
