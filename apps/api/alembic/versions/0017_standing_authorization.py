"""A Director may delegate small allocations to an agent, within bounds.

"Agents propose, humans dispose" survives this intact: an auto-approved
allocation still carries a named Director's signature and the
reauthentication they performed. What changes is when they performed it —
once, for a bounded class of claims, instead of once per claim. The bounds
are enforced here rather than in the agent that reads them, so an agent
cannot approve above the ceiling, from an unauthorized pool, or under a
revoked authorization.

Revision ID: 0017_standing_authorization
Revises: 0016_director_sized_grants
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic import op

from app.config import SCHEMA_SQL

revision = "0017_standing_authorization"
down_revision = "0016_director_sized_grants"
branch_labels = None
depends_on = None

_FUNCTIONS = ("approval_role_guard", "allocation_signed_guard")


def _vendored_sql(name: str) -> str:
    """The prior function bodies, kept beside this file.

    A downgrade cannot read them out of ``schema.sql``: that file has moved on
    and no longer contains the versions this migration replaced.
    """
    return (Path(__file__).resolve().parent / "downgrade_sql" / name).read_text(
        encoding="utf-8"
    )


def _canonical_function(name: str) -> str:
    source = SCHEMA_SQL.read_text(encoding="utf-8")
    begin, end = f"-- {name.upper()}_FN_BEGIN", f"-- {name.upper()}_FN_END"
    try:
        start = source.index(begin) + len(begin)
        stop = source.index(end, start)
    except ValueError as exc:  # pragma: no cover - release packaging failure
        raise RuntimeError(f"canonical {name} block is missing") from exc
    block = source[start:stop].strip()
    if not re.match(rf"CREATE OR REPLACE FUNCTION {name}\(\)", block):
        raise RuntimeError(f"canonical {name} block does not define {name}")
    return block


def upgrade() -> None:
    # ``0001_initial`` applies the whole canonical schema, which now contains
    # everything below. So a fresh chain reaches 0017 with these objects
    # already present, and every statement here has to be idempotent — the
    # same shape 0015 and 0016 take for the same reason.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_approval_policy (
          id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          hazard_event_id   uuid NOT NULL REFERENCES hazard_event(id) ON DELETE RESTRICT,
          max_amount        numeric(14,2) NOT NULL CHECK (max_amount > 0),
          min_confidence    numeric(4,3) NOT NULL
            CHECK (min_confidence >= 0 AND min_confidence <= 1),
          min_signals       smallint NOT NULL CHECK (min_signals BETWEEN 1 AND 5),
          requires_assessment boolean NOT NULL DEFAULT true,
          payer_route       payer_route NOT NULL,
          pool_id           uuid REFERENCES donation_pool(id),
          created_by        uuid NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT,
          role_at_time      app_role NOT NULL,
          reauth_at         timestamptz NOT NULL,
          created_at        timestamptz NOT NULL DEFAULT now(),
          revoked_at        timestamptz,
          revoked_by        uuid REFERENCES app_user(id),
          CONSTRAINT auto_approval_policy_route_chk CHECK (
            (payer_route = 'DONOR_POOL' AND pool_id IS NOT NULL)
            OR (payer_route = 'GOV_RELIEF' AND pool_id IS NULL)
          ),
          CONSTRAINT auto_approval_policy_revocation_chk CHECK (
            (revoked_at IS NULL) = (revoked_by IS NULL)
          ),
          CONSTRAINT auto_approval_policy_author_chk CHECK (role_at_time = 'DIRECTOR')
        );

        CREATE INDEX IF NOT EXISTS auto_approval_policy_active_idx
          ON auto_approval_policy (hazard_event_id) WHERE revoked_at IS NULL;

        ALTER TABLE approval
          ADD COLUMN IF NOT EXISTS policy_id uuid
            REFERENCES auto_approval_policy(id) ON DELETE RESTRICT;

        ALTER TABLE approval DROP CONSTRAINT approval_recent_reauth_chk;
        ALTER TABLE approval ADD CONSTRAINT approval_recent_reauth_chk CHECK (
          policy_id IS NOT NULL
          OR (
            reauth_at >= approved_at - interval '5 minutes'
            AND reauth_at <= approved_at + interval '1 minute'
          )
        );
        """
    )
    for name in _FUNCTIONS:
        op.execute(_canonical_function(name))


def downgrade() -> None:
    """Withdraw delegation entirely: every claim returns to a human.

    Any auto-approved allocation keeps its signature and its ledger receipt —
    those are immutable and stay true, because a Director really did authorize
    them. What goes is the ability to sign that way again.
    """
    op.execute(
        """
        DELETE FROM approval WHERE policy_id IS NOT NULL;
        ALTER TABLE approval DROP CONSTRAINT approval_recent_reauth_chk;
        ALTER TABLE approval ADD CONSTRAINT approval_recent_reauth_chk CHECK (
          reauth_at >= approved_at - interval '5 minutes'
          AND reauth_at <= approved_at + interval '1 minute'
        );
        ALTER TABLE approval DROP COLUMN IF EXISTS policy_id;
        DROP TABLE IF EXISTS auto_approval_policy;
        """
    )
    op.execute(_vendored_sql("0017_downgrade.sql"))
