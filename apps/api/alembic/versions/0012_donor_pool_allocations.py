"""Let a donation pool fund an allocation (DON-03).

Revision ID: 0012_donor_pool_allocations
Revises: 0011_allocation_goods
Create Date: 2026-08-27

0011 widened allocations to carry goods and said pools would stay barred until
DON landed. DON has landed.

RTE-01 describes DONOR_POOL as "orthogonal to the other two as a funding
source": it answers who paid, not who carries the loss. So it is a payer route
like GOV_RELIEF, and the pool it draws on is required exactly when it is the
route and barred otherwise — a relief-funded allocation naming a pool would be
claiming a donor paid for something a donor did not.

INSURER and BOTH remain absent from the allocation path on purpose. An insurer
settles with its policyholder; it does not fund a relief allocation. Routing a
claim to a carrier (RTE) and funding a basket (DON) are different questions
that happen to share an enum.

``donation_pool.balance`` gains a ``>= 0`` check, for the reason
``stock_item.quantity`` already has one: it is the last thing standing between
a signed allocation and money that was never given. The signed-allocation
guard refuses a pool that cannot cover the grant, and the check catches the
race the guard cannot.

As with 0010 and 0011, ``schema.sql`` is canonical and 0001 applies it
wholesale, so a fresh database arrives here already widened and this checks
first.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0012_donor_pool_allocations"
down_revision = "0011_allocation_goods"
branch_labels = None
depends_on = None


def _already_widened() -> bool:
    return bool(
        op.get_bind()
        .execute(
            text(
                """
                SELECT 1 FROM pg_constraint
                 WHERE conname = 'allocation_payer_source_chk'
                   AND connamespace = current_schema()::regnamespace
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
ALTER TABLE donation_pool
  ADD CONSTRAINT donation_pool_balance_chk CHECK (balance >= 0);

ALTER TABLE allocation
  DROP CONSTRAINT allocation_release_policy_chk,
  ADD CONSTRAINT allocation_release_policy_chk CHECK (
    (
      resource = 'CASH'
      AND amount = 45000.00
      AND currency = 'JMD'
      AND sku IS NULL
      AND quantity IS NULL
      AND warehouse_id IS NULL
    ) OR (
      resource = 'ITEM'
      AND amount IS NULL
      AND sku IS NOT NULL
      AND quantity IS NOT NULL
      AND quantity > 0
      AND warehouse_id IS NOT NULL
    )
  ),
  ADD CONSTRAINT allocation_payer_source_chk CHECK (
    (payer_route = 'GOV_RELIEF' AND pool_id IS NULL)
    OR (payer_route = 'DONOR_POOL' AND pool_id IS NOT NULL)
  );

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
       OR NEW.sku IS NOT NULL
       OR NEW.quantity IS NOT NULL
       OR NEW.warehouse_id IS NOT NULL THEN
      RAISE EXCEPTION 'allocation does not match the fixed release grant policy';
    END IF;
  ELSIF NEW.resource = 'ITEM' THEN
    IF NEW.amount IS NOT NULL
       OR NEW.sku IS NULL
       OR NEW.quantity IS NULL
       OR NEW.quantity <= 0
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

  IF NEW.payer_route = 'DONOR_POOL' THEN
    -- DON-03. Signing against a pool that cannot cover it is the failure this
    -- catches; the draw-down's own ``balance >= 0`` check catches the race.
    IF NEW.pool_id IS NULL THEN
      RAISE EXCEPTION 'donor-funded allocation must name a pool';
    END IF;
    IF NEW.resource = 'CASH' AND NOT EXISTS (
      SELECT 1 FROM donation_pool p
       WHERE p.id = NEW.pool_id AND p.balance >= NEW.amount
    ) THEN
      RAISE EXCEPTION 'donor pool cannot cover this allocation';
    END IF;
  ELSIF NEW.payer_route <> 'GOV_RELIEF' THEN
    RAISE EXCEPTION 'allocation payer route is not a funding source';
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
     OR NEW.payload ->> 'payer_route'
          IS DISTINCT FROM allocation_row.payer_route::text
     OR (allocation_row.payer_route = 'DONOR_POOL'
         AND NEW.payload ->> 'pool_id' IS DISTINCT FROM allocation_row.pool_id::text)
     OR (allocation_row.payer_route = 'GOV_RELIEF' AND NEW.payload ? 'pool_id')
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
    """Bar pools again, but only while no allocation is drawing on one.

    Restores 0011's guards verbatim rather than merely dropping the new
    constraint: the payer route was pinned to GOV_RELIEF *inside* the release
    check before this migration moved it out, so dropping alone would leave
    the route unconstrained, which is looser than either state.
    """
    funded = (
        op.get_bind()
        .execute(
            text("SELECT count(*) FROM allocation WHERE payer_route = 'DONOR_POOL'")
        )
        .scalar_one()
    )
    if funded:
        raise RuntimeError(
            f"refusing to bar donor pools: {funded} signed allocation(s) draw on "
            "one and would have to be deleted to fit"
        )
    op.execute(
        """
ALTER TABLE allocation
  DROP CONSTRAINT allocation_payer_source_chk,
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

ALTER TABLE donation_pool DROP CONSTRAINT IF EXISTS donation_pool_balance_chk;

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
