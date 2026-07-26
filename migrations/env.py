"""Alembic environment.

The DSN is read from ``AW_DATABASE__DSN`` rather than from ``alembic.ini``: a
migration runs against the same database the application does, from the same
secret source, and a URL committed to an ini file is a credential waiting to be
committed with it.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from agent_workbench.adapters.persistence.models import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata

DSN_ENV_VAR = "AW_DATABASE__DSN"


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV_VAR)
    if not dsn:
        raise RuntimeError(f"{DSN_ENV_VAR} must be set to run migrations")
    return dsn


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Alembic compares types only when asked; without it a widened column
        # is a change no autogenerate run would ever mention.
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        {"sqlalchemy.url": _dsn()},
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
