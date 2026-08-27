"""Widen the allocation path to carry PAY-06's goods half.

Revision ID: 0011_allocation_goods
Revises: 0010_damage_assessment
Create Date: 2026-08-27

0007 hardened Act 3 around the one path the demo walks: a signed plan releases
exactly one flat cash grant. Five guards said so — a CHECK constraint, a unique
index, the signed-plan completeness trigger, the signed-allocation guard, and
the ledger receipt guard. That is half of PAY-06.

The other half — "goods tiered by triage severity (URGENT / HIGH / MED
baskets)" — was structurally impossible to record. ``ResourceKind.ITEM``,
``allocation.sku``, ``allocation.quantity``, ``allocation.warehouse_id`` and
the whole ``warehouse``/``stock_item`` pair existed and could never be used,
and LGX-01's "decremented by approved allocations" had nothing to decrement.

This widens all five, and widens them narrowly. Cash is untouched: still
exactly 45000.00 JMD, still GOV_RELIEF, still one per signed plan, still with
every goods column null. What changes is that an ITEM row beside it is legal,
provided it names a SKU, a positive count, and the warehouse the stock leaves —
and provided that stock is on the shelf. Donation pools stay barred on both
halves until DON lands, and both stay pinned to GOV_RELIEF until RTE does, so
nothing can quietly record that an insurer bought a tarpaulin.

``schema.sql`` is the canonical current schema and 0001 applies it wholesale,
so a fresh database arrives here already widened; this exists for a database
deployed against the narrow version, and checks first. The function bodies
below are copied verbatim from schema.sql so the two cannot drift.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0011_allocation_goods"
down_revision = "0010_damage_assessment"
branch_labels = None
depends_on = None


def _already_widened() -> bool:
    return bool(
        op.get_bind()
        .execute(
            text(
                """
                SELECT 1 FROM pg_indexes
                 WHERE schemaname = current_schema()
                   AND indexname = 'allocation_plan_cash_uidx'
                """
            )
        )
        .first()
    )


def upgrade() -> None:
    if _already_widened():
        return
    op.execute(
        """
ALTER TABLE allocation
  DROP CONSTRAINT allocation_release_policy_chk,
  ADD CONSTRAINT allocation_release_policy_chk CHECK (
    (
      resource = 'CASH'
      AND amount = 45000.00
      AND currency = 'JMD'
      AND payer_route = 'GOV_RELIEF'
      AND sku IS NULL
      AND quantity IS NULL
      AND pool_id IS NULL
      AND warehouse_id IS NULL
    ) OR (
      resource = 'ITEM'
      AND amount IS NULL
      AND payer_route = 'GOV_RELIEF'
      AND sku IS NOT NULL
      AND quantity IS NOT NULL
      AND quantity > 0
      AND warehouse_id IS NOT NULL
      AND pool_id IS NULL
    )
  );

DROP INDEX IF EXISTS allocation_plan_uidx;
CREATE UNIQUE INDEX allocation_plan_cash_uidx
  ON allocation (plan_id) WHERE resource = 'CASH';
CREATE UNIQUE INDEX allocation_plan_item_uidx
  ON allocation (plan_id, sku) WHERE resource = 'ITEM';

CREATE OR REPLACE FUNCTION signed_plan_must_be_complete()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
  -- The invariant is that a signature releases at most one grant, not that it
  -- releases exactly one row. A plan carrying only goods is legitimate — not
  -- every household on a run sheet is a cash stop — but a plan carrying two
  -- cash grants is a double payment, and a signed plan releasing nothing at
  -- all is a signature over an empty set.
  IF NEW.approval_id IS NOT NULL THEN
    IF (SELECT count(*) FROM allocation WHERE plan_id = NEW.id) = 0 THEN
      RAISE EXCEPTION 'signed allocation plan must release at least one allocation';
    END IF;
    IF (
      SELECT count(*) FROM allocation
       WHERE plan_id = NEW.id AND resource = 'CASH'
    ) > 1 THEN
      RAISE EXCEPTION 'signed allocation plan cannot release two cash grants';
    END IF;
  END IF;
  RETURN NULL;
