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


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE approval DROP CONSTRAINT approval_gate_role_chk;
        ALTER TABLE approval ADD CONSTRAINT approval_gate_role_chk CHECK (
          (gate IN ('ALERT_CASCADE', 'ALLOCATION_PLAN') AND role_at_time = 'DIRECTOR')
          OR (gate = 'DISBURSEMENT_BATCH' AND role_at_time = 'FINANCE_OFFICER')
        );

        CREATE OR REPLACE FUNCTION approval_role_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
          signer_role app_role;
          signer_active boolean;
        BEGIN
          SELECT role, active
            INTO signer_role, signer_active
            FROM app_user
           WHERE id = NEW.approved_by;

          IF NOT FOUND OR NOT signer_active THEN
            RAISE EXCEPTION 'approval signer % is missing or inactive', NEW.approved_by;
          END IF;

          IF NEW.role_at_time IS DISTINCT FROM signer_role THEN
            RAISE EXCEPTION
              'approval role snapshot % does not match signer role %',
              NEW.role_at_time, signer_role;
          END IF;

          IF NEW.gate IN ('ALERT_CASCADE', 'ALLOCATION_PLAN')
             AND signer_role <> 'DIRECTOR' THEN
            RAISE EXCEPTION 'gate % requires DIRECTOR, got %', NEW.gate, signer_role;
          END IF;

          IF NEW.gate = 'DISBURSEMENT_BATCH'
             AND signer_role <> 'FINANCE_OFFICER' THEN
            RAISE EXCEPTION
              'gate DISBURSEMENT_BATCH requires FINANCE_OFFICER, got %', signer_role;
          END IF;

          NEW.approved_at := statement_timestamp();
          RETURN NEW;
        END
        $function$;
        """
    )
    # The pre-0015 bodies of the other three functions live in git history;
    # restoring them wholesale here would freeze stale copies of unrelated
    # logic into this file. Downgrading past 0015 is a git checkout of the
    # schema plus a manual re-apply, and the demo decision it reverses should
    # be reverted forward anyway.
    raise RuntimeError(
        "0015 downgrade is intentionally partial: re-apply the pre-0015 "
        "guard functions from git history deliberately."
    )
