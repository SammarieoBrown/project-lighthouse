"""Test harness.

Tests run against the real Neon database, in a throwaway schema that is created
per run and dropped afterwards. ``public`` is never touched.

Running against real Postgres rather than SQLite is not optional here: the
things most worth testing — the SETTLED trigger, the append-only ledger rules,
``FOR UPDATE SKIP LOCKED``, PostGIS columns, native enums — do not exist in a
substitute. A green test suite against a database that cannot enforce our
invariants would be worse than no suite at all.

Each test runs inside a transaction that is rolled back at teardown, so tests
neither see nor disturb each other. Application code flushes rather than
commits, which is what makes that work.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.schema_sql import load_schema_sql  # noqa: E402


@pytest.fixture(scope="session")
def schema_name() -> str:
    return f"lh_test_{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def _prepared_schema(schema_name: str):
    url = get_settings().sqlalchemy_url
    admin = create_engine(url, poolclass=NullPool, future=True)

    # Extensions are database-wide. They are needed by the real migration too,
    # so creating them here is not a test artefact we have to clean up.
    with admin.begin() as conn:
        for ext in ("postgis", "vector", "pg_trgm"):
            conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
        conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    yield schema_name

    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine(_prepared_schema: str):
    """Engine pinned to the throwaway schema, with the canonical DDL applied."""
    url = get_settings().sqlalchemy_url
    eng = create_engine(
        url,
        poolclass=NullPool,
        future=True,
        connect_args={"options": f"-csearch_path={_prepared_schema},public"},
    )
    with eng.begin() as conn:
        conn.execute(text(load_schema_sql()))
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    conn = engine.connect()
    outer = conn.begin()
    s = Session(bind=conn, expire_on_commit=False)
    try:
        yield s
    finally:
        s.close()
        outer.rollback()
        conn.close()
