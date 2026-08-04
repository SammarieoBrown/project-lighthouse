"""Bind every Review Clerk verdict to one immutable agent evidence snapshot.

Revision ID: 0009_verification_override_guard
Revises: 0008_act3_settlement
Create Date: 2026-08-03

The migration refuses ambiguous historical overrides.  It never invents a
parent row or silently copies evidence into an already-signed decision.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from app.config import SCHEMA_SQL

revision = "0009_verification_override_guard"
down_revision = "0008_act3_settlement"
branch_labels = None
depends_on = None

_GUARDS_BEGIN = "-- VERIFICATION_OVERRIDE_GUARDS_BEGIN"
_GUARDS_END = "-- VERIFICATION_OVERRIDE_GUARDS_END"
_FUNCTION_MARKER = "verification override must bind latest agent review evidence"


def _canonical_guard_sql() -> str:
    source = SCHEMA_SQL.read_text(encoding="utf-8")
    try:
        start = source.index(_GUARDS_BEGIN) + len(_GUARDS_BEGIN)
        end = source.index(_GUARDS_END, start)
    except ValueError as exc:  # pragma: no cover - release packaging failure
        raise RuntimeError("canonical verification override guard block is missing") from exc
    block = source[start:end].strip()
    if not block:
        raise RuntimeError("canonical verification override guard block is empty")
    return block


def _state() -> dict[str, bool]:
    return dict(
        op.get_bind()
        .execute(
            text(
                """
                SELECT
                  to_regclass(format(
                    '%I.%I', current_schema(), 'verification_overrides_uidx'
                  )) IS NOT NULL AS override_unique_index,
                  EXISTS (
                    SELECT 1
                      FROM pg_proc p
                      JOIN pg_namespace n ON n.oid = p.pronamespace
                     WHERE n.nspname = current_schema()
                       AND p.proname = 'verification_snapshot_guard'
                       AND position(
                         'verification override must bind latest agent review evidence'
                         IN pg_get_functiondef(p.oid)
                       ) > 0
                  ) AS hardened_snapshot_guard
                """
            )
        )
        .mappings()
        .one()
    )


def upgrade() -> None:
    state = _state()
    present = [bool(value) for value in state.values()]
    if all(present):
        return
    if any(present):
        missing = [name for name, value in state.items() if not value]
        raise RuntimeError(
            "0009 found a partially applied verification override guard; missing: "
            + ", ".join(missing)
        )

    counts = op.get_bind().execute(
        text(
            """
            SELECT
              (
                SELECT count(*)
                  FROM verification v
                 WHERE v.actor_kind = 'AGENT'
                   AND (
                     v.overrides_id IS NOT NULL
                     OR v.verdict NOT IN ('AUTO_VERIFIED', 'REVIEW', 'FLAGGED')
                   )
              ) AS invalid_agent_rows,
              (
                SELECT count(*)
                  FROM verification child
                  LEFT JOIN verification parent ON parent.id = child.overrides_id
                 WHERE child.actor_kind = 'HUMAN'
                   AND (
                     child.verdict NOT IN ('APPROVED', 'REJECTED')
                     OR parent.id IS NULL
                     OR parent.claim_id IS DISTINCT FROM child.claim_id
                     OR parent.actor_kind <> 'AGENT'
                     OR parent.actor_id IS NOT NULL
                     OR parent.agent_name IS DISTINCT FROM 'verification_agent'
                     OR parent.verdict NOT IN ('REVIEW', 'FLAGGED')
                     OR parent.overrides_id IS NOT NULL
                     OR child.signals IS DISTINCT FROM parent.signals
                     OR child.confidence IS DISTINCT FROM parent.confidence
                     OR child.model_version IS DISTINCT FROM parent.model_version
                     OR child.threshold_version IS DISTINCT FROM parent.threshold_version
                     OR child.capped IS DISTINCT FROM parent.capped
                     OR parent.id IS DISTINCT FROM (
                       SELECT prior.id
                         FROM verification prior
                        WHERE prior.claim_id = child.claim_id
                          AND (
                            prior.created_at < child.created_at
                            OR (
                              prior.created_at = child.created_at
                              AND prior.id < child.id
                            )
                          )
                        ORDER BY prior.created_at DESC, prior.id DESC
                        LIMIT 1
                     )
                   )
              ) AS invalid_human_rows,
              (
                SELECT count(*)
                  FROM (
                    SELECT overrides_id
                      FROM verification
                     WHERE overrides_id IS NOT NULL
                     GROUP BY overrides_id
                    HAVING count(*) > 1
                  ) duplicate_parent
              ) AS duplicate_override_parents
            """
        )
    ).mappings().one()
    if any(int(value) for value in counts.values()):
        raise RuntimeError(
            "0009 refuses ambiguous verification history because evidence cannot "
            "be truthfully inferred; reconcile first "
            f"(invalid_agent_rows={counts['invalid_agent_rows']}, "
            f"invalid_human_rows={counts['invalid_human_rows']}, "
            f"duplicate_override_parents={counts['duplicate_override_parents']})"
        )

    op.execute(_canonical_guard_sql())


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS verification_overrides_uidx;

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
        """
    )


__all__ = ["down_revision", "downgrade", "revision", "upgrade"]
