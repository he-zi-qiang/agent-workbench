"""Task-input artifacts are schema-validated before a Worker sees them."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.adapters.persistence.models import task_runs
from agent_workbench.application.task_inputs import (
    TASK_INPUT_MEDIA_TYPE,
    TaskInputArtifactError,
    TaskInputService,
    TaskInputStore,
)
from agent_workbench.application.tasks import SubmittedSemantics, TaskService
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.task_inputs import TaskInput
from agent_workbench.ports.task_registry import (
    OBJECTIVE_PREVIEW_LIMIT,
    TaskRun,
    TaskSubmission,
    TaskSubmissionConflictError,
    objective_preview,
)

OWNER = PrincipalContext(principal_id="user_1", tenant_id="tenant_a")
OTHER_OWNER = PrincipalContext(principal_id="user_2", tenant_id="tenant_a")
SEMANTICS = SubmittedSemantics(
    run_semantics_snapshot={"model": {"provider": "fake"}},
    run_semantics_revision="test-v1",
    policy_revision="policy-v1",
    policy_fingerprint="f" * 16,
    authorization_envelope=AuthorizationEnvelope(),
)


def _task(
    *,
    input_ref: str,
    principal: PrincipalContext = OWNER,
    input_fingerprint: str = "a" * 64,
) -> TaskRun:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    return TaskRun.model_validate(
        {
            "task_id": "task_1",
            "tenant_id": principal.tenant_id,
            "owner_id": principal.principal_id,
            "thread_id": "thr_1",
            "graph_version": "v1",
            "input_ref": input_ref,
            "input_fingerprint": input_fingerprint,
            "submission_dedup_key": "dedup_1",
            "run_semantics_snapshot": {"model": {"provider": "fake"}},
            "run_semantics_revision": "test-v1",
            "submitted_policy_revision": "policy-v1",
            "submitted_policy_fingerprint": "f" * 16,
            "submitted_authorization_envelope": {},
            "status": "queued",
            "available_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )


class _RecordingRegistry:
    def __init__(self) -> None:
        self.submissions: list[TaskSubmission] = []

    async def submit(self, submission: TaskSubmission) -> TaskRun:
        self.submissions.append(submission)
        return _task(
            input_ref=submission.input_ref,
            principal=PrincipalContext(
                principal_id=submission.owner_id,
                tenant_id=submission.tenant_id,
            ),
        )

    async def get(self, task_id: str) -> None:  # pragma: no cover - not used
        del task_id
        return None


class _DeduplicatingRegistry(_RecordingRegistry):
    """The Registry's DB identity rule, exposed to the TaskInput use case."""

    def __init__(self) -> None:
        super().__init__()
        self._by_key: dict[tuple[str, str, str], TaskRun] = {}

    async def submit(self, submission: TaskSubmission) -> TaskRun:
        self.submissions.append(submission)
        # Force the two coroutines to overlap around the same submission key.
        await asyncio.sleep(0)
        key = (
            submission.tenant_id,
            submission.owner_id,
            submission.submission_dedup_key,
        )
        previous = self._by_key.get(key)
        if previous is not None:
            if previous.input_fingerprint != submission.input_fingerprint:
                raise TaskSubmissionConflictError(
                    owner_id=submission.owner_id,
                    submission_dedup_key=submission.submission_dedup_key,
                )
            return previous
        initial = _task(
            input_ref=submission.input_ref,
            principal=PrincipalContext(
                principal_id=submission.owner_id,
                tenant_id=submission.tenant_id,
            ),
        )
        opened = TaskRun.model_validate(
            {**initial.model_dump(), "input_fingerprint": submission.input_fingerprint}
        )
        self._by_key[key] = opened
        return opened


def _task_service(registry: Any) -> TaskService:
    return TaskService(registry=registry, semantics=lambda: SEMANTICS)


def test_a_stored_task_input_rehydrates_the_owned_task_state() -> None:
    async def scenario() -> tuple[Any, Any]:
        inputs = TaskInputStore(InMemoryArtifactStore())
        task_input = TaskInput(
            objective="Compare internal and external retrieval evidence.",
            max_revisions=3,
            knowledge_base_id="kb_main",
        )
        stored = await inputs.store(principal=OWNER, task_input=task_input)
        return stored, await inputs.load_state(
            _task(
                input_ref=stored.artifact_id,
                input_fingerprint=task_input.fingerprint,
            )
        )

    stored, state = asyncio.run(scenario())

    assert stored.kind == "task_input"
    assert stored.media_type == TASK_INPUT_MEDIA_TYPE
    assert state.task_id == "task_1"
    assert state.objective == "Compare internal and external retrieval evidence."
    assert state.max_revisions == 3
    assert state.knowledge_base_id == "kb_main"


def test_a_submission_labels_its_task_with_the_objective_it_was_given() -> None:
    """Without this the Registry row is an id, and so is every list that shows it."""

    registry = _RecordingRegistry()

    async def scenario() -> None:
        service = TaskInputService(
            inputs=TaskInputStore(InMemoryArtifactStore()),
            tasks=_task_service(registry),
        )
        await service.submit(
            principal=OWNER,
            task_input=TaskInput(objective="整理这批资料并输出一份建议报告"),
            submission_dedup_key="dedup_1",
        )

    asyncio.run(scenario())

    assert registry.submissions[0].objective_preview == "整理这批资料并输出一份建议报告"


@pytest.mark.parametrize(
    ("objective", "expected"),
    [
        ("  spaced   out\n objective ", "spaced out objective"),
        ("x" * OBJECTIVE_PREVIEW_LIMIT, "x" * OBJECTIVE_PREVIEW_LIMIT),
        (
            "y" * (OBJECTIVE_PREVIEW_LIMIT + 50),
            "y" * (OBJECTIVE_PREVIEW_LIMIT - 1) + "…",
        ),
    ],
)
def test_the_label_is_bounded_and_says_when_it_was_cut(
    objective: str, expected: str
) -> None:
    preview = objective_preview(objective)

    assert preview == expected
    assert preview is not None
    assert len(preview) <= OBJECTIVE_PREVIEW_LIMIT


