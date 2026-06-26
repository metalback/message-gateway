"""Alembic environment (async).

This file is invoked by ``alembic`` for both online (DB-connected)
and offline (SQL emission) runs. The important customisations over
the default async template are:

- ``target_metadata`` is wired to :data:`app.models.base.Base.metadata`
  so autogenerate can detect model changes.
- The database URL is read from :class:`app.config.Settings` (which
  in turn reads the environment / ``.env``) instead of the static
  ``sqlalchemy.url`` value in ``alembic.ini``. This keeps the
  connection string consistent with the rest of the backend.
- The model package is imported for its side-effects so every
  module-level ``Base`` subclass registers itself.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ---------------------------------------------------------------------------
# Application wiring: import the settings object and the declarative
# base. Importing the routes / models package ensures every model is
# registered before Alembic reads ``target_metadata``.
# ---------------------------------------------------------------------------
from app.config import get_settings  # noqa: E402
from app.models.base import Base  # noqa: E402
import app.models  # noqa: F401, E402  -- side-effect import

# Alembic Config object (parses alembic.ini).
config = context.config

# Configure Python logging from alembic.ini if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the application database URL so ``alembic`` and the
# FastAPI runtime share a single source of truth.
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Metadata for ``autogenerate``.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL statements without an active DB connection.

    Useful for generating a script that a DBA can run by hand.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point invoked by Alembic in online mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
