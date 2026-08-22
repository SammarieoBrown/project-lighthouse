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
CREATE EXTENSION IF NOT EXISTS pgcrypto;

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

CREATE TYPE damage_assessment_verdict AS ENUM ('PROPOSED', 'APPROVED', 'REJECTED');

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

-- Approval credentials are deliberately narrow and short lived. The API never
-- stores the bearer secret: only its SHA-256 digest lands here. A credential is
-- proof that the human re-authenticated recently; it is not a long-lived login
-- session and cannot be used after five minutes.
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

CREATE INDEX human_credential_user_idx ON human_credential (user_id);

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
-- random seed rather than about Jamaica. These tables provide the
-- mapped-reference denominator from VIDA's combined build (Google Open
-- Buildings + Microsoft GlobalML + OSM, deduplicated) — 1,844,379 source
-- footprints, ODbL.
--
-- **Aggregates, not the buildings themselves, and that design was benchmarked
-- rather than assumed.** The individual centroids were loaded once: 423 MB for
-- the table, 144 MB for the GIST index, 43 MB for the key — 610 MB against a 512 MB
-- project limit. And a single band on a single advisory took 93.9 seconds,
-- because geography predicates do spheroid math 1.8 million times; the full
-- 41 advisories would have run for hours.
--
-- Nothing needs a building row at query time. Counting is an aggregate,
-- exposure is an aggregate, the dasymetric population weight is an aggregate,
-- and the map draws source geometry from the dedicated structure archive. So
-- the footprints stay
-- in the cached parquet, DuckDB does the planar spatial work, and Postgres
-- holds the answers. Storage falls from 610 MB to kilobytes.
--
-- **The one external public-source dataset here, and it holds no PII.** A
-- footprint is a geospatial feature with no occupant attached, so the
-- synthetic-data-only rule is untouched: this records where source footprints
-- are mapped, never who lives in them or whether every feature is current.

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

-- The aggregate table above is replaceable reference data, so its identity
-- cannot live in a migration number. This singleton records the exact source
-- bytes, boundary bytes and aggregation recipe that produced the current
-- rows. Exporters validate the exact canonical row digest as well as the counts
-- and input fingerprint before describing the mapped inventory as available.
CREATE TABLE place_structure_build (
  singleton             boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  inventory_fingerprint text NOT NULL UNIQUE
    CHECK (inventory_fingerprint ~ '^[0-9a-f]{64}$'),
  source_sha256          text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  boundaries_sha256      text NOT NULL CHECK (boundaries_sha256 ~ '^[0-9a-f]{64}$'),
  recipe_version         text NOT NULL,
  structure_count        bigint NOT NULL CHECK (structure_count > 0),
  place_count            integer NOT NULL CHECK (place_count > 0),
  structure_rows_sha256  text NOT NULL
    CHECK (structure_rows_sha256 ~ '^[0-9a-f]{64}$'),
  completed_at           timestamptz NOT NULL DEFAULT now()
);

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

-- Application code resolves events by external identity and must never choose
-- an arbitrary row. PostgreSQL permits multiple NULLs in a unique index, so
-- locally-authored events without an external identity remain valid.
CREATE UNIQUE INDEX hazard_event_external_ref_uidx
  ON hazard_event (external_ref) WHERE external_ref IS NOT NULL;

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

-- Structures inside each wind band, per advisory. This table is declared only
-- after advisory so a fresh/isolated schema cannot accidentally bind its
-- foreign key to an advisory table later on the search path (for example
-- public.advisory in a cloned CI database).
--
-- Bands are **mutually exclusive**: a structure is counted once, at the highest
-- band it reaches, matching the CASE ladder the risk mapper already uses. They
-- are nested geometrically, so counting each band independently would report
-- the same building three times and inflate exposure by the width of the storm.
--
-- Only non-zero rows exist. A community absent for an advisory had no structure
-- in that band, which is different from having no data — and rows for every
-- (advisory, community, band) would be 95,325 of which most say nothing. A
-- separate completed-build record distinguishes a valid all-zero advisory from
-- an exposure build that has not run.
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

