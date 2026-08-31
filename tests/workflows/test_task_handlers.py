"""Real handler contracts for the fixed v1 Task graph.

These tests deliberately use one text-only executor.  The handlers, not a
fake-specific shortcut, must turn that text into either strict structured state
or an owned artifact reference.

Every handler here runs inside a claim, because that is the only way a node
runs in production: the Worker enters the lease it was given and the graph
executes inside it.  ``_run`` is what makes the tests say that, and it is not
ceremony -- a node resolved outside a claim refuses, which is its own test
below.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.langgraph.workflow import LangGraphTaskWorkflow
from agent_workbench.adapters.memory.artifact_store import InMemoryArtifactStore
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.tools.workspace import (
    WorkspaceListTool,
    WorkspaceWriteTool,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    RunBudget,
    TokenUsage,
    TraceContext,
)
from agent_workbench.domain.tasks import TaskState
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.task_registry import ExecutionLease, TaskRegistry, TaskRun
from agent_workbench.ports.tools import ToolInvocation
from agent_workbench.workflows.agent_nodes import AgentNodeFailedError, TaskRunContext
from agent_workbench.workflows.agent_profiles import WORKSPACE_TOOLS
from agent_workbench.workflows.execution_scope import TaskExecutionScope
from agent_workbench.workflows.task_handlers import (
    V1_HANDLER_NODES,
    V2_HANDLER_NODES,
    BoundedParallelExecutor,
    StructuredOutputError,
    TaskExportHandlers,
    TaskExportPreconditionError,
    TaskExportUnavailableError,
    TaskLeaseLostError,
    TaskLeaseUnavailableError,
    TaskNodeInvocationProvider,
    TaskNodeRunFailedError,
    build_task_handlers,
    build_task_v1_handlers,
    build_task_v2_handlers,
    decode_plan_output,
    decode_review_output,
)

SCOPE = TaskExecutionScope()

#: What the Worker claimed. Every ``_task()`` row below is this claim as the
#: Registry holds it, so a test that wants a lost lease changes the row and
#: leaves the claim alone -- which is the direction the failure comes from.
LEASE = ExecutionLease(task_id="task_1", worker_id="worker_1", epoch=1)


def _run(scenario: Any) -> Any:
    """Run one scenario the way a Worker runs a graph: inside its claim."""

    async def under_claim() -> Any:
        with SCOPE.executing(LEASE):
            return await scenario()

    return asyncio.run(under_claim())


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
        "status": "running",
        "lease_owner": LEASE.worker_id,
        "lease_epoch": LEASE.epoch,
        "lease_until": now,
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


def _provider(
    registry: _Registry,
    *,
    max_seconds_per_invocation: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> TaskNodeInvocationProvider:
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

    provider = TaskNodeInvocationProvider(
        registry=cast("TaskRegistry", registry),
        budget=RunBudget(max_steps=12, max_tool_calls=24),
        sink_for=sink_for,
        cancellation_for=lambda _: NullCancellationToken(),
        principal_for=lambda task: PrincipalContext(
            tenant_id=task.tenant_id,
            principal_id=task.owner_id,
        ),
        scope=SCOPE,
        max_seconds_per_invocation=max_seconds_per_invocation,
    )
    if clock is None:
        return provider
    return replace(provider, clock=clock)


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


def test_a_verdict_missing_a_required_key_is_refused() -> None:
    """The 2026-08-13 failure, at the decoder rather than at the prompt.

    A critic returned a complete, correct verdict -- `decision`, the right
    draft, the right revision, a summary and eight issues -- and omitted
    `score`, which the template then showed *after* the variable-length array.
    `finish_reason` was `stop` and the answer was 255 tokens, so nothing was
    truncated: the model simply stopped before the last key.

    `test_no_required_review_field_is_shown_behind_the_variable_length_array`
    keeps the template from inviting it again. This keeps the decoder refusing
    it, which is the half that still matters when a model omits a key the
    template did show -- and no case in the tuple above removes a required key
    at all: they change one, add one, or contradict one.
    """

    state = _state(draft_ref="draft_1", revision_count=0)
    complete = (
        '{"decision":"revise","reviewed_draft_ref":"draft_1",'
        '"revision_number":0,"summary":"The draft repeats the objective.",'
        '"score":10,"issues":["Write the body."]}'
    )
    # The control, and it has to come first: if this did not decode, the
    # assertion below would pass on a decoder that refuses everything.
    assert decode_review_output(complete, state=state).decision == "revise"

    for missing in ("score", "summary", "revision_number", "reviewed_draft_ref"):
        without = json.loads(complete)
        del without[missing]
        with pytest.raises(StructuredOutputError):
            decode_review_output(json.dumps(without), state=state)


@dataclass
class _SequencedExecutor:
    """Replies to a node in order, so its second run can differ from its first.

    The last reply repeats rather than running out, so a node that kept asking
    would keep getting the same answer and the request count would not stop.
    """

    replies: dict[str, list[str]]
    requests: list[AgentRunRequest] = dataclass_field(default_factory=list)

    async def run(
        self, request: AgentRunRequest, emit: object, cancellation: object
    ) -> AgentOutcome:
        del emit, cancellation
        self.requests.append(request)
        pending = self.replies[str(request.trace.graph_node_id)]
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=pending.pop(0) if len(pending) > 1 else pending[0],
            usage=BudgetUsage(steps=1),
        )


def test_a_narrated_plan_is_asked_for_the_object_alone() -> None:
    """The planner shares the external researcher's decoder, and its exposure.

    Nothing here is specific to reading pages: any of these nodes can be handed
    a model that thinks out loud in front of its answer, and the correction
    belongs where the strictness does rather than at one node (ADR-034 §3.1).
    """

    async def scenario() -> tuple[_SequencedExecutor, dict[str, Any]]:
        narrated = f"Three steps should do it.\n\n{_text_for('plan')}"
        executor = _SequencedExecutor({"plan": [narrated, _text_for("plan")]})
        handlers = build_task_v1_handlers(
            executor=cast(Any, executor),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task())),
        )
        return executor, dict(await handlers["plan"](_state()))

    executor, update = _run(scenario)

    assert update["plan"][0]["step_id"] == "step_1"
    assert len(executor.requests) == 2
    # Two runs, so two run ids and the cost of both.
    assert len(update["agent_outcome_refs"]) == 2
    assert update["budget_usage"]["steps"] == 2
    corrective = executor.requests[1]
    assert corrective.messages[-2].role == "assistant"
    assert "one JSON object" in corrective.messages[-1].text()


def test_a_plan_that_was_already_one_json_object_buys_no_second_run() -> None:
    """The control, for the planner. No correction where none is needed."""

    async def scenario() -> _SequencedExecutor:
        executor = _SequencedExecutor({"plan": [_text_for("plan")]})
        handlers = build_task_v1_handlers(
            executor=cast(Any, executor),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task())),
        )
        await handlers["plan"](_state())
        return executor

    executor = _run(scenario)

    assert len(executor.requests) == 1


def test_a_critic_that_reviewed_the_wrong_draft_is_not_asked_again() -> None:
    """The boundary, on the node where getting it wrong would matter most.

    A review naming another draft *is* one JSON object. What it is not is the
    value this node requires -- and a corrective turn there would be nudging a
    model toward an answer that passes rather than asking it to say the answer
    it already gave (ADR-034 §3.2).
    """

    async def scenario() -> tuple[_SequencedExecutor, _SequencedExecutor]:
        state = _state(draft_ref="draft_1", revision_count=0)
        review = _text_for("critic").replace("PLACEHOLDER", "draft_1")

        wrong = _SequencedExecutor(
            {"critic": [review.replace("draft_1", "an_older_draft")]}
        )
        handlers = build_task_v1_handlers(
            executor=cast(Any, wrong),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task())),
        )
        with pytest.raises(TaskNodeRunFailedError, match="review schema"):
            await handlers["critic"](state)

        # The control group: the same node, the same wrong-shaped message plus
        # narration in front of it. That one *is* corrected.
        narrated = _SequencedExecutor({"critic": [f"Looks solid.\n\n{review}", review]})
        corrected = build_task_v1_handlers(
            executor=cast(Any, narrated),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task())),
        )
        assert (await corrected["critic"](state))["review_result"]["decision"] == "pass"
        return wrong, narrated

    wrong, narrated = _run(scenario)

    assert len(wrong.requests) == 1
    assert len(narrated.requests) == 2


def test_a_narration_that_survives_the_correction_turn_fails_the_node() -> None:
    """The other half of the test above, and a shape that really happened.

    That one proves the corrective turn *works*: narration in front of the
    object is asked once more and the second answer decodes. It does not say
    what happens when the second answer is no better -- and in a measured v2
    failure (`task_9a595830...`) it was worse: the reviewer explained the empty
    workspace in prose, was asked for the object alone, and answered with prose
    again and no JSON at all.

    Two things are pinned here, and the second is the one with a distant
    reader. The node asks **exactly twice** -- ADR-034 grants one restatement,
    not a loop that argues. And the raised error keeps the decoder's own
    complaint as its ``__cause__``: `workers/task.py` reads exactly one link of
    that chain to say *which* decode failed, so a `raise` that dropped the
    `from error` would silently return every structured failure to the one
    undifferentiated sentence known-gaps C-05 is about.
    """

    async def scenario() -> tuple[_SequencedExecutor, BaseException]:
        state = _state(draft_ref="draft_1", revision_count=0)
        executor = _SequencedExecutor(
            {
                "critic": [
                    "The draft looks thin to me.",
                    "As I said, it is thin. I stand by that.",
                ]
            }
        )
        handlers = build_task_v1_handlers(
            executor=cast(Any, executor),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task())),
        )
        with pytest.raises(TaskNodeRunFailedError) as raised:
            await handlers["critic"](state)
        return executor, raised.value

    executor, error = _run(scenario)

    assert len(executor.requests) == 2, "one restatement, not a negotiation"
    assert isinstance(error.__cause__, StructuredOutputError)


def test_a_report_longer_than_a_preview_reaches_its_artifact_whole() -> None:
    """The Task's product, measured through the node that stores it.

    `ArtifactPersistingExecutor` writes `output_text` and nothing else, so
    whatever ceiling that field carries is the ceiling on the report the export
    node exports. Measured before ADR-035: an 18,010-character report became a
    4,098-byte artifact, ending in a truncation marker nothing downstream reads.
    """

    report = "# Report\n\n" + ("Grounded paragraph. " * 900)

    async def scenario() -> tuple[bytes, str]:
        store = InMemoryArtifactStore()
        handlers = build_task_v1_handlers(
            executor=cast(Any, _SequencedExecutor({"synthesize": [report]})),
            artifacts=store,
            invocations=_provider(_Registry(_task())),
        )
        update = await handlers["synthesize"](_state())
        draft_ref = str(update["draft_ref"])
        return await store.get(
            tenant_id="tenant_a", principal_id="user_1", artifact_id=draft_ref
        ), draft_ref

    stored, draft_ref = _run(scenario)

    assert draft_ref
    assert stored.decode("utf-8") == report


def test_real_handlers_persist_text_only_artifacts_and_complete_the_graph() -> None:
    """Every node, through the real compiled graph, inside one claim.

    That last part is load-bearing rather than incidental. The claim reaches a
    node through the execution context the Worker entered, and between the two
    sits LangGraph -- which runs each node in a task of its own, several
    suspensions deep. If it did not carry the context, the export node here
    would refuse instead of exporting, and this is where that would show.
    """

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

    _run(scenario)


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

    _run(scenario)


def test_task_handlers_offer_mcp_only_to_synthesis_under_submitted_authority() -> None:
    async def scenario() -> None:
        tool_name = "mcp_office_render_document"
        registry = _Registry(
            _task(
                submitted_authorization_envelope=AuthorizationEnvelope(
                    allowed_tools=(tool_name,),
                    max_tool_risk="external",
                    approval_required_risks=(),
                )
            )
        )
        executor = _TextExecutor()
        handlers = build_task_v1_handlers(
            executor=executor,
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(registry),
            dynamic_tools={"synthesis": (tool_name,)},
        )

        await handlers["understand"](_state())
        await handlers["synthesize"](_state())

        by_node = {
            request.trace.graph_node_id: request.tool_names
            for request in executor.requests
        }
        assert by_node == {"understand": (), "synthesize": (tool_name,)}

    _run(scenario)


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

    _run(scenario)


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

    _run(scenario)


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

    _run(scenario)


def _approved() -> TaskState:
    return _state(
        draft_ref="art_draft_1",
        approval_id="apr_1",
        approval_decision="approved",
        review_result=_passing_review("art_draft_1"),
    )


def test_the_export_node_writes_under_the_claim_the_worker_holds() -> None:
    """The epoch reaching the ledger is the Worker's, not the Registry's answer.

    They agree here, which is the point of the control group: the write happens,
    and it happens under epoch 1 because that is the claim this execution was
    entered with. The test below changes only which of the two the Registry
    reports.
    """

    async def scenario() -> None:
        registry = _Registry(_task())
        exporter = _RecordingExport()
        handlers = build_task_v1_handlers(
            executor=_TextExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(registry),
            export=_export(exporter),
        )

        update = await handlers["export"](_approved())

        assert update == {"export_ref": "art_report_1"}
        assert exporter.calls[0]["lease_epoch"] == LEASE.epoch

    _run(scenario)


def test_a_worker_that_lost_the_task_cannot_export_under_its_successor() -> None:
    """The defect, at the node that performs the irreversible act.

    W1 is inside the graph when its lease lapses and W2 claims the Task at
    epoch 2. Asked for the epoch, the Registry answers 2 -- so a node that reads
    it there hands the ledger the claim of the Worker that replaced it, and the
    ledger's fence, which compares what it is given against the live claim,
    agrees. The export is dispatched by a Worker with no authority to run at
    all, and W2 will dispatch it again.

    So the node refuses before the port is reached: what a node executes under
    is the claim it was entered with, and the Registry is where that claim is
    *checked*, not where it is obtained.
    """

    async def scenario() -> None:
        registry = _Registry(_task())
        exporter = _RecordingExport()
        handlers = build_task_v1_handlers(
            executor=_TextExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(registry),
            export=_export(exporter),
        )
        # The reaper ran and somebody else claimed it, mid-invocation.
        registry.task = registry.task.model_copy(
            update={"lease_owner": "worker_2", "lease_epoch": 2}
        )

        with pytest.raises(TaskLeaseLostError):
            await handlers["export"](_approved())

        # Nothing was exported under anybody's epoch.
        assert exporter.calls == []

    _run(scenario)


def test_a_node_reached_outside_any_claim_refuses_to_run() -> None:
    """No claim is not "look one up". It is no authority to spend or write.

    This is what a handler set assembled with a scope nothing enters does, and
    the reason the wiring is safe to get wrong loudly rather than quietly: the
    failure is every node refusing, not one node running under a stranger's
    lease.
    """

    async def scenario() -> None:
        registry = _Registry(_task())
        exporter = _RecordingExport()
        handlers = build_task_v1_handlers(
            executor=_TextExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(registry),
            export=_export(exporter),
        )

        with pytest.raises(TaskLeaseUnavailableError):
            await handlers["export"](_approved())
        with pytest.raises(TaskLeaseUnavailableError):
            await handlers["understand"](_state())

        assert exporter.calls == []

    # Deliberately not ``_run``: this scenario is the one that runs outside a
    # claim, and wrapping it in one would test nothing.
    asyncio.run(scenario())


def test_no_more_agent_invocations_run_at_once_than_the_deployment_allows() -> None:
    """``max_parallel_agent_invocations`` used to describe rather than bound.

    The fixed graph fans out to two researchers and LangGraph runs them
    concurrently, so the configured number happened to match what the graph did.
    A third branch would have raised the real parallelism and left the setting
    reading the same, which is the difference between a ceiling and a comment.

    Both directions in one test: at a limit of one the two invocations cannot
    overlap, and at two they do -- so what is measured is the bound and not the
    executor being serial anyway.
    """

    async def peak_overlap(limit: int) -> int:
        overlapping = 0
        peak = 0
        started = asyncio.Event()

        class _Overlapping:
            async def run(
                self, request: AgentRunRequest, emit: Any, cancellation: Any
            ) -> AgentOutcome:
                nonlocal overlapping, peak
                overlapping += 1
                peak = max(peak, overlapping)
                started.set()
                # Long enough for the other invocation to arrive if it may.
                await asyncio.sleep(0.02)
                overlapping -= 1
                return AgentOutcome(
                    agent_run_id=request.trace.agent_run_id,
                    status="completed",
                    stop_reason="completed",
                    output_text="text-only result",
                )

        bounded = BoundedParallelExecutor(
            cast("Any", _Overlapping()), max_parallel=limit
        )
        request = AgentRunRequest(
            trace=TraceContext(agent_run_id="run_1"),
            run_kind="task",
            stream_id="thread_1",
            principal=PrincipalContext(tenant_id="tenant_a", principal_id="user_1"),
            envelope=AuthorizationEnvelope(),
            budget=RunBudget(max_steps=2, max_tool_calls=2),
            messages=(user_message("go"),),
        )
        await asyncio.gather(
            *(
                bounded.run(request, cast("Any", object()), NullCancellationToken())
                for _ in range(2)
            )
        )
        return peak

    assert asyncio.run(peak_overlap(1)) == 1
    assert asyncio.run(peak_overlap(2)) == 2


# --- one attempt's wall clock (ADR-030) --------------------------------------


def test_an_invocation_carries_no_deadline_unless_one_was_configured() -> None:
    """The control, and the pre-ADR-030 behaviour verbatim.

    Absent has to stay absent. A deployment that configured no wall clock must
    not acquire one from an upgrade -- a node that reliably takes four minutes
    would start failing on a default nobody chose.
    """

    registry = _Registry(_task())

    with SCOPE.executing(LEASE):
        invocation = asyncio.run(_provider(registry).resolve(_state(), "plan"))

    assert invocation.context.budget.deadline is None


def test_the_node_runs_under_the_epoch_it_was_claimed_with() -> None:
    """The trace carries the claim, and it carries the *lease's* copy of it.

    The Registry row agrees at this instant -- `resolve` has just compared them
    and raised if they differed -- so the two are indistinguishable here and
    would be indistinguishable in any test that only read one of them. They
    stop agreeing at exactly the moment that matters: a Worker whose lease
    expired mid-node, whose row now names its replacement's epoch. Reading the
    row there would hand the fence the number it is fencing against.
    """

    registry = _Registry(_task())

    with SCOPE.executing(LEASE):
        invocation = asyncio.run(_provider(registry).resolve(_state(), "plan"))

    assert invocation.context.trace.lease_epoch == LEASE.epoch
    assert invocation.lease.epoch == LEASE.epoch


def test_a_configured_wall_clock_becomes_a_deadline_on_this_attempt() -> None:
    """Stamped from the clock at resolve time, not from process start."""

    registry = _Registry(_task())
    start = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    provider = _provider(registry, max_seconds_per_invocation=90, clock=lambda: start)

    with SCOPE.executing(LEASE):
        invocation = asyncio.run(provider.resolve(_state(), "plan"))

    assert invocation.context.budget.deadline == start + timedelta(seconds=90)


def test_each_attempt_gets_its_own_deadline_rather_than_a_shared_one() -> None:
    """The reason this is not folded into the composition-time budget.

    A deadline built once would be shared by every invocation for the life of
    the process: the first node would get its ninety seconds, and a node
    reached an hour later would be born already overdue. Two resolutions at
    different times must therefore produce two different deadlines.

    This is also the replay rule. A retried attempt is a new attempt and gets
    the full allowance again -- storing the deadline with the Task instead
    would make a node that waited out an outage permanently unrunnable.
    """

    registry = _Registry(_task())
    times = iter(
        [
            datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 13, 0, 0, tzinfo=UTC),
        ]
    )
    provider = _provider(
        registry, max_seconds_per_invocation=90, clock=lambda: next(times)
    )

    with SCOPE.executing(LEASE):
        first = asyncio.run(provider.resolve(_state(), "plan"))
        second = asyncio.run(provider.resolve(_state(), "plan"))

    assert first.context.budget.deadline is not None
    assert second.context.budget.deadline is not None
    assert second.context.budget.deadline > first.context.budget.deadline
    # And each is its own allowance rather than one clock shared between them.
    assert second.context.budget.deadline - first.context.budget.deadline == timedelta(
        hours=1
    )


def test_stamping_a_deadline_leaves_the_rest_of_the_budget_alone() -> None:
    """A copy, not a rebuild. The other ceilings are still the configured ones."""

    registry = _Registry(_task())
    provider = _provider(registry, max_seconds_per_invocation=30)

    with SCOPE.executing(LEASE):
        stamped = asyncio.run(provider.resolve(_state(), "plan")).context.budget

    assert stamped.max_steps == 12
    assert stamped.max_tool_calls == 24


# --------------------------------------------------------------------------
# The v2 general graph's nodes, through the same factory (ADR-031)
# --------------------------------------------------------------------------


def test_one_factory_serves_both_graphs_and_shares_the_common_nodes() -> None:
    """The sharing ADR-031 §3 asks for, asserted structurally.

    ``build_task_handlers`` is what the composition root calls once, and both
    graph builders index the one mapping it returns -- so at the shared node
    ids there is one handler object, not two implementations that agree today.
    The factory returning the union is what makes that composition possible;
    a factory that produced only one graph's nodes would leave the other graph
    running pass-throughs.
    """

    handlers = build_task_handlers(
        executor=_TextExecutor(),
        artifacts=InMemoryArtifactStore(),
        invocations=_provider(_Registry(_task())),
    )

    assert set(handlers) == set(V1_HANDLER_NODES) | set(V2_HANDLER_NODES)
    # The two public selectors name exactly their graph's nodes.
    selected_v1 = build_task_v1_handlers(
        executor=_TextExecutor(),
        artifacts=InMemoryArtifactStore(),
        invocations=_provider(_Registry(_task())),
    )
    selected_v2 = build_task_v2_handlers(
        executor=_TextExecutor(),
        artifacts=InMemoryArtifactStore(),
        invocations=_provider(_Registry(_task())),
    )
    assert set(selected_v1) == set(V1_HANDLER_NODES)
    assert set(selected_v2) == set(V2_HANDLER_NODES)


def test_v2_real_handlers_run_the_whole_graph_and_bind_review_to_the_draft() -> None:
    """Every v2 node with real handlers, through the real compiled graph.

    The v2 twin of the v1 whole-graph test above, and the same claim: text in,
    owned artifacts out, inside one claim. What is specifically v2 here is the
    binding -- the reviewer's verdict names the exact artifact the work node
    persisted this pass, which is only checkable once real ids exist.
    """

    async def scenario() -> None:
        registry = _Registry(_task(graph_version="v2_general"))
        store = InMemoryArtifactStore()
        executor = _TextExecutor()

        async def decided_approval(_: TaskState) -> dict[str, Any]:
            return {"approval_id": "apr_1", "approval_decision": "approved"}

        exporter = _RecordingExport()
        handlers = build_task_v2_handlers(
            executor=executor,
            artifacts=store,
            invocations=_provider(registry),
            export=_export(exporter),
        ) | {"approval": decided_approval}

        original = executor.run

        async def with_current_draft(
            request: AgentRunRequest, emit: object, cancellation: object
        ) -> AgentOutcome:
            if request.trace.graph_node_id == "review":
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
            _state(), thread_id="thread_v2", graph_version="v2_general"
        )

        assert result.disposition == "completed"
        assert result.state.draft_ref is not None
        assert result.state.export_ref == "art_report_1"
        assert exporter.calls == [
            {
                "draft_ref": result.state.draft_ref,
                "approval_id": "apr_1",
                "task_id": "task_1",
                "lease_epoch": registry.task.lease_epoch,
            }
        ]
        # No research nodes ran and none were asked for: v2 contributes no
        # evidence, and the nodes that executed are exactly its own.
        assert result.state.evidence_refs == ()
        assert {request.trace.graph_node_id for request in executor.requests} == {
            "understand",
            "work",
            "review",
        }
        # The work node's product is the Task's deliverable, stored as one.
        stored = await store.head(
            tenant_id="tenant_a",
            artifact_id=result.state.draft_ref,
            principal_id="user_1",
        )
        assert stored.kind == "report"

    _run(scenario)


def test_the_reviewer_refuses_to_review_before_there_is_work() -> None:
    async def scenario() -> None:
        handlers = build_task_v2_handlers(
            executor=_TextExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task(graph_version="v2_general"))),
        )
        with pytest.raises(StructuredOutputError):
            await handlers["review"](_state())

    _run(scenario)


def test_a_narrated_verdict_is_asked_for_the_object_alone_at_both_reviewers() -> None:
    """v2's reviewer shares v1's decoder, so it shares its exposure (ADR-034 §3.4).

    And it is the likelier of the two to need this: the reviewer holds the
    read-only workspace tools, and a model that has just used tools tends to
    say what it did before it answers -- which is exactly how the same defect
    reached a real model at ``research_external``.
    """

    review = _text_for("critic").replace("PLACEHOLDER", "draft_1")
    state = _state(draft_ref="draft_1", revision_count=0)

    async def scenario() -> tuple[_SequencedExecutor, dict[str, Any]]:
        executor = _SequencedExecutor(
            {"review": [f"The working set looks complete.\n\n{review}", review]}
        )
        handlers = build_task_v2_handlers(
            executor=cast(Any, executor),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(
                _Registry(
                    _task(
                        graph_version="v2_general",
                        # Without this the profile's tools are narrowed away by
                        # the envelope and the assertion below would pass on a
                        # reviewer that never held any.
                        submitted_authorization_envelope=AuthorizationEnvelope(
                            allowed_tools=WORKSPACE_TOOLS
                        ),
                    )
                )
            ),
        )
        return executor, dict(await handlers["review"](state))

    executor, update = _run(scenario)

    assert update["review_result"]["decision"] == "pass"
    assert len(executor.requests) == 2
    assert update["budget_usage"]["steps"] == 2
    # The reviewer's tools come from its profile rather than a catalog, so the
    # restatement is stripped centrally. A second look at the working set could
    # only produce a different verdict than the one being restated.
    assert executor.requests[0].tool_names != ()
    assert executor.requests[1].tool_names == ()


def test_a_verdict_that_was_already_one_json_object_buys_no_second_run() -> None:
    """The control, at the reviewer that holds tools."""

    async def scenario() -> _SequencedExecutor:
        review = _text_for("critic").replace("PLACEHOLDER", "draft_1")
        executor = _SequencedExecutor({"review": [review]})
        handlers = build_task_v2_handlers(
            executor=cast(Any, executor),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task(graph_version="v2_general"))),
        )
        await handlers["review"](_state(draft_ref="draft_1", revision_count=0))
        return executor

    executor = _run(scenario)

    assert len(executor.requests) == 1


def test_a_failed_work_run_keeps_the_usage_it_spent() -> None:
    """The v2 twin of the artifact-failure test: a charge without a product."""

    async def scenario() -> None:
        handlers = build_task_v2_handlers(
            executor=_TextExecutor(failing_node="work"),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task(graph_version="v2_general"))),
        )
        with pytest.raises(AgentNodeFailedError) as failure:
            await handlers["work"](_state())
        assert failure.value.state.budget_usage.steps == 1
        assert len(failure.value.state.agent_outcome_refs) == 1

    _run(scenario)


def test_v2s_two_workspace_nodes_enter_the_session_their_tools_need() -> None:
    """The worker writes and the reviewer reads, through real workspace tools.

    The gate under test is ``_uses_workspace``, which derives "who pays for a
    session" from the profiles instead of a node-name list -- and v2's nodes
    are exactly the change that would have left the list stale. Asserted
    through the tools rather than the scope, because the failure being
    prevented is a run that advertises tools, fails every call with
    ``WorkspaceUnavailableError``, and still reports success (ADR-028 §3.2).
    """

    scope = WorkspaceScope()
    tool_results: list[ToolResult] = []
    principal = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")

    def _call(tool: Any, name: str, arguments: dict[str, Any]) -> Any:
        return tool.handle(
            ToolInvocation(
                call=ToolCall(
                    tool_call_id="toolu_" + "0" * 20,
                    tool_name=name,
                    arguments=arguments,
                ),
                context=ExecutionContext(
                    principal=principal,
                    envelope=AuthorizationEnvelope(),
                    agent_run_id="run_workspace",
                    policy_identity="policy-v1:test",
                ),
                cancellation=NullCancellationToken(),
                timeout_seconds=30,
            )
        )

    class _WorkspaceUsingExecutor(_TextExecutor):
        async def run(
            self, request: AgentRunRequest, emit: object, cancellation: object
        ) -> AgentOutcome:
            node = request.trace.graph_node_id
            if node == "work":
                tool_results.append(
                    await _call(
                        WorkspaceWriteTool(scope),
                        "workspace_write",
                        {"name": "result.md", "content": "cleaned"},
                    )
                )
            if node == "review":
                tool_results.append(
                    await _call(WorkspaceListTool(scope), "workspace_list", {})
                )
                # The verdict must name the exact draft the prompt supplied,
                # like any reviewing agent's.
                prompt = request.messages[0].text()
                draft_id = prompt.split("draft_ref=", 1)[1].split("\n", 1)[0]
                self.requests.append(request)
                return AgentOutcome(
                    agent_run_id=request.trace.agent_run_id,
                    status="completed",
                    stop_reason="completed",
                    output_text=_text_for("critic").replace("PLACEHOLDER", draft_id),
                    usage=BudgetUsage(
                        steps=1, tokens=TokenUsage(input_tokens=3, output_tokens=5)
                    ),
                )
            return await super().run(request, emit, cancellation)

    async def scenario() -> None:
        handlers = build_task_v2_handlers(
            executor=_WorkspaceUsingExecutor(),
            artifacts=InMemoryArtifactStore(),
            invocations=_provider(_Registry(_task(graph_version="v2_general"))),
            workspace_scope=scope,
        )
        work_update = await handlers["work"](_state())
        # The version the work node committed is what the graph carries
        # forward; a write nothing commits is one the next attempt cannot see.
        assert work_update["workspace_version"]

        reviewed = TaskState.model_validate(
            {**_state().model_dump(), **dict(work_update)}
        )
        review_update = dict(await handlers["review"](reviewed))
        # Read-only: the reviewer entered a session and advanced nothing.
        assert "workspace_version" not in review_update

    _run(scenario)

    write_result, list_result = tool_results
    assert write_result.status == "ok"
    assert list_result.status == "ok"
    # The reviewer's listing read the set the worker's session committed.
    assert list_result.content is not None
    assert "result.md" in list_result.content
