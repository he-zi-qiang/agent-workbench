"""PostgreSQL session-advisory implementation of :mod:`execution_guard`.

This adapter intentionally has its own ``NullPool`` engine.  A session advisory
lock belongs to one backend session, so borrowing a normal pooled connection
for each health check would make the lock disappear between graph nodes.  The
acquired guard owns one ``AsyncConnection`` from lock acquisition through
release; it must be pointed at a direct PostgreSQL endpoint or a session pool,
never PgBouncer transaction pooling.

The Task Worker composition constructs this factory separately from its normal
query engine. It acquires before graph execution, monitors ``lost`` alongside
the graph and lease heartbeat, then releases in the Worker's cleanup path.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from agent_workbench.adapters.persistence.engine import ASYNCPG_PREFIX
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.execution_guard import GuardUnavailableError

_TRY_LOCK: Final = text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))")
_UNLOCK: Final = text("SELECT pg_advisory_unlock(CAST(:key AS bigint))")
_BACKEND_PID: Final = text("SELECT pg_backend_pid()")


def stable_signed_int64(task_id: str) -> int:
    """Map an identifier to PostgreSQL's signed bigint advisory-lock key.

    Python's ``hash()`` is deliberately unsuitable: its randomised per-process
    seed would make two Workers choose different lock keys for one Task.  The
    first SHA-256 eight bytes are stable across platforms and interpreted as a
    signed big-endian int64 because that is PostgreSQL's one-argument advisory
    lock domain.
    """

    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class PostgresExecutionGuardFactory:
    """Acquire pinned PostgreSQL advisory-lock sessions for Task execution."""

    def __init__(
        self,
        guard_dsn: str,
        *,
        healthcheck_seconds: float = 5.0,
        application_name: str = "agent-workbench-guard",
    ) -> None:
        if not guard_dsn.startswith(ASYNCPG_PREFIX):
            raise ValueError(f"the guard requires a {ASYNCPG_PREFIX} DSN")
        if healthcheck_seconds <= 0:
            raise ValueError("healthcheck_seconds must be positive")
        self._healthcheck_seconds = healthcheck_seconds
        # AUTOCOMMIT prevents a monitor SELECT from leaving one long-running
        # transaction around a Task. The *session* remains pinned; only the
        # transaction boundary is automatic.
        self._engine: AsyncEngine = create_async_engine(
            guard_dsn,
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=False,
            connect_args={
                "server_settings": {"application_name": application_name},
            },
        )

    async def acquire(
        self,
        *,
        task_id: Identifier,
        worker_id: Identifier,
        epoch: int,
    ) -> PostgresExecutionGuard:
        """Pin a physical backend, then acquire its session advisory lock."""

        if epoch < 1:
            raise ValueError("epoch must be positive")
        key = stable_signed_int64(task_id)
        connection: AsyncConnection | None = None
        try:
            connection = await self._engine.connect()
            backend_pid = await _backend_pid(connection)
            acquired = bool(
                (await connection.execute(_TRY_LOCK, {"key": key})).scalar_one()
            )
            if not acquired:
                raise GuardUnavailableError(task_id)
            # It is the same connection object, but make the invariant visible
            # and verify it before handing a guard to the caller.
            if await _backend_pid(connection) != backend_pid:
                raise GuardUnavailableError(task_id)
            return PostgresExecutionGuard(
                connection=connection,
                task_id=task_id,
                worker_id=worker_id,
                epoch=epoch,
                backend_pid=backend_pid,
                lock_key=key,
                healthcheck_seconds=self._healthcheck_seconds,
            )
        except GuardUnavailableError:
            if connection is not None:
                await connection.close()
            raise
        except (SQLAlchemyError, OSError) as error:
            if connection is not None:
                await connection.close()
            raise GuardUnavailableError(task_id) from error

    async def dispose(self) -> None:
        """Close the dedicated guard engine after all acquired guards release."""

        await self._engine.dispose()


class PostgresExecutionGuard:
    """An acquired session lock and the one physical connection that owns it."""

    __slots__ = (
        "_connection",
        "_healthcheck_seconds",
        "_lock_key",
        "_lost",
        "_monitor",
        "_released",
        "backend_pid",
        "epoch",
        "task_id",
        "worker_id",
    )

    def __init__(
        self,
        *,
        connection: AsyncConnection,
        task_id: Identifier,
        worker_id: Identifier,
        epoch: int,
        backend_pid: int,
        lock_key: int,
        healthcheck_seconds: float,
    ) -> None:
        self._connection = connection
        self.task_id = task_id
        self.worker_id = worker_id
        self.epoch = epoch
        self.backend_pid = backend_pid
        self._lock_key = lock_key
        self._healthcheck_seconds = healthcheck_seconds
        self._lost = asyncio.Event()
        self._released = False
        self._monitor = asyncio.create_task(
            self._monitor_connection(),
            name=f"execution-guard:{task_id}",
        )

    @property
    def lost(self) -> asyncio.Event:
        return self._lost

    @property
    def lock_key(self) -> int:
        """Expose the exact key for the checkpointer's guard fence."""

        return self._lock_key

    async def healthcheck(self) -> bool:
        """Verify the connection is live and still names its original backend."""

        if self._released or self._lost.is_set():
            return False
        try:
            current_pid = await _backend_pid(self._connection)
        except (SQLAlchemyError, OSError):
            self._lost.set()
            return False
        if current_pid != self.backend_pid:
            self._lost.set()
            return False
        return True

    async def release(self) -> None:
        """Explicitly unlock, then close the exact physical lock-owning session."""

        if self._released:
            return
        self._released = True
        self._monitor.cancel()
        with suppress(asyncio.CancelledError):
            await self._monitor
        try:
            # A lost backend has already released its session locks. For a
            # healthy backend this explicit statement is the orderly handoff
            # that makes lock release testable rather than an accident of GC.
            if not self._lost.is_set():
                unlocked = bool(
                    (
                        await self._connection.execute(
                            _UNLOCK,
                            {"key": self._lock_key},
                        )
                    ).scalar_one()
                )
                if not unlocked:
                    self._lost.set()
        except (SQLAlchemyError, OSError):
            self._lost.set()
        finally:
            await self._connection.close()

    async def _monitor_connection(self) -> None:
        try:
            while not self._released:
                await asyncio.sleep(self._healthcheck_seconds)
                if not await self.healthcheck():
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            # A monitor is a loss detector. Do not let a monitoring failure
            # become an unobserved task exception while graph work continues.
            self._lost.set()


async def _backend_pid(connection: AsyncConnection) -> int:
    return int((await connection.execute(_BACKEND_PID)).scalar_one())


__all__ = [
    "PostgresExecutionGuard",
    "PostgresExecutionGuardFactory",
    "stable_signed_int64",
]
