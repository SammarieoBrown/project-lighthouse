"""Add the aggregated building inventory.

Revision ID: 0003_building_inventory
Revises: 0002_wind_field_rename
Create Date: 2026-08-03

The registry is 2,000 synthetic households placed by ``ST_GeneratePoints``
inside community polygons. Every one sits where nothing necessarily stands, so
"413 of our 500 synthetic homes are in the 64 kt band" is a statement about our
random seed. These tables are the real denominator, from 1,844,379 building
footprints for Jamaica.

**Why aggregates rather than the buildings.** The individual centroids were
loaded first, and the measurements decided it: 423 MB table, 144 MB GIST index,
43 MB key — 610 MB against Neon's 512 MB project limit — and one wind band on
one advisory took 93.9 seconds, because a geography predicate does spheroid
math 1.8 million times. Forty-one advisories would have run for hours.

Nothing needs a building row at query time. Counting, exposure and the
population weight are all aggregates, and the map draws footprints from the
basemap tiles. So the footprints stay in the cached parquet where DuckDB does
the planar spatial work, and Postgres holds only the answers.

**On the contract freeze.** The frozen contracts are the claim lifecycle —
StormFile, Claim, Evidence, Verification, Allocation, Disbursement, LedgerEntry,
Approval — the state machine over them, and the agent I/O models that double as
JSON schema for structured output. These are none of those. They add no column
to an existing table and nothing frozen references them. place_exposure carries
the one foreign key, to advisory, and it points outward. Adding reference data
alongside the contracts is not the same act as changing them.

0001 executes packages/contracts/schema.sql directly, so a fresh database gets
these from the canonical file and never runs the body below. This exists for
databases already stamped at 0002 — hence IF NOT EXISTS, so the two paths
converge rather than collide.
"""

from __future__ import annotations

from alembic import op

revision = "0003_building_inventory"
down_revision = "0002_wind_field_rename"
branch_labels = None
depends_on = None


_CREATE = """
CREATE TABLE IF NOT EXISTS place_structures (
  parish      text NOT NULL,
  district    text NOT NULL,
  community   text NOT NULL,
  structures  integer NOT NULL,
  built_m2    double precision NOT NULL,
  PRIMARY KEY (parish, district, community)
);

CREATE TABLE IF NOT EXISTS place_exposure (
  advisory_id uuid NOT NULL REFERENCES advisory(id) ON DELETE CASCADE,
  parish      text NOT NULL,
  district    text NOT NULL,
  community   text NOT NULL,
  band        smallint NOT NULL CHECK (band IN (34, 50, 64)),
  structures  integer NOT NULL CHECK (structures > 0),
  PRIMARY KEY (advisory_id, parish, district, community, band)
);

CREATE INDEX IF NOT EXISTS place_exposure_advisory_idx ON place_exposure (advisory_id);
"""


def upgrade() -> None:
    op.execute(_CREATE)


def downgrade() -> None:
    # Safe to drop, unlike 0001. Nothing references these, they hold no ledger
    # history, and they rebuild in minutes from a cached parquet by
    # apps/api/app/registry/buildings.py.
    op.execute("DROP TABLE IF EXISTS place_exposure")
    op.execute("DROP TABLE IF EXISTS place_structures")