END $function$;

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

  IF NEW.resource = 'CASH' THEN
    IF NEW.amount IS DISTINCT FROM 45000.00
       OR NEW.currency <> 'JMD'
       OR NEW.payer_route <> 'GOV_RELIEF'
       OR NEW.sku IS NOT NULL
       OR NEW.quantity IS NOT NULL
       OR NEW.pool_id IS NOT NULL
       OR NEW.warehouse_id IS NOT NULL THEN
      RAISE EXCEPTION 'allocation does not match the fixed release grant policy';
    END IF;
  ELSIF NEW.resource = 'ITEM' THEN
    IF NEW.amount IS NOT NULL
       OR NEW.payer_route <> 'GOV_RELIEF'
       OR NEW.sku IS NULL
       OR NEW.quantity IS NULL
       OR NEW.quantity <= 0
       OR NEW.pool_id IS NOT NULL
       OR NEW.warehouse_id IS NULL THEN
      RAISE EXCEPTION 'allocation does not match the tiered goods policy';
    END IF;
    -- LGX-01. Signing for stock that is not on the shelf is the failure this
    -- catches; the decrement's own ``quantity >= 0`` check catches the race.
    IF NOT EXISTS (
      SELECT 1 FROM stock_item s
       WHERE s.warehouse_id = NEW.warehouse_id
         AND s.sku = NEW.sku
         AND s.quantity >= NEW.quantity
    ) THEN
      RAISE EXCEPTION 'goods allocation exceeds the stock on hand';
    END IF;
  ELSE
    RAISE EXCEPTION 'allocation resource is not releasable';
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
     OR NEW.verification_snapshot_hash IS DISTINCT FROM verification_row.snapshot_hash THEN
    RAISE EXCEPTION 'allocation must bind the latest verification and its snapshot hash';
  END IF;

  IF jsonb_typeof(verification_row.signals) IS DISTINCT FROM 'object'
     OR (SELECT count(*) FROM jsonb_object_keys(verification_row.signals)) <> 5
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
       OR jsonb_typeof(verification_row.signals -> signal_name -> 'present')
         IS DISTINCT FROM 'boolean' THEN
      RAISE EXCEPTION 'verification signal presence is not boolean';
    END IF;
    signal_present :=
      (verification_row.signals -> signal_name ->> 'present')::boolean;
    IF signal_present THEN
      IF jsonb_typeof(verification_row.signals -> signal_name -> 'score')
           IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION 'present verification signals require a numeric score';
      END IF;
      signal_score :=
        (verification_row.signals -> signal_name ->> 'score')::numeric;
      IF signal_score < 0 OR signal_score > 1 THEN
        RAISE EXCEPTION 'verification signal score is outside 0..1';
      END IF;
    ELSIF (verification_row.signals -> signal_name) ? 'score'
          AND verification_row.signals -> signal_name -> 'score' <> 'null'::jsonb THEN
      RAISE EXCEPTION 'absent verification signals cannot assert a score';
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
       OR verification_row.agent_name IS DISTINCT FROM 'verification_agent' THEN
      RAISE EXCEPTION 'automatic verification does not meet the release threshold';
    END IF;
  ELSIF verification_row.verdict = 'APPROVED' THEN
    IF verification_row.actor_kind <> 'HUMAN'
       OR verification_row.actor_id IS NULL THEN
      RAISE EXCEPTION 'approved verification requires an identified human actor';
    END IF;
  ELSE
    RAISE EXCEPTION 'verification verdict is not eligible for allocation';
  END IF;

  RETURN NEW;
