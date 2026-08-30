-- Function bodies as they stood before 0015, when each gate accepted only
-- the one role that owned it. Vendored for the same reason as 0016's.

CREATE OR REPLACE FUNCTION verification_snapshot_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  reviewer_role app_role;
  reviewer_active boolean;
  parent verification%ROWTYPE;
  latest_verification_id uuid;
BEGIN
  IF NEW.actor_kind = 'HUMAN' THEN
    SELECT role, active INTO reviewer_role, reviewer_active
      FROM app_user WHERE id = NEW.actor_id;
    IF NOT FOUND OR NOT reviewer_active OR reviewer_role <> 'REVIEW_CLERK' THEN
      RAISE EXCEPTION
        'human verification verdicts require an active REVIEW_CLERK';
    END IF;
    IF NEW.agent_name IS NOT NULL THEN
      RAISE EXCEPTION 'human verification verdicts cannot assert an agent name';
    END IF;
    IF NEW.verdict NOT IN ('APPROVED', 'REJECTED') THEN
      RAISE EXCEPTION 'human verification verdict must be APPROVED or REJECTED';
    END IF;
    IF NEW.overrides_id IS NULL THEN
      RAISE EXCEPTION
        'verification override must bind latest agent review evidence';
    END IF;

    SELECT * INTO parent FROM verification WHERE id = NEW.overrides_id;
    IF NOT FOUND
       OR parent.claim_id IS DISTINCT FROM NEW.claim_id
       OR parent.actor_kind <> 'AGENT'
       OR parent.actor_id IS NOT NULL
       OR parent.agent_name IS DISTINCT FROM 'verification_agent'
       OR parent.verdict NOT IN ('REVIEW', 'FLAGGED')
       OR parent.overrides_id IS NOT NULL THEN
      RAISE EXCEPTION
        'verification override must bind latest agent review evidence';
    END IF;

    SELECT id INTO latest_verification_id
      FROM verification
     WHERE claim_id = NEW.claim_id
     ORDER BY created_at DESC, id DESC
     LIMIT 1;
    IF latest_verification_id IS DISTINCT FROM parent.id THEN
      RAISE EXCEPTION
        'verification override must bind latest agent review evidence';
    END IF;

    IF NEW.signals IS DISTINCT FROM parent.signals
       OR NEW.confidence IS DISTINCT FROM parent.confidence
       OR NEW.model_version IS DISTINCT FROM parent.model_version
       OR NEW.threshold_version IS DISTINCT FROM parent.threshold_version
       OR NEW.capped IS DISTINCT FROM parent.capped THEN
      RAISE EXCEPTION
        'verification override must copy parent evidence snapshot';
    END IF;
  ELSIF NEW.actor_kind = 'AGENT' THEN
    IF NEW.actor_id IS NOT NULL
       OR NEW.agent_name IS DISTINCT FROM 'verification_agent' THEN
      RAISE EXCEPTION
        'agent verification verdicts require verification_agent authority';
    END IF;
    IF NEW.overrides_id IS NOT NULL THEN
      RAISE EXCEPTION 'agent verification verdicts cannot override another row';
    END IF;
    IF NEW.verdict NOT IN ('AUTO_VERIFIED', 'REVIEW', 'FLAGGED') THEN
      RAISE EXCEPTION
        'agent verification verdict must be AUTO_VERIFIED, REVIEW, or FLAGGED';
    END IF;
  ELSE
    RAISE EXCEPTION 'system actors cannot issue verification verdicts';
  END IF;

  NEW.snapshot_hash := verification_snapshot_digest(NEW);
  RETURN NEW;
END $$;

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
     AND signer_role <> 'FINANCE_OFFICER' THEN
    RAISE EXCEPTION
      'gate DISBURSEMENT_BATCH requires FINANCE_OFFICER, got %', signer_role;
  END IF;

  NEW.approved_at := statement_timestamp();
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
       AND a.role_at_time = 'FINANCE_OFFICER'
  ) THEN
    RAISE EXCEPTION 'batch requires an exact Finance Officer signature';
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
    IF NOT FOUND OR NOT executor_active OR executor_role <> 'FINANCE_OFFICER' THEN
      RAISE EXCEPTION 'disbursement execution requires an active Finance Officer';
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
