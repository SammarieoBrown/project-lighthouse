"""Alembic environment.

The database URL comes from settings, never from alembic.ini — there is exactly
one place the connection string lives and it is the repo-root ``.env``.
"""

from __future__ import annotations

import re

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().sqlalchemy_url)

target_metadata = Base.metadata


def _migration_schema() -> str | None:
    """Return the explicitly requested isolated schema, when one is supplied.

    Production runs leave this unset and keep Alembic's normal search path.
    CI sets it through ``Config.attributes`` so the complete revision chain can
    be exercised without ever creating or modifying objects in ``public``.
    """

    schema = config.attributes.get("schema")
    if schema is None:
        return None
    if not isinstance(schema, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema) is None:
        raise ValueError("Alembic schema must be a simple PostgreSQL identifier")
    return schema


def run_migrations_offline() -> None:
    schema = _migration_schema()
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    schema = _migration_schema()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        if schema is not None:
            connection.exec_driver_sql(f'SET search_path TO "{schema}", public')
            # ``SET`` autobegins under SQLAlchemy 2. Commit that tiny setup
            # transaction before Alembic opens its own migration transaction.
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=schema,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
