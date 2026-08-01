"""Real handler contracts for the fixed v1 Task graph.

These tests deliberately use one text-only executor.  The handlers, not a
fake-specific shortcut, must turn that text into either strict structured state
or an owned artifact reference.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.langgraph.workflow import LangGraphTaskWorkflow
from agent_workbench.adapters.memory.artifact_store import InMemoryArtifactStore
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    RunBudget,
    TokenUsage,
)
from agent_workbench.domain.tasks import TaskState
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.task_registry import TaskRegistry, TaskRun
from agent_workbench.workflows.agent_nodes import AgentNodeFailedError, TaskRunContext
from agent_workbench.workflows.task_handlers import (
    StructuredOutputError,
    TaskExportHandlers,
    TaskExportPreconditionError,
    TaskExportUnavailableError,
    TaskNodeInvocationProvider,
    TaskNodeRunFailedError,
    build_task_v1_handlers,
    decode_plan_output,
    decode_review_output,
)


class _RecordingExport:
    """A ReportExportPort that records what the node asked it to export."""

    def __init__(self, artifact_id: str = "art_report_1") -> None:
        self.artifact_id = artifact_id
        self.calls: list[dict[str, Any]] = []

    async def export(
        self,
        *,
        draft_ref: str,
        approval_id: str,
        execution: Any,
        sink: Any,
        cancellation: Any,
    ) -> str:
        del sink, cancellation
        self.calls.append(
            {
                "draft_ref": draft_ref,
                "approval_id": approval_id,
                "task_id": execution.task_id,
                "lease_epoch": execution.lease_epoch,
            }
        )
        return self.artifact_id


def _export(port: _RecordingExport) -> TaskExportHandlers:
    return TaskExportHandlers(export=port, policy_identity="rev_1:fingerprint")


class _Registry:
    """A mutable Registry row makes each node's context lookup observable."""

    def __init__(self, task: TaskRun) -> None:
        self.task = task
        self.get_calls = 0

    async def get(self, task_id: str) -> TaskRun | None:
        assert task_id == self.task.task_id
        self.get_calls += 1
        return self.task


class _TextExecutor:
    """One executor used by artifact, planner and critic nodes alike."""

    def __init__(self, *, failing_node: str | None = None) -> None:
        self.failing_node = failing_node
        self.requests: list[AgentRunRequest] = []

    async def run(
        self, request: AgentRunRequest, emit: object, cancellation: object
    ) -> AgentOutcome:
        self.requests.append(request)
        node = request.trace.graph_node_id
        usage = BudgetUsage(steps=1, tokens=TokenUsage(input_tokens=3, output_tokens=5))
        if node == self.failing_node:
            return AgentOutcome(
                agent_run_id=request.trace.agent_run_id,
                status="failed",
                stop_reason="error",
                error=ErrorInfo(code="provider_error", message="model failed"),
                usage=usage,
            )
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=_text_for(node),
            usage=usage,
        )


class _FailingArtifactStore(InMemoryArtifactStore):
    async def put(self, **kwargs: Any) -> Any:
        raise OSError("object storage unavailable")


def _text_for(node: str | None) -> str:
    if node == "plan":
        return (
            '{"steps":[{"step_id":"step_1","sequence":1,'
            '"objective":"Collect evidence","depends_on":[]}]}'
        )
    if node == "critic":
        return (
            '{"decision":"pass","reviewed_draft_ref":"PLACEHOLDER",'
            '"revision_number":0,"summary":"Grounded report.",'
            '"issues":[],"score":91}'
        )
    return f"text-only result from {node}"


def _task(**overrides: object) -> TaskRun:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    values: dict[str, object] = {
        "task_id": "task_1",
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thread_1",
        "graph_version": "v1",
        "input_ref": "input_1",
        "input_fingerprint": "a" * 64,
        "submission_dedup_key": "dedup_1",
        "run_semantics_snapshot": {"model": {"provider": "fake"}},
        "run_semantics_revision": "1.3:v1.0:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": AuthorizationEnvelope(),
        "status": "queued",
        "available_at": now,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return TaskRun.model_validate(values)


def _state(**overrides: object) -> TaskState:
    values: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Compare hybrid retrieval strategies.",
        "max_revisions": 1,
    }
    values.update(overrides)
    return TaskState.model_validate(values)


def _passing_review(draft_ref: str) -> dict[str, Any]:
    """The review an approved state must carry, since TaskState checks for it."""

    return {
        "decision": "pass",
        "reviewed_draft_ref": draft_ref,
        "revision_number": 0,
        "summary": "Grounded report.",
        "score": 91,
    }


