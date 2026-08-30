"""The Director carries universal gate authority.

Demo decision, 2026-08-30, made deliberately by the operator of record: one
person runs every human gate, so the Director role must pass verification
review (Review Clerk's gate), batch signing, and execution (Finance Officer's
gates). Every enforcement stays in the database and every signature still
records who signed with which role — what changes is only which roles each
gate accepts. The Review Clerk and Finance Officer keep exactly the powers
they had; nothing widens for them.

The replaced function bodies are extracted from the canonical schema between
per-function markers, the same pattern 0009 uses, so this migration cannot
drift from packages/contracts/schema.sql.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic import op

from app.config import SCHEMA_SQL

revision = "0015_director_all_gates"
down_revision = "0014_damage_evidence_ids"
branch_labels = None
depends_on = None

_FUNCTIONS = (
    "verification_snapshot_guard",
    "approval_role_guard",
    "disbursement_batch_signed_guard",
    "disbursement_lifecycle_guard",
)


def _canonical_function(name: str) -> str:
    source = SCHEMA_SQL.read_text(encoding="utf-8")
    begin, end = f"-- {name.upper()}_FN_BEGIN", f"-- {name.upper()}_FN_END"
    try:
        start = source.index(begin) + len(begin)
        stop = source.index(end, start)
    except ValueError as exc:  # pragma: no cover - release packaging failure
        raise RuntimeError(f"canonical {name} block is missing") from exc
    block = source[start:stop].strip()
    if not re.match(rf"CREATE OR REPLACE FUNCTION {name}\(\)", block):
        raise RuntimeError(f"canonical {name} block does not define {name}")
    return block


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE approval DROP CONSTRAINT approval_gate_role_chk;
        ALTER TABLE approval ADD CONSTRAINT approval_gate_role_chk CHECK (
          (gate IN ('ALERT_CASCADE', 'ALLOCATION_PLAN') AND role_at_time = 'DIRECTOR')
          OR (gate = 'DISBURSEMENT_BATCH'
              AND role_at_time IN ('FINANCE_OFFICER', 'DIRECTOR'))
        );
        """
    )
    for name in _FUNCTIONS:
        op.execute(_canonical_function(name))


def _vendored_sql(name: str) -> str:
    """The pre-0015 function bodies, kept beside this file — ``schema.sql`` has
    moved on and no longer contains them."""
    return (Path(__file__).resolve().parent / "downgrade_sql" / name).read_text(
        encoding="utf-8"
    )


def downgrade() -> None:
    """Return each gate to the single role that owned it.

    Signatures a Director gave under the widened rules would violate the
    restored constraint, so they go with the rows that depend on them. Like
    0016, this is a development path: withdrawing delegated authority in
    production is a decision to make forward.
    """
    op.execute(
        """
        DELETE FROM disbursement;
        DELETE FROM disbursement_batch;
        DELETE FROM approval
         WHERE gate = 'DISBURSEMENT_BATCH' AND role_at_time <> 'FINANCE_OFFICER';

        ALTER TABLE approval DROP CONSTRAINT approval_gate_role_chk;
        ALTER TABLE approval ADD CONSTRAINT approval_gate_role_chk CHECK (
          (gate IN ('ALERT_CASCADE', 'ALLOCATION_PLAN') AND role_at_time = 'DIRECTOR')
          OR (gate = 'DISBURSEMENT_BATCH' AND role_at_time = 'FINANCE_OFFICER')
        );
        """
    )
    op.execute(_vendored_sql("0015_downgrade.sql"))
