"""Make non-null hazard event external references unique.

Revision ID: 0005_unique_hazard_external_ref
Revises: 0004_exposure_build_provenance
Create Date: 2026-08-03

The migration refuses to guess which duplicate is authoritative. If legacy
duplicates exist it reports them and leaves the database untouched; an operator
must reconcile references deliberately before retrying.
"""

from __future__ import annotations

from alembic import op

revision = "0005_unique_hazard_external_ref"
down_revision = "0004_exposure_build_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
          duplicate_refs text;
        BEGIN
          SELECT string_agg(format('%L (%s rows)', external_ref, copies), ', ')
          INTO duplicate_refs
          FROM (
            SELECT external_ref, count(*) AS copies
            FROM hazard_event
            WHERE external_ref IS NOT NULL
            GROUP BY external_ref
            HAVING count(*) > 1
            ORDER BY external_ref
            LIMIT 10
          ) duplicate;

          IF duplicate_refs IS NOT NULL THEN
            RAISE EXCEPTION
              'hazard_event.external_ref contains duplicates: %', duplicate_refs
              USING HINT = 'Reconcile duplicate event references explicitly, then retry the migration.';
          END IF;
        END
        $$;

        CREATE UNIQUE INDEX IF NOT EXISTS hazard_event_external_ref_uidx
          ON hazard_event (external_ref) WHERE external_ref IS NOT NULL;

        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_index i
            JOIN pg_class index_relation ON index_relation.oid = i.indexrelid
            JOIN pg_class table_relation ON table_relation.oid = i.indrelid
            WHERE index_relation.relname = 'hazard_event_external_ref_uidx'
              AND table_relation.oid = 'hazard_event'::regclass
              AND i.indisunique
              AND pg_get_expr(i.indpred, i.indrelid) = '(external_ref IS NOT NULL)'
          ) THEN
            RAISE EXCEPTION
              'hazard_event_external_ref_uidx exists but does not enforce the required unique partial index';
          END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS hazard_event_external_ref_uidx")
