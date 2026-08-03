"""Bind every Act 3 release to immutable verification and signed evidence.

Revision ID: 0007_act3_release_hardening
Revises: 0006_act3_approval
Create Date: 2026-08-03

The incremental path deliberately refuses to invent history. Legacy
allocations, signed plans, and allocation-approval ledger receipts require an
operator-led reconciliation before these invariants can be enabled. Existing
verification rows are safe to retain because their snapshot hashes can be
derived exactly from the evidentiary columns already stored in PostgreSQL.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0007_act3_release_hardening"
down_revision = "0006_act3_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 executes the current canonical schema. A fresh database therefore
    # reaches this revision with every object below already installed. Probe
    # the active Alembic schema explicitly: an object of the same name in
    # ``public`` or another tenant schema must never satisfy convergence.
    state = op.get_bind().execute(
        text(
            """
            SELECT
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'verification'
                   AND column_name = 'snapshot_hash'
              ) AS verification_snapshot_column_exists,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'verification'
                   AND column_name = 'snapshot_hash'
                   AND data_type = 'text'
                   AND is_nullable = 'NO'
              ) AS verification_snapshot_column_valid,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = to_regclass(
                         format('%I.%I', current_schema(), 'verification')
                       )
                   AND conname = 'verification_snapshot_hash_check'
                   AND contype = 'c'
              ) AS verification_snapshot_check,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'verification_snapshot_digest'
              ) AS verification_digest_function,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'verification_snapshot_guard'
              ) AS verification_snapshot_function,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'verification_immutable_guard'
              ) AS verification_immutable_function,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = to_regclass(
                         format('%I.%I', current_schema(), 'verification')
                       )
                   AND tgname = 'verification_snapshot_guard_trigger'
                   AND NOT tgisinternal
              ) AS verification_snapshot_trigger,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = to_regclass(
                         format('%I.%I', current_schema(), 'verification')
                       )
                   AND tgname = 'verification_immutable_guard_trigger'
                   AND NOT tgisinternal
              ) AS verification_immutable_trigger,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation_plan')
                       )
                   AND conname = 'allocation_plan_approval_id_fkey'
                   AND contype = 'f'
                   AND confdeltype = 'r'
              ) AS plan_approval_restrict_fk,
              EXISTS (
                SELECT 1
                 WHERE to_regclass(
                   format(
                     '%I.%I', current_schema(), 'allocation_plan_approval_uidx'
                   )
                 ) IS NOT NULL
              ) AS plan_approval_index_exists,
              EXISTS (
                SELECT 1 FROM pg_index
                 WHERE indexrelid = to_regclass(
                         format(
                           '%I.%I', current_schema(),
                           'allocation_plan_approval_uidx'
                         )
                       )
                   AND indisunique
              ) AS plan_approval_unique_index,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'allocation_plan_signed_guard'
              ) AS plan_guard_function,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation_plan')
                       )
                   AND tgname = 'allocation_plan_signed_guard_trigger'
                   AND NOT tgisinternal
              ) AS plan_guard_trigger,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'allocation'
                   AND column_name = 'plan_id'
                   AND is_nullable = 'NO'
              ) AS allocation_plan_not_null,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'allocation'
                   AND column_name = 'verification_id'
              ) AS allocation_verification_column_exists,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'allocation'
                   AND column_name = 'verification_id'
                   AND data_type = 'uuid'
                   AND is_nullable = 'NO'
              ) AS allocation_verification_column_valid,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'allocation'
                   AND column_name = 'verification_snapshot_hash'
              ) AS allocation_verification_hash_column_exists,
              EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'allocation'
                   AND column_name = 'verification_snapshot_hash'
                   AND data_type = 'text'
                   AND is_nullable = 'NO'
              ) AS allocation_verification_hash_column_valid,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation')
                       )
                   AND conname = 'allocation_verification_snapshot_hash_check'
                   AND contype = 'c'
              ) AS allocation_verification_hash_check,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation')
                       )
                   AND conname = 'allocation_plan_id_fkey'
                   AND contype = 'f'
                   AND confdeltype = 'r'
              ) AS allocation_plan_restrict_fk,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation')
                       )
                   AND conname = 'allocation_claim_id_fkey'
                   AND contype = 'f'
                   AND confdeltype = 'r'
              ) AS allocation_claim_restrict_fk,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation')
                       )
                   AND conname = 'allocation_verification_id_fkey'
                   AND contype = 'f'
                   AND confdeltype = 'r'
              ) AS allocation_verification_restrict_fk,
              EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation')
                       )
                   AND conname = 'allocation_release_policy_chk'
                   AND contype = 'c'
              ) AS allocation_release_constraint,
              EXISTS (
                SELECT 1
                 WHERE to_regclass(
                   format('%I.%I', current_schema(), 'allocation_plan_uidx')
                 ) IS NOT NULL
              ) AS allocation_plan_index_exists,
              EXISTS (
                SELECT 1 FROM pg_index
                 WHERE indexrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation_plan_uidx')
                       )
                   AND indisunique
              ) AS allocation_plan_unique_index,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'allocation_signed_guard'
              ) AS allocation_guard_function,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation')
                       )
                   AND tgname = 'allocation_signed_guard_trigger'
                   AND NOT tgisinternal
              ) AS allocation_guard_trigger,
              EXISTS (
                SELECT 1
                 WHERE to_regclass(
                   format(
                     '%I.%I', current_schema(),
                     'ledger_allocation_approved_subject_uidx'
                   )
                 ) IS NOT NULL
              ) AS ledger_allocation_index_exists,
              EXISTS (
                SELECT 1 FROM pg_index
                 WHERE indexrelid = to_regclass(
                         format(
                           '%I.%I', current_schema(),
                           'ledger_allocation_approved_subject_uidx'
                         )
                       )
                   AND indisunique
              ) AS ledger_allocation_unique_index,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'ledger_allocation_approval_guard'
              ) AS ledger_guard_function,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = to_regclass(
                         format('%I.%I', current_schema(), 'ledger_entry')
                       )
                   AND tgname = 'ledger_allocation_approval_guard_trigger'
                   AND NOT tgisinternal
              ) AS ledger_guard_trigger,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'signed_plan_must_be_complete'
              ) AS signed_plan_complete_function,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation_plan')
                       )
                   AND tgname = 'signed_plan_complete_trigger'
                   AND NOT tgisinternal
                   AND tgdeferrable
                   AND tginitdeferred
              ) AS signed_plan_complete_trigger,
              EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = current_schema()
                   AND p.proname = 'allocation_must_have_ledger_receipt'
              ) AS allocation_ledger_complete_function,
              EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgrelid = to_regclass(
                         format('%I.%I', current_schema(), 'allocation')
                       )
                   AND tgname = 'allocation_ledger_complete_trigger'
                   AND NOT tgisinternal
                   AND tgdeferrable
                   AND tginitdeferred
              ) AS allocation_ledger_complete_trigger
            """
        )
    ).mappings().one()
    present = [bool(value) for value in state.values()]
    if all(present):
        return
    if any(present):
        missing = [name for name, value in state.items() if not value]
        raise RuntimeError(
            "0007 found a partially applied Act 3 release schema; missing: "
            + ", ".join(missing)
        )

    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # An existing verification is complete historical input, so its digest is
    # derivable. The other three record types would require guessing which
    # signature/evidence snapshot they meant, which this migration refuses to
    # do even if individual rows happen to resemble the new fixed grant.
    op.execute(
        """
        DO $preflight$
        DECLARE
          legacy_allocations bigint;
          signed_plans bigint;
          approval_receipts bigint;
          invalid_verification_authority bigint;
        BEGIN
          SELECT count(*) INTO legacy_allocations FROM allocation;
          SELECT count(*) INTO signed_plans
            FROM allocation_plan WHERE approval_id IS NOT NULL;
          SELECT count(*) INTO approval_receipts
            FROM ledger_entry WHERE action = 'allocation.approved';
          SELECT count(*) INTO invalid_verification_authority
            FROM verification v
            LEFT JOIN app_user u ON u.id = v.actor_id
           WHERE CASE
                   WHEN v.actor_kind = 'HUMAN' THEN
                     u.id IS NULL
                     OR NOT u.active
                     OR u.role IS DISTINCT FROM 'REVIEW_CLERK'
                     OR v.agent_name IS NOT NULL
                   WHEN v.actor_kind = 'AGENT' THEN
                     v.actor_id IS NOT NULL
                     OR v.agent_name IS DISTINCT FROM 'verification_agent'
                   ELSE true
                 END
              OR (
                v.verdict = 'AUTO_VERIFIED'
                AND v.actor_kind <> 'AGENT'
              )
              OR (
                v.verdict IN ('APPROVED', 'REJECTED')
                AND v.actor_kind <> 'HUMAN'
              );

          IF legacy_allocations > 0
             OR signed_plans > 0
             OR approval_receipts > 0
             OR invalid_verification_authority > 0 THEN
            RAISE EXCEPTION
              '0007 cannot infer Act 3 evidence: % allocations, % signed plans, % allocation approval receipts, % invalid verification authorities',
              legacy_allocations, signed_plans, approval_receipts,
              invalid_verification_authority
              USING HINT =
                'Reconcile legacy release records and verification authority explicitly, then retry.';
          END IF;
        END
        $preflight$;
        """
    )

    op.execute(
        """
        ALTER TABLE verification
          ADD COLUMN snapshot_hash text,
          ADD CONSTRAINT verification_snapshot_hash_check
            CHECK (snapshot_hash ~ '^[0-9a-f]{64}$');

        CREATE OR REPLACE FUNCTION verification_snapshot_digest(v verification)
        RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $function$
          SELECT encode(
            digest(
              convert_to(
                jsonb_build_object(
                  'id', v.id::text,
                  'claim_id', v.claim_id::text,
                  'signals', v.signals,
                  'confidence', v.confidence,
                  'verdict', v.verdict::text,
                  'actor_kind', v.actor_kind::text,
                  'actor_id', v.actor_id::text,
                  'agent_name', v.agent_name,
                  'model_version', v.model_version,
                  'threshold_version', v.threshold_version,
                  'rationale', v.rationale,
                  'capped', v.capped,
                  'overrides_id', v.overrides_id::text,
                  'created_at_epoch_us',
                    (extract(epoch FROM v.created_at) * 1000000)::bigint
                )::text,
                'UTF8'
              ),
              'sha256'
            ),
            'hex'
          )
        $function$;

        UPDATE verification AS v
           SET snapshot_hash = verification_snapshot_digest(v);

        ALTER TABLE verification ALTER COLUMN snapshot_hash SET NOT NULL;

        CREATE OR REPLACE FUNCTION verification_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
          reviewer_role app_role;
          reviewer_active boolean;
        BEGIN
          IF NEW.actor_kind = 'HUMAN' THEN
            SELECT role, active INTO reviewer_role, reviewer_active
              FROM app_user WHERE id = NEW.actor_id;
            IF NOT FOUND OR NOT reviewer_active OR reviewer_role <> 'REVIEW_CLERK' THEN
              RAISE EXCEPTION
                'human verification verdicts require an active REVIEW_CLERK';
            END IF;
            IF NEW.agent_name IS NOT NULL THEN
              RAISE EXCEPTION
                'human verification verdicts cannot assert an agent name';
            END IF;
          ELSIF NEW.actor_kind = 'AGENT' THEN
            IF NEW.actor_id IS NOT NULL
               OR NEW.agent_name IS DISTINCT FROM 'verification_agent' THEN
              RAISE EXCEPTION
                'agent verification verdicts require verification_agent authority';
            END IF;
          ELSE
            RAISE EXCEPTION 'system actors cannot issue verification verdicts';
          END IF;

          IF NEW.verdict = 'AUTO_VERIFIED' AND NEW.actor_kind <> 'AGENT' THEN
            RAISE EXCEPTION 'AUTO_VERIFIED requires verification_agent authority';
          END IF;
          IF NEW.verdict IN ('APPROVED', 'REJECTED')
             AND NEW.actor_kind <> 'HUMAN' THEN
            RAISE EXCEPTION
              'APPROVED and REJECTED require REVIEW_CLERK authority';
          END IF;

          NEW.snapshot_hash := verification_snapshot_digest(NEW);
          RETURN NEW;
        END
        $function$;

        CREATE TRIGGER verification_snapshot_guard_trigger
          BEFORE INSERT ON verification
          FOR EACH ROW EXECUTE FUNCTION verification_snapshot_guard();

        CREATE OR REPLACE FUNCTION verification_immutable_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
          RAISE EXCEPTION 'verification evidence is immutable; append a new row';
        END
        $function$;

        CREATE TRIGGER verification_immutable_guard_trigger
          BEFORE UPDATE OR DELETE ON verification
          FOR EACH ROW EXECUTE FUNCTION verification_immutable_guard();

        ALTER TABLE allocation_plan
          DROP CONSTRAINT allocation_plan_approval_id_fkey,
          ADD CONSTRAINT allocation_plan_approval_id_fkey
            FOREIGN KEY (approval_id) REFERENCES approval(id) ON DELETE RESTRICT;

        CREATE UNIQUE INDEX allocation_plan_approval_uidx
          ON allocation_plan (approval_id) WHERE approval_id IS NOT NULL;

        CREATE OR REPLACE FUNCTION allocation_plan_signed_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.approval_id IS NOT NULL THEN
              RAISE EXCEPTION 'signed allocation plan is immutable';
            END IF;
            RETURN OLD;
          END IF;

          IF TG_OP = 'UPDATE' AND OLD.approval_id IS NOT NULL THEN
            RAISE EXCEPTION 'signed allocation plan is immutable';
          END IF;

          IF NEW.approval_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
              FROM approval a
             WHERE a.id = NEW.approval_id
               AND a.gate = 'ALLOCATION_PLAN'
               AND a.subject_type = 'allocation_plan'
               AND a.subject_id = NEW.id
          ) THEN
            RAISE EXCEPTION
              'allocation plan approval must sign this exact allocation_plan subject';
          END IF;
          RETURN NEW;
        END
        $function$;

        CREATE TRIGGER allocation_plan_signed_guard_trigger
          BEFORE INSERT OR UPDATE OR DELETE ON allocation_plan
          FOR EACH ROW EXECUTE FUNCTION allocation_plan_signed_guard();

        ALTER TABLE allocation
          DROP CONSTRAINT allocation_plan_id_fkey,
          DROP CONSTRAINT allocation_claim_id_fkey,
          DROP CONSTRAINT allocation_check,
          DROP CONSTRAINT allocation_check1,
          ALTER COLUMN plan_id SET NOT NULL,
          ADD COLUMN verification_id uuid NOT NULL,
          ADD COLUMN verification_snapshot_hash text NOT NULL,
          ADD CONSTRAINT allocation_plan_id_fkey
            FOREIGN KEY (plan_id) REFERENCES allocation_plan(id) ON DELETE RESTRICT,
          ADD CONSTRAINT allocation_claim_id_fkey
            FOREIGN KEY (claim_id) REFERENCES claim(id) ON DELETE RESTRICT,
          ADD CONSTRAINT allocation_verification_id_fkey
            FOREIGN KEY (verification_id) REFERENCES verification(id) ON DELETE RESTRICT,
          ADD CONSTRAINT allocation_verification_snapshot_hash_check
            CHECK (verification_snapshot_hash ~ '^[0-9a-f]{64}$'),
          ADD CONSTRAINT allocation_release_policy_chk CHECK (
            resource = 'CASH'
            AND amount = 45000.00
            AND currency = 'JMD'
            AND payer_route = 'GOV_RELIEF'
            AND sku IS NULL
            AND quantity IS NULL
            AND pool_id IS NULL
            AND warehouse_id IS NULL
          );

        CREATE UNIQUE INDEX allocation_plan_uidx ON allocation (plan_id);

        CREATE UNIQUE INDEX ledger_allocation_approved_subject_uidx
          ON ledger_entry (subject_id)
          WHERE action = 'allocation.approved' AND subject_type = 'allocation';

        CREATE OR REPLACE FUNCTION allocation_signed_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
          plan_event uuid;
          claim_event uuid;
          claim_state claim_status;
          file_state storm_file_state;
          verification_row verification%ROWTYPE;
          latest_verification_id uuid;
          signal_name text;
          signal_present boolean;
          signal_score numeric;
          all_present boolean := true;
        BEGIN
          IF TG_OP IN ('UPDATE', 'DELETE') THEN
            RAISE EXCEPTION 'signed allocation is immutable';
          END IF;

          IF NEW.resource <> 'CASH'
             OR NEW.amount IS DISTINCT FROM 45000.00
             OR NEW.currency <> 'JMD'
             OR NEW.payer_route <> 'GOV_RELIEF'
             OR NEW.sku IS NOT NULL
             OR NEW.quantity IS NOT NULL
             OR NEW.pool_id IS NOT NULL
             OR NEW.warehouse_id IS NOT NULL THEN
            RAISE EXCEPTION 'allocation does not match the fixed release grant policy';
          END IF;

          SELECT p.hazard_event_id
            INTO plan_event
            FROM allocation_plan p
            JOIN approval a ON a.id = p.approval_id
           WHERE p.id = NEW.plan_id
             AND a.gate = 'ALLOCATION_PLAN'
             AND a.subject_type = 'allocation_plan'
             AND a.subject_id = p.id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'allocation requires an exact signed allocation plan';
          END IF;

          SELECT c.hazard_event_id, c.status, sf.state
            INTO claim_event, claim_state, file_state
            FROM claim c
            JOIN storm_file sf ON sf.id = c.storm_file_id
           WHERE c.id = NEW.claim_id;
          IF NOT FOUND
             OR claim_state <> 'VERIFIED'
             OR file_state NOT IN ('VERIFIED', 'SETTLED')
             OR claim_event IS DISTINCT FROM plan_event THEN
            RAISE EXCEPTION 'allocation claim is not eligible for its signed plan';
          END IF;

          SELECT * INTO verification_row
            FROM verification
           WHERE id = NEW.verification_id
             AND claim_id = NEW.claim_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'allocation verification does not belong to the claim';
          END IF;

          SELECT id INTO latest_verification_id
            FROM verification
           WHERE claim_id = NEW.claim_id
           ORDER BY created_at DESC, id DESC
           LIMIT 1;
          IF verification_row.id IS DISTINCT FROM latest_verification_id
             OR NEW.verification_snapshot_hash
                  IS DISTINCT FROM verification_row.snapshot_hash THEN
            RAISE EXCEPTION
              'allocation must bind the latest verification and its snapshot hash';
          END IF;

          IF jsonb_typeof(verification_row.signals) IS DISTINCT FROM 'object'
             OR (
               SELECT count(*) FROM jsonb_object_keys(verification_row.signals)
             ) <> 5
             OR NOT (verification_row.signals ?& ARRAY[
               'hazard_sufficiency', 'satellite_change',
               'neighbour_corroboration', 'registry_match', 'media_integrity'
             ]) THEN
            RAISE EXCEPTION 'verification signal set is not eligible';
          END IF;

          FOREACH signal_name IN ARRAY ARRAY[
            'hazard_sufficiency', 'satellite_change',
            'neighbour_corroboration', 'registry_match', 'media_integrity'
          ] LOOP
            IF jsonb_typeof(verification_row.signals -> signal_name)
                 IS DISTINCT FROM 'object'
               OR jsonb_typeof(
                    verification_row.signals -> signal_name -> 'present'
                  ) IS DISTINCT FROM 'boolean' THEN
              RAISE EXCEPTION 'verification signal presence is not boolean';
            END IF;
            signal_present :=
              (verification_row.signals -> signal_name ->> 'present')::boolean;
            IF signal_present THEN
              IF jsonb_typeof(
                   verification_row.signals -> signal_name -> 'score'
                 ) IS DISTINCT FROM 'number' THEN
                RAISE EXCEPTION
                  'present verification signals require a numeric score';
              END IF;
              signal_score :=
                (verification_row.signals -> signal_name ->> 'score')::numeric;
              IF signal_score < 0 OR signal_score > 1 THEN
                RAISE EXCEPTION 'verification signal score is outside 0..1';
              END IF;
            ELSIF (verification_row.signals -> signal_name) ? 'score'
                  AND verification_row.signals -> signal_name -> 'score'
                    <> 'null'::jsonb THEN
              RAISE EXCEPTION
                'absent verification signals cannot assert a score';
            END IF;
            all_present := all_present AND signal_present;
          END LOOP;

          IF verification_row.confidence = 'NaN'::real
             OR verification_row.confidence < 0
             OR verification_row.confidence > 1 THEN
            RAISE EXCEPTION 'verification confidence is outside finite 0..1';
          END IF;

          IF verification_row.verdict = 'AUTO_VERIFIED' THEN
            IF verification_row.confidence < 0.85
               OR verification_row.capped
               OR NOT all_present
               OR verification_row.actor_kind <> 'AGENT'
               OR verification_row.actor_id IS NOT NULL
               OR verification_row.agent_name
                    IS DISTINCT FROM 'verification_agent' THEN
              RAISE EXCEPTION
                'automatic verification does not meet the release threshold';
            END IF;
          ELSIF verification_row.verdict = 'APPROVED' THEN
            IF verification_row.actor_kind <> 'HUMAN'
               OR verification_row.actor_id IS NULL THEN
              RAISE EXCEPTION
                'approved verification requires an identified human actor';
            END IF;
          ELSE
            RAISE EXCEPTION
              'verification verdict is not eligible for allocation';
          END IF;

          RETURN NEW;
        END
        $function$;

        CREATE TRIGGER allocation_signed_guard_trigger
          BEFORE INSERT OR UPDATE OR DELETE ON allocation
          FOR EACH ROW EXECUTE FUNCTION allocation_signed_guard();

        CREATE OR REPLACE FUNCTION ledger_allocation_approval_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
          allocation_row allocation%ROWTYPE;
          plan_row allocation_plan%ROWTYPE;
          approval_row approval%ROWTYPE;
          verification_hash text;
          claim_synthetic boolean;
        BEGIN
          IF NEW.action <> 'allocation.approved' THEN
            RETURN NEW;
          END IF;

          IF NEW.subject_type <> 'allocation' OR NEW.subject_id IS NULL
             OR NEW.actor_kind <> 'HUMAN' OR NEW.actor_id IS NULL THEN
            RAISE EXCEPTION
              'allocation approval ledger subject or actor is invalid';
          END IF;

          SELECT * INTO allocation_row FROM allocation WHERE id = NEW.subject_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'allocation approval ledger subject does not exist';
          END IF;
          SELECT * INTO plan_row
            FROM allocation_plan WHERE id = allocation_row.plan_id;
          SELECT * INTO approval_row FROM approval WHERE id = plan_row.approval_id;
          SELECT v.snapshot_hash, sf.synthetic
            INTO verification_hash, claim_synthetic
            FROM verification v
            JOIN claim c ON c.id = v.claim_id
            JOIN storm_file sf ON sf.id = c.storm_file_id
           WHERE v.id = allocation_row.verification_id
             AND c.id = allocation_row.claim_id;

          IF approval_row.id IS NULL
             OR NEW.actor_id IS DISTINCT FROM approval_row.approved_by
             OR NEW.payload ->> 'approval_id'
                  IS DISTINCT FROM approval_row.id::text
             OR NEW.payload ->> 'plan_id' IS DISTINCT FROM plan_row.id::text
             OR NEW.payload ->> 'allocation_id'
                  IS DISTINCT FROM allocation_row.id::text
             OR NEW.payload ->> 'claim_id'
                  IS DISTINCT FROM allocation_row.claim_id::text
             OR NEW.payload ->> 'verification_id'
                  IS DISTINCT FROM allocation_row.verification_id::text
             OR NEW.payload ->> 'verification_snapshot_hash'
                  IS DISTINCT FROM verification_hash
             OR NEW.payload ->> 'verification_snapshot_hash'
                  IS DISTINCT FROM allocation_row.verification_snapshot_hash
             OR NEW.payload ->> 'gate' IS DISTINCT FROM 'ALLOCATION_PLAN'
             OR NEW.payload ->> 'resource' IS DISTINCT FROM 'CASH'
             OR NEW.payload ->> 'amount' IS DISTINCT FROM '45000.00'
             OR NEW.payload ->> 'currency' IS DISTINCT FROM 'JMD'
             OR NEW.payload ->> 'payer_route' IS DISTINCT FROM 'GOV_RELIEF'
             OR NEW.payload ->> 'money_movement'
                  IS DISTINCT FROM 'NOT_INITIATED_AT_APPROVAL'
             OR jsonb_typeof(NEW.payload -> 'synthetic')
                  IS DISTINCT FROM 'boolean'
             OR (NEW.payload ->> 'synthetic')::boolean
                  IS DISTINCT FROM claim_synthetic
             OR NEW.payload ->> 'parish' IS NULL
             OR NEW.payload ->> 'parish' NOT IN (
               'Clarendon', 'Hanover', 'Kingston', 'Manchester', 'Portland',
               'Saint Andrew', 'Saint Ann', 'Saint Catherine', 'Saint Elizabeth',
               'Saint James', 'Saint Mary', 'Saint Thomas', 'Trelawny',
               'UNSPECIFIED', 'Westmoreland'
             )
             OR NEW.payload ->> 'need_category' IS NULL
             OR NEW.payload ->> 'need_category' NOT IN (
               'ACCESS_BLOCKED', 'CONTENTS_DAMAGE', 'ESSENTIAL_SERVICES',
               'FLOOD_DAMAGE', 'OTHER_DAMAGE', 'ROOF_DAMAGE',
               'STRUCTURAL_DAMAGE'
             ) THEN
            RAISE EXCEPTION
              'allocation approval ledger payload does not match signed records';
          END IF;

          RETURN NEW;
        END
        $function$;

        CREATE TRIGGER ledger_allocation_approval_guard_trigger
          BEFORE INSERT ON ledger_entry
          FOR EACH ROW EXECUTE FUNCTION ledger_allocation_approval_guard();

        CREATE OR REPLACE FUNCTION signed_plan_must_be_complete()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
          IF NEW.approval_id IS NOT NULL
             AND (SELECT count(*) FROM allocation WHERE plan_id = NEW.id) <> 1 THEN
            RAISE EXCEPTION
              'signed allocation plan must contain exactly one allocation';
          END IF;
          RETURN NULL;
        END
        $function$;

        CREATE CONSTRAINT TRIGGER signed_plan_complete_trigger
          AFTER INSERT OR UPDATE ON allocation_plan
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION signed_plan_must_be_complete();

        CREATE OR REPLACE FUNCTION allocation_must_have_ledger_receipt()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
          IF (
            SELECT count(*)
              FROM ledger_entry
             WHERE action = 'allocation.approved'
               AND subject_type = 'allocation'
               AND subject_id = NEW.id
          ) <> 1 THEN
            RAISE EXCEPTION
              'signed allocation must have exactly one ledger receipt';
          END IF;
          RETURN NULL;
        END
        $function$;

        CREATE CONSTRAINT TRIGGER allocation_ledger_complete_trigger
          AFTER INSERT ON allocation
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION allocation_must_have_ledger_receipt();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS allocation_ledger_complete_trigger ON allocation;
        DROP FUNCTION IF EXISTS allocation_must_have_ledger_receipt();
        DROP TRIGGER IF EXISTS signed_plan_complete_trigger ON allocation_plan;
        DROP FUNCTION IF EXISTS signed_plan_must_be_complete();

        DROP TRIGGER IF EXISTS ledger_allocation_approval_guard_trigger
          ON ledger_entry;
        DROP FUNCTION IF EXISTS ledger_allocation_approval_guard();
        DROP INDEX IF EXISTS ledger_allocation_approved_subject_uidx;

        DROP TRIGGER IF EXISTS allocation_signed_guard_trigger ON allocation;
        DROP FUNCTION IF EXISTS allocation_signed_guard();
        DROP INDEX IF EXISTS allocation_plan_uidx;

        ALTER TABLE allocation
          DROP CONSTRAINT IF EXISTS allocation_release_policy_chk,
          DROP CONSTRAINT IF EXISTS allocation_verification_snapshot_hash_check,
          DROP CONSTRAINT IF EXISTS allocation_verification_id_fkey,
          DROP CONSTRAINT IF EXISTS allocation_plan_id_fkey,
          DROP CONSTRAINT IF EXISTS allocation_claim_id_fkey,
          ALTER COLUMN plan_id DROP NOT NULL,
          DROP COLUMN IF EXISTS verification_snapshot_hash,
          DROP COLUMN IF EXISTS verification_id,
          ADD CONSTRAINT allocation_plan_id_fkey
            FOREIGN KEY (plan_id) REFERENCES allocation_plan(id) ON DELETE SET NULL,
          ADD CONSTRAINT allocation_claim_id_fkey
            FOREIGN KEY (claim_id) REFERENCES claim(id) ON DELETE CASCADE,
          ADD CONSTRAINT allocation_check
            CHECK ((resource = 'CASH') = (amount IS NOT NULL)),
          ADD CONSTRAINT allocation_check1
            CHECK (
              (resource = 'ITEM') = (sku IS NOT NULL AND quantity IS NOT NULL)
            );

        DROP TRIGGER IF EXISTS allocation_plan_signed_guard_trigger
          ON allocation_plan;
        DROP FUNCTION IF EXISTS allocation_plan_signed_guard();
        DROP INDEX IF EXISTS allocation_plan_approval_uidx;
        ALTER TABLE allocation_plan
          DROP CONSTRAINT IF EXISTS allocation_plan_approval_id_fkey,
          ADD CONSTRAINT allocation_plan_approval_id_fkey
            FOREIGN KEY (approval_id) REFERENCES approval(id);

        DROP TRIGGER IF EXISTS verification_immutable_guard_trigger
          ON verification;
        DROP FUNCTION IF EXISTS verification_immutable_guard();
        DROP TRIGGER IF EXISTS verification_snapshot_guard_trigger
          ON verification;
        DROP FUNCTION IF EXISTS verification_snapshot_guard();
        DROP FUNCTION IF EXISTS verification_snapshot_digest(verification);
        ALTER TABLE verification
          DROP CONSTRAINT IF EXISTS verification_snapshot_hash_check,
          DROP COLUMN IF EXISTS snapshot_hash;
        """
    )
