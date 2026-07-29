"""Submitting a Task, and asking about one.

Two things live here that the Registry deliberately does not do.

The Registry takes a ``thread_id`` and a ``graph_version`` because they are
facts about a row. Choosing them is a decision, and leaving it to callers means
every interface has to invent an id nothing enforces the uniqueness of, and to
name a graph version that a deployment -- not a request -- is entitled to
choose. Both are minted here, once.

And reading a Task is an authorization boundary. A Task belongs to an owner
inside a tenant, so a query by id must answer the same way for "no such Task"
and "not yours". Answering differently is itself the disclosure: the difference
confirms the id exists. That is why this returns ``NotFoundError`` for both and
never a "forbidden".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.task_registry import TaskRegistry, TaskRun, TaskSubmission
from agent_workbench.ports.task_workflow import GraphVersion
from agent_workbench.workflows.research_graph import GRAPH_VERSION_V1

TASK_THREAD_PREFIX: Final[str] = "thr"


@dataclass(frozen=True, slots=True)
class TaskService:
    """Open Tasks for a caller, and answer questions about their own."""

    registry: TaskRegistry
    # Which graph a newly submitted Task runs. A deployment decision rather
    # than a request parameter: a caller that could name a version could pin
    # itself to one nobody deploys any more, or to one that means something
    # else now.
    graph_version: GraphVersion = GRAPH_VERSION_V1

    async def submit(
        self,
        principal: PrincipalContext,
        *,
        input_ref: Identifier,
        submission_dedup_key: Identifier,
    ) -> TaskRun:
        """Open a Task, or return the one this caller's key already opened.

        The thread id is minted here and never supplied. Two submissions of one
        key must reach the same Task, and a caller-supplied thread would let a
        retry hand the Registry a *different* thread for the same key -- which
        the unique constraint would then refuse, turning an idempotent retry
        into an error.
        """

        return await self.registry.submit(
            TaskSubmission(
                tenant_id=principal.tenant_id,
                owner_id=principal.principal_id,
                thread_id=new_id(TASK_THREAD_PREFIX),
                graph_version=self.graph_version,
                input_ref=input_ref,
                submission_dedup_key=submission_dedup_key,
            )
        )

    async def get(self, principal: PrincipalContext, task_id: Identifier) -> TaskRun:
        """One Task, if it is this caller's.

        Raises ``NotFoundError`` when it does not exist *and* when it belongs
        to somebody else, because those two answers have to be the same one.
        """

        task = await self.registry.get(task_id)
        if task is None or not _belongs_to(task, principal):
            raise NotFoundError(f"task not found: {task_id}")
        return task


def _belongs_to(task: TaskRun, principal: PrincipalContext) -> bool:
    # Both, not either. A tenant match alone would expose one tenant's Tasks to
    # every principal in it, and an owner match alone would let an id collide
    # across tenants into somebody else's Task.
    return (
        task.tenant_id == principal.tenant_id
        and task.owner_id == principal.principal_id
    )


__all__ = ["TASK_THREAD_PREFIX", "TaskService"]
