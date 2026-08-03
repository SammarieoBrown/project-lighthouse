"""Exercise the deployed Alembic path, not only the canonical schema fixture."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.config import get_settings

API_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    return config


@pytest.fixture(scope="session")
def migrated_schema() -> Iterator[str]:
    schema = f"lh_migration_{uuid.uuid4().hex[:10]}"
    admin = create_engine(get_settings().sqlalchemy_url, poolclass=NullPool, future=True)

    with admin.begin() as connection:
        for extension in ("postgis", "vector", "pg_trgm"):
            connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS {extension}"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    try:
        config = _alembic_config()
        config.attributes["schema"] = schema
        command.upgrade(config, "head")
        yield schema
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_alembic_upgrade_reaches_head_with_current_contract(migrated_schema: str) -> None:
    engine = create_engine(get_settings().sqlalchemy_url, poolclass=NullPool, future=True)

    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text(f'SELECT version_num FROM "{migrated_schema}".alembic_version')
            ).scalar_one()
            tables = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            advisory_columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = 'advisory'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            exposure_fk_schemas = set(
                connection.execute(
                    text(
                        "SELECT ccu.table_schema "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.constraint_column_usage ccu "
                        "  ON ccu.constraint_name = tc.constraint_name "
                        " AND ccu.constraint_schema = tc.constraint_schema "
                        "WHERE tc.table_schema = :schema "
                        "  AND tc.table_name = 'place_exposure' "
                        "  AND tc.constraint_type = 'FOREIGN KEY'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            exposure_build_fk_schemas = set(
                connection.execute(
                    text(
                        "SELECT ccu.table_schema "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.constraint_column_usage ccu "
                        "  ON ccu.constraint_name = tc.constraint_name "
                        " AND ccu.constraint_schema = tc.constraint_schema "
                        "WHERE tc.table_schema = :schema "
                        "  AND tc.table_name = 'place_exposure_build' "
                        "  AND tc.constraint_type = 'FOREIGN KEY'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            exposure_build_columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "  AND table_name = 'place_exposure_build'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            structure_build_columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "  AND table_name = 'place_structure_build'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
    finally:
        engine.dispose()

    assert revision == ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert {
        "advisory",
        "place_structures",
        "place_structure_build",
        "place_exposure",
        "place_exposure_build",
    } <= tables
    assert {"wind_field_34", "wind_field_50", "wind_field_64"} <= advisory_columns
    assert not {"wind_prob_34", "wind_prob_50", "wind_prob_64"} & advisory_columns
    assert exposure_fk_schemas == {migrated_schema}
    assert exposure_build_fk_schemas == {migrated_schema}
    assert "structure_rows_sha256" in structure_build_columns
    assert {
        "hazard_event_id",
        "inventory_fingerprint",
        "structure_rows_sha256",
        "advisory_fingerprint",
        "advisory_count",
        "exposure_row_count",
        "exposed_structure_count",
        "exposure_rows_sha256",
    } <= exposure_build_columns


def test_alembic_incremental_0003_to_0004_creates_digest_markers(
    migrated_schema: str,
) -> None:
    """Exercise 0004's DDL instead of only fresh 0001's canonical schema."""
    config = _alembic_config()
    config.attributes["schema"] = migrated_schema
    engine = create_engine(get_settings().sqlalchemy_url, poolclass=NullPool, future=True)

    try:
        command.downgrade(config, "0003_building_inventory")
        with engine.connect() as connection:
            tables_at_0003 = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
        assert "place_structure_build" not in tables_at_0003
        assert "place_exposure_build" not in tables_at_0003

        command.upgrade(config, "head")
        with engine.connect() as connection:
            marker_columns = {
                table: set(
                    connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = :schema AND table_name = :table"
                        ),
                        {"schema": migrated_schema, "table": table},
                    ).scalars()
                )
                for table in ("place_structure_build", "place_exposure_build")
            }

        assert "structure_rows_sha256" in marker_columns["place_structure_build"]
        assert {
            "structure_rows_sha256",
            "exposure_rows_sha256",
        } <= marker_columns["place_exposure_build"]
    finally:
        # Leave the shared migration fixture at head even when an assertion fails.
        command.upgrade(config, "head")
        engine.dispose()
