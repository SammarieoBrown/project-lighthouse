"""Rename advisory.wind_prob_* to wind_field_*.

Revision ID: 0002_wind_field_rename
Revises: 0001_initial
Create Date: 2026-08-02

The columns were named for a product NHC does not publish. Its public archive
carries forecast wind radii — four quadrant distances per threshold per forecast
hour, a deterministic extent — and a wind speed probability text product giving
real percentages at 26 named locations, two of them in Jamaica. Neither is a
probability surface, so a geography column called wind_prob_34 could only ever
hold something other than what it claimed.

The geometry we can honestly build is the union across forecast hours of the
quadrant polygons at each threshold: the area expected to see at least 34/50/64
kt during the advisory's forecast period. That is a wind field. The probability
is a scalar and already has a correctly named home in risk_assessment.p34/p50/p64.

Done now because the table holds no rows and nothing references it. In two weeks
the misnomer would be load-bearing, and by then someone — probably me — would
have written a risk model that trusted the column name.

0001 executes packages/contracts/schema.sql directly, so the canonical file is
edited in the same commit and a fresh database never sees the old names. This
migration exists for databases already stamped at 0001.
"""

from __future__ import annotations

from alembic import op

revision = "0002_wind_field_rename"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

_THRESHOLDS = (34, 50, 64)

# Postgres has no ALTER TABLE ... RENAME COLUMN IF EXISTS, and this migration
# has to be a no-op rather than an error on a database built fresh from the
# updated schema.sql — which already has the new names, because 0001 executes
# that file rather than restating it. Only a database stamped at 0001 from
# before this commit still carries wind_prob_*.
_RENAME = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'advisory' AND column_name = '{old}'
  ) THEN
    ALTER TABLE advisory RENAME COLUMN {old} TO {new};
  END IF;
END $$;
"""


def upgrade() -> None:
    for kt in _THRESHOLDS:
        op.execute(_RENAME.format(old=f"wind_prob_{kt}", new=f"wind_field_{kt}"))


def downgrade() -> None:
    for kt in _THRESHOLDS:
        op.execute(_RENAME.format(old=f"wind_field_{kt}", new=f"wind_prob_{kt}"))
