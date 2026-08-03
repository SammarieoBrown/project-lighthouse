"""Add inventory provenance and exposure completion markers.

Revision ID: 0004_exposure_build_provenance
Revises: 0003_building_inventory
Create Date: 2026-08-03

``place_exposure`` deliberately stores only non-zero rows. Before this
revision, an advisory with no rows was therefore ambiguous: it could be a valid
zero or an exposure build that had never run. The event marker added here binds
a complete advisory set to the exact inventory build used to calculate it. Both
markers also carry canonical derived-row SHA-256 digests: counts and sums cannot
detect a same-total redistribution between places.

This revision is new and undeployed on this branch, so its create contract is
updated in place rather than followed by an avoidable corrective revision.

0001 executes the canonical schema directly, so a fresh database already has
these tables by the time this revision runs. ``IF NOT EXISTS`` keeps the fresh
and incremental paths convergent.
"""

from __future__ import annotations

from alembic import op

revision = "0004_exposure_build_provenance"
down_revision = "0003_building_inventory"
branch_labels = None
depends_on = None


_CREATE = """
CREATE TABLE IF NOT EXISTS place_structure_build (
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

CREATE TABLE IF NOT EXISTS place_exposure_build (
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
"""


def upgrade() -> None:
    op.execute(_CREATE)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS place_exposure_build")
    op.execute("DROP TABLE IF EXISTS place_structure_build")
