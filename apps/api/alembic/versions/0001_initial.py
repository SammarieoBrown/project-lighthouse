"""Initial schema.

Revision ID: 0001_initial
Create Date: 2026-08-01

This migration does not restate the schema in Python. It executes
``packages/contracts/schema.sql`` directly.

That is deliberate. The build spec says the SQL file is canonical and the
migration must mirror it exactly — so rather than trusting two hand-maintained
copies to stay in step, there is only one copy and the migration reads it. A
hand-written Python translation would drift within a week, and the first symptom
would be a demo failing on a column that exists locally and not on Render.
"""

from __future__ import annotations

from alembic import op

from app.schema_sql import load_schema_sql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(load_schema_sql())


def downgrade() -> None:
    # Phase 0 has no downgrade. Before there is real data the recovery path is
    # to drop the schema and re-run; after there is real data, a blind teardown
    # of the ledger is not something we want one keystroke away.
    raise NotImplementedError("0001_initial is not reversible by design")
