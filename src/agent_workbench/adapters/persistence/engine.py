"""Building the database engine, with the limits set at connection time.

A statement timeout applied per query is a timeout somebody eventually forgets.
Setting it as a server-side session parameter means every statement on every
connection from this engine inherits it, including the ones written later by
someone who has not read this file.

The three DSNs in the configuration exist because the three uses have different
connection rules: ordinary short transactions may be pooled, the task guard
must pin one physical session for as long as it holds its advisory lock, and
the listener needs a session of its own. This module builds the ordinary one.
The guard and listener engines arrive with the coordination work package, which
is where the rules they need start to matter.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_POOL_SIZE = 10
DEFAULT_MAX_OVERFLOW = 10

ASYNCPG_PREFIX = "postgresql+asyncpg://"


def create_query_engine(
    dsn: str,
    *,
    application_name: str = "agent-workbench",
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    pool_size: int = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
) -> AsyncEngine:
    """Create the engine used for ordinary short transactions.

    ``application_name`` is not decoration: it is what makes a connection
    identifiable in ``pg_stat_activity`` when something has to be diagnosed or
    terminated from outside the process.
    """

    if not dsn.startswith(ASYNCPG_PREFIX):
        # The configuration already refuses anything else. Repeating the check
        # here keeps a directly constructed engine from quietly falling back to
        # a blocking driver inside an event loop.
        raise ValueError(f"the query engine requires a {ASYNCPG_PREFIX} DSN")
    if statement_timeout_ms < 1:
        raise ValueError("statement_timeout_ms must be positive")

    return create_async_engine(
        dsn,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": application_name,
                "statement_timeout": str(statement_timeout_ms),
            }
        },
    )


__all__ = [
    "DEFAULT_MAX_OVERFLOW",
    "DEFAULT_POOL_SIZE",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "create_query_engine",
]