def _provider(registry: _Registry) -> TaskNodeInvocationProvider:
    log = InMemoryEventLog()

    def sink_for(context: TaskRunContext) -> ScopedEventSink:
        return ScopedEventSink(
            log,
            EventScope(
                stream_id=context.stream_id,
                run_id=context.trace.agent_run_id,
                task_id=context.trace.task_id,
                graph_node_id=context.trace.graph_node_id,
            ),
        )

    return TaskNodeInvocationProvider(
        registry=cast("TaskRegistry", registry),
        budget=RunBudget(max_steps=12, max_tool_calls=24),
        sink_for=sink_for,
        cancellation_for=lambda _: NullCancellationToken(),
        principal_for=lambda task: PrincipalContext(
            tenant_id=task.tenant_id,
            principal_id=task.owner_id,
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        "```json\n{}\n```",
        '{"steps":[]} trailing',
        '{"steps":[],"steps":[]}',
        '{"steps":[]}',
        '{"steps":[{"step_id":"step_1","sequence":"1","objective":"x","depends_on":[]}]}',
        '{"steps":[{"step_id":"step_1","sequence":1,"objective":"x","depends_on":["step_1"]}]}',
        '{"steps":[{"step_id":"step_1","sequence":1,"objective":"x","depends_on":[]}],"extra":1}',
    ],
)
def test_planner_rejects_malformed_or_unsafe_json(payload: str) -> None:
    with pytest.raises(StructuredOutputError):
        decode_plan_output(payload)


def test_planner_validates_the_full_task_plan_including_dependency_order() -> None:
    decoded = decode_plan_output(
        '{"steps":[{"step_id":"step_1","sequence":1,"objective":"First","depends_on":[]},'
        '{"step_id":"step_2","sequence":2,"objective":"Second","depends_on":["step_1"]}]}'
    )

    assert tuple(step.step_id for step in decoded) == ("step_1", "step_2")
    with pytest.raises(StructuredOutputError):
        decode_plan_output(
            '{"steps":[{"step_id":"step_2","sequence":1,"objective":"Second",'
            '"depends_on":["step_1"]}]}'
        )


def test_critic_binds_its_decision_to_the_current_draft_and_revision() -> None:
    state = _state(draft_ref="draft_1", revision_count=0)
    valid = (
        '{"decision":"pass","reviewed_draft_ref":"draft_1",'
        '"revision_number":0,"summary":"Sufficiently grounded.",'
        '"issues":[],"score":89}'
    )
    assert decode_review_output(valid, state=state).decision == "pass"

    for invalid in (
        valid.replace("draft_1", "old_draft"),
        valid.replace('"revision_number":0', '"revision_number":1'),
        valid.replace('"issues":[]', '"issues":[],"extra":true'),
        valid.replace('"decision":"pass"', '"decision":"revise"'),
    ):
        with pytest.raises(StructuredOutputError):
            decode_review_output(invalid, state=state)


def test_real_handlers_persist_text_only_artifacts_and_complete_the_graph() -> None:
    async def scenario() -> None:
        registry = _Registry(_task())
        store = InMemoryArtifactStore()
        executor = _TextExecutor()

        async def decided_approval(_: TaskState) -> dict[str, Any]:
            # build_task_v1_handlers builds no approval node: the real one has
            # to interrupt, which is the adapter's job. A graph assembled
            # without one now fails closed at the gate, so this test supplies
            # the answer the composition root's interrupting node would have
            # obtained from the ledger.
            return {"approval_id": "apr_1", "approval_decision": "approved"}

        exporter = _RecordingExport()
        handlers = build_task_v1_handlers(
            executor=executor,
            artifacts=store,
            invocations=_provider(registry),
            export=_export(exporter),
        ) | {"approval": decided_approval}

        # The critic needs the generated id, which is only known once synthesis
        # has persisted its text. Patch its scripted response at run time.
        original = executor.run

        async def with_current_draft(
            request: AgentRunRequest, emit: object, cancellation: object
        ) -> AgentOutcome:
            if request.trace.graph_node_id == "critic":
                # The generated artifact id is present in the handler state and
                # request prompt. It is not discoverable from model text.
                requested = request.messages[0].text()
                draft_id = requested.split("draft_ref=", 1)[1].split("\n", 1)[0]
                executor.requests.append(request)
                return AgentOutcome(
                    agent_run_id=request.trace.agent_run_id,
                    status="completed",
                    stop_reason="completed",
                    output_text=_text_for("critic").replace("PLACEHOLDER", draft_id),
                    usage=BudgetUsage(
                        steps=1, tokens=TokenUsage(input_tokens=3, output_tokens=5)
                    ),
                )
            return await original(request, emit, cancellation)

        executor.run = with_current_draft  # type: ignore[method-assign]
        result = await LangGraphTaskWorkflow(handlers=handlers).run(
            _state(), thread_id="thread_1", graph_version="v1"
        )

        assert result.disposition == "completed"
        assert result.state.draft_ref is not None
        # The write node ran, and what it exported reached the checkpoint. A
        # graph that "completed" without this is one where a human approved an
        # export that never happened.
        assert result.state.export_ref == "art_report_1"
        assert exporter.calls == [
            {
                "draft_ref": result.state.draft_ref,
                "approval_id": "apr_1",
                "task_id": "task_1",
                "lease_epoch": registry.task.lease_epoch,
            }
        ]
        assert len(result.state.evidence_refs) == 2
        assert len(result.state.agent_outcome_refs) == 6
        assert result.state.budget_usage.steps == 6
        assert {request.trace.graph_node_id for request in executor.requests} == {
            "understand",
            "plan",
            "research_internal",
            "research_external",
            "synthesize",
            "critic",
        }
        stored = await store.get(
            tenant_id="tenant_a",
            artifact_id=result.state.draft_ref,
            principal_id="user_1",
        )
        assert stored == b"text-only result from synthesize"
        assert (
            await store.head(
                tenant_id="tenant_a",
                artifact_id=result.state.draft_ref,
                principal_id="user_1",
            )
        ).kind == "report"

    asyncio.run(scenario())


