"""Persist and recover the immutable input accepted for a Task.

The Registry owns a Task's lifecycle, while this module owns the bytes its
``input_ref`` names.  The two stores cannot commit atomically.  Submission
therefore writes the input artifact first and only then opens the Task: a
failed registry write can leave an unreferenced artifact for retention cleanup,
but no Task can ever reference an artifact that was never stored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar

from pydantic import ValidationError

from agent_workbench.application.tasks import TaskGraphChoice, TaskService
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import AgentWorkbenchError, ErrorCode
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.task_inputs import TaskInput
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.task_registry import TaskRun, objective_preview

TASK_INPUT_MEDIA_TYPE = "application/json"
TASK_INPUT_FILENAME = "task-input.json"


class TaskInputArtifactError(AgentWorkbenchError):
    """A readable artifact is not a valid TaskInput payload."""

    code: ClassVar[ErrorCode] = "invalid_tool_input"


@dataclass(frozen=True, slots=True)
class TaskInputStore:
    """Map an immutable ``TaskInput`` to and from an owned artifact."""

    artifacts: ArtifactStore

    async def store(
        self, *, principal: PrincipalContext, task_input: TaskInput
    ) -> ArtifactRef:
        """Write the schema-versioned JSON before any Task references it."""

        return await self.artifacts.put(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            kind="task_input",
            media_type=TASK_INPUT_MEDIA_TYPE,
            content=task_input.canonical_bytes(),
            filename=TASK_INPUT_FILENAME,
        )

    async def load_state(self, task: TaskRun) -> TaskState:
        """Read one Task's own input under its tenant-and-owner boundary."""

        task_input = await self._load(
            tenant_id=task.tenant_id,
            owner_id=task.owner_id,
            artifact_id=task.input_ref,
            expected_fingerprint=task.input_fingerprint,
        )
        return TaskState(
            task_id=task.task_id,
            objective=task_input.objective,
            max_revisions=task_input.max_revisions,
            knowledge_base_id=task_input.knowledge_base_id,
            # Always passed explicitly. TaskState's own default exists for
            # checkpoints older than the field, and a Task loading its input
            # is never one of those.
            wants_report=task_input.wants_report,
        )

    async def _load(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        artifact_id: str,
        expected_fingerprint: str,
    ) -> TaskInput:
        # Both methods are authorization-scoped by the adapter. In particular,
        # do not catch NotFoundError here: unknown, wrong-tenant and wrong-owner
        # ids must remain indistinguishable to a Worker or a caller.
        reference = await self.artifacts.head(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            principal_id=owner_id,
        )
        if reference.kind != "task_input":
            raise TaskInputArtifactError("the task input artifact has the wrong kind")
        if reference.media_type != TASK_INPUT_MEDIA_TYPE:
            raise TaskInputArtifactError(
                "the task input artifact has the wrong media type"
            )

        content = await self.artifacts.get(
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            principal_id=owner_id,
        )
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TaskInputArtifactError(
                "the task input artifact does not contain valid JSON"
            ) from error
        try:
            task_input = TaskInput.model_validate(decoded)
        except ValidationError as error:
            raise TaskInputArtifactError(
                "the task input artifact does not match the TaskInput schema"
            ) from error
        if task_input.fingerprint != expected_fingerprint:
            raise TaskInputArtifactError(
                "the task input artifact does not match the submitted fingerprint"
            )
        return task_input


@dataclass(frozen=True, slots=True)
class TaskInputService:
    """Store a TaskInput and submit its resulting reference atomically enough.

    An artifact write succeeds before ``TaskService.submit`` is called. If the
    latter fails, the artifact is intentionally an orphan: it has no Task row
    that can expose it, and a retention job may delete it later. Reversing the
    order would create a Task whose input cannot be recovered after a crash.
    """

    inputs: TaskInputStore
    tasks: TaskService

    async def submit(
        self,
        *,
        principal: PrincipalContext,
        task_input: TaskInput,
        submission_dedup_key: str,
        graph: TaskGraphChoice | None = None,
    ) -> TaskRun:
        """Store the input, then open the Task that references it.

        ``graph`` passes straight through and is deliberately *not* part of
        ``TaskInput``: the input artifact is what the Task was asked to do, and
        which pipeline runs it is a property of the submission. Putting it in
        both places would give idempotency two answers -- the artifact's
        fingerprint and the Registry's own ``graph_version`` comparison -- for
        one question.
        """

        stored = await self.inputs.store(principal=principal, task_input=task_input)
        return await self.tasks.submit(
            principal,
            input_ref=stored.artifact_id,
            input_fingerprint=task_input.fingerprint,
            submission_dedup_key=submission_dedup_key,
            # Taken here because this is the only layer holding both the whole
            # objective and the submission. TaskService receives references, and
            # asking it to open the artifact again to build a label would make
            # every submission read back what it just wrote.
            objective_preview=objective_preview(task_input.objective),
            graph=graph,
        )


__all__ = [
    "TASK_INPUT_FILENAME",
    "TASK_INPUT_MEDIA_TYPE",
    "TaskInputArtifactError",
    "TaskInputService",
    "TaskInputStore",
]