-- Sparse exposure rows need an explicit completion record. Zero rows for one
-- advisory can mean a valid zero; without this marker the same absence means
-- "not built". The advisory fingerprint covers the selected advisory IDs,
-- numbers and wind geometry, while the inventory fingerprint binds the result
-- to the exact denominator used by the build. The row digest covers every
-- material event-scoped exposure field; counts and totals alone cannot detect a
-- same-total redistribution between places.
CREATE TABLE place_exposure_build (
  hazard_event_id         uuid PRIMARY KEY REFERENCES hazard_event(id) ON DELETE CASCADE,
  inventory_fingerprint   text NOT NULL
    CHECK (inventory_fingerprint ~ '^[0-9a-f]{64}$'),
  structure_rows_sha256   text NOT NULL
    CHECK (structure_rows_sha256 ~ '^[0-9a-f]{64}$'),
  advisory_fingerprint    text NOT NULL
    CHECK (advisory_fingerprint ~ '^[0-9a-f]{64}$'),
  advisory_count          integer NOT NULL CHECK (advisory_count > 0),
  exposure_row_count      integer NOT NULL CHECK (exposure_row_count >= 0),
  exposed_structure_count bigint NOT NULL CHECK (exposed_structure_count >= 0),
  exposure_rows_sha256    text NOT NULL
    CHECK (exposure_rows_sha256 ~ '^[0-9a-f]{64}$'),
  completed_at            timestamptz NOT NULL DEFAULT now()
);

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
  snapshot_hash     text NOT NULL
    CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),

  CHECK ((actor_kind = 'HUMAN') = (actor_id IS NOT NULL))
);

CREATE INDEX verification_claim_idx ON verification (claim_id, created_at);

-- VERIFICATION_OVERRIDE_GUARDS_BEGIN
-- One agent decision can receive one human disposition.  A correction is a
-- new agent decision/review cycle, never a second competing override.
CREATE UNIQUE INDEX verification_overrides_uidx
  ON verification (overrides_id) WHERE overrides_id IS NOT NULL;

-- The digest covers every evidentiary field using PostgreSQL's canonical jsonb
-- representation. It is generated by the database, never accepted from an
-- agent, and the row becomes immutable immediately after insert.
CREATE OR REPLACE FUNCTION verification_snapshot_digest(v verification)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
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
$$;

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
-- VERIFICATION_OVERRIDE_GUARDS_END

CREATE TRIGGER verification_snapshot_guard_trigger
  BEFORE INSERT ON verification
  FOR EACH ROW EXECUTE FUNCTION verification_snapshot_guard();

CREATE OR REPLACE FUNCTION verification_immutable_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'verification evidence is immutable; append a new row';
END $$;

CREATE TRIGGER verification_immutable_guard_trigger
  BEFORE UPDATE OR DELETE ON verification
  FOR EACH ROW EXECUTE FUNCTION verification_immutable_guard();

-- Post-hoc, photo-based damage estimate tied to the same claim location
-- verification already resolves (COALESCE(claim.location, storm_file.location)).
-- Like verification, every proposal is stored raw and is never edited in
-- place — a Director's decision is a new row, linked by overrides_id. Unlike
-- verification there is no confidence-gated auto path: a dollar figure always
-- waits for a Director (Damage Assessment Agent authority, transitions.md).
CREATE TABLE damage_assessment (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id          uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
  storm_file_id     uuid NOT NULL REFERENCES storm_file(id),

  band              damage_band NOT NULL,
  estimate_low      numeric(14,2) NOT NULL CHECK (estimate_low >= 0),
  estimate_high     numeric(14,2) NOT NULL CHECK (estimate_high >= estimate_low),
  currency          text NOT NULL DEFAULT 'JMD',
  confidence        real NOT NULL CHECK (confidence BETWEEN 0 AND 1),

  -- [{evidence_id, observed_damage, band, confidence}, ...] — one per photo read.
  findings          jsonb NOT NULL DEFAULT '[]'::jsonb,
  location_source   text NOT NULL CHECK (location_source IN ('claim', 'storm_file')),

  verdict           damage_assessment_verdict NOT NULL,
  actor_kind        actor_kind NOT NULL,
  actor_id          uuid REFERENCES app_user(id),
  agent_name        text,
  model_version     text,
  rationale         text,

  overrides_id      uuid REFERENCES damage_assessment(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  snapshot_hash     text NOT NULL
    CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),

  CHECK ((actor_kind = 'HUMAN') = (actor_id IS NOT NULL))
);

