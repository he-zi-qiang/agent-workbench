"""LangGraph's ``BaseCheckpointSaver``, over this project's PostgreSQL stack.

ADR-014 chose to write this rather than install ``langgraph-checkpoint-postgres``:
that package resolves ``psycopg`` and ``psycopg-pool``, both LGPL-3.0-only, which
is what the licence gate exists to catch, and it would be a second PostgreSQL
driver beside asyncpg. So the saver is implemented against the engine and tables
the rest of the project already uses.

What it stores is not interpreted. ``serde.dumps_typed`` returns a (type, bytes)
pair and ``loads_typed`` takes the same pair back, so both halves are written and
neither is read as anything but bytes. The exception is checkpoint metadata,
which the contract documents as a mapping of JSON scalars and which ``alist``
filters by key -- bytes cannot answer that query, so metadata is JSONB.

Only the async half exists. Every caller in this project is async, and a sync
entry point that quietly started an event loop would be a deadlock waiting for
the first caller who already had one; the sync methods refuse instead.

The contract this implements is not a stable public API. ``langgraph`` is pinned
``>=0.6,<0.7`` for that reason, and raising the bound means re-running this
module's tests rather than trusting a green CI.
"""

from __future__ import annotations

import random
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Final, NoReturn, cast

# langgraph ships no type stubs, so strict pyright cannot see through it.
# Narrowed here the same way the workflow adapter narrows it, rather than by
# relaxing the type checker for the whole package.
from langgraph.checkpoint.base import (  # pyright: ignore[reportMissingTypeStubs]
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.base import (
    CheckpointTuple as LangGraphCheckpointTuple,
)
from sqlalchemy import Select, and_, delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import (
    task_runs,
    workflow_checkpoint_blobs,
    workflow_checkpoint_writes,
    workflow_checkpoints,
)
from agent_workbench.domain.task_registry import TERMINAL_STATUSES
from agent_workbench.ports.fault_injector import FaultInjector
from agent_workbench.ports.task_registry import ExecutionLease, StaleExecutionError
from agent_workbench.ports.task_workflow import (
    CHECKPOINT_FENCE_EPOCH_KEY,
    CHECKPOINT_FENCE_GUARD_KEY_KEY,
    CHECKPOINT_FENCE_GUARD_PID_KEY,
    CHECKPOINT_FENCE_TASK_ID_KEY,
    CHECKPOINT_FENCE_WORKER_ID_KEY,
    CheckpointFence,
)

# The shapes this module exchanges with LangGraph, all opaque to the type
# checker: RunnableConfig, Checkpoint, CheckpointMetadata, CheckpointTuple.
Config = Any
Checkpoint = Any
CheckpointMetadata = Any
CheckpointTuple = Any

# A channel whose value at this version is nothing. The type carries the fact
# and the payload is empty, so "written as nothing" stays distinguishable from
# "never written" -- which is a missing row.
EMPTY_BLOB_TYPE: Final[str] = "empty"

_SYNC_REFUSAL: Final[str] = (
    "PostgresCheckpointSaver is async-only: call the a-prefixed method. "
    "A synchronous entry point here would have to start an event loop, and "
    "every caller in this project already has one."
)

# A one-argument ``pg_advisory_lock(bigint)`` occupies the two unsigned 32-bit
# fields below with ``objsubid = 1``. Casting the OID columns to bigint avoids
# treating either half as a signed SQL integer when the high bit is set.
_ADVISORY_LOCK_HELD: Final = text(
    """
    SELECT EXISTS (
        SELECT 1
        FROM pg_locks
        WHERE locktype = 'advisory'
          AND granted
          AND pid = CAST(:backend_pid AS integer)
          AND classid::bigint = :classid
          AND objid::bigint = :objid
          AND objsubid = 1
          AND database = (
              SELECT oid FROM pg_database WHERE datname = current_database()
          )
    )
    """
)


def _refuse_sync() -> NoReturn:
    raise NotImplementedError(_SYNC_REFUSAL)


class CheckpointFenceRequiredError(RuntimeError):
    """A fenced saver was asked to write without a complete lease token."""


class StaleCheckpointWriteError(StaleExecutionError):
    """The lease or its guard session no longer authorizes a checkpoint write."""

    def __init__(self, fence: CheckpointFence, *, reason: str) -> None:
        self.reason = reason
        super().__init__(
            ExecutionLease(
                task_id=fence.task_id,
                worker_id=fence.worker_id,
                epoch=fence.epoch,
            )
        )


class ThreadStillExecutingError(RuntimeError):
    """A thread's checkpoints were asked for while its Task can still run.

    Checkpoints *are* the execution position, so deleting them for a Task that
    is not finished is not retention -- it is destroying the only thing a
    restarted Worker could recover from, and it would look like a Task that
    simply started over.
    """

    def __init__(self, *, thread_id: str, task_id: str, status: str) -> None:
        self.thread_id = thread_id
        self.task_id = task_id
        self.status = status
        super().__init__(
            f"thread {thread_id} belongs to task {task_id}, which is {status}"
        )


class CheckpointCorruptionError(RuntimeError):
    """A checkpoint references durable bytes that are no longer present."""


class PostgresCheckpointSaver(BaseCheckpointSaver[str]):
    """Graph execution position, durable across process restarts."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        serde: Any | None = None,
        require_fence: bool = False,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        super().__init__(serde=serde)
        self._engine = engine
        self._require_fence = require_fence
        self._fault_injector = fault_injector

    # -- writing ------------------------------------------------------------

    async def aput(
        self,
        config: Config,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Mapping[str, str | int | float],
    ) -> Config:
        configurable = config["configurable"]
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        fence = self._fence_for_write(config)

        remainder = dict(checkpoint)
        # Channel values live in their own table keyed by version, so they are
        # removed from the checkpoint before it is serialised. Leaving them in
        # would store every channel again on every step, and store each one
        # twice on the step that changed it.
        values = cast("dict[str, Any]", remainder.pop("channel_values"))
        payload_type, payload = self.serde.dumps_typed(remainder)

        blob_rows = [
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "channel": channel,
                "version": str(version),
                **_serialised(
                    self.serde.dumps_typed(values[channel])
                    if channel in values
                    else (EMPTY_BLOB_TYPE, b"")
                ),
            }
            for channel, version in new_versions.items()
        ]

        # One transaction. A checkpoint whose blobs were only half written
        # would restore a state missing channels, and it would restore it
        # during recovery, which is the one time nobody is watching.
        async with self._engine.begin() as connection:
            await self._assert_fence(connection, thread_id, fence)
            if blob_rows:
                blob_insert = pg_insert(workflow_checkpoint_blobs)
                await connection.execute(
                    blob_insert.on_conflict_do_update(
                        index_elements=[
                            "thread_id",
                            "checkpoint_ns",
                            "channel",
                            "version",
                        ],
                        set_={
                            "payload_type": blob_insert.excluded.payload_type,
                            "payload": blob_insert.excluded.payload,
                        },
                    ),
                    blob_rows,
                )
            if self._fault_injector is not None:
                # Deliberately after the first durable operation but before
                # the checkpoint row. A raised/crashed transaction must leave
                # neither half visible, proving the atomic boundary.
                await self._fault_injector.hit("inside_checkpoint_put")
            checkpoint_insert = pg_insert(workflow_checkpoints)
            await connection.execute(
                checkpoint_insert.on_conflict_do_update(
                    index_elements=["thread_id", "checkpoint_ns", "checkpoint_id"],
                    set_={
                        "parent_checkpoint_id": (
                            checkpoint_insert.excluded.parent_checkpoint_id
                        ),
                        "payload_type": checkpoint_insert.excluded.payload_type,
                        "payload": checkpoint_insert.excluded.payload,
                        "metadata": checkpoint_insert.excluded.metadata,
                    },
                ),
                [
                    {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint["id"],
                        "parent_checkpoint_id": configurable.get("checkpoint_id"),
                        "payload_type": payload_type,
                        "payload": payload,
                        # Merges the scalars LangGraph puts on the config, and
                        # strips the NUL that JSONB would refuse.
                        "metadata": dict(get_checkpoint_metadata(config, metadata)),
                    }
                ],
            )
        return _config(thread_id, checkpoint_ns, checkpoint["id"], fence=fence)

    async def aput_writes(
        self,
        config: Config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config["configurable"]
        fence = self._fence_for_write(config)
        common = {
            "thread_id": configurable["thread_id"],
            "checkpoint_ns": configurable.get("checkpoint_ns", ""),
            "checkpoint_id": configurable["checkpoint_id"],
            "task_id": task_id,
            "task_path": task_path,
        }

        # Two groups, because they resolve a collision differently. An ordinary
        # write keeps the first value stored for its slot: a task that is
        # retried after its writes were already durable must not replace them,
        # or a resumed step would see a second, later result for work that had
        # already been recorded. The special slots -- error, interrupt, resume,
        # scheduled -- are the opposite: the newest one is the current state of
        # that task, so it overwrites.
        first_wins: list[dict[str, Any]] = []
        newest_wins: list[dict[str, Any]] = []
        for position, (channel, value) in enumerate(writes):
            index = WRITES_IDX_MAP.get(channel, position)
            row = {
                **common,
                "idx": index,
                "channel": channel,
                **_serialised(self.serde.dumps_typed(value)),
            }
            (first_wins if index >= 0 else newest_wins).append(row)

        if not first_wins and not newest_wins:
            return

        key = ["thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"]
        async with self._engine.begin() as connection:
            await self._assert_fence(connection, common["thread_id"], fence)
            if first_wins:
                await connection.execute(
                    pg_insert(workflow_checkpoint_writes).on_conflict_do_nothing(
                        index_elements=key
                    ),
                    first_wins,
                )
            if newest_wins:
                write_insert = pg_insert(workflow_checkpoint_writes)
                await connection.execute(
                    write_insert.on_conflict_do_update(
                        index_elements=key,
                        set_={
                            "channel": write_insert.excluded.channel,
                            "task_path": write_insert.excluded.task_path,
                            "payload_type": write_insert.excluded.payload_type,
                            "payload": write_insert.excluded.payload,
                        },
                    ),
                    newest_wins,
                )

    # -- reading ------------------------------------------------------------

    async def aget_tuple(self, config: Config) -> CheckpointTuple | None:
        configurable = config["configurable"]
        thread_id = configurable["thread_id"]
        checkpoint_ns = configurable.get("checkpoint_ns", "")
        checkpoint_id = configurable.get("checkpoint_id")

        query = select(workflow_checkpoints).where(
            workflow_checkpoints.c.thread_id == thread_id,
            workflow_checkpoints.c.checkpoint_ns == checkpoint_ns,
        )
        if checkpoint_id is not None:
            query = query.where(workflow_checkpoints.c.checkpoint_id == checkpoint_id)
        else:
            # Checkpoint ids are UUIDv6: ordering them is ordering them by the
            # time they were minted, so the greatest one is the latest step.
            query = query.order_by(workflow_checkpoints.c.checkpoint_id.desc())

        fence = _fence_from_config(config)
        async with self._repeatable_read_connection() as connection:
            row = (await connection.execute(query.limit(1))).mappings().first()
            if row is None:
                return None
            assembled = await self._assemble(connection, [row], fence=fence)
        return assembled[0]

    async def alist(
        self,
        config: Config | None,
        *,
        filter: Mapping[str, Any] | None = None,
        before: Config | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        query = select(workflow_checkpoints).order_by(
            workflow_checkpoints.c.checkpoint_id.desc()
        )
        query = _restrict(query, config, before, filter)
        if limit is not None:
            query = query.limit(limit)

        # Read fully, then yield. An async generator that held a connection
        # across its yields would keep one for as long as the caller took to
        # consume it, and leak it outright if the caller stopped early. The
        # cost is that an unlimited listing holds a thread's history in memory
        # -- bounded by the steps that thread has run, and bounded by `limit`
        # whenever the caller supplies one.
        fence = _fence_from_config(config) if config is not None else None
        async with self._repeatable_read_connection() as connection:
            rows = (await connection.execute(query)).mappings().all()
            if not rows:
                return
            assembled = await self._assemble(connection, list(rows), fence=fence)
        for item in assembled:
            yield item

    async def _assemble(
        self,
        connection: Any,
        rows: list[RowMapping],
        *,
        fence: CheckpointFence | None = None,
    ) -> list[CheckpointTuple]:
        """Turn checkpoint rows into tuples, in three queries rather than 2n+1."""

        restored = [
            (row, self.serde.loads_typed((row["payload_type"], bytes(row["payload"]))))
            for row in rows
        ]

        wanted_blobs = {
            (row["thread_id"], row["checkpoint_ns"], channel, str(version))
            for row, checkpoint in restored
            for channel, version in checkpoint["channel_versions"].items()
        }
        blobs = await self._load_blobs(connection, wanted_blobs)
        writes = await self._load_writes(
            connection,
            {
                (row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"])
                for row in rows
            },
        )

        assembled: list[CheckpointTuple] = []
        for row, checkpoint in restored:
            thread_id = row["thread_id"]
            checkpoint_ns = row["checkpoint_ns"]
            parent = row["parent_checkpoint_id"]
            channel_values: dict[str, Any] = {}
            for channel, version in checkpoint["channel_versions"].items():
                key = (thread_id, checkpoint_ns, channel, str(version))
                blob = blobs.get(key)
                if blob is None:
                    raise CheckpointCorruptionError(
                        "checkpoint references a missing channel blob: "
                        f"thread={thread_id} namespace={checkpoint_ns!r} "
                        f"channel={channel} version={version}"
                    )
                payload_type, value = blob
                if payload_type != EMPTY_BLOB_TYPE:
                    channel_values[channel] = value
            returned_fence = fence or _fence_from_metadata(row["metadata"])
            assembled.append(
                LangGraphCheckpointTuple(
                    config=_config(
                        thread_id,
                        checkpoint_ns,
                        row["checkpoint_id"],
                        fence=returned_fence,
                    ),
                    # The checkpoint came out of LangGraph's own serialiser, so
                    # it is already whatever shape LangGraph put in; the cast
                    # says that rather than re-asserting the TypedDict here.
                    checkpoint=cast(
                        "Any", {**checkpoint, "channel_values": channel_values}
                    ),
                    metadata=row["metadata"],
                    parent_config=(
                        _config(
                            thread_id,
                            checkpoint_ns,
                            parent,
                            fence=returned_fence,
                        )
                        if parent is not None
                        else None
                    ),
                    pending_writes=writes.get(
                        (thread_id, checkpoint_ns, row["checkpoint_id"]), []
                    ),
                )
            )
        return assembled

    async def _load_blobs(
        self, connection: Any, keys: set[tuple[str, str, str, str]]
    ) -> dict[tuple[str, str, str, str], tuple[str, Any | None]]:
        if not keys:
            return {}
        rows = (
            await connection.execute(
                select(workflow_checkpoint_blobs).where(
                    tuple_(
                        workflow_checkpoint_blobs.c.thread_id,
                        workflow_checkpoint_blobs.c.checkpoint_ns,
                        workflow_checkpoint_blobs.c.channel,
                        workflow_checkpoint_blobs.c.version,
                    ).in_(sorted(keys))
                )
            )
        ).mappings()
        return {
            (
                row["thread_id"],
                row["checkpoint_ns"],
                row["channel"],
                row["version"],
            ): (
                row["payload_type"],
                None
                if row["payload_type"] == EMPTY_BLOB_TYPE
                else self.serde.loads_typed(
                    (row["payload_type"], bytes(row["payload"]))
                ),
            )
            for row in rows
        }

    async def _assert_fence(
        self,
        connection: Any,
        thread_id: str,
        fence: CheckpointFence | None,
    ) -> None:
        """Lock and validate the owning Task row before any checkpoint write."""

        if fence is None:
            return
        row = (
            await connection.execute(
                select(task_runs.c.task_id)
                .where(
                    task_runs.c.task_id == fence.task_id,
                    task_runs.c.thread_id == thread_id,
                    task_runs.c.status == "running",
                    task_runs.c.lease_owner == fence.worker_id,
                    task_runs.c.lease_epoch == fence.epoch,
                    task_runs.c.lease_until > func.statement_timestamp(),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise StaleCheckpointWriteError(fence, reason="lease is not live")
        if fence.guard_backend_pid is None or fence.guard_lock_key is None:
            if self._require_fence:
                raise CheckpointFenceRequiredError(
                    "PostgresCheckpointSaver requires a guard-backed checkpoint fence"
                )
            return
        classid, objid = _advisory_lock_parts(fence.guard_lock_key)
        held = bool(
            (
                await connection.execute(
                    _ADVISORY_LOCK_HELD,
                    {
                        "backend_pid": fence.guard_backend_pid,
                        "classid": classid,
                        "objid": objid,
                    },
                )
            ).scalar_one()
        )
        if not held:
            raise StaleCheckpointWriteError(fence, reason="execution guard is lost")

    def _fence_for_write(self, config: Config) -> CheckpointFence | None:
        fence = _fence_from_config(config)
        if self._require_fence and fence is None:
            raise CheckpointFenceRequiredError(
                "PostgresCheckpointSaver requires a checkpoint fence for writes"
            )
        return fence

    @asynccontextmanager
    async def _repeatable_read_connection(self) -> AsyncGenerator[Any]:
        """Read a checkpoint and all blobs/writes from one consistent snapshot."""

        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
            async with connection.begin():
                yield connection

    async def _load_writes(
        self, connection: Any, keys: set[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], list[tuple[str, str, Any]]]:
        if not keys:
            return {}
        rows = (
            await connection.execute(
                select(workflow_checkpoint_writes)
                .where(
                    tuple_(
                        workflow_checkpoint_writes.c.thread_id,
                        workflow_checkpoint_writes.c.checkpoint_ns,
                        workflow_checkpoint_writes.c.checkpoint_id,
                    ).in_(sorted(keys))
                )
                # Within a task the special slots are negative, so they come
                # first, and the ordinary writes follow in the order the task
                # produced them.
                .order_by(
                    workflow_checkpoint_writes.c.task_id,
                    workflow_checkpoint_writes.c.idx,
                )
            )
        ).mappings()
        grouped: dict[tuple[str, str, str], list[tuple[str, str, Any]]] = {}
        for row in rows:
            key = (row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"])
            grouped.setdefault(key, []).append(
                (
                    row["task_id"],
                    row["channel"],
                    self.serde.loads_typed(
                        (row["payload_type"], bytes(row["payload"]))
                    ),
                )
            )
        return grouped

    # -- versions -----------------------------------------------------------

    def get_next_version(self, current: str | None, channel: None = None) -> str:
        """A monotonic version that two writers cannot mint identically.

        The base class would hand out plain integers. Then two processes
        writing the same thread would both produce version ``n+1`` for a
        channel and write different bytes to the same blob key, and one would
        silently win. The random suffix makes their keys differ; the
        zero-padded counter keeps ``>`` -- which the pregel loop uses to decide
        which nodes have already seen a channel -- ordering them as numbers.
        """

        if current is None:
            counter = 0
        elif isinstance(current, int):
            counter = current
        else:
            counter = int(current.split(".")[0])
        return f"{counter + 1:032}.{random.random():016}"

    # -- the sync half, refused -------------------------------------------

    def get_tuple(self, config: Config) -> CheckpointTuple | None:
        _refuse_sync()

    def list(
        self,
        config: Config | None,
        *,
        filter: Mapping[str, Any] | None = None,
        before: Config | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        _refuse_sync()

    def put(
        self,
        config: Config,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Mapping[str, str | int | float],
    ) -> Config:
        _refuse_sync()

    def put_writes(
        self,
        config: Config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        _refuse_sync()

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete one thread's checkpoints, blobs and writes in one transaction.

        Refused while the owning Task can still run. This is the same ordering
        the index reservation uses: the thing that keeps a resource alive is the
        unfinished work referencing it, and only after that work reaches a
        terminal state may the resource go.

        Orphan writes need no separate rule. A write that names a checkpoint
        which was never committed -- routine under LangGraph's default
        durability -- still carries the thread it belongs to, so it is removed
        with that thread and nothing else can strand it.

        All three tables in one transaction, because a half-deleted thread is
        the one shape worse than either keeping it or removing it: blobs whose
        checkpoints are gone are unreachable bytes, and checkpoints whose blobs
        are gone fail closed on read.
        """

        async with self._engine.begin() as connection:
            owner = (
                (
                    await connection.execute(
                        select(task_runs.c.task_id, task_runs.c.status)
                        .where(task_runs.c.thread_id == thread_id)
                        # Locked as defence in depth rather than because a
                        # race is reachable: terminal statuses have no outgoing
                        # edge in the transition table, so a row that reads
                        # terminal here cannot become live before the deletes,
                        # and a sabotage of this lock changes nothing
                        # observable. It stays because the day a terminal Task
                        # can be reopened -- a retry-from-finished, say -- it is
                        # the only thing between that reopening and a thread
                        # whose position has just been removed.
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if owner is not None and owner["status"] not in TERMINAL_STATUSES:
                raise ThreadStillExecutingError(
                    thread_id=thread_id,
                    task_id=owner["task_id"],
                    status=str(owner["status"]),
                )
            for table in (
                workflow_checkpoint_writes,
                workflow_checkpoint_blobs,
                workflow_checkpoints,
            ):
                await connection.execute(
                    delete(table).where(table.c.thread_id == thread_id)
                )

    def delete_thread(self, thread_id: str) -> None:
        _refuse_sync()


def _serialised(pair: tuple[str, bytes]) -> dict[str, Any]:
    payload_type, payload = pair
    return {"payload_type": payload_type, "payload": payload}


def _config(
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str,
    *,
    fence: CheckpointFence | None = None,
) -> Config:
    configurable: dict[str, Any] = {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
        "checkpoint_id": checkpoint_id,
    }
    if fence is not None:
        configurable.update(_fence_configurable(fence))
    return {"configurable": configurable}


def _fence_configurable(fence: CheckpointFence) -> dict[str, str | int]:
    configurable: dict[str, str | int] = {
        CHECKPOINT_FENCE_TASK_ID_KEY: fence.task_id,
        CHECKPOINT_FENCE_WORKER_ID_KEY: fence.worker_id,
        CHECKPOINT_FENCE_EPOCH_KEY: fence.epoch,
    }
    if fence.guard_backend_pid is not None:
        # The model requires this companion field whenever a PID exists.
        assert fence.guard_lock_key is not None
        configurable.update(
            {
                CHECKPOINT_FENCE_GUARD_PID_KEY: fence.guard_backend_pid,
                CHECKPOINT_FENCE_GUARD_KEY_KEY: fence.guard_lock_key,
            }
        )
    return configurable


def _fence_from_config(config: Config) -> CheckpointFence | None:
    configurable = config.get("configurable", {})
    fields = {
        "task_id": configurable.get(CHECKPOINT_FENCE_TASK_ID_KEY),
        "worker_id": configurable.get(CHECKPOINT_FENCE_WORKER_ID_KEY),
        "epoch": configurable.get(CHECKPOINT_FENCE_EPOCH_KEY),
        "guard_backend_pid": configurable.get(CHECKPOINT_FENCE_GUARD_PID_KEY),
        "guard_lock_key": configurable.get(CHECKPOINT_FENCE_GUARD_KEY_KEY),
    }
    lease_fields = (fields["task_id"], fields["worker_id"], fields["epoch"])
    if not any(value is not None for value in lease_fields):
        return None
    if not all(value is not None for value in lease_fields):
        raise CheckpointFenceRequiredError("checkpoint fence is incomplete")
    try:
        return CheckpointFence.model_validate(fields)
    except Exception as error:
        raise CheckpointFenceRequiredError("checkpoint fence is invalid") from error


def _fence_from_metadata(metadata: Mapping[str, Any]) -> CheckpointFence | None:
    return _fence_from_config({"configurable": dict(metadata)})


def _advisory_lock_parts(lock_key: int) -> tuple[int, int]:
    """Return PostgreSQL ``pg_locks`` classid/objid halves for one bigint key."""

    unsigned_key = lock_key & ((1 << 64) - 1)
    return unsigned_key >> 32, unsigned_key & ((1 << 32) - 1)


def _restrict(
    query: Select[Any],
    config: Config | None,
    before: Config | None,
    metadata_filter: Mapping[str, Any] | None,
) -> Select[Any]:
    """Apply the four ways ``alist`` narrows a listing."""

    if config is not None:
        configurable = config["configurable"]
        query = query.where(
            workflow_checkpoints.c.thread_id == configurable["thread_id"]
        )
        namespace = configurable.get("checkpoint_ns")
        if namespace is not None:
            query = query.where(workflow_checkpoints.c.checkpoint_ns == namespace)
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id is not None:
            query = query.where(workflow_checkpoints.c.checkpoint_id == checkpoint_id)
    if before is not None:
        before_id = before["configurable"].get("checkpoint_id")
        if before_id is not None:
            query = query.where(workflow_checkpoints.c.checkpoint_id < before_id)
    if metadata_filter:
        # Containment, evaluated by PostgreSQL. On the top-level scalars this
        # metadata holds, `@>` is equality on each named key -- which is what
        # the in-memory reference implementation does in Python.
        query = query.where(
            and_(
                *(
                    workflow_checkpoints.c.metadata.contains({key: value})
                    for key, value in metadata_filter.items()
                )
            )
        )
    return query


__all__ = [
    "EMPTY_BLOB_TYPE",
    "CheckpointCorruptionError",
    "CheckpointFenceRequiredError",
    "PostgresCheckpointSaver",
    "StaleCheckpointWriteError",
    "ThreadStillExecutingError",
]