END $function$;

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
    RAISE EXCEPTION 'allocation approval ledger subject or actor is invalid';
  END IF;

  SELECT * INTO allocation_row FROM allocation WHERE id = NEW.subject_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'allocation approval ledger subject does not exist';
  END IF;
  SELECT * INTO plan_row FROM allocation_plan WHERE id = allocation_row.plan_id;
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
     OR NEW.payload ->> 'approval_id' IS DISTINCT FROM approval_row.id::text
     OR NEW.payload ->> 'plan_id' IS DISTINCT FROM plan_row.id::text
     OR NEW.payload ->> 'allocation_id' IS DISTINCT FROM allocation_row.id::text
     OR NEW.payload ->> 'claim_id' IS DISTINCT FROM allocation_row.claim_id::text
     OR NEW.payload ->> 'verification_id' IS DISTINCT FROM allocation_row.verification_id::text
     OR NEW.payload ->> 'verification_snapshot_hash' IS DISTINCT FROM verification_hash
     OR NEW.payload ->> 'verification_snapshot_hash'
          IS DISTINCT FROM allocation_row.verification_snapshot_hash
     OR NEW.payload ->> 'gate' IS DISTINCT FROM 'ALLOCATION_PLAN'
     -- The receipt states what was signed for, and the row is the referee.
     -- A cash receipt still has to read exactly 45000.00 JMD; a goods receipt
     -- has to name the SKU and count that came off the shelf.
     OR NEW.payload ->> 'resource' IS DISTINCT FROM allocation_row.resource::text
     OR (allocation_row.resource = 'CASH' AND (
          NEW.payload ->> 'amount' IS DISTINCT FROM '45000.00'
       OR NEW.payload ->> 'currency' IS DISTINCT FROM 'JMD'
       OR NEW.payload ? 'sku'
       OR NEW.payload ? 'quantity'
     ))
     OR (allocation_row.resource = 'ITEM' AND (
          NEW.payload ? 'amount'
       OR NEW.payload ->> 'sku' IS DISTINCT FROM allocation_row.sku
       OR NEW.payload ->> 'quantity' IS DISTINCT FROM allocation_row.quantity::text
       OR NEW.payload ->> 'warehouse_id'
            IS DISTINCT FROM allocation_row.warehouse_id::text
     ))
     OR NEW.payload ->> 'payer_route' IS DISTINCT FROM 'GOV_RELIEF'
     OR NEW.payload ->> 'money_movement'
          IS DISTINCT FROM 'NOT_INITIATED_AT_APPROVAL'
     OR jsonb_typeof(NEW.payload -> 'synthetic') IS DISTINCT FROM 'boolean'
     OR (NEW.payload ->> 'synthetic')::boolean IS DISTINCT FROM claim_synthetic
     OR NEW.payload ->> 'parish' IS NULL
     OR NEW.payload ->> 'parish' NOT IN (
       'Clarendon', 'Hanover', 'Kingston', 'Manchester', 'Portland',
       'Saint Andrew', 'Saint Ann', 'Saint Catherine', 'Saint Elizabeth',
       'Saint James', 'Saint Mary', 'Saint Thomas', 'Trelawny', 'UNSPECIFIED',
       'Westmoreland'
     )
     OR NEW.payload ->> 'need_category' IS NULL
     OR NEW.payload ->> 'need_category' NOT IN (
       'ACCESS_BLOCKED', 'CONTENTS_DAMAGE', 'ESSENTIAL_SERVICES',
       'FLOOD_DAMAGE', 'OTHER_DAMAGE', 'ROOF_DAMAGE', 'STRUCTURAL_DAMAGE'
     ) THEN
    RAISE EXCEPTION 'allocation approval ledger payload does not match signed records';
  END IF;

  RETURN NEW;
END $function$;
"""
    )


def downgrade() -> None:
    """Narrow the path again — but only while narrowing is still honest.

    A goods allocation that has been signed for is an immutable release
    record, and reversing past it would mean deleting one. So this refuses
    rather than destroys: clear the goods rows deliberately, or restore from a
    database branch. With none present there is nothing to lose and the narrow
    guards go back exactly as 0007 left them.
    """
    goods = (
        op.get_bind()
        .execute(text("SELECT count(*) FROM allocation WHERE resource = 'ITEM'"))
        .scalar_one()
    )
    if goods:
        raise RuntimeError(
            f"refusing to narrow the allocation path: {goods} signed goods "
            "allocation(s) exist and would have to be deleted to fit"
        )
    op.execute(
        """
ALTER TABLE allocation
  DROP CONSTRAINT allocation_release_policy_chk,
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

DROP INDEX IF EXISTS allocation_plan_cash_uidx;
DROP INDEX IF EXISTS allocation_plan_item_uidx;
CREATE UNIQUE INDEX allocation_plan_uidx ON allocation (plan_id);

CREATE OR REPLACE FUNCTION signed_plan_must_be_complete()
RETURNS trigger LANGUAGE plpgsql AS $function$
BEGIN
  IF NEW.approval_id IS NOT NULL
     AND (SELECT count(*) FROM allocation WHERE plan_id = NEW.id) <> 1 THEN
    RAISE EXCEPTION 'signed allocation plan must contain exactly one allocation';
  END IF;
  RETURN NULL;
