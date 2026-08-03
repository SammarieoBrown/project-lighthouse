"""Make Act 3 approvals authenticated, durable, and idempotent.

Revision ID: 0006_act3_approval
Revises: 0005_unique_hazard_external_ref
Create Date: 2026-08-03

Bearer credentials are short lived and stored only as SHA-256 digests. Approval
rows carry the request identity that produced them, are role checked at insert,
and become immutable evidence immediately afterward.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0006_act3_approval"
down_revision = "0005_unique_hazard_external_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 executes the current canonical schema. A fresh database therefore
    # already has this revision's objects before Alembic reaches 0006, while an
    # existing database stamped at 0005 has none of them. Keep those two paths
    # convergent and refuse an ambiguous, partially applied state.
    state = op.get_bind().execute(
        text(
            """
            SELECT
              to_regclass(format('%I.%I', current_schema(), 'human_credential'))
                IS NOT NULL AS credential_table,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'approval'
                   AND column_name = 'idempotency_key'
              ) AS idempotency_column,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'approval'
                   AND column_name = 'request_hash'
              ) AS request_hash_column,
              to_regclass(format('%I.%I', current_schema(), 'approval_gate_subject_uidx'))
                IS NOT NULL AS subject_index,
              to_regclass(format('%I.%I', current_schema(), 'approval_idempotency_uidx'))
                IS NOT NULL AS idempotency_index,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = 'approval'::regclass
                   AND tgname = 'approval_role_guard_trigger'
                   AND NOT tgisinternal
              ) AS role_trigger,
              EXISTS (
                SELECT 1 FROM pg_rules
                 WHERE schemaname = current_schema()
                   AND tablename = 'approval'
                   AND rulename = 'approval_no_update'
              ) AS immutable_update_rule,
              EXISTS (
                SELECT 1 FROM pg_rules
                 WHERE schemaname = current_schema()
                   AND tablename = 'approval'
                   AND rulename = 'approval_no_delete'
              ) AS immutable_delete_rule,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'approval'::regclass
                   AND conname = 'approval_gate_role_chk'
              ) AS gate_role_constraint,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'approval'::regclass
                   AND conname = 'approval_recent_reauth_chk'
              ) AS recent_reauth_constraint,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = 'approval'::regclass
                   AND conname = 'approval_request_pair_chk'
              ) AS request_pair_constraint
            """
        )
    ).mappings().one()
    present = [bool(value) for value in state.values()]
    if all(present):
        return
    if any(present):
        missing = [name for name, value in state.items() if not value]
        raise RuntimeError(
            "0006 found a partially applied Act 3 schema; missing: " + ", ".join(missing)
        )

    op.execute(
        """
        CREATE TABLE human_credential (
          id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          user_id            uuid NOT NULL REFERENCES app_user(id),
          token_hash         text NOT NULL UNIQUE
            CHECK (token_hash ~ '^[0-9a-f]{64}$'),
          reauthenticated_at timestamptz NOT NULL,
          expires_at         timestamptz NOT NULL,
          revoked_at         timestamptz,
          created_at         timestamptz NOT NULL DEFAULT now(),
          CHECK (expires_at > reauthenticated_at),
          CHECK (expires_at <= reauthenticated_at + interval '5 minutes')
        );

        CREATE INDEX human_credential_user_idx
          ON human_credential (user_id);

        ALTER TABLE approval
          ADD COLUMN idempotency_key text,
          ADD COLUMN request_hash text;

        DO $$
        DECLARE
          duplicate_subjects text;
          invalid_roles text;
          stale_reauth text;
        BEGIN
          SELECT string_agg(
                   format('%s/%s/%s (%s rows)', gate, subject_type, subject_id, copies),
                   ', '
                 )
            INTO duplicate_subjects
            FROM (
              SELECT gate, subject_type, subject_id, count(*) AS copies
                FROM approval
               GROUP BY gate, subject_type, subject_id
              HAVING count(*) > 1
               ORDER BY gate, subject_type, subject_id
               LIMIT 10
            ) duplicate;

          IF duplicate_subjects IS NOT NULL THEN
            RAISE EXCEPTION 'approval contains duplicate gate subjects: %', duplicate_subjects
              USING HINT = 'Reconcile duplicate signatures explicitly, then retry the migration.';
          END IF;

          SELECT string_agg(id::text, ', ' ORDER BY id)
            INTO invalid_roles
            FROM approval
           WHERE NOT (
             (gate IN ('ALERT_CASCADE', 'ALLOCATION_PLAN') AND role_at_time = 'DIRECTOR')
             OR (gate = 'DISBURSEMENT_BATCH' AND role_at_time = 'FINANCE_OFFICER')
           );

          IF invalid_roles IS NOT NULL THEN
            RAISE EXCEPTION 'approval rows have invalid gate roles: %', invalid_roles
              USING HINT = 'Audit the historical signatures before enabling the gate-role constraint.';
          END IF;

          SELECT string_agg(id::text, ', ' ORDER BY id)
            INTO stale_reauth
            FROM approval
           WHERE reauth_at < approved_at - interval '5 minutes'
              OR reauth_at > approved_at + interval '1 minute';

          IF stale_reauth IS NOT NULL THEN
            RAISE EXCEPTION 'approval rows have stale or future reauthentication: %', stale_reauth
              USING HINT = 'Audit the historical signatures before enabling recent reauthentication.';
          END IF;
        END
        $$;

        ALTER TABLE approval
          ADD CONSTRAINT approval_request_pair_chk
            CHECK ((idempotency_key IS NULL) = (request_hash IS NULL)),
          ADD CONSTRAINT approval_idempotency_key_length_chk
            CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 200),
          ADD CONSTRAINT approval_request_hash_chk
            CHECK (request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT approval_gate_role_chk
            CHECK (
              (gate IN ('ALERT_CASCADE', 'ALLOCATION_PLAN') AND role_at_time = 'DIRECTOR')
              OR (gate = 'DISBURSEMENT_BATCH' AND role_at_time = 'FINANCE_OFFICER')
            ),
          ADD CONSTRAINT approval_recent_reauth_chk
            CHECK (
              reauth_at >= approved_at - interval '5 minutes'
              AND reauth_at <= approved_at + interval '1 minute'
            );

        CREATE UNIQUE INDEX approval_gate_subject_uidx
          ON approval (gate, subject_type, subject_id);

        CREATE UNIQUE INDEX approval_idempotency_uidx
          ON approval (approved_by, idempotency_key)
          WHERE idempotency_key IS NOT NULL;

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

        CREATE TRIGGER approval_role_guard_trigger
          BEFORE INSERT ON approval
          FOR EACH ROW EXECUTE FUNCTION approval_role_guard();

        CREATE RULE approval_no_update
          AS ON UPDATE TO approval DO INSTEAD NOTHING;
        CREATE RULE approval_no_delete
          AS ON DELETE TO approval DO INSTEAD NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP RULE IF EXISTS approval_no_delete ON approval;
        DROP RULE IF EXISTS approval_no_update ON approval;
        DROP TRIGGER IF EXISTS approval_role_guard_trigger ON approval;
        DROP FUNCTION IF EXISTS approval_role_guard();
        DROP INDEX IF EXISTS approval_idempotency_uidx;
        DROP INDEX IF EXISTS approval_gate_subject_uidx;
        ALTER TABLE approval
          DROP CONSTRAINT IF EXISTS approval_recent_reauth_chk,
          DROP CONSTRAINT IF EXISTS approval_gate_role_chk,
          DROP CONSTRAINT IF EXISTS approval_request_hash_chk,
          DROP CONSTRAINT IF EXISTS approval_idempotency_key_length_chk,
          DROP CONSTRAINT IF EXISTS approval_request_pair_chk,
          DROP COLUMN IF EXISTS request_hash,
          DROP COLUMN IF EXISTS idempotency_key;
        DROP TABLE IF EXISTS human_credential;
        """
    )
