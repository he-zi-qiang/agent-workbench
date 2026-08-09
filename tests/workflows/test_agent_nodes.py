"""Contract for the agent-backed nodes of the fixed research graph.

The executor is a fake throughout: these assert what a node does with an
outcome, which is exactly the part that must not depend on a real model.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    RunBudget,
    TokenUsage,
    TraceContext,
)
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventScope
from agent_workbench.workflows.agent_nodes import (
    ARTIFACT_PRODUCING_NODES,
    AgentNodeFailedError,
    ArtifactProducingAgentNode,
    TaskRunContext,
    absorb_draft,
    build_request,
    node_prompt,
    research_contribution,
)

_TENANT = "tenant_1"


class _FakeExecutor:
    """Returns a scripted outcome and records the request it was given."""

    def __init__(self, outcome: AgentOutcome) -> None:
        self._outcome = outcome
        self.requests: list[AgentRunRequest] = []

    async def run(
        self,
        request: AgentRunRequest,
        emit: object,
        cancellation: object,
    ) -> AgentOutcome:
        self.requests.append(request)
        return self._outcome


def _artifact(artifact_id: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        tenant_id=_TENANT,
        kind="agent_outcome",
        media_type="text/markdown",
        size_bytes=128,
        sha256="a" * 64,
    )


def _completed(run_id: str, artifact_id: str | None) -> AgentOutcome:
    return AgentOutcome(
        agent_run_id=run_id,
        status="completed",
        stop_reason="completed",
        output_ref=None if artifact_id is None else _artifact(artifact_id),
        usage=BudgetUsage(
            steps=2,
            tool_calls=1,
            tokens=TokenUsage(input_tokens=100, output_tokens=50),
            cost_micro_usd=700,
        ),
    )


def _failed(run_id: str) -> AgentOutcome:
    return AgentOutcome(
        agent_run_id=run_id,
        status="failed",
        stop_reason="error",
        error=ErrorInfo(code="provider_error", message="upstream refused"),
        usage=BudgetUsage(
            steps=1,
            tokens=TokenUsage(input_tokens=90, output_tokens=0),
            cost_micro_usd=400,
        ),
    )


def _context() -> TaskRunContext:
    return TaskRunContext(
        trace=TraceContext(agent_run_id="run_node"),
        stream_id="stream_1",
        principal=PrincipalContext(tenant_id=_TENANT, principal_id="user_1"),
        envelope=AuthorizationEnvelope(),
        budget=RunBudget(max_steps=8, max_tool_calls=16),
    )


def _state(**overrides: object) -> TaskState:
    base: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Compare retrieval strategies.",
        "plan": (
            TaskStep(step_id="step_1", sequence=1, objective="Gather internal notes."),
        ),
    }
    base.update(overrides)
    return TaskState.model_validate(base)


def _sink() -> ScopedEventSink:
    return ScopedEventSink(
        InMemoryEventLog(),
        EventScope(stream_id="stream_1", run_id="run_node", task_id="task_1"),
    )


async def _run(node: str, outcome: AgentOutcome, state: TaskState):
    executor = _FakeExecutor(outcome)
    reporter = ArtifactProducingAgentNode(executor)
    report = await reporter.run(
        node,  # type: ignore[arg-type]
        state,
        _context(),
        _sink(),
        NullCancellationToken(),
    )
    return report, executor


# --------------------------------------------------------------------------
# The node depends on the agent boundary and nothing else
# --------------------------------------------------------------------------


def test_the_node_reaches_the_model_only_through_the_agent_executor() -> None:
    # A node holding a registry or a model port would be a second tool loop
    # without the first one's budget, cancellation or policy guarantees.
    node = ArtifactProducingAgentNode(_FakeExecutor(_completed("run_1", "art_1")))
    held = {
        name: type(value).__name__
        for name, value in vars(node).items()
        if not callable(value)
    }
    assert set(held) == {"_executor"}


def test_a_request_carries_the_task_context_and_advertises_no_tools() -> None:
    request = build_request("research_internal", _state(), _context())

    assert request.run_kind == "task"
    assert request.stream_id == "stream_1"
    assert request.principal.principal_id == "user_1"
    # v1 agent nodes are read-only reasoning; opening a tool list here would
    # let a node act without the graph's approval node ever running.
    assert request.tool_names == ()


def test_each_audience_reaches_its_own_node_and_the_envelope_still_narrows() -> None:
    context = _context()
    context = TaskRunContext(
        trace=context.trace,
        stream_id=context.stream_id,
        principal=context.principal,
        envelope=AuthorizationEnvelope(
            allowed_tools=("mcp_office_render_document", "mcp_web_fetch_page"),
            max_tool_risk="external",
            approval_required_risks=(),
        ),
        budget=context.budget,
    )
    catalogs = {
        "synthesis": ("mcp_office_lookup", "mcp_office_render_document"),
        "research": ("mcp_web_fetch_page",),
    }

    writer = build_request("synthesize", _state(), context, dynamic_tools=catalogs)
    researcher = build_request(
        "research_external", _state(), context, dynamic_tools=catalogs
    )
    planner = build_request("plan", _state(), context, dynamic_tools=catalogs)

    # `mcp_office_lookup` is in the writer's audience and not in the envelope,
    # so it is dropped -- the audience decides which agent may reach up, and the
    # Task's envelope still decides how far up.
    assert writer.tool_names == ("mcp_office_render_document",)
    assert researcher.tool_names == ("mcp_web_fetch_page",)
    assert planner.tool_names == ()


def test_the_prompt_projects_the_state_instead_of_replaying_a_transcript() -> None:
    state = _state(
        evidence_refs=("ev_1", "ev_2"),
        agent_outcome_refs=("run_earlier",),
    )
    text = node_prompt("synthesize", state).content[0].text  # type: ignore[union-attr]

    assert "Compare retrieval strategies." in text
    assert "Gather internal notes." in text
    # Earlier output lives in the artifact store. Copying it in would make
    # context grow with the graph rather than with the question.
    assert "ev_1" not in text
    assert "run_earlier" not in text


# --------------------------------------------------------------------------
# Cost is recorded whether or not the run succeeded
# --------------------------------------------------------------------------


def test_a_successful_run_records_its_reference_and_usage() -> None:
    report, _ = asyncio.run(
        _run("research_internal", _completed("run_1", "art_1"), _state())
    )

    assert report.produced_ref == "art_1"
    assert report.state.agent_outcome_refs == ("run_1",)
    assert report.state.budget_usage.cost_micro_usd == 700
    assert report.state.budget_usage.tokens.input_tokens == 100


def test_a_failed_run_still_charges_the_state_it_raises_with() -> None:
    # The control group is the test above: same node, same call, and the only
    # difference is that the outcome failed. A node that recorded only
    # successes would let a Task retry forever inside a budget that never
    # appears to move.
    with pytest.raises(AgentNodeFailedError) as captured:
        asyncio.run(_run("research_internal", _failed("run_1"), _state()))

    charged = captured.value.state
    assert charged.agent_outcome_refs == ("run_1",)
    assert charged.budget_usage.cost_micro_usd == 400
    assert captured.value.outcome.error is not None
    assert captured.value.node == "research_internal"


def test_usage_accumulates_across_nodes_rather_than_replacing() -> None:
    first, _ = asyncio.run(
        _run("research_internal", _completed("run_1", "art_1"), _state())
    )
    second, _ = asyncio.run(
        _run("research_external", _completed("run_2", "art_2"), first.state)
    )

    assert second.state.budget_usage.cost_micro_usd == 1400
    assert second.state.agent_outcome_refs == ("run_1", "run_2")


# --------------------------------------------------------------------------
# A run that produced nothing is a failure, not an empty success
# --------------------------------------------------------------------------


def test_a_completed_run_without_an_artifact_fails_the_node() -> None:
    with pytest.raises(AgentNodeFailedError):
        asyncio.run(_run("synthesize", _completed("run_1", None), _state()))


def test_a_cancelled_run_never_becomes_content() -> None:
    cancelled = AgentOutcome(
        agent_run_id="run_1",
        status="cancelled",
        stop_reason="cancelled",
        output_ref=_artifact("art_1"),
    )

    # Even though an artifact reference is present, a cancelled run must not
    # supply one: the work behind it was stopped part way.
    with pytest.raises(AgentNodeFailedError):
        asyncio.run(_run("synthesize", cancelled, _state()))


def test_a_non_artifact_node_cannot_use_the_artifact_shaped_path() -> None:
    # plan and critic decode structured values; routing them through here
    # would silently drop the value that is their whole product.
    assert "plan" not in ARTIFACT_PRODUCING_NODES
    assert "critic" not in ARTIFACT_PRODUCING_NODES

    with pytest.raises(ValueError, match="not an artifact-producing agent node"):
        asyncio.run(_run("plan", _completed("run_1", "art_1"), _state()))


# --------------------------------------------------------------------------
# Absorbing a report into the state
# --------------------------------------------------------------------------


def test_a_research_report_becomes_a_fan_in_contribution() -> None:
    report, _ = asyncio.run(
        _run("research_external", _completed("run_2", "art_2"), _state())
    )
    contribution = research_contribution(report)

    assert contribution.evidence_refs == ("art_2",)
    assert contribution.agent_outcome_refs == ("run_2",)


def test_only_a_research_branch_produces_a_contribution() -> None:
    report, _ = asyncio.run(_run("synthesize", _completed("run_1", "art_1"), _state()))
    with pytest.raises(ValueError, match="not a research branch"):
        research_contribution(report)


def test_a_new_draft_drops_the_review_of_the_draft_it_replaces() -> None:
    reviewed = _state(
        draft_ref="draft_old",
        review_result=ReviewResult(
            decision="revise",
            reviewed_draft_ref="draft_old",
            revision_number=0,
            summary="Needs more evidence.",
            issues=("Evidence is thin.",),
            score=40,
        ),
    )
    report, _ = asyncio.run(
        _run("synthesize", _completed("run_3", "draft_new"), reviewed)
    )
    updated = absorb_draft(report)

    assert updated.draft_ref == "draft_new"
    # A review that outlived its draft would let the quality gate pass a
    # rewrite nobody read.
    assert updated.review_result is None


def test_only_synthesize_produces_a_draft() -> None:
    report, _ = asyncio.run(
        _run("research_internal", _completed("run_1", "art_1"), _state())
    )
    with pytest.raises(ValueError, match="does not produce a draft"):
        absorb_draft(report)
