-- Function bodies as they stood before 0017, vendored so the downgrade
-- does not depend on a future schema.sql that no longer contains them.

CREATE OR REPLACE FUNCTION approval_role_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
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
     AND signer_role NOT IN ('FINANCE_OFFICER', 'DIRECTOR') THEN
    RAISE EXCEPTION
      'gate DISBURSEMENT_BATCH requires FINANCE_OFFICER or DIRECTOR, got %',
      signer_role;
  END IF;

  NEW.approved_at := statement_timestamp();
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION allocation_signed_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF NEW.amount IS NULL OR NEW.amount <= 0
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
END $$;
