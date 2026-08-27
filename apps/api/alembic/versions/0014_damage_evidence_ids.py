"""Add damage_assessment.evidence_ids to a database that ran 0010 before it.

Revision ID: 0014_damage_evidence_ids
Revises: 0013_alert_delivery
Create Date: 2026-08-27

The revision id is short because ``alembic_version.version_num`` is
``varchar(32)`` and a longer one fails at the very end of a successful
upgrade, after every DDL statement has already run.

This exists because of a mistake worth naming. ``evidence_ids`` was added to
``damage_assessment`` by editing migration 0010 in place, on the reasoning that
the table had never shipped and so no database had rows to reconcile. The table
had shipped: 0010 was applied to a development database when it was written,
and editing the migration afterwards is invisible to any database that already
ran it.

Nothing caught it. A fresh database applies ``schema.sql`` wholesale at 0001
and arrives with the column, so CI and the test suite were green throughout —
the only environment that could see the fault was one that had migrated before
the edit, which is exactly the environment nobody runs tests against. It
surfaced as `column damage_assessment.evidence_ids does not exist` behind an
"internal error" in the console.

The rule this breaks, stated plainly for the next time: **a migration that has
been applied anywhere is history and gets a successor, not an edit.** "It has
not shipped to production" is not the same claim as "no database has run it".

Adds the column when absent and re-issues the digest and guard functions from
their canonical definitions, because a database that ran the original 0010 also
has the versions of those that predate the column.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0014_damage_evidence_ids"
down_revision = "0013_alert_delivery"
branch_labels = None
depends_on = None


def _column_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(
            text(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'damage_assessment'
                   AND column_name = 'evidence_ids'
                """
            )
        )
        .first()
    )


def upgrade() -> None:
    if _column_exists():
        return
    op.execute(
        """
        ALTER TABLE damage_assessment
          ADD COLUMN evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
        """
    )
    # The digest covers every evidentiary field, and the override guard has to
    # require the new one be copied forward like the rest.
    op.execute(
        """
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

    -- The Director may adjust the dollar range; every other observed fact
    -- about the photos carries over unchanged.
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
END $function$;
"""
    )


def downgrade() -> None:
    """Drop the column only while nothing has been recorded in it.

    A stored assessment names the photos it was made from, and a proposal that
    no longer says which photos it read is not the same record.
    """
    used = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM damage_assessment "
                "WHERE evidence_ids IS NOT NULL AND evidence_ids <> '[]'::jsonb"
            )
        )
        .scalar_one()
    )
    if used:
        raise RuntimeError(
            f"refusing to drop evidence_ids: {used} assessment(s) record which "
            "photos they were made from"
        )
    op.execute("ALTER TABLE damage_assessment DROP COLUMN evidence_ids;")
