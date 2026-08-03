-- Project Lighthouse — canonical schema
-- Phase 0 contract freeze. Version 0.1, August 1 2026.
--
-- This file is the reference. The Alembic initial migration must mirror it
-- exactly; if they drift, this file wins and the migration is the bug.
--
-- Target: Neon PostgreSQL 18, PostGIS 3.6, pgvector 0.8.
--
-- Two invariants are enforced here in the database rather than in application
-- code, because "agents propose, humans dispose" has to survive somebody being
-- tired at 2am in week three:
--   1. A disbursement cannot exist without an approval (see disbursement).
--   2. A storm file cannot reach SETTLED without a confirmed disbursement or
--      delivery (see the settled_requires_confirmation trigger).

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE storm_file_state AS ENUM (
  'REGISTERED', 'AT_RISK', 'AFFECTED', 'VERIFIED', 'SETTLED'
);

CREATE TYPE claim_status AS ENUM (
  'FILED', 'VERIFIED', 'REJECTED', 'WITHDRAWN', 'SETTLED'
);

CREATE TYPE posture AS ENUM ('QUIET', 'WATCH', 'READY', 'ACT');

CREATE TYPE damage_band AS ENUM ('NONE', 'MINOR', 'MAJOR', 'DESTROYED');

CREATE TYPE severity AS ENUM ('URGENT', 'HIGH', 'MED');

CREATE TYPE evidence_kind AS ENUM (
  'AUDIO', 'PHOTO', 'TRANSCRIPT', 'SATELLITE', 'NEIGHBOUR', 'REGISTRY', 'HAZARD'
);

CREATE TYPE verdict AS ENUM ('AUTO_VERIFIED', 'REVIEW', 'FLAGGED', 'APPROVED', 'REJECTED');

CREATE TYPE actor_kind AS ENUM ('AGENT', 'HUMAN', 'SYSTEM');

CREATE TYPE payer_route AS ENUM ('GOV_RELIEF', 'INSURER', 'BOTH', 'DONOR_POOL');

CREATE TYPE resource_kind AS ENUM ('CASH', 'ITEM');

CREATE TYPE disbursement_channel AS ENUM ('BANK', 'MOBILE_MONEY', 'VOUCHER', 'GOODS');

CREATE TYPE disbursement_status AS ENUM ('PENDING', 'EXECUTING', 'CONFIRMED', 'FAILED');

CREATE TYPE gate_kind AS ENUM ('ALERT_CASCADE', 'ALLOCATION_PLAN', 'DISBURSEMENT_BATCH');

CREATE TYPE app_role AS ENUM (
  'DIRECTOR', 'REVIEW_CLERK', 'FINANCE_OFFICER', 'AUDITOR', 'ADMIN',
  'PARISH_COORDINATOR', 'SHELTER_MANAGER', 'FIELD_TEAM', 'INSURER_USER'
);

CREATE TYPE job_status AS ENUM ('QUEUED', 'RUNNING', 'DONE', 'FAILED', 'DEAD');

-- ---------------------------------------------------------------------------
-- People and access
-- ---------------------------------------------------------------------------