END $function$;

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
     OR NEW.verification_snapshot_hash IS DISTINCT FROM verification_row.snapshot_hash THEN
    RAISE EXCEPTION 'allocation must bind the latest verification and its snapshot hash';
  END IF;

  IF jsonb_typeof(verification_row.signals) IS DISTINCT FROM 'object'
     OR (SELECT count(*) FROM jsonb_object_keys(verification_row.signals)) <> 5
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
       OR jsonb_typeof(verification_row.signals -> signal_name -> 'present')
         IS DISTINCT FROM 'boolean' THEN
      RAISE EXCEPTION 'verification signal presence is not boolean';
    END IF;
    signal_present :=
      (verification_row.signals -> signal_name ->> 'present')::boolean;
    IF signal_present THEN
      IF jsonb_typeof(verification_row.signals -> signal_name -> 'score')
           IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION 'present verification signals require a numeric score';
      END IF;
      signal_score :=
        (verification_row.signals -> signal_name ->> 'score')::numeric;
      IF signal_score < 0 OR signal_score > 1 THEN
        RAISE EXCEPTION 'verification signal score is outside 0..1';
      END IF;
    ELSIF (verification_row.signals -> signal_name) ? 'score'
          AND verification_row.signals -> signal_name -> 'score' <> 'null'::jsonb THEN
      RAISE EXCEPTION 'absent verification signals cannot assert a score';
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
       OR verification_row.agent_name IS DISTINCT FROM 'verification_agent' THEN
      RAISE EXCEPTION 'automatic verification does not meet the release threshold';
    END IF;
  ELSIF verification_row.verdict = 'APPROVED' THEN
    IF verification_row.actor_kind <> 'HUMAN'
       OR verification_row.actor_id IS NULL THEN
      RAISE EXCEPTION 'approved verification requires an identified human actor';
    END IF;
  ELSE
    RAISE EXCEPTION 'verification verdict is not eligible for allocation';
  END IF;

  RETURN NEW;
END $function$;

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
    RAISE EXCEPTION 'allocation approval ledger subject or actor is invalid';
  END IF;

  SELECT * INTO allocation_row FROM allocation WHERE id = NEW.subject_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'allocation approval ledger subject does not exist';
  END IF;
  SELECT * INTO plan_row FROM allocation_plan WHERE id = allocation_row.plan_id;
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
     OR NEW.payload ->> 'approval_id' IS DISTINCT FROM approval_row.id::text
     OR NEW.payload ->> 'plan_id' IS DISTINCT FROM plan_row.id::text
     OR NEW.payload ->> 'allocation_id' IS DISTINCT FROM allocation_row.id::text
     OR NEW.payload ->> 'claim_id' IS DISTINCT FROM allocation_row.claim_id::text
     OR NEW.payload ->> 'verification_id' IS DISTINCT FROM allocation_row.verification_id::text
     OR NEW.payload ->> 'verification_snapshot_hash' IS DISTINCT FROM verification_hash
     OR NEW.payload ->> 'verification_snapshot_hash'
          IS DISTINCT FROM allocation_row.verification_snapshot_hash
     OR NEW.payload ->> 'gate' IS DISTINCT FROM 'ALLOCATION_PLAN'
     OR NEW.payload ->> 'resource' IS DISTINCT FROM 'CASH'
     OR NEW.payload ->> 'amount' IS DISTINCT FROM '45000.00'
     OR NEW.payload ->> 'currency' IS DISTINCT FROM 'JMD'
     OR NEW.payload ->> 'payer_route' IS DISTINCT FROM 'GOV_RELIEF'
     OR NEW.payload ->> 'money_movement'
          IS DISTINCT FROM 'NOT_INITIATED_AT_APPROVAL'
     OR jsonb_typeof(NEW.payload -> 'synthetic') IS DISTINCT FROM 'boolean'
     OR (NEW.payload ->> 'synthetic')::boolean IS DISTINCT FROM claim_synthetic
     OR NEW.payload ->> 'parish' IS NULL
     OR NEW.payload ->> 'parish' NOT IN (
       'Clarendon', 'Hanover', 'Kingston', 'Manchester', 'Portland',
       'Saint Andrew', 'Saint Ann', 'Saint Catherine', 'Saint Elizabeth',
       'Saint James', 'Saint Mary', 'Saint Thomas', 'Trelawny', 'UNSPECIFIED',
       'Westmoreland'
     )
     OR NEW.payload ->> 'need_category' IS NULL
     OR NEW.payload ->> 'need_category' NOT IN (
       'ACCESS_BLOCKED', 'CONTENTS_DAMAGE', 'ESSENTIAL_SERVICES',
       'FLOOD_DAMAGE', 'OTHER_DAMAGE', 'ROOF_DAMAGE', 'STRUCTURAL_DAMAGE'
     ) THEN
    RAISE EXCEPTION 'allocation approval ledger payload does not match signed records';
  END IF;

  RETURN NEW;
END $function$;
"""
    )