def test_an_objective_of_only_whitespace_records_no_label() -> None:
    """Empty string and "no label" have to stay distinguishable in the column."""

    assert objective_preview("   \n\t ") is None


def test_the_stored_column_is_wide_enough_for_every_label_the_port_allows() -> None:
    """A widened preview must not turn into a truncated insert at runtime."""

    column = task_runs.c.objective_preview

    assert column.type.length == OBJECTIVE_PREVIEW_LIMIT
    assert column.nullable is True


def test_another_owner_cannot_make_a_task_load_the_input_artifact() -> None:
    async def scenario() -> None:
        inputs = TaskInputStore(InMemoryArtifactStore())
        task_input = TaskInput(objective="Draft a brief.")
        stored = await inputs.store(principal=OWNER, task_input=task_input)
        await inputs.load_state(
            _task(
                input_ref=stored.artifact_id,
                principal=OTHER_OWNER,
                input_fingerprint=task_input.fingerprint,
            )
        )

    with pytest.raises(NotFoundError, match="artifact not found"):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "media_type", "content", "message"),
    [
        ("task_input", TASK_INPUT_MEDIA_TYPE, b"{", "valid JSON"),
        ("report", TASK_INPUT_MEDIA_TYPE, b"{}", "wrong kind"),
        ("task_input", "text/plain", b"{}", "wrong media type"),
        ("task_input", TASK_INPUT_MEDIA_TYPE, b'{"objective": 7}', "schema"),
    ],
)
def test_corrupt_or_mistyped_task_input_artifacts_fail_closed(
    kind: str, media_type: str, content: bytes, message: str
) -> None:
    async def scenario() -> None:
        artifacts = InMemoryArtifactStore()
        stored = await artifacts.put(
            tenant_id=OWNER.tenant_id,
            owner_id=OWNER.principal_id,
            kind=kind,  # type: ignore[arg-type]
            media_type=media_type,
            content=content,
        )
        await TaskInputStore(artifacts).load_state(_task(input_ref=stored.artifact_id))

    with pytest.raises(TaskInputArtifactError, match=message):
        asyncio.run(scenario())


def test_a_readable_input_with_another_content_fingerprint_is_rejected() -> None:
    async def scenario() -> None:
        task_input = TaskInput(objective="Create a market brief.")
        artifacts = InMemoryArtifactStore()
        stored = await TaskInputStore(artifacts).store(
            principal=OWNER, task_input=task_input
        )
        await TaskInputStore(artifacts).load_state(
            _task(input_ref=stored.artifact_id, input_fingerprint="b" * 64)
        )

    with pytest.raises(TaskInputArtifactError, match="submitted fingerprint"):
        asyncio.run(scenario())


def test_submission_stores_the_artifact_before_passing_its_reference_to_tasks() -> None:
    async def scenario() -> tuple[TaskRun, list[TaskSubmission], bytes]:
        artifacts = InMemoryArtifactStore()
        registry = _RecordingRegistry()
        service = TaskInputService(
            inputs=TaskInputStore(artifacts), tasks=_task_service(registry)
        )
        opened = await service.submit(
            principal=OWNER,
            task_input=TaskInput(objective="Create a market brief."),
            submission_dedup_key="dedup_1",
        )
        submitted = registry.submissions[0]
        content = await artifacts.get(
            tenant_id=OWNER.tenant_id,
            artifact_id=submitted.input_ref,
            principal_id=OWNER.principal_id,
        )
        return opened, registry.submissions, content

    opened, submissions, content = asyncio.run(scenario())

    assert len(submissions) == 1
    assert opened.input_ref == submissions[0].input_ref
    assert TaskInput.model_validate_json(content).objective == "Create a market brief."


def test_concurrent_equal_inputs_have_one_task_despite_distinct_artifacts() -> None:
    async def scenario() -> tuple[TaskRun, TaskRun, list[TaskSubmission]]:
        registry = _DeduplicatingRegistry()
        service = TaskInputService(
            inputs=TaskInputStore(InMemoryArtifactStore()),
            tasks=_task_service(registry),
        )
        task_input = TaskInput(objective="Create a market brief.")
        first, second = await asyncio.gather(
            service.submit(
                principal=OWNER,
                task_input=task_input,
                submission_dedup_key="dedup_1",
            ),
            service.submit(
                principal=OWNER,
                task_input=task_input,
                submission_dedup_key="dedup_1",
            ),
        )
        return first, second, registry.submissions

    first, second, submissions = asyncio.run(scenario())

    assert first == second
    assert len(submissions) == 2
    assert submissions[0].input_ref != submissions[1].input_ref
    assert {submission.input_fingerprint for submission in submissions} == {
        TaskInput(objective="Create a market brief.").fingerprint
    }


def test_a_dedup_key_with_different_task_input_is_still_a_conflict() -> None:
    async def scenario() -> None:
        registry = _DeduplicatingRegistry()
        service = TaskInputService(
            inputs=TaskInputStore(InMemoryArtifactStore()),
            tasks=_task_service(registry),
        )
        await service.submit(
            principal=OWNER,
            task_input=TaskInput(objective="Create a market brief."),
            submission_dedup_key="dedup_1",
        )
        await service.submit(
            principal=OWNER,
            task_input=TaskInput(objective="Create a different market brief."),
            submission_dedup_key="dedup_1",
        )

    with pytest.raises(TaskSubmissionConflictError):
        asyncio.run(scenario())