def test_each_handler_refreshes_registry_context_and_mints_a_new_run_id() -> None:
    async def scenario() -> None:
        registry = _Registry(_task())
        executor = _TextExecutor()
        handlers = build_task_v1_handlers(
            executor=executor,
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(registry),
        )
        await handlers["understand"](_state())
        registry.task = registry.task.model_copy(
            update={"owner_id": "user_2", "tenant_id": "tenant_b"}
        )
        await handlers["understand"](_state())

        first, second = executor.requests
        assert registry.get_calls == 2
        assert first.principal.principal_id == "user_1"
        assert second.principal.principal_id == "user_2"
        assert first.principal.tenant_id == "tenant_a"
        assert second.principal.tenant_id == "tenant_b"
        assert first.trace.agent_run_id != second.trace.agent_run_id

    asyncio.run(scenario())


def test_structured_or_artifact_failure_keeps_the_usage_that_was_spent() -> None:
    async def scenario() -> None:
        registry = _Registry(_task())
        failed = build_task_v1_handlers(
            executor=_TextExecutor(failing_node="plan"),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(registry),
        )
        with pytest.raises(TaskNodeRunFailedError) as planner_error:
            await failed["plan"](_state())
        assert planner_error.value.state.budget_usage.steps == 1
        assert len(planner_error.value.state.agent_outcome_refs) == 1

        storage_failure = build_task_v1_handlers(
            executor=_TextExecutor(),
            artifacts=_FailingArtifactStore(),
            invocations=_provider(registry),
        )
        with pytest.raises(AgentNodeFailedError) as artifact_error:
            await storage_failure["understand"](_state())
        assert artifact_error.value.state.budget_usage.steps == 1
        assert len(artifact_error.value.state.agent_outcome_refs) == 1

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# The export node
# --------------------------------------------------------------------------


def test_a_worker_without_an_export_capability_fails_rather_than_passes_through() -> (
    None
):
    """The old behaviour was silence, and silence settled the Task as succeeded.

    A graph that reached export has a human's approval behind it. Producing no
    report and reporting success is the exact outcome the approval was for.
    """

    async def scenario() -> None:
        handlers = build_task_v1_handlers(
            executor=_TextExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task())),
        )

        with pytest.raises(TaskExportUnavailableError):
            await handlers["export"](
                _state(
                    draft_ref="art_draft_1",
                    approval_id="apr_1",
                    approval_decision="approved",
                    review_result=_passing_review("art_draft_1"),
                )
            )

    asyncio.run(scenario())


def test_export_without_an_approved_draft_is_a_precondition_failure() -> None:
    """Reaching here without one means routing let it through, not that the
    export should invent something to export."""

    async def scenario() -> None:
        exporter = _RecordingExport()
        handlers = build_task_v1_handlers(
            executor=_TextExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task())),
            export=_export(exporter),
        )

        with pytest.raises(TaskExportPreconditionError):
            await handlers["export"](_state())

        assert exporter.calls == []

    asyncio.run(scenario())


def test_the_export_node_passes_the_live_lease_epoch_to_the_ledger() -> None:
    """Read from the Registry on every node call, never from the checkpoint.

    An epoch a resume carried forward would let a Worker that lost the Task
    keep writing under ownership it no longer has.
    """

    async def scenario() -> None:
        registry = _Registry(_task())
        registry.task = registry.task.model_copy(update={"lease_epoch": 9})
        exporter = _RecordingExport()
        handlers = build_task_v1_handlers(
            executor=_TextExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(registry),
            export=_export(exporter),
        )

        update = await handlers["export"](
            _state(
                draft_ref="art_draft_1",
                approval_id="apr_1",
                approval_decision="approved",
                review_result=_passing_review("art_draft_1"),
            )
        )

        assert update == {"export_ref": "art_report_1"}
        assert exporter.calls[0]["lease_epoch"] == 9

    asyncio.run(scenario())