CREATE INDEX damage_assessment_claim_idx ON damage_assessment (claim_id, created_at);

-- One agent proposal can receive one Director disposition, same rule as
-- verification_overrides_uidx above.
CREATE UNIQUE INDEX damage_assessment_overrides_uidx
  ON damage_assessment (overrides_id) WHERE overrides_id IS NOT NULL;

CREATE OR REPLACE FUNCTION damage_assessment_snapshot_digest(d damage_assessment)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(
    digest(
      convert_to(
        jsonb_build_object(
          'id', d.id::text,
          'claim_id', d.claim_id::text,
          'storm_file_id', d.storm_file_id::text,
          'band', d.band::text,
          'estimate_low', d.estimate_low,
          'estimate_high', d.estimate_high,
          'currency', d.currency,
          'confidence', d.confidence,
          'findings', d.findings,
          'location_source', d.location_source,
          'verdict', d.verdict::text,
          'actor_kind', d.actor_kind::text,
          'actor_id', d.actor_id::text,
          'agent_name', d.agent_name,
          'model_version', d.model_version,
          'rationale', d.rationale,
          'overrides_id', d.overrides_id::text,
          'created_at_epoch_us',
            (extract(epoch FROM d.created_at) * 1000000)::bigint
        )::text,
        'UTF8'
      ),
      'sha256'
    ),
    'hex'
  )
$$;

CREATE OR REPLACE FUNCTION damage_assessment_snapshot_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  director_role app_role;
  director_active boolean;
  parent damage_assessment%ROWTYPE;
  latest_id uuid;
BEGIN
  IF NEW.actor_kind = 'HUMAN' THEN
    SELECT role, active INTO director_role, director_active
      FROM app_user WHERE id = NEW.actor_id;
    IF NOT FOUND OR NOT director_active OR director_role <> 'DIRECTOR' THEN
      RAISE EXCEPTION
        'human damage assessment verdicts require an active DIRECTOR';
    END IF;
    IF NEW.agent_name IS NOT NULL THEN
      RAISE EXCEPTION
        'human damage assessment verdicts cannot assert an agent name';
    END IF;
    IF NEW.verdict NOT IN ('APPROVED', 'REJECTED') THEN
      RAISE EXCEPTION
        'human damage assessment verdict must be APPROVED or REJECTED';
    END IF;
    IF NEW.overrides_id IS NULL THEN
      RAISE EXCEPTION
        'damage assessment override must bind latest agent evidence';
    END IF;

    SELECT * INTO parent FROM damage_assessment WHERE id = NEW.overrides_id;
    IF NOT FOUND
       OR parent.claim_id IS DISTINCT FROM NEW.claim_id
       OR parent.actor_kind <> 'AGENT'
       OR parent.actor_id IS NOT NULL
       OR parent.agent_name IS DISTINCT FROM 'damage_assessment_agent'
       OR parent.verdict <> 'PROPOSED'
       OR parent.overrides_id IS NOT NULL THEN
      RAISE EXCEPTION
        'damage assessment override must bind latest agent evidence';
    END IF;

    SELECT id INTO latest_id
      FROM damage_assessment
     WHERE claim_id = NEW.claim_id
     ORDER BY created_at DESC, id DESC
     LIMIT 1;
    IF latest_id IS DISTINCT FROM parent.id THEN
      RAISE EXCEPTION
        'damage assessment override must bind latest agent evidence';
    END IF;

    -- The Director may adjust the dollar range; every other observed fact
    -- about the photos carries over unchanged.
    IF NEW.storm_file_id IS DISTINCT FROM parent.storm_file_id
       OR NEW.band IS DISTINCT FROM parent.band
       OR NEW.currency IS DISTINCT FROM parent.currency
       OR NEW.confidence IS DISTINCT FROM parent.confidence
       OR NEW.findings IS DISTINCT FROM parent.findings
       OR NEW.location_source IS DISTINCT FROM parent.location_source
       OR NEW.model_version IS DISTINCT FROM parent.model_version THEN
      RAISE EXCEPTION
        'damage assessment override must copy parent observed evidence';
    END IF;
  ELSIF NEW.actor_kind = 'AGENT' THEN
    IF NEW.actor_id IS NOT NULL
       OR NEW.agent_name IS DISTINCT FROM 'damage_assessment_agent' THEN
      RAISE EXCEPTION
        'agent damage assessment verdicts require damage_assessment_agent authority';
    END IF;
    IF NEW.overrides_id IS NOT NULL THEN
      RAISE EXCEPTION
        'agent damage assessment verdicts cannot override another row';
    END IF;
    IF NEW.verdict <> 'PROPOSED' THEN
      RAISE EXCEPTION 'agent damage assessment verdict must be PROPOSED';
    END IF;
  ELSE
    RAISE EXCEPTION 'system actors cannot issue damage assessment verdicts';
  END IF;

  NEW.snapshot_hash := damage_assessment_snapshot_digest(NEW);
  RETURN NEW;