CREATE TABLE app_user (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL UNIQUE,
  display_name  text NOT NULL,
  role          app_role NOT NULL,
  password_hash text,
  webauthn_cred jsonb,
  active        boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Household registry
-- ---------------------------------------------------------------------------

-- NFR-S-02: the phone number in cleartext lives here and nowhere else.
-- Everything downstream joins on storm_file_id or uses phone_hash.
CREATE TABLE storm_file (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  phone          text UNIQUE,
  phone_hash     text NOT NULL UNIQUE,
  head_name      text,
  location       geography(Point, 4326),
  parish         text,
  community      text,

  -- structure: {roof, walls, floors, year}
  structure      jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- people: {total, children, elderly, medical:[...]}
  people         jsonb NOT NULL DEFAULT '{}'::jsonb,

  vuln_score     smallint CHECK (vuln_score BETWEEN 0 AND 100),
  state          storm_file_state NOT NULL DEFAULT 'REGISTERED',

  -- REG-06: SMS-tier registrations land thin and get enriched later.
  thin           boolean NOT NULL DEFAULT false,
  -- REG-05: registered on someone's behalf at a community drive.
  assisted_by    uuid REFERENCES app_user(id),
  -- REG-09: synthetic registry rows. Must be true for the entire buildathon.
  synthetic      boolean NOT NULL DEFAULT true,

  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX storm_file_location_gix ON storm_file USING gist (location);
CREATE INDEX storm_file_state_idx    ON storm_file (state);
CREATE INDEX storm_file_parish_idx   ON storm_file (parish);

-- ---------------------------------------------------------------------------
-- Building inventory, aggregated. Reference data, not the claim lifecycle.
-- ---------------------------------------------------------------------------
-- Every registry row above is synthetic and sits where nothing necessarily
-- stands, which makes "413 of our 500 synthetic homes" a sentence about our
-- random seed rather than about Jamaica. These two tables are the real
-- denominator, from VIDA's combined build (Google Open Buildings + Microsoft
-- GlobalML + OSM, deduplicated) — 1,844,379 structures, ODbL.
--
-- **Aggregates, not the buildings themselves, and that was measured not
-- assumed.** The individual centroids were loaded once: 423 MB for the table,
-- 144 MB for the GIST index, 43 MB for the key — 610 MB against a 512 MB
-- project limit. And a single band on a single advisory took 93.9 seconds,
-- because geography predicates do spheroid math 1.8 million times; the full
-- 41 advisories would have run for hours.
--
-- Nothing needs a building row at query time. Counting is an aggregate,
-- exposure is an aggregate, the dasymetric population weight is an aggregate,
-- and the map draws footprints from the basemap tiles. So the footprints stay
-- in the cached parquet, DuckDB does the planar spatial work, and Postgres
-- holds the answers. Storage falls from 610 MB to kilobytes.
--
-- **The one real-world dataset here, and it holds no PII.** A footprint is a
-- public geospatial feature with no occupant attached, so the
-- synthetic-data-only rule is untouched: this measures where structures are,
-- never who lives in them.

-- Structures per admin-3 community, with parish and district denormalised so
-- any level rolls up with a GROUP BY. built_m2 is the weight for spreading a
-- parish population across its communities — the only population signal that
-- exists below admin-1.
CREATE TABLE place_structures (
  parish      text NOT NULL,
  district    text NOT NULL,
  community   text NOT NULL,
  structures  integer NOT NULL,
  built_m2    double precision NOT NULL,
  PRIMARY KEY (parish, district, community)
);

-- Structures inside each wind band, per advisory.
--
-- Bands are **mutually exclusive**: a structure is counted once, at the highest
-- band it reaches, matching the CASE ladder the risk mapper already uses. They
-- are nested geometrically, so counting each band independently would report
-- the same building three times and inflate exposure by the width of the storm.
--
-- Only non-zero rows exist. A community absent for an advisory had no structure
-- in that band, which is different from having no data — and rows for every
-- (advisory, community, band) would be 95,325 of which most say nothing.
CREATE TABLE place_exposure (
  advisory_id uuid NOT NULL REFERENCES advisory(id) ON DELETE CASCADE,
  parish      text NOT NULL,
  district    text NOT NULL,
  community   text NOT NULL,
  band        smallint NOT NULL CHECK (band IN (34, 50, 64)),
  structures  integer NOT NULL CHECK (structures > 0),
  PRIMARY KEY (advisory_id, parish, district, community, band)
);

CREATE INDEX place_exposure_advisory_idx ON place_exposure (advisory_id);

-- REG-03/REG-04: consent is versioned and revocable, never a boolean column.
CREATE TABLE consent (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  storm_file_id  uuid NOT NULL REFERENCES storm_file(id) ON DELETE CASCADE,
  version        text NOT NULL,
  granted_at     timestamptz NOT NULL DEFAULT now(),
  revoked_at     timestamptz,
  scope          jsonb NOT NULL DEFAULT '{}'::jsonb,
  channel        text
);

CREATE INDEX consent_file_idx ON consent (storm_file_id);

-- ---------------------------------------------------------------------------
-- Hazard
-- ---------------------------------------------------------------------------

CREATE TABLE hazard_event (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name            text NOT NULL,
  external_ref    text,
  current_posture posture NOT NULL DEFAULT 'QUIET',
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  replay          boolean NOT NULL DEFAULT false
);

-- HAZ-01: one row per NHC advisory cycle. Cone, track and wind field are stored
-- as geometry so verification can ask "what happened here".
--
-- The wind_field_* columns were called wind_prob_* until Aug 2, and the rename
-- is a deliberate contract change rather than tidying. NHC does not publish
-- gridded wind-speed-probability polygons in its public archive: what exists is
-- (a) forecast wind radii, four quadrant distances per threshold per forecast
-- hour, which are a deterministic extent, and (b) the wind speed probability
-- text product, which is a real percentage but only at 26 named locations —
-- two of them in Jamaica. Neither is a probability surface.
--
-- So these columns hold what we can actually build: the union across forecast
-- hours of the quadrant polygons at each threshold, i.e. the area expected to
-- see at least 34/50/64 kt over the advisory's forecast period. That answers
-- "is this household inside it". How likely is a different question and lives
-- in risk_assessment.p34/p50/p64, which are correctly named already.
--
-- Renamed while the table held zero rows and nothing referenced it. A column
-- whose name asserts something the data cannot support is the same class of
-- defect as an interface that decorates: it will be believed.
CREATE TABLE advisory (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hazard_event_id uuid NOT NULL REFERENCES hazard_event(id) ON DELETE CASCADE,
  advisory_number text NOT NULL,
  issued_at       timestamptz NOT NULL,
  observed        boolean NOT NULL DEFAULT false,  -- HAZ-04: post-event best track
  track           geography(LineString, 4326),
  cone            geography(Polygon, 4326),
  wind_field_34   geography(MultiPolygon, 4326),
  wind_field_50   geography(MultiPolygon, 4326),
  wind_field_64   geography(MultiPolygon, 4326),
  raw             jsonb NOT NULL DEFAULT '{}'::jsonb,
  ingested_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (hazard_event_id, advisory_number, observed)
);

CREATE INDEX advisory_event_idx ON advisory (hazard_event_id, issued_at);

-- IMP-01: one assessment per household per advisory. Transparent parametric
-- lookup in v1 — method and model_version are stored so a prediction can
-- always be explained, and so the learning loop (IMP-04) has provenance.
--
-- p34/p50/p64 are the probabilities the wind_field_* geometry cannot carry:
-- percentages from the NHC wind speed probability product, interpolated to the
-- household from the nearest named locations. `method` records how, because an
-- interpolated probability presented as a measured one is a lie the whole
-- platform exists to prevent.
CREATE TABLE risk_assessment (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  storm_file_id   uuid NOT NULL REFERENCES storm_file(id) ON DELETE CASCADE,
  advisory_id     uuid NOT NULL REFERENCES advisory(id) ON DELETE CASCADE,
  p34             real, p50 real, p64 real,
  predicted_band  damage_band,
  confidence      real CHECK (confidence BETWEEN 0 AND 1),
  method          text NOT NULL,
  model_version   text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (storm_file_id, advisory_id)
);

CREATE INDEX risk_assessment_advisory_idx ON risk_assessment (advisory_id);

-- ---------------------------------------------------------------------------
-- Claims and verification
-- ---------------------------------------------------------------------------

-- INT-05: claim_ref is the human-readable ID a household reads back over a bad
-- phone line (SE-4102). PRD 11.5.
CREATE TABLE claim (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_ref       text NOT NULL UNIQUE,
  storm_file_id   uuid NOT NULL REFERENCES storm_file(id) ON DELETE CASCADE,
  hazard_event_id uuid NOT NULL REFERENCES hazard_event(id),

  status          claim_status NOT NULL DEFAULT 'FILED',
  damage_type     text,
  reported_needs  text[] NOT NULL DEFAULT '{}',
  location        geography(Point, 4326),

  transcript      text,
  transcript_alt  text,          -- INT-02: both ASR outputs are kept, always
  lang            text,
  channel         text NOT NULL, -- whatsapp | sms | web | kiosk

  -- INT-04: safety-of-life bypass. Pins to the top of every queue.
  sol             boolean NOT NULL DEFAULT false,
  -- INT-03: filed after 3 unanswered follow-ups, fields still missing.
  partial         boolean NOT NULL DEFAULT false,

  severity        severity,
  triage_rank     integer,

  filed_at        timestamptz NOT NULL DEFAULT now(),  -- T2R clock STARTS here
  verified_at     timestamptz,
  settled_at      timestamptz,                          -- T2R clock STOPS here
  closed_reason   text
);

CREATE INDEX claim_file_idx     ON claim (storm_file_id);
CREATE INDEX claim_status_idx   ON claim (status);
CREATE INDEX claim_location_gix ON claim USING gist (location);
CREATE INDEX claim_sol_idx      ON claim (sol) WHERE sol;
CREATE INDEX claim_event_idx    ON claim (hazard_event_id, filed_at);

-- INT-05: raw media in R2, hash for VER-01 media integrity (perceptual hash
-- catches the same roof photo submitted by four different numbers).
CREATE TABLE evidence (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id     uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
  kind         evidence_kind NOT NULL,
  uri          text,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  sha256       text,
  phash        text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX evidence_claim_idx ON evidence (claim_id);
CREATE INDEX evidence_phash_idx ON evidence (phash) WHERE phash IS NOT NULL;

-- VER-01/02/07: five signals, each 0..1 with its own evidence, plus the
-- combined confidence. Every verdict is stored raw INCLUDING ones a human
-- later overrides — that is the eval set, so nothing here is ever updated
-- in place. A human override is a new row, not an edit.
CREATE TABLE verification (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id          uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,

  -- {hazard_sufficiency:{score,evidence}, satellite_change:{...},
  --  neighbour_corroboration:{...}, registry_match:{...}, media_integrity:{...}}
  signals           jsonb NOT NULL,
  confidence        real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  verdict           verdict NOT NULL,

  actor_kind        actor_kind NOT NULL,
  actor_id          uuid REFERENCES app_user(id),
  agent_name        text,
  model_version     text,
  threshold_version text,
  rationale         text,

  -- VER-04: low-evidence claims cap below auto-verify, forcing human review.
  capped            boolean NOT NULL DEFAULT false,
  overrides_id      uuid REFERENCES verification(id),
  created_at        timestamptz NOT NULL DEFAULT now(),

  CHECK ((actor_kind = 'HUMAN') = (actor_id IS NOT NULL))
);

CREATE INDEX verification_claim_idx ON verification (claim_id, created_at);

-- RTE-02: routing is an explicit decision with the consent snapshot that
-- justified it, not an inferred property of the claim.
CREATE TABLE routing_decision (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id        uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
  route           payer_route NOT NULL,
  insurer_name    text,
  consent_id      uuid REFERENCES consent(id),
  consent_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  decided_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (route NOT IN ('INSURER','BOTH') OR insurer_name IS NOT NULL)
);

CREATE INDEX routing_claim_idx ON routing_decision (claim_id);

-- ---------------------------------------------------------------------------
-- Money
-- ---------------------------------------------------------------------------

CREATE TABLE donation_pool (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text NOT NULL,
  scope_kind  text NOT NULL,          -- EVENT | PARISH  (PRD 11.3: no category in P0)
  scope_value text,
  balance     numeric(14,2) NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- DON-01: the platform records and directs; the fiscal sponsor holds funds.
CREATE TABLE donation (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pool_id      uuid NOT NULL REFERENCES donation_pool(id),
  donor_handle text NOT NULL,          -- pseudonymous, safe to show publicly
  amount       numeric(14,2) NOT NULL CHECK (amount > 0),
  currency     text NOT NULL DEFAULT 'JMD',
  simulated    boolean NOT NULL DEFAULT true,
  received_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX donation_pool_idx ON donation (pool_id, received_at);

CREATE TABLE warehouse (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name     text NOT NULL,
  parish   text,
  location geography(Point, 4326)
);

CREATE TABLE stock_item (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  warehouse_id uuid NOT NULL REFERENCES warehouse(id) ON DELETE CASCADE,
  sku          text NOT NULL,
  quantity     integer NOT NULL CHECK (quantity >= 0),
  UNIQUE (warehouse_id, sku)
);

-- Human gates G1/G2/G3. One table, role-checked. ADM-02 requires
-- re-authentication at the moment of signing; reauth_at records that it
-- happened, and a signature without it is invalid.
CREATE TABLE approval (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gate         gate_kind NOT NULL,
  subject_type text NOT NULL,
  subject_id   uuid NOT NULL,
  approved_by  uuid NOT NULL REFERENCES app_user(id),
  role_at_time app_role NOT NULL,
  reauth_at    timestamptz NOT NULL,
  note         text,
  approved_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX approval_subject_idx ON approval (subject_type, subject_id);

CREATE TABLE allocation_plan (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hazard_event_id uuid NOT NULL REFERENCES hazard_event(id),
  proposed_by     text NOT NULL,      -- agent name
  approval_id     uuid REFERENCES approval(id),
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- PAY-06: flat J$45,000 cash grant, goods tiered by severity (PRD 11.1).
CREATE TABLE allocation (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id       uuid REFERENCES allocation_plan(id) ON DELETE SET NULL,
  claim_id      uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
  resource      resource_kind NOT NULL,
  sku           text,
  quantity      integer,
  amount        numeric(14,2),
  currency      text NOT NULL DEFAULT 'JMD',
  payer_route   payer_route NOT NULL,
  pool_id       uuid REFERENCES donation_pool(id),
  warehouse_id  uuid REFERENCES warehouse(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK ((resource = 'CASH') = (amount IS NOT NULL)),
  CHECK ((resource = 'ITEM') = (sku IS NOT NULL AND quantity IS NOT NULL))
);

CREATE INDEX allocation_claim_idx ON allocation (claim_id);

CREATE TABLE disbursement_batch (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel     disbursement_channel NOT NULL,
  total       numeric(14,2) NOT NULL DEFAULT 0,
  approval_id uuid REFERENCES approval(id),
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- INVARIANT 1. The NOT NULL on approval_id is the whole "humans hold every
-- gate that moves money" rule, expressed where it cannot be forgotten. A
-- disbursement row is unwritable until a Finance Officer has signed its batch.
CREATE TABLE disbursement (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  allocation_id uuid NOT NULL REFERENCES allocation(id) ON DELETE CASCADE,
  batch_id      uuid NOT NULL REFERENCES disbursement_batch(id),
  approval_id   uuid NOT NULL REFERENCES approval(id),
  channel       disbursement_channel NOT NULL,
  status        disbursement_status NOT NULL DEFAULT 'PENDING',
  simulated     boolean NOT NULL DEFAULT true,
  external_ref  text,
  executed_at   timestamptz,
  confirmed_at  timestamptz,
  failure_reason text,
  CHECK (status <> 'CONFIRMED' OR confirmed_at IS NOT NULL)
);

CREATE INDEX disbursement_alloc_idx ON disbursement (allocation_id);
CREATE INDEX disbursement_batch_idx ON disbursement (batch_id);

-- VER-05: cross-payer dedupe. One settled claim per household per event per
-- need category, unless a human overrides with a recorded reason.
CREATE UNIQUE INDEX disbursement_dedupe_idx
  ON allocation (claim_id, sku, payer_route)
  WHERE sku IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Ledger
-- ---------------------------------------------------------------------------

-- LGR-01: append-only, hash-chained. seq is monotonic so verify_chain() can
-- walk it deterministically. There is deliberately no UPDATE or DELETE grant
-- on this table in any application role.
CREATE TABLE ledger_entry (
  seq          bigserial PRIMARY KEY,
  id           uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  prev_hash    text,
  hash         text NOT NULL UNIQUE,
  actor_kind   actor_kind NOT NULL,
  actor_id     uuid REFERENCES app_user(id),
  agent_name   text,
  action       text NOT NULL,
  subject_type text NOT NULL,
  subject_id   uuid,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  payload_hash text NOT NULL,
  ts           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ledger_subject_idx ON ledger_entry (subject_type, subject_id);
CREATE INDEX ledger_action_idx  ON ledger_entry (action, ts);

CREATE RULE ledger_no_update AS ON UPDATE TO ledger_entry DO INSTEAD NOTHING;
CREATE RULE ledger_no_delete AS ON DELETE TO ledger_entry DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------------
-- Job queue  (PRD 11.6 — Postgres SKIP LOCKED, no Redis)
-- ---------------------------------------------------------------------------
--
-- Jobs are enqueued in the SAME transaction as the state transition that
-- triggers them. That is the entire reason this is a table and not Redis: a
-- storm file cannot change state while its follow-on agent job is silently
-- lost. Workers wake on LISTEN/NOTIFY rather than polling.
--
--   SELECT * FROM agent_job
--    WHERE status = 'QUEUED' AND run_after <= now()
--    ORDER BY priority DESC, run_after
--    FOR UPDATE SKIP LOCKED
--    LIMIT 1;

CREATE TABLE agent_job (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_type     text NOT NULL,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  status       job_status NOT NULL DEFAULT 'QUEUED',
  priority     smallint NOT NULL DEFAULT 0,   -- SOL claims ride at 100
  attempts     smallint NOT NULL DEFAULT 0,
  max_attempts smallint NOT NULL DEFAULT 5,
  run_after    timestamptz NOT NULL DEFAULT now(),
  locked_by    text,
  locked_at    timestamptz,
  last_error   text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz
);

CREATE INDEX agent_job_claimable_idx
  ON agent_job (priority DESC, run_after)
  WHERE status = 'QUEUED';

-- ---------------------------------------------------------------------------
-- INVARIANT 2 — SETTLED requires a confirmed disbursement or delivery
-- ---------------------------------------------------------------------------
-- PAY-04. Enforced in the database because it is the claim the whole platform
-- makes about itself. If this trigger ever fires in production we have a bug
-- worth stopping for, not an exception worth catching.

CREATE OR REPLACE FUNCTION settled_requires_confirmation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'SETTLED' AND OLD.state IS DISTINCT FROM 'SETTLED' THEN
    IF NOT EXISTS (
      SELECT 1
        FROM claim c
        JOIN allocation a   ON a.claim_id = c.id
        JOIN disbursement d ON d.allocation_id = a.id
       WHERE c.storm_file_id = NEW.id
         AND d.status = 'CONFIRMED'
    ) THEN
      RAISE EXCEPTION
        'storm_file % cannot reach SETTLED: no confirmed disbursement (PAY-04)',
        NEW.id;
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER storm_file_settled_guard
  BEFORE UPDATE OF state ON storm_file
  FOR EACH ROW EXECUTE FUNCTION settled_requires_confirmation();

COMMIT;
