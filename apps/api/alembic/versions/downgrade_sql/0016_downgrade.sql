-- Function bodies as they stood before 0016, when the flat grant was
-- pinned in every guard. Vendored for the same reason as 0017's.

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
END $$;

CREATE OR REPLACE FUNCTION ledger_allocation_approval_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
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
END $$;

CREATE OR REPLACE FUNCTION disbursement_batch_signed_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  expected_hash text;
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    RAISE EXCEPTION 'signed disbursement batch is immutable';
  END IF;

  IF NEW.total IS DISTINCT FROM 45000.00
     OR NEW.channel NOT IN ('BANK', 'MOBILE_MONEY', 'VOUCHER') THEN
    RAISE EXCEPTION 'disbursement batch is outside the fixed release policy';
  END IF;
  IF NOT EXISTS (
    SELECT 1
      FROM approval a
     WHERE a.id = NEW.approval_id
       AND a.gate = 'DISBURSEMENT_BATCH'
       AND a.subject_type = 'disbursement_batch'
       AND a.subject_id = NEW.id
       AND a.role_at_time IN ('FINANCE_OFFICER', 'DIRECTOR')
  ) THEN
    RAISE EXCEPTION
      'batch requires an exact Finance Officer or Director signature';
  END IF;

  expected_hash := disbursement_batch_snapshot_digest(NEW);
  IF NEW.snapshot_hash IS NOT NULL
     AND NEW.snapshot_hash IS DISTINCT FROM expected_hash THEN
    RAISE EXCEPTION 'disbursement batch snapshot hash is invalid';
  END IF;
  NEW.snapshot_hash := expected_hash;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION disbursement_lifecycle_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  batch_row disbursement_batch%ROWTYPE;
  allocation_row allocation%ROWTYPE;
  expected_hash text;
  executor_role app_role;
  executor_active boolean;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'disbursement evidence is immutable';
  END IF;

  IF TG_OP = 'INSERT' THEN
    SELECT * INTO batch_row FROM disbursement_batch WHERE id = NEW.batch_id;
    SELECT * INTO allocation_row FROM allocation WHERE id = NEW.allocation_id;
    IF batch_row.id IS NULL OR allocation_row.id IS NULL
       OR NEW.approval_id IS DISTINCT FROM batch_row.approval_id
       OR NEW.channel IS DISTINCT FROM batch_row.channel
       OR batch_row.total IS DISTINCT FROM allocation_row.amount
       OR allocation_row.amount IS DISTINCT FROM 45000.00
       OR allocation_row.currency <> 'JMD'
       OR allocation_row.resource <> 'CASH'
       OR allocation_row.payer_route <> 'GOV_RELIEF' THEN
      RAISE EXCEPTION 'disbursement is not exactly bound to its signed allocation';
    END IF;
    IF NEW.status <> 'PENDING'
       OR NOT NEW.simulated
       OR NEW.executor_provider <> 'LIGHTHOUSE_DEMO_EXECUTOR_V1'
       OR NEW.execution_requested_by IS NOT NULL
       OR NEW.execution_idempotency_key IS NOT NULL
       OR NEW.execution_request_hash IS NOT NULL
       OR NEW.external_ref IS NOT NULL
       OR NEW.provider_confirmation_hash IS NOT NULL
       OR NEW.executed_at IS NOT NULL
       OR NEW.confirmed_at IS NOT NULL
       OR NEW.failure_reason IS NOT NULL THEN
      RAISE EXCEPTION 'new disbursement must be a pending demo instruction';
    END IF;

    expected_hash := disbursement_snapshot_digest(NEW);
    IF NEW.snapshot_hash IS NOT NULL
       AND NEW.snapshot_hash IS DISTINCT FROM expected_hash THEN
      RAISE EXCEPTION 'disbursement snapshot hash is invalid';
    END IF;
    NEW.snapshot_hash := expected_hash;
    RETURN NEW;
  END IF;

  IF NEW.id IS DISTINCT FROM OLD.id
     OR NEW.allocation_id IS DISTINCT FROM OLD.allocation_id
     OR NEW.batch_id IS DISTINCT FROM OLD.batch_id
     OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
     OR NEW.channel IS DISTINCT FROM OLD.channel
     OR NEW.simulated IS DISTINCT FROM OLD.simulated
     OR NEW.executor_provider IS DISTINCT FROM OLD.executor_provider
     OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash THEN
    RAISE EXCEPTION 'disbursement signed bindings are immutable';
  END IF;

  IF OLD.status = 'PENDING' AND NEW.status = 'EXECUTING' THEN
    SELECT role, active INTO executor_role, executor_active
      FROM app_user WHERE id = NEW.execution_requested_by;
    IF NOT FOUND OR NOT executor_active
       OR executor_role NOT IN ('FINANCE_OFFICER', 'DIRECTOR') THEN
      RAISE EXCEPTION
        'disbursement execution requires an active Finance Officer or Director';
    END IF;
    IF NEW.execution_idempotency_key IS NULL
       OR NEW.execution_request_hash IS NULL
       OR NEW.executed_at IS NULL
       OR NEW.external_ref IS NOT NULL
       OR NEW.provider_confirmation_hash IS NOT NULL
       OR NEW.confirmed_at IS NOT NULL
       OR NEW.failure_reason IS NOT NULL THEN
      RAISE EXCEPTION 'executing disbursement is missing its idempotent intent';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.status = 'EXECUTING' AND NEW.status = 'CONFIRMED' THEN
    IF NEW.execution_requested_by IS DISTINCT FROM OLD.execution_requested_by
       OR NEW.execution_idempotency_key
            IS DISTINCT FROM OLD.execution_idempotency_key
       OR NEW.execution_request_hash IS DISTINCT FROM OLD.execution_request_hash
       OR NEW.executed_at IS DISTINCT FROM OLD.executed_at
       OR NEW.external_ref !~ '^DEMO-[0-9A-F]{24}$'
       OR NEW.provider_confirmation_hash IS NULL
       OR NEW.confirmed_at IS NULL
       OR NEW.confirmed_at < NEW.executed_at
       OR NEW.failure_reason IS NOT NULL THEN
      RAISE EXCEPTION 'confirmation is not bound to the executed demo intent';
    END IF;
    RETURN NEW;
  END IF;

  IF OLD.status = 'EXECUTING' AND NEW.status = 'FAILED' THEN
    IF NEW.execution_requested_by IS DISTINCT FROM OLD.execution_requested_by
       OR NEW.execution_idempotency_key
            IS DISTINCT FROM OLD.execution_idempotency_key
       OR NEW.execution_request_hash IS DISTINCT FROM OLD.execution_request_hash
       OR NEW.executed_at IS DISTINCT FROM OLD.executed_at
       OR NEW.external_ref IS NOT NULL
       OR NEW.provider_confirmation_hash IS NOT NULL
       OR NEW.confirmed_at IS NOT NULL
       OR NEW.failure_reason IS NULL THEN
      RAISE EXCEPTION 'failed execution is not bound to its demo intent';
    END IF;
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'illegal disbursement lifecycle transition % -> %',
    OLD.status, NEW.status;
