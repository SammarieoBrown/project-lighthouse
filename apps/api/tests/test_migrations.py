"""Exercise the deployed Alembic path, not only the canonical schema fixture."""

from __future__ import annotations

import uuid
import json
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
        for extension in ("postgis", "vector", "pg_trgm", "pgcrypto"):
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
            hazard_external_ref_indexes = set(
                connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = :schema AND tablename = 'hazard_event'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            approval_columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = 'approval'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            approval_indexes = set(
                connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = :schema AND tablename = 'approval'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            approval_rules = set(
                connection.execute(
                    text(
                        "SELECT rulename FROM pg_rules "
                        "WHERE schemaname = :schema AND tablename = 'approval'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            approval_constraints = set(
                connection.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_schema = :schema AND table_name = 'approval'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            verification_indexes = set(
                connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = :schema AND tablename = 'verification'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            verification_guard = connection.execute(
                text(
                    """
                    SELECT pg_get_functiondef(p.oid)
                      FROM pg_proc p
                      JOIN pg_namespace n ON n.oid = p.pronamespace
                     WHERE n.nspname = :schema
                       AND p.proname = 'verification_snapshot_guard'
                    """
                ),
                {"schema": migrated_schema},
            ).scalar_one()
    finally:
        engine.dispose()

    assert revision == ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert {
        "advisory",
        "place_structures",
        "place_structure_build",
        "place_exposure",
        "place_exposure_build",
        "human_credential",
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
    assert "hazard_event_external_ref_uidx" in hazard_external_ref_indexes
    assert {"idempotency_key", "request_hash"} <= approval_columns
    assert {
        "approval_gate_subject_uidx",
        "approval_idempotency_uidx",
    } <= approval_indexes
    assert {"approval_no_update", "approval_no_delete"} <= approval_rules
    assert {
        "approval_gate_role_chk",
        "approval_recent_reauth_chk",
        "approval_request_pair_chk",
    } <= approval_constraints
    assert "verification_overrides_uidx" in verification_indexes
    assert (
        "verification override must bind latest agent review evidence"
        in verification_guard
    )


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


def test_0009_refuses_agent_rows_that_claim_override_authority() -> None:
    schema = f"lh_verification_migration_{uuid.uuid4().hex[:10]}"
    admin = create_engine(get_settings().sqlalchemy_url, poolclass=NullPool, future=True)
    config = _alembic_config()
    config.attributes["schema"] = schema
    event_id, storm_file_id, claim_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    parent_id, forged_id = uuid.uuid4(), uuid.uuid4()
    signals = {
        name: {"present": False, "score": None, "evidence": {}}
        for name in (
            "hazard_sufficiency",
            "satellite_change",
            "neighbour_corroboration",
            "registry_match",
            "media_integrity",
        )
    }

    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        command.upgrade(config, "head")
        command.downgrade(config, "0008_act3_settlement")

        isolated = create_engine(
            get_settings().sqlalchemy_url,
            poolclass=NullPool,
            future=True,
            connect_args={"options": f"-csearch_path={schema},public"},
        )
        try:
            with isolated.begin() as connection:
                connection.execute(
                    text("INSERT INTO hazard_event (id, name) VALUES (:id, 'test')"),
                    {"id": event_id},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO storm_file (
                          id, phone_hash, state, structure, people, synthetic
                        ) VALUES (
                          :id, :phone_hash, 'AFFECTED', '{}'::jsonb, '{}'::jsonb, true
                        )
                        """
                    ),
                    {
                        "id": storm_file_id,
                        "phone_hash": f"migration-{storm_file_id.hex}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO claim (
                          id, claim_ref, storm_file_id, hazard_event_id, channel
                        ) VALUES (
                          :id, :claim_ref, :storm_file_id, :event_id, 'test'
                        )
                        """
                    ),
                    {
                        "id": claim_id,
                        "claim_ref": f"MIG-{claim_id.hex[:12]}",
                        "storm_file_id": storm_file_id,
                        "event_id": event_id,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO verification (
                          id, claim_id, signals, confidence, verdict, actor_kind,
                          agent_name, model_version, threshold_version, rationale,
                          capped, created_at
                        ) VALUES (
                          :parent_id, :claim_id, CAST(:signals AS jsonb), 0.2,
                          'REVIEW', 'AGENT', 'verification_agent', 'test-v1',
                          'threshold-v1', 'parent', true, '2026-08-03T12:00:00Z'
                        )
                        """
                    ),
                    {
                        "parent_id": parent_id,
                        "claim_id": claim_id,
                        "signals": json.dumps(signals),
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO verification (
                          id, claim_id, signals, confidence, verdict, actor_kind,
                          agent_name, model_version, threshold_version, rationale,
                          capped, overrides_id, created_at
                        ) VALUES (
                          :forged_id, :claim_id, CAST(:signals AS jsonb), 0.2,
                          'REVIEW', 'AGENT', 'verification_agent', 'test-v1',
                          'threshold-v1', 'forged agent override', true,
                          :parent_id, '2026-08-03T12:00:01Z'
                        )
                        """
                    ),
                    {
                        "forged_id": forged_id,
                        "parent_id": parent_id,
                        "claim_id": claim_id,
                        "signals": json.dumps(signals),
                    },
                )
        finally:
            isolated.dispose()

        with pytest.raises(RuntimeError, match="invalid_agent_rows=1"):
            command.upgrade(config, "head")
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_act3_settlement_migration_installs_receipt_guards(
    migrated_schema: str,
) -> None:
    """Prove the deployed migration path has the same hard gates as 0001."""
    config = _alembic_config()
    config.attributes["schema"] = migrated_schema
    command.upgrade(config, "head")
    engine = create_engine(get_settings().sqlalchemy_url, poolclass=NullPool, future=True)

    try:
        with engine.connect() as connection:
            batch_columns = {
                row.column_name: row.is_nullable
                for row in connection.execute(
                    text(
                        "SELECT column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "AND table_name = 'disbursement_batch'"
                    ),
                    {"schema": migrated_schema},
                )
            }
            disbursement_columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = :schema "
                        "AND table_name = 'disbursement'"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            indexes = set(
                connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = :schema "
                        "AND tablename IN ('disbursement', 'ledger_entry')"
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
            triggers = set(
                connection.execute(
                    text(
                        """
                        SELECT tgname FROM pg_trigger
                         WHERE tgrelid IN (
                           to_regclass(format(
                             '%I.%I', CAST(:schema AS text), 'disbursement_batch'
                           )),
                           to_regclass(format(
                             '%I.%I', CAST(:schema AS text), 'disbursement'
                           )),
                           to_regclass(format(
                             '%I.%I', CAST(:schema AS text), 'ledger_entry'
                           ))
                         ) AND NOT tgisinternal
                        """
                    ),
                    {"schema": migrated_schema},
                ).scalars()
            )
    finally:
        engine.dispose()

    assert batch_columns["approval_id"] == "NO"
    assert batch_columns["snapshot_hash"] == "NO"
    assert {
        "executor_provider",
        "snapshot_hash",
        "execution_requested_by",
        "execution_idempotency_key",
        "execution_request_hash",
        "provider_confirmation_hash",
    } <= disbursement_columns
    assert {
        "disbursement_allocation_uidx",
        "disbursement_batch_uidx",
        "disbursement_execution_idempotency_uidx",
        "ledger_disbursement_batch_signed_subject_uidx",
        "ledger_disbursement_executed_subject_uidx",
        "ledger_disbursement_confirmed_subject_uidx",
    } <= indexes
    assert {
        "disbursement_batch_signed_guard_trigger",
        "disbursement_lifecycle_guard_trigger",
        "ledger_disbursement_receipt_guard_trigger",
        "disbursement_batch_receipt_complete_trigger",
        "disbursement_receipt_complete_trigger",
    } <= triggers