END $$;

CREATE TRIGGER damage_assessment_snapshot_guard_trigger
  BEFORE INSERT ON damage_assessment
  FOR EACH ROW EXECUTE FUNCTION damage_assessment_snapshot_guard();

CREATE OR REPLACE FUNCTION damage_assessment_immutable_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'damage assessment evidence is immutable; append a new row';
END $$;

CREATE TRIGGER damage_assessment_immutable_guard_trigger
  BEFORE UPDATE OR DELETE ON damage_assessment
  FOR EACH ROW EXECUTE FUNCTION damage_assessment_immutable_guard();

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
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  gate            gate_kind NOT NULL,
  subject_type    text NOT NULL,
  subject_id      uuid NOT NULL,
  approved_by     uuid NOT NULL REFERENCES app_user(id),
  role_at_time    app_role NOT NULL,
  reauth_at       timestamptz NOT NULL,
  note            text,
  idempotency_key text,
  request_hash    text,
  approved_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT approval_request_pair_chk
    CHECK ((idempotency_key IS NULL) = (request_hash IS NULL)),
  CONSTRAINT approval_idempotency_key_length_chk
    CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 200),
  CONSTRAINT approval_request_hash_chk
    CHECK (request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'),
  CONSTRAINT approval_gate_role_chk CHECK (
    (gate IN ('ALERT_CASCADE', 'ALLOCATION_PLAN') AND role_at_time = 'DIRECTOR')
    OR (gate = 'DISBURSEMENT_BATCH' AND role_at_time = 'FINANCE_OFFICER')
  ),
  CONSTRAINT approval_recent_reauth_chk CHECK (
    reauth_at >= approved_at - interval '5 minutes'
    AND reauth_at <= approved_at + interval '1 minute'
  )
);

CREATE INDEX approval_subject_idx ON approval (subject_type, subject_id);
CREATE UNIQUE INDEX approval_gate_subject_uidx
  ON approval (gate, subject_type, subject_id);
CREATE UNIQUE INDEX approval_idempotency_uidx
  ON approval (approved_by, idempotency_key)
  WHERE idempotency_key IS NOT NULL;

-- The role snapshot is evidence, not an assertion supplied by the client. The
-- database checks it against the active user at the moment of signing and
-- enforces the role assigned to each gate. approved_at is always database time
-- so a caller cannot make stale reauthentication look current.
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

CREATE TRIGGER approval_role_guard_trigger
  BEFORE INSERT ON approval
  FOR EACH ROW EXECUTE FUNCTION approval_role_guard();

-- A signature is historical evidence. Corrections are new rows; the signed
-- record itself cannot be edited or removed.
CREATE RULE approval_no_update AS ON UPDATE TO approval DO INSTEAD NOTHING;
CREATE RULE approval_no_delete AS ON DELETE TO approval DO INSTEAD NOTHING;

