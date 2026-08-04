"""Bind Finance signatures to demo execution and confirmation receipts.

Revision ID: 0008_act3_settlement
Revises: 0007_act3_release_hardening
Create Date: 2026-08-03

The migration refuses legacy batch/disbursement rows.  Those rows could claim a
confirmation without the new provider receipt and cannot be truthfully
backfilled.  An operator must audit/reconcile them explicitly instead.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.config import SCHEMA_SQL

revision = "0008_act3_settlement"
down_revision = "0007_act3_release_hardening"
branch_labels = None
depends_on = None

_GUARDS_BEGIN = "-- ACT3_SETTLEMENT_GUARDS_BEGIN"
_GUARDS_END = "-- ACT3_SETTLEMENT_GUARDS_END"


def _canonical_guard_sql() -> str:
    """Load the canonical guard block also used by fresh 0001 databases.

    Lighthouse already treats ``schema.sql`` as canonical in 0001.  Keeping
    this one explicitly marked block shared prevents the incremental path from
    silently installing weaker receipt triggers than a fresh database.
    """
    source = SCHEMA_SQL.read_text(encoding="utf-8")
    try:
        start = source.index(_GUARDS_BEGIN) + len(_GUARDS_BEGIN)
        end = source.index(_GUARDS_END, start)
    except ValueError as exc:  # pragma: no cover - release packaging failure
        raise RuntimeError("canonical Act 3 settlement guard block is missing") from exc
    block = source[start:end].strip()
    if not block:
        raise RuntimeError("canonical Act 3 settlement guard block is empty")
    return block


def _state() -> dict[str, bool]:
    return dict(
        op.get_bind()
        .execute(
            text(
                """
                SELECT
                  EXISTS (
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = 'disbursement_batch'
                       AND column_name = 'snapshot_hash'
                  ) AS batch_snapshot_column,
                  EXISTS (
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = 'disbursement'
                       AND column_name = 'executor_provider'
                  ) AS executor_provider_column,
                  EXISTS (
                    SELECT 1 FROM information_schema.columns
                     WHERE table_schema = current_schema()
                       AND table_name = 'disbursement'
                       AND column_name = 'provider_confirmation_hash'
                  ) AS confirmation_hash_column,
                  to_regclass(format(
                    '%I.%I', current_schema(), 'disbursement_allocation_uidx'
                  )) IS NOT NULL AS allocation_unique_index,
                  to_regclass(format(
                    '%I.%I', current_schema(),
                    'disbursement_execution_idempotency_uidx'
                  )) IS NOT NULL AS execution_idempotency_index,
                  to_regclass(format(
                    '%I.%I', current_schema(),
                    'ledger_disbursement_confirmed_subject_uidx'
                  )) IS NOT NULL AS confirmation_ledger_index,
                  EXISTS (
                    SELECT 1 FROM pg_trigger
                     WHERE tgrelid = to_regclass(format(
                             '%I.%I', current_schema(), 'disbursement_batch'
                           ))
                       AND tgname = 'disbursement_batch_signed_guard_trigger'
                       AND NOT tgisinternal
                  ) AS batch_guard_trigger,
                  EXISTS (
                    SELECT 1 FROM pg_trigger
                     WHERE tgrelid = to_regclass(format(
                             '%I.%I', current_schema(), 'disbursement'
                           ))
                       AND tgname = 'disbursement_lifecycle_guard_trigger'
                       AND NOT tgisinternal
                  ) AS lifecycle_guard_trigger,
                  EXISTS (
                    SELECT 1 FROM pg_trigger
                     WHERE tgrelid = to_regclass(format(
                             '%I.%I', current_schema(), 'ledger_entry'
                           ))
                       AND tgname = 'ledger_disbursement_receipt_guard_trigger'
                       AND NOT tgisinternal
                  ) AS ledger_guard_trigger,
                  EXISTS (
                    SELECT 1 FROM pg_trigger
                     WHERE tgrelid = to_regclass(format(
                             '%I.%I', current_schema(), 'disbursement'
                           ))
                       AND tgname = 'disbursement_receipt_complete_trigger'
                       AND NOT tgisinternal
                  ) AS receipt_complete_trigger
                """
            )
        )
        .mappings()
        .one()
    )


def upgrade() -> None:
    state = _state()
    present = [bool(value) for value in state.values()]
    if all(present):
        return
    if any(present):
        missing = [name for name, value in state.items() if not value]
        raise RuntimeError(
            "0008 found a partially applied settlement schema; missing: "
            + ", ".join(missing)
        )

    counts = op.get_bind().execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM disbursement_batch) AS batches,
              (SELECT count(*) FROM disbursement) AS disbursements
            """
        )
    ).mappings().one()
    if counts["batches"] or counts["disbursements"]:
        raise RuntimeError(
            "0008 refuses legacy settlement rows because provider confirmation "
            "cannot be truthfully backfilled; audit/reconcile first "
            f"(batches={counts['batches']}, disbursements={counts['disbursements']})"
        )

    op.execute(
        """
        ALTER TABLE disbursement_batch
          DROP CONSTRAINT disbursement_batch_approval_id_fkey,
          ALTER COLUMN total DROP DEFAULT,
          ALTER COLUMN approval_id SET NOT NULL,
          ADD COLUMN snapshot_hash text;
        ALTER TABLE disbursement_batch
          ALTER COLUMN snapshot_hash SET NOT NULL,
          ADD CONSTRAINT disbursement_batch_approval_id_fkey
            FOREIGN KEY (approval_id) REFERENCES approval(id) ON DELETE RESTRICT,
          ADD CONSTRAINT disbursement_batch_snapshot_hash_check
            CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT disbursement_batch_release_policy_chk CHECK (
            total = 45000.00
            AND channel IN ('BANK', 'MOBILE_MONEY', 'VOUCHER')
          );

        ALTER TABLE disbursement
          DROP CONSTRAINT disbursement_allocation_id_fkey,
          DROP CONSTRAINT disbursement_batch_id_fkey,
          DROP CONSTRAINT disbursement_approval_id_fkey,
          DROP CONSTRAINT IF EXISTS disbursement_check,
          ADD COLUMN executor_provider text,
          ADD COLUMN snapshot_hash text,
          ADD COLUMN execution_requested_by uuid,
          ADD COLUMN execution_idempotency_key text,
          ADD COLUMN execution_request_hash text,
          ADD COLUMN provider_confirmation_hash text;
        ALTER TABLE disbursement
          ALTER COLUMN executor_provider SET NOT NULL,
          ALTER COLUMN snapshot_hash SET NOT NULL,
          ADD CONSTRAINT disbursement_allocation_id_fkey
            FOREIGN KEY (allocation_id) REFERENCES allocation(id) ON DELETE RESTRICT,
          ADD CONSTRAINT disbursement_batch_id_fkey
            FOREIGN KEY (batch_id) REFERENCES disbursement_batch(id) ON DELETE RESTRICT,
          ADD CONSTRAINT disbursement_approval_id_fkey
            FOREIGN KEY (approval_id) REFERENCES approval(id) ON DELETE RESTRICT,
          ADD CONSTRAINT disbursement_execution_requested_by_fkey
            FOREIGN KEY (execution_requested_by) REFERENCES app_user(id)
            ON DELETE RESTRICT,
          ADD CONSTRAINT disbursement_snapshot_hash_check
            CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT disbursement_execution_key_length_chk CHECK (
            execution_idempotency_key IS NULL
            OR length(execution_idempotency_key) BETWEEN 1 AND 200
          ),
          ADD CONSTRAINT disbursement_execution_request_hash_chk CHECK (
            execution_request_hash IS NULL
            OR execution_request_hash ~ '^[0-9a-f]{64}$'
          ),
          ADD CONSTRAINT disbursement_confirmation_hash_chk CHECK (
            provider_confirmation_hash IS NULL
            OR provider_confirmation_hash ~ '^[0-9a-f]{64}$'
          ),
          ADD CONSTRAINT disbursement_demo_executor_chk CHECK (
            simulated AND executor_provider = 'LIGHTHOUSE_DEMO_EXECUTOR_V1'
          ),
          ADD CONSTRAINT disbursement_lifecycle_shape_chk CHECK (
            (
              status = 'PENDING'
              AND execution_requested_by IS NULL
              AND execution_idempotency_key IS NULL
              AND execution_request_hash IS NULL
              AND external_ref IS NULL
              AND provider_confirmation_hash IS NULL
              AND executed_at IS NULL
              AND confirmed_at IS NULL
              AND failure_reason IS NULL
            ) OR (
              status = 'EXECUTING'
              AND execution_requested_by IS NOT NULL
              AND execution_idempotency_key IS NOT NULL
              AND execution_request_hash IS NOT NULL
              AND external_ref IS NULL
              AND provider_confirmation_hash IS NULL
              AND executed_at IS NOT NULL
              AND confirmed_at IS NULL
              AND failure_reason IS NULL
            ) OR (
              status = 'CONFIRMED'
              AND execution_requested_by IS NOT NULL
              AND execution_idempotency_key IS NOT NULL
              AND execution_request_hash IS NOT NULL
              AND external_ref IS NOT NULL
              AND provider_confirmation_hash IS NOT NULL
              AND executed_at IS NOT NULL
              AND confirmed_at IS NOT NULL
              AND confirmed_at >= executed_at
              AND failure_reason IS NULL
            ) OR (
              status = 'FAILED'
              AND execution_requested_by IS NOT NULL
              AND execution_idempotency_key IS NOT NULL
              AND execution_request_hash IS NOT NULL
              AND external_ref IS NULL
              AND provider_confirmation_hash IS NULL
              AND executed_at IS NOT NULL
              AND confirmed_at IS NULL
              AND failure_reason IS NOT NULL
            )
          );

        CREATE UNIQUE INDEX disbursement_allocation_uidx
          ON disbursement (allocation_id);
        CREATE UNIQUE INDEX disbursement_batch_uidx
          ON disbursement (batch_id);
        CREATE UNIQUE INDEX disbursement_external_ref_uidx
          ON disbursement (executor_provider, external_ref)
          WHERE external_ref IS NOT NULL;
        CREATE UNIQUE INDEX disbursement_execution_idempotency_uidx
          ON disbursement (execution_requested_by, execution_idempotency_key)
          WHERE execution_idempotency_key IS NOT NULL;

        CREATE UNIQUE INDEX ledger_disbursement_batch_signed_subject_uidx
          ON ledger_entry (subject_id)
          WHERE action = 'disbursement.batch_signed'
            AND subject_type = 'disbursement_batch';
        CREATE UNIQUE INDEX ledger_disbursement_executed_subject_uidx
          ON ledger_entry (subject_id)
          WHERE action = 'disbursement.executed'
            AND subject_type = 'disbursement';
        CREATE UNIQUE INDEX ledger_disbursement_confirmed_subject_uidx
          ON ledger_entry (subject_id)
          WHERE action = 'disbursement.confirmed'
            AND subject_type = 'disbursement';
        CREATE UNIQUE INDEX ledger_disbursement_failed_subject_uidx
          ON ledger_entry (subject_id)
          WHERE action = 'disbursement.failed'
            AND subject_type = 'disbursement';
        """
    )
    op.execute(_canonical_guard_sql())


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS disbursement_receipt_complete_trigger
          ON disbursement;
        DROP FUNCTION IF EXISTS disbursement_must_have_receipts();
        DROP TRIGGER IF EXISTS disbursement_batch_receipt_complete_trigger
          ON disbursement_batch;
        DROP FUNCTION IF EXISTS disbursement_batch_must_have_receipt();
        DROP TRIGGER IF EXISTS ledger_disbursement_receipt_guard_trigger
          ON ledger_entry;
        DROP FUNCTION IF EXISTS ledger_disbursement_receipt_guard();
        DROP TRIGGER IF EXISTS disbursement_lifecycle_guard_trigger
          ON disbursement;
        DROP FUNCTION IF EXISTS disbursement_lifecycle_guard();
        DROP FUNCTION IF EXISTS disbursement_snapshot_digest(disbursement);
        DROP TRIGGER IF EXISTS disbursement_batch_signed_guard_trigger
          ON disbursement_batch;
        DROP FUNCTION IF EXISTS disbursement_batch_signed_guard();
        DROP FUNCTION IF EXISTS
          disbursement_batch_snapshot_digest(disbursement_batch);

        DROP INDEX IF EXISTS ledger_disbursement_failed_subject_uidx;
        DROP INDEX IF EXISTS ledger_disbursement_confirmed_subject_uidx;
        DROP INDEX IF EXISTS ledger_disbursement_executed_subject_uidx;
        DROP INDEX IF EXISTS ledger_disbursement_batch_signed_subject_uidx;

        DROP INDEX IF EXISTS disbursement_execution_idempotency_uidx;
        DROP INDEX IF EXISTS disbursement_external_ref_uidx;
        DROP INDEX IF EXISTS disbursement_batch_uidx;
        DROP INDEX IF EXISTS disbursement_allocation_uidx;

        ALTER TABLE disbursement
          DROP CONSTRAINT IF EXISTS disbursement_lifecycle_shape_chk,
          DROP CONSTRAINT IF EXISTS disbursement_demo_executor_chk,
          DROP CONSTRAINT IF EXISTS disbursement_confirmation_hash_chk,
          DROP CONSTRAINT IF EXISTS disbursement_execution_request_hash_chk,
          DROP CONSTRAINT IF EXISTS disbursement_execution_key_length_chk,
          DROP CONSTRAINT IF EXISTS disbursement_snapshot_hash_check,
          DROP CONSTRAINT IF EXISTS disbursement_execution_requested_by_fkey,
          DROP CONSTRAINT IF EXISTS disbursement_allocation_id_fkey,
          DROP CONSTRAINT IF EXISTS disbursement_batch_id_fkey,
          DROP CONSTRAINT IF EXISTS disbursement_approval_id_fkey,
          DROP COLUMN IF EXISTS provider_confirmation_hash,
          DROP COLUMN IF EXISTS execution_request_hash,
          DROP COLUMN IF EXISTS execution_idempotency_key,
          DROP COLUMN IF EXISTS execution_requested_by,
          DROP COLUMN IF EXISTS snapshot_hash,
          DROP COLUMN IF EXISTS executor_provider,
          ADD CONSTRAINT disbursement_allocation_id_fkey
            FOREIGN KEY (allocation_id) REFERENCES allocation(id) ON DELETE CASCADE,
          ADD CONSTRAINT disbursement_batch_id_fkey
            FOREIGN KEY (batch_id) REFERENCES disbursement_batch(id),
          ADD CONSTRAINT disbursement_approval_id_fkey
            FOREIGN KEY (approval_id) REFERENCES approval(id),
          ADD CONSTRAINT disbursement_check
            CHECK (status <> 'CONFIRMED' OR confirmed_at IS NOT NULL);

        ALTER TABLE disbursement_batch
          DROP CONSTRAINT IF EXISTS disbursement_batch_release_policy_chk,
          DROP CONSTRAINT IF EXISTS disbursement_batch_snapshot_hash_check,
          DROP CONSTRAINT IF EXISTS disbursement_batch_approval_id_fkey,
          DROP COLUMN IF EXISTS snapshot_hash,
          ALTER COLUMN approval_id DROP NOT NULL,
          ALTER COLUMN total SET DEFAULT 0,
          ADD CONSTRAINT disbursement_batch_approval_id_fkey
            FOREIGN KEY (approval_id) REFERENCES approval(id);
        """
    )
