"""Add the append-only, Director-gated damage assessment table.

Revision ID: 0010_damage_assessment
Revises: 0009_verification_override_guard
Create Date: 2026-08-22

Mirrors the verification table's shape: every proposal is stored raw and is
never edited in place, and a snapshot-guard trigger enforces actor authority
(Damage Assessment Agent for a proposal, an active Director for a disposition)
before computing the row's tamper-evident digest. Unlike verification there is
no confidence-gated auto-verify path — a dollar figure always waits for a
Director, so the verdict enum is deliberately smaller
(``PROPOSED``/``APPROVED``/``REJECTED``, no ``AUTO_VERIFIED``).

This is a brand-new table with no historical rows to reconcile, so the DDL is
inlined here rather than following 0009's canonical-block-extraction pattern —
that machinery exists to retrofit guards onto a table that already has data,
which does not apply to a table created for the first time.

``schema.sql`` is the *current* canonical schema, and 0001_initial applies it
wholesale — so on any fresh database this table already exists by the time
0010 runs, exactly like every table added after 0001 was written. This
migration exists only for a database that was deployed before ``schema.sql``
gained this table, so it must check first rather than assume it is starting
from a pre-damage-assessment world.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0010_damage_assessment"
down_revision = "0009_verification_override_guard"
branch_labels = None
depends_on = None


def _table_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(
            text(
                """
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = current_schema()
                   AND table_name = 'damage_assessment'
                """
            )
        )
        .first()
    )


def upgrade() -> None:
    if _table_exists():
        return
    op.execute(
        """
        CREATE TYPE damage_assessment_verdict AS ENUM ('PROPOSED', 'APPROVED', 'REJECTED');

        CREATE TABLE damage_assessment (
          id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          claim_id          uuid NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
          storm_file_id     uuid NOT NULL REFERENCES storm_file(id),

          band              damage_band NOT NULL,
          estimate_low      numeric(14,2) NOT NULL CHECK (estimate_low >= 0),
          estimate_high     numeric(14,2) NOT NULL CHECK (estimate_high >= estimate_low),
          currency          text NOT NULL DEFAULT 'JMD',
          confidence        real NOT NULL CHECK (confidence BETWEEN 0 AND 1),

          findings          jsonb NOT NULL DEFAULT '[]'::jsonb,
          evidence_ids      jsonb NOT NULL DEFAULT '[]'::jsonb,
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

        CREATE UNIQUE INDEX damage_assessment_overrides_uidx
          ON damage_assessment (overrides_id) WHERE overrides_id IS NOT NULL;

        CREATE OR REPLACE FUNCTION damage_assessment_snapshot_digest(d damage_assessment)
        RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $function$
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
                  'evidence_ids', d.evidence_ids,
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
        $function$;

        CREATE OR REPLACE FUNCTION damage_assessment_snapshot_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
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

            IF NEW.storm_file_id IS DISTINCT FROM parent.storm_file_id
               OR NEW.band IS DISTINCT FROM parent.band
               OR NEW.currency IS DISTINCT FROM parent.currency
               OR NEW.confidence IS DISTINCT FROM parent.confidence
               OR NEW.findings IS DISTINCT FROM parent.findings
               OR NEW.evidence_ids IS DISTINCT FROM parent.evidence_ids
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
        END
        $function$;

        CREATE TRIGGER damage_assessment_snapshot_guard_trigger
          BEFORE INSERT ON damage_assessment
          FOR EACH ROW EXECUTE FUNCTION damage_assessment_snapshot_guard();

        CREATE OR REPLACE FUNCTION damage_assessment_immutable_guard()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
          RAISE EXCEPTION 'damage assessment evidence is immutable; append a new row';
        END
        $function$;

        CREATE TRIGGER damage_assessment_immutable_guard_trigger
          BEFORE UPDATE OR DELETE ON damage_assessment
          FOR EACH ROW EXECUTE FUNCTION damage_assessment_immutable_guard();
        """
    )


def downgrade() -> None:
    # Two dependency directions, so the table has to be dropped in the middle:
    # the digest function takes the table's implicit row type as a parameter
    # (must go before the table), while the guard function is referenced by a
    # trigger *on* the table (must go after — dropping the table takes its
    # triggers with it).
    op.execute(
        """
        DROP FUNCTION IF EXISTS damage_assessment_snapshot_digest(damage_assessment);
        DROP TABLE IF EXISTS damage_assessment;
        DROP FUNCTION IF EXISTS damage_assessment_snapshot_guard();
        DROP FUNCTION IF EXISTS damage_assessment_immutable_guard();
        DROP TYPE IF EXISTS damage_assessment_verdict;
        """
    )