CREATE TABLE allocation_plan (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  hazard_event_id uuid NOT NULL REFERENCES hazard_event(id),
  proposed_by     text NOT NULL,      -- agent name
  approval_id     uuid REFERENCES approval(id) ON DELETE RESTRICT,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX allocation_plan_approval_uidx
  ON allocation_plan (approval_id) WHERE approval_id IS NOT NULL;

CREATE OR REPLACE FUNCTION allocation_plan_signed_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
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
END $$;

CREATE TRIGGER allocation_plan_signed_guard_trigger
  BEFORE INSERT OR UPDATE OR DELETE ON allocation_plan
  FOR EACH ROW EXECUTE FUNCTION allocation_plan_signed_guard();

-- PAY-06: flat J$45,000 cash grant, goods tiered by severity (PRD 11.1).
CREATE TABLE allocation (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id       uuid NOT NULL REFERENCES allocation_plan(id) ON DELETE RESTRICT,
  claim_id      uuid NOT NULL REFERENCES claim(id) ON DELETE RESTRICT,
  resource      resource_kind NOT NULL,
  sku           text,
  quantity      integer,
  amount        numeric(14,2),
  currency      text NOT NULL DEFAULT 'JMD',
  payer_route   payer_route NOT NULL,
  pool_id       uuid REFERENCES donation_pool(id),
  warehouse_id  uuid REFERENCES warehouse(id),
  verification_id uuid NOT NULL REFERENCES verification(id) ON DELETE RESTRICT,
  verification_snapshot_hash text NOT NULL
    CHECK (verification_snapshot_hash ~ '^[0-9a-f]{64}$'),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT allocation_release_policy_chk CHECK (
    resource = 'CASH'
    AND amount = 45000.00
    AND currency = 'JMD'
    AND payer_route = 'GOV_RELIEF'
    AND sku IS NULL
    AND quantity IS NULL
    AND pool_id IS NULL
    AND warehouse_id IS NULL
  )
);

CREATE INDEX allocation_claim_idx ON allocation (claim_id);
CREATE UNIQUE INDEX allocation_plan_uidx ON allocation (plan_id);

CREATE TABLE disbursement_batch (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel       disbursement_channel NOT NULL,
  total         numeric(14,2) NOT NULL,
  approval_id   uuid NOT NULL REFERENCES approval(id) ON DELETE RESTRICT,
  snapshot_hash text NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT disbursement_batch_release_policy_chk CHECK (
    total = 45000.00 AND channel IN ('BANK', 'MOBILE_MONEY', 'VOUCHER')
  )
);

-- INVARIANT 1. The NOT NULL on approval_id is the whole "humans hold every
-- gate that moves money" rule, expressed where it cannot be forgotten. A
-- disbursement row is unwritable until a Finance Officer has signed its batch.
CREATE TABLE disbursement (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  allocation_id uuid NOT NULL REFERENCES allocation(id) ON DELETE RESTRICT,
  batch_id      uuid NOT NULL REFERENCES disbursement_batch(id) ON DELETE RESTRICT,
  approval_id   uuid NOT NULL REFERENCES approval(id) ON DELETE RESTRICT,
  channel       disbursement_channel NOT NULL,
  status        disbursement_status NOT NULL DEFAULT 'PENDING',
  simulated     boolean NOT NULL DEFAULT true,
  executor_provider text NOT NULL,
  snapshot_hash text NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
  execution_requested_by uuid REFERENCES app_user(id) ON DELETE RESTRICT,
  execution_idempotency_key text,
  execution_request_hash text,
  external_ref  text,
  provider_confirmation_hash text,
  executed_at   timestamptz,
  confirmed_at  timestamptz,
  failure_reason text,
  CONSTRAINT disbursement_execution_key_length_chk CHECK (
    execution_idempotency_key IS NULL
    OR length(execution_idempotency_key) BETWEEN 1 AND 200
  ),
  CONSTRAINT disbursement_execution_request_hash_chk CHECK (
    execution_request_hash IS NULL
    OR execution_request_hash ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT disbursement_confirmation_hash_chk CHECK (
    provider_confirmation_hash IS NULL
    OR provider_confirmation_hash ~ '^[0-9a-f]{64}$'
  ),
  CONSTRAINT disbursement_demo_executor_chk CHECK (
    simulated AND executor_provider = 'LIGHTHOUSE_DEMO_EXECUTOR_V1'
  ),
  CONSTRAINT disbursement_lifecycle_shape_chk CHECK (
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
  )
);

CREATE INDEX disbursement_alloc_idx ON disbursement (allocation_id);
CREATE INDEX disbursement_batch_idx ON disbursement (batch_id);
CREATE UNIQUE INDEX disbursement_allocation_uidx ON disbursement (allocation_id);
CREATE UNIQUE INDEX disbursement_batch_uidx ON disbursement (batch_id);
CREATE UNIQUE INDEX disbursement_external_ref_uidx
  ON disbursement (executor_provider, external_ref) WHERE external_ref IS NOT NULL;
CREATE UNIQUE INDEX disbursement_execution_idempotency_uidx
  ON disbursement (execution_requested_by, execution_idempotency_key)
  WHERE execution_idempotency_key IS NOT NULL;

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

CREATE UNIQUE INDEX ledger_allocation_approved_subject_uidx
  ON ledger_entry (subject_id)
  WHERE action = 'allocation.approved' AND subject_type = 'allocation';
CREATE UNIQUE INDEX ledger_disbursement_batch_signed_subject_uidx
  ON ledger_entry (subject_id)
  WHERE action = 'disbursement.batch_signed'
    AND subject_type = 'disbursement_batch';
CREATE UNIQUE INDEX ledger_disbursement_executed_subject_uidx
  ON ledger_entry (subject_id)
  WHERE action = 'disbursement.executed' AND subject_type = 'disbursement';
CREATE UNIQUE INDEX ledger_disbursement_confirmed_subject_uidx
  ON ledger_entry (subject_id)
  WHERE action = 'disbursement.confirmed' AND subject_type = 'disbursement';
CREATE UNIQUE INDEX ledger_disbursement_failed_subject_uidx
  ON ledger_entry (subject_id)
  WHERE action = 'disbursement.failed' AND subject_type = 'disbursement';

-- An allocation can only be inserted as the one fixed release grant under an
-- exact Director signature and the latest qualifying verification.  Updates
-- and deletes are never corrections: the signed row is historical evidence.
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
END $$;

CREATE TRIGGER allocation_signed_guard_trigger
  BEFORE INSERT OR UPDATE OR DELETE ON allocation
  FOR EACH ROW EXECUTE FUNCTION allocation_signed_guard();

-- Public classification remains inside the internal receipt so auditors can
-- reproduce the decision, but only canonical closed-set values may be bound.
-- The public view validates these again and then redacts them per household.
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
END $$;

CREATE TRIGGER ledger_allocation_approval_guard_trigger
  BEFORE INSERT ON ledger_entry
  FOR EACH ROW EXECUTE FUNCTION ledger_allocation_approval_guard();

CREATE OR REPLACE FUNCTION signed_plan_must_be_complete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.approval_id IS NOT NULL
     AND (SELECT count(*) FROM allocation WHERE plan_id = NEW.id) <> 1 THEN
    RAISE EXCEPTION 'signed allocation plan must contain exactly one allocation';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER signed_plan_complete_trigger
  AFTER INSERT OR UPDATE ON allocation_plan
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION signed_plan_must_be_complete();

CREATE OR REPLACE FUNCTION allocation_must_have_ledger_receipt()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (
    SELECT count(*)
      FROM ledger_entry
     WHERE action = 'allocation.approved'
       AND subject_type = 'allocation'
       AND subject_id = NEW.id
  ) <> 1 THEN
    RAISE EXCEPTION 'signed allocation must have exactly one ledger receipt';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER allocation_ledger_complete_trigger
  AFTER INSERT ON allocation
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION allocation_must_have_ledger_receipt();

-- ---------------------------------------------------------------------------
-- Act 3 settlement receipts
-- ---------------------------------------------------------------------------
-- A batch and its one release disbursement are immutable signed instructions.
-- Only lifecycle evidence may advance, and then only PENDING -> EXECUTING ->
-- CONFIRMED/FAILED.  This release's executor is deliberately demo-only.
-- ACT3_SETTLEMENT_GUARDS_BEGIN

CREATE OR REPLACE FUNCTION disbursement_batch_snapshot_digest(
  row_value disbursement_batch
) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(
    digest(
      concat_ws(
        E'\x1f',
        row_value.id::text,
        row_value.channel::text,
        row_value.total::text,
        row_value.approval_id::text
      ),
      'sha256'
    ),
    'hex'
  )
$$;

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

CREATE TRIGGER disbursement_batch_signed_guard_trigger
  BEFORE INSERT OR UPDATE OR DELETE ON disbursement_batch
  FOR EACH ROW EXECUTE FUNCTION disbursement_batch_signed_guard();

CREATE OR REPLACE FUNCTION disbursement_snapshot_digest(row_value disbursement)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(
    digest(
      concat_ws(
        E'\x1f',
        row_value.id::text,
        row_value.allocation_id::text,
        row_value.batch_id::text,
        row_value.approval_id::text,
        row_value.channel::text,
        row_value.simulated::text,
        row_value.executor_provider
      ),
      'sha256'
    ),
    'hex'
  )
$$;

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

CREATE TRIGGER disbursement_lifecycle_guard_trigger
  BEFORE INSERT OR UPDATE OR DELETE ON disbursement
  FOR EACH ROW EXECUTE FUNCTION disbursement_lifecycle_guard();

-- Settlement payloads are closed contracts.  No arbitrary key can smuggle
-- household data into an otherwise publishable money event.
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

CREATE TRIGGER ledger_disbursement_receipt_guard_trigger
  BEFORE INSERT ON ledger_entry
  FOR EACH ROW EXECUTE FUNCTION ledger_disbursement_receipt_guard();

CREATE OR REPLACE FUNCTION disbursement_batch_must_have_receipt()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (
    SELECT count(*) FROM ledger_entry
     WHERE action = 'disbursement.batch_signed'
       AND subject_type = 'disbursement_batch'
       AND subject_id = NEW.id
  ) <> 1 THEN
    RAISE EXCEPTION 'signed disbursement batch must have exactly one receipt';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER disbursement_batch_receipt_complete_trigger
  AFTER INSERT ON disbursement_batch
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION disbursement_batch_must_have_receipt();

CREATE OR REPLACE FUNCTION disbursement_must_have_receipts()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  current_status disbursement_status;
  executed_count integer;
  confirmed_count integer;
  failed_count integer;
BEGIN
  SELECT status INTO current_status FROM disbursement WHERE id = NEW.id;
  SELECT count(*) INTO executed_count FROM ledger_entry
   WHERE action = 'disbursement.executed'
     AND subject_type = 'disbursement' AND subject_id = NEW.id;
  SELECT count(*) INTO confirmed_count FROM ledger_entry
   WHERE action = 'disbursement.confirmed'
     AND subject_type = 'disbursement' AND subject_id = NEW.id;
  SELECT count(*) INTO failed_count FROM ledger_entry
   WHERE action = 'disbursement.failed'
     AND subject_type = 'disbursement' AND subject_id = NEW.id;

  IF current_status = 'PENDING'
     AND (executed_count <> 0 OR confirmed_count <> 0 OR failed_count <> 0) THEN
    RAISE EXCEPTION 'pending disbursement cannot have execution receipts';
  ELSIF current_status = 'EXECUTING'
     AND (executed_count <> 1 OR confirmed_count <> 0 OR failed_count <> 0) THEN
    RAISE EXCEPTION 'executing disbursement requires exactly one execution receipt';
  ELSIF current_status = 'CONFIRMED'
     AND (executed_count <> 1 OR confirmed_count <> 1 OR failed_count <> 0) THEN
    RAISE EXCEPTION 'confirmed disbursement requires execution and confirmation receipts';
  ELSIF current_status = 'FAILED'
     AND (executed_count <> 1 OR confirmed_count <> 0 OR failed_count <> 1) THEN
    RAISE EXCEPTION 'failed disbursement requires execution and failure receipts';
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER disbursement_receipt_complete_trigger
  AFTER INSERT OR UPDATE ON disbursement
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION disbursement_must_have_receipts();
-- ACT3_SETTLEMENT_GUARDS_END

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
