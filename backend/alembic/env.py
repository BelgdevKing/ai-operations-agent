"""Alembic migration environment.

Runs against the async engine, so migrations use the same driver and the same
URL as the application - there is no second, sync database URL to keep in step.

Offline mode (``alembic upgrade head --sql``) emits SQL without connecting,
which is how the migration chain can be checked in CI without a database.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings

# Importing the models package populates Base.metadata; without it autogenerate
# would see an empty schema and propose dropping every table.
from app.models import Base  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes, which Alembic ignores by default.
        compare_type=True,
        # Detect changes to server-side defaults.
        compare_server_default=True,
        # Wrap each migration in its own transaction.
        transaction_per_migration=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL for the migrations without a database connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Apply the migrations against a live database."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