END $$;

CREATE OR REPLACE FUNCTION ledger_disbursement_receipt_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  disbursement_row disbursement%ROWTYPE;
  batch_row disbursement_batch%ROWTYPE;
  approval_row approval%ROWTYPE;
  allocation_row allocation%ROWTYPE;
  expected_keys text[];
BEGIN
  IF NEW.action NOT IN (
    'disbursement.batch_signed',
    'disbursement.executed',
    'disbursement.confirmed',
    'disbursement.failed'
  ) THEN
    RETURN NEW;
  END IF;

  IF NEW.action = 'disbursement.batch_signed' THEN
    IF NEW.subject_type <> 'disbursement_batch' OR NEW.subject_id IS NULL THEN
      RAISE EXCEPTION 'batch signature ledger subject is invalid';
    END IF;
    SELECT * INTO batch_row FROM disbursement_batch WHERE id = NEW.subject_id;
    SELECT * INTO disbursement_row
      FROM disbursement WHERE batch_id = batch_row.id;
  ELSE
    IF NEW.subject_type <> 'disbursement' OR NEW.subject_id IS NULL THEN
      RAISE EXCEPTION 'disbursement ledger subject is invalid';
    END IF;
    SELECT * INTO disbursement_row FROM disbursement WHERE id = NEW.subject_id;
    SELECT * INTO batch_row
      FROM disbursement_batch WHERE id = disbursement_row.batch_id;
  END IF;
  SELECT * INTO approval_row FROM approval WHERE id = batch_row.approval_id;
  SELECT * INTO allocation_row
    FROM allocation WHERE id = disbursement_row.allocation_id;

  IF disbursement_row.id IS NULL
     OR batch_row.id IS NULL
     OR approval_row.id IS NULL
     OR allocation_row.id IS NULL
     OR NEW.payload ->> 'approval_id' IS DISTINCT FROM approval_row.id::text
     OR NEW.payload ->> 'batch_id' IS DISTINCT FROM batch_row.id::text
     OR NEW.payload ->> 'batch_snapshot_hash'
          IS DISTINCT FROM batch_row.snapshot_hash
     OR NEW.payload ->> 'disbursement_id'
          IS DISTINCT FROM disbursement_row.id::text
     OR NEW.payload ->> 'disbursement_snapshot_hash'
          IS DISTINCT FROM disbursement_row.snapshot_hash
     OR NEW.payload ->> 'allocation_id'
          IS DISTINCT FROM allocation_row.id::text
     OR NEW.payload ->> 'allocation_verification_snapshot_hash'
          IS DISTINCT FROM allocation_row.verification_snapshot_hash
     OR NEW.payload ->> 'gate' IS DISTINCT FROM 'DISBURSEMENT_BATCH'
     OR NEW.payload ->> 'resource' IS DISTINCT FROM 'CASH'
     OR NEW.payload ->> 'amount' IS DISTINCT FROM '45000.00'
     OR NEW.payload ->> 'currency' IS DISTINCT FROM 'JMD'
     OR NEW.payload ->> 'payer_route' IS DISTINCT FROM 'GOV_RELIEF'
     OR NEW.payload ->> 'channel' IS DISTINCT FROM disbursement_row.channel::text
     OR NEW.payload ->> 'executor_provider'
          IS DISTINCT FROM 'LIGHTHOUSE_DEMO_EXECUTOR_V1'
     OR NEW.payload ->> 'executor_provenance' IS DISTINCT FROM 'SIMULATED_DEMO'
     OR jsonb_typeof(NEW.payload -> 'simulated') IS DISTINCT FROM 'boolean'
     OR (NEW.payload ->> 'simulated')::boolean IS DISTINCT FROM true
     OR NEW.payload ->> 'event' IS DISTINCT FROM NEW.action THEN
    RAISE EXCEPTION 'disbursement ledger payload is not bound to signed records';
  END IF;

  expected_keys := ARRAY[
    'approval_id', 'batch_id', 'batch_snapshot_hash', 'disbursement_id',
    'disbursement_snapshot_hash', 'allocation_id',
    'allocation_verification_snapshot_hash', 'gate', 'resource', 'amount',
    'currency', 'payer_route', 'channel', 'executor_provider',
    'executor_provenance', 'simulated', 'money_movement', 'event'
  ];

  IF NEW.action = 'disbursement.batch_signed' THEN
    IF NEW.actor_kind <> 'HUMAN'
       OR NEW.actor_id IS DISTINCT FROM approval_row.approved_by
       OR NEW.agent_name IS NOT NULL
       OR disbursement_row.status <> 'PENDING'
       OR NEW.payload ->> 'money_movement'
            IS DISTINCT FROM 'NOT_INITIATED_AT_BATCH_SIGNATURE'
       OR (SELECT count(*) FROM jsonb_object_keys(NEW.payload))
            <> cardinality(expected_keys)
       OR NOT (NEW.payload ?& expected_keys) THEN
      RAISE EXCEPTION 'batch signature ledger receipt is invalid';
    END IF;
    RETURN NEW;
  END IF;

  expected_keys := expected_keys || ARRAY['execution_request_hash'];
  IF NEW.payload ->> 'execution_request_hash'
       IS DISTINCT FROM disbursement_row.execution_request_hash THEN
    RAISE EXCEPTION 'execution ledger request identity is invalid';
  END IF;

  IF NEW.action = 'disbursement.executed' THEN
    IF NEW.actor_kind <> 'HUMAN'
       OR NEW.actor_id IS DISTINCT FROM disbursement_row.execution_requested_by
       OR NEW.agent_name IS NOT NULL
       OR disbursement_row.status <> 'EXECUTING'
       OR NEW.payload ->> 'money_movement'
            IS DISTINCT FROM 'SIMULATION_EXECUTED_NO_REAL_FUNDS'
       OR (SELECT count(*) FROM jsonb_object_keys(NEW.payload))
            <> cardinality(expected_keys)
       OR NOT (NEW.payload ?& expected_keys) THEN
      RAISE EXCEPTION 'simulated execution ledger receipt is invalid';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.action = 'disbursement.failed' THEN
    RAISE EXCEPTION 'failed execution receipts are not implemented in this release';
  END IF;

  expected_keys := expected_keys
    || ARRAY['provider_confirmation_ref', 'provider_confirmation_hash'];
  IF NEW.actor_kind <> 'AGENT'
     OR NEW.actor_id IS NOT NULL
     OR NEW.agent_name IS DISTINCT FROM 'ledger_agent'
     OR disbursement_row.status <> 'CONFIRMED'
     OR NEW.payload ->> 'provider_confirmation_ref'
          IS DISTINCT FROM disbursement_row.external_ref
     OR NEW.payload ->> 'provider_confirmation_hash'
          IS DISTINCT FROM disbursement_row.provider_confirmation_hash
     OR NEW.payload ->> 'money_movement'
          IS DISTINCT FROM 'SIMULATED_CONFIRMATION_RECORDED_NO_REAL_FUNDS'
     OR (SELECT count(*) FROM jsonb_object_keys(NEW.payload))
          <> cardinality(expected_keys)
     OR NOT (NEW.payload ?& expected_keys) THEN
    RAISE EXCEPTION 'simulated confirmation ledger receipt is invalid';
  END IF;
  RETURN NEW;
END $$;
