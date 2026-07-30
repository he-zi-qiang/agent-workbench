"""Retiring, releasing and collecting index generations, in PostgreSQL.

The order these three enforce is the whole point. A generation may only be
deleted when it is retired *and* unreferenced, and a reference may only be
dropped by a Task that has finished -- so no sequence of these calls can take an
index away from a Task that is still going to read from it.

The foreign key is the backstop, not the check. It would refuse a bad delete
anyway, but it would do it with a constraint name at whatever layer happened to
be holding the transaction; the explicit count says what is actually true.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import (
    qdrant_index_generations,
    task_runs,
)
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.task_registry import TERMINAL_STATUSES
from agent_workbench.ports.index_generations import GenerationStillReferencedError


class PostgresIndexGenerationStore:
    """``IndexGenerationStore`` over ``qdrant_index_generations``."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def retire(self, generation_id: str) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(qdrant_index_generations)
                .where(qdrant_index_generations.c.generation_id == generation_id)
                .values(status="retired")
            )

    async def release(self, task_id: Identifier) -> bool:
        async with self._engine.begin() as connection:
            released = (
                await connection.execute(
                    update(task_runs)
                    .where(
                        task_runs.c.task_id == task_id,
                        # Terminal only. A running Task's reservation is what
                        # keeps its index alive for the rest of the run, so
                        # releasing early is the one mistake this must refuse
                        # rather than report.
                        task_runs.c.status.in_(sorted(TERMINAL_STATUSES)),
                        task_runs.c.resolved_qdrant_index_generation_id.isnot(None),
                    )
                    .values(
                        resolved_qdrant_collection=None,
                        resolved_qdrant_index_version=None,
                        resolved_qdrant_index_generation_id=None,
                        updated_at=func.now(),
                    )
                    .returning(task_runs.c.task_id)
                )
            ).first()
        return released is not None

    async def collect(self, generation_id: str) -> None:
        async with self._engine.begin() as connection:
            # Locked before counting, so a submission cannot reserve this
            # generation between the count and the delete. A submission holds
            # the same row for its own transaction, so the two serialise.
            status = (
                await connection.execute(
                    select(qdrant_index_generations.c.status)
                    .where(qdrant_index_generations.c.generation_id == generation_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if status is None:
                # Already collected. Deleting twice is not an error a sweep
                # needs to distinguish from deleting once.
                return
            references = (
                await connection.execute(
                    select(func.count())
                    .select_from(task_runs)
                    .where(
                        task_runs.c.resolved_qdrant_index_generation_id == generation_id
                    )
                )
            ).scalar_one()
            if references or status != "retired":
                raise GenerationStillReferencedError(
                    generation_id=generation_id,
                    references=int(references),
                )
            await connection.execute(
                delete(qdrant_index_generations).where(
                    qdrant_index_generations.c.generation_id == generation_id
                )
            )


__all__ = ["PostgresIndexGenerationStore"]
