"""Contract for the LangGraph-backed workflow adapter.

The adapter is checked against the same ``TaskWorkflowPort`` contract the
in-memory fake satisfies, plus the two things only a real graph can show: that
it compiles the declared edges rather than a restatement of them, and that a
checkpoint round trip does not lose state.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langgraph.checkpoint.memory import (  # pyright: ignore[reportMissingTypeStubs]
    InMemorySaver,
)

from agent_workbench.adapters.langgraph.workflow import (
    GRAPH_DEFINITIONS,
    GRAPH_VERSION_KEY,
    UNRECORDED_GRAPH_VERSION,
    GraphState,
    LangGraphTaskWorkflow,
    build_v1_graph,
)
from agent_workbench.adapters.testing import FailpointController, InjectedFaultError
from agent_workbench.domain.tasks import ReviewResult, TaskState, TaskStep
from agent_workbench.ports.task_workflow import (
    TaskWorkflowPort,
    WorkflowGraphVersionMismatchError,
    WorkflowThreadAlreadyExistsError,
    WorkflowThreadNotFoundError,
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


def _passing_review(revision: int = 0) -> ReviewResult:
    return ReviewResult(
        decision="pass",
        reviewed_draft_ref="draft_1",
        revision_number=revision,
        summary="Grounded in the evidence.",
        score=90,
    )


def _handlers() -> dict[str, Any]:
    """Deterministic stand-ins: each node writes only its own channel."""

    async def understand(state: TaskState) -> dict[str, Any]:
        return {"agent_outcome_refs": ("run_understand",)}

    async def internal(state: TaskState) -> dict[str, Any]:
        return {
            "evidence_refs": ("ev_internal",),
            "agent_outcome_refs": ("run_internal",),
        }

    async def external(state: TaskState) -> dict[str, Any]:
        return {
            "evidence_refs": ("ev_external",),
            "agent_outcome_refs": ("run_external",),
        }

    async def synthesize(state: TaskState) -> dict[str, Any]:
        return {"draft_ref": "draft_1", "review_result": None}

    async def critic(state: TaskState) -> dict[str, Any]:
        return {"review_result": _passing_review(state.revision_count).model_dump()}

    async def approval(state: TaskState) -> dict[str, Any]:
        # Answers its own gate. The interrupting node is the adapter's
        # build_approval_node, tested separately; these handlers exist to
        # exercise the edges, and a graph whose approval node returns nothing
        # now fails at the router rather than exporting unapproved.
        return {"approval_id": "approval_1", "approval_decision": "approved"}

    return {
        "understand": understand,
        "research_internal": internal,
        "research_external": external,
        "synthesize": synthesize,
        "critic": critic,
        "approval": approval,
    }


def _workflow(**kwargs: Any) -> LangGraphTaskWorkflow:
    return LangGraphTaskWorkflow(handlers=_handlers(), **kwargs)


# --------------------------------------------------------------------------
# Port conformance and workflow identity
# --------------------------------------------------------------------------


def test_the_adapter_satisfies_the_framework_neutral_port() -> None:
    assert isinstance(_workflow(), TaskWorkflowPort)


def test_a_run_reaches_the_terminal_node_through_the_declared_edges() -> None:
    result = asyncio.run(
        _workflow().run(_state(), thread_id="thread_1", graph_version="v1")
    )

    assert result.disposition == "completed"
    assert result.thread_id == "thread_1"
    assert result.graph_version == "v1"
    # Both branches ran and merged: the sorted union is the adapter's channel
    # reducer, so LangGraph's fan-in agrees with the control flow's fan_in.
    assert result.state.evidence_refs == ("ev_external", "ev_internal")
    assert result.state.draft_ref == "draft_1"


def test_node_failpoint_runs_after_a_handler_and_before_checkpointing() -> None:
    async def scenario() -> None:
        controller = FailpointController(frozenset({"after_node_before_checkpoint"}))
        controller.arm("after_node_before_checkpoint", mode="raise")
        workflow = _workflow(fault_injector=controller)

        with pytest.raises(InjectedFaultError):
            await workflow.run(
                _state(), thread_id="thread_failpoint", graph_version="v1"
            )
        await controller.wait_until_hit("after_node_before_checkpoint")

    asyncio.run(scenario())


def test_a_first_run_refuses_a_thread_that_already_exists() -> None:
    async def scenario() -> None:
        workflow = _workflow()
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        with pytest.raises(WorkflowThreadAlreadyExistsError):
            await workflow.run(_state(), thread_id="thread_1", graph_version="v1")

    asyncio.run(scenario())


def test_resume_rejects_an_unknown_thread() -> None:
    with pytest.raises(WorkflowThreadNotFoundError):
        asyncio.run(_workflow().resume(thread_id="thread_absent", graph_version="v1"))


def test_resume_fails_closed_on_a_graph_version_mismatch() -> None:
    async def scenario() -> None:
        workflow = _workflow()
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")

        with pytest.raises(WorkflowGraphVersionMismatchError) as captured:
            await workflow.resume(thread_id="thread_1", graph_version="v2")
        assert captured.value.checkpoint_graph_version == "v1"
        assert captured.value.requested_graph_version == "v2"

        # The rejected resume must not have migrated or damaged the checkpoint.
        recovered = await workflow.resume(thread_id="thread_1", graph_version="v1")
        assert recovered.disposition == "completed"

    asyncio.run(scenario())


def test_an_unregistered_graph_version_never_falls_back_to_the_newest() -> None:
    with pytest.raises(WorkflowGraphVersionMismatchError):
        asyncio.run(
            _workflow().run(_state(), thread_id="thread_1", graph_version="v99")
        )


# --------------------------------------------------------------------------
# Workflow identity lives in the checkpoint, not in this object


def test_every_checkpoint_records_which_graph_wrote_it() -> None:
    """The mechanism the durable answers rest on.

    ``get_checkpoint_metadata`` copies configurable scalars into each
    checkpoint's metadata, so putting the version on the config is what makes
    it survive the process. If it stopped landing there, ``resume`` would
    silently start treating every thread as unversioned.
    """

    saver = InMemorySaver()

    async def scenario() -> list[str]:
        workflow = LangGraphTaskWorkflow(handlers=_handlers(), checkpointer=saver)
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        return [
            tuple_.metadata.get(GRAPH_VERSION_KEY, UNRECORDED_GRAPH_VERSION)
            async for tuple_ in saver.alist({"configurable": {"thread_id": "thread_1"}})
        ]

    recorded = asyncio.run(scenario())

    assert recorded
    assert set(recorded) == {"v1"}


def test_a_second_adapter_over_the_same_checkpoint_sees_the_same_thread() -> None:
    """Existence and version are read from the store, not from this instance.

    Two adapters over one saver stand in for two processes: the second never
    saw the run start, and must still refuse to start it again and still know
    which graph wrote it.
    """

    saver = InMemorySaver()

    async def scenario() -> tuple[Any, Any]:
        first = LangGraphTaskWorkflow(handlers=_handlers(), checkpointer=saver)
        await first.run(_state(), thread_id="thread_1", graph_version="v1")

        second = LangGraphTaskWorkflow(handlers=_handlers(), checkpointer=saver)
        with pytest.raises(WorkflowThreadAlreadyExistsError):
            await second.run(_state(), thread_id="thread_1", graph_version="v1")
        with pytest.raises(WorkflowGraphVersionMismatchError) as mismatch:
            await second.resume(thread_id="thread_1", graph_version="v2")
        resumed = await second.resume(thread_id="thread_1", graph_version="v1")
        return mismatch.value.checkpoint_graph_version, resumed.disposition

    checkpoint_version, disposition = asyncio.run(scenario())

    assert checkpoint_version == "v1"
    assert disposition == "completed"


def test_a_checkpoint_with_no_recorded_version_refuses_to_resume() -> None:
    """A thread this adapter did not write is not one it can claim to read.

    The graph is driven directly here, with a config that carries no version --
    which is what a checkpoint written before this was recorded looks like.
    Guessing "it is probably the only registered version" would be a guess made
    exactly when a wrong answer costs the most.
    """

    saver = InMemorySaver()

    async def scenario() -> Any:
        graph = build_v1_graph(_handlers()).compile(checkpointer=saver)
        await graph.ainvoke(
            _state().model_dump(), {"configurable": {"thread_id": "thread_1"}}
        )
        workflow = LangGraphTaskWorkflow(handlers=_handlers(), checkpointer=saver)
        with pytest.raises(WorkflowGraphVersionMismatchError) as captured:
            await workflow.resume(thread_id="thread_1", graph_version="v1")
        return captured.value.checkpoint_graph_version

    assert asyncio.run(scenario()) == UNRECORDED_GRAPH_VERSION


def test_resume_does_not_resubmit_the_original_input() -> None:
    calls: list[str] = []

    async def counting_understand(state: TaskState) -> dict[str, Any]:
        calls.append(state.task_id)
        return {"agent_outcome_refs": ("run_understand",)}

    handlers = _handlers() | {"understand": counting_understand}

    async def scenario() -> None:
        workflow = LangGraphTaskWorkflow(handlers=handlers)
        await workflow.run(_state(), thread_id="thread_1", graph_version="v1")
        await workflow.resume(thread_id="thread_1", graph_version="v1")
        await workflow.resume(thread_id="thread_1", graph_version="v1")

    asyncio.run(scenario())

    # A resume that passed the initial state again would re-enter the graph
    # from START and run understand a second time.
    assert calls == ["task_1"]


# --------------------------------------------------------------------------
# The adapter compiles the declaration rather than restating it
# --------------------------------------------------------------------------


def test_every_task_state_field_is_a_graph_channel() -> None:
    # A field added to TaskState without a channel here would be silently
    # dropped on the first checkpoint round trip.
    assert set(GraphState.__annotations__) == set(TaskState.model_fields)


def test_a_checkpoint_round_trip_preserves_the_state() -> None:
    started = _state(max_revisions=1)
    result = asyncio.run(
        _workflow().run(started, thread_id="thread_1", graph_version="v1")
    )

    assert result.state.task_id == started.task_id
    assert result.state.objective == started.objective
    assert result.state.plan == started.plan
    assert result.state.max_revisions == 1


def test_only_the_registered_versions_are_buildable() -> None:
    assert set(GRAPH_DEFINITIONS) == {"v1", "v2_general"}


# --------------------------------------------------------------------------
# The conditional edges behave as the control flow specifies
# --------------------------------------------------------------------------


def test_an_exhausted_revision_budget_fails_the_graph_without_approving() -> None:
    async def revising_critic(state: TaskState) -> dict[str, Any]:
        return {
            "review_result": ReviewResult(
                decision="revise",
                reviewed_draft_ref="draft_1",
                revision_number=state.revision_count,
                summary="Still thin.",
                issues=("Evidence is thin.",),
                score=30,
            ).model_dump()
        }

    async def approval(state: TaskState) -> dict[str, Any]:
        return {"approval_id": "approval_1"}

    handlers = _handlers() | {"critic": revising_critic, "approval": approval}

    result = asyncio.run(
        LangGraphTaskWorkflow(handlers=handlers).run(
            _state(max_revisions=0),
            thread_id="thread_1",
            graph_version="v1",
        )
    )

    # The critic rejected the draft and there was no budget to revise. The
    # graph must fail, not walk into approval or report an empty queue as
    # successful completion.
    assert result.state.approval_id is None
    assert result.state.review_result is not None
    assert result.state.review_result.decision == "revise"
    assert result.disposition == "failed"
    assert result.failure_reason is not None


def test_a_pending_quality_gate_is_not_misread_as_a_terminal_failure() -> None:
    """A crash after critic but before gate leaves work to resume."""

    async def revising_critic(state: TaskState) -> dict[str, Any]:
        return {
            "review_result": ReviewResult(
                decision="revise",
                reviewed_draft_ref="draft_1",
                revision_number=state.revision_count,
                summary="Still thin.",
                issues=("Evidence is thin.",),
                score=30,
            ).model_dump()
        }

    async def interrupted_gate(_: TaskState) -> dict[str, Any]:
        raise RuntimeError("simulated process interruption")

    async def scenario() -> Any:
        workflow = LangGraphTaskWorkflow(
            handlers=_handlers()
            | {"critic": revising_critic, "quality_gate": interrupted_gate}
        )
        with pytest.raises(RuntimeError, match="simulated process interruption"):
            await workflow.run(
                _state(max_revisions=0),
                thread_id="thread_1",
                graph_version="v1",
            )
        return await workflow.inspect("thread_1")

    position = asyncio.run(scenario())

    assert position is not None
    assert position.pending_nodes == ("quality_gate",)
    assert position.failure_reason is None


def test_revisions_are_counted_before_each_retry_and_stop_at_the_budget() -> None:
    calls: dict[str, int] = {"synthesize": 0, "critic": 0}

    async def synthesize(state: TaskState) -> dict[str, Any]:
        calls["synthesize"] += 1
        return {"draft_ref": f"draft_{state.revision_count}", "review_result": None}

    async def revising_critic(state: TaskState) -> dict[str, Any]:
        calls["critic"] += 1
        return {
            "review_result": ReviewResult(
                decision="revise",
                reviewed_draft_ref=f"draft_{state.revision_count}",
                revision_number=state.revision_count,
                summary="Needs another pass.",
                issues=("Evidence is thin.",),
                score=30,
            ).model_dump()
        }

    handlers = _handlers() | {
        "synthesize": synthesize,
        "critic": revising_critic,
    }
    result = asyncio.run(
        LangGraphTaskWorkflow(handlers=handlers).run(
            _state(max_revisions=2),
            thread_id="thread_1",
            graph_version="v1",
        )
    )

    # One initial draft plus exactly two critic-requested revisions. This
    # guards against the old loop, where revision_count was never advanced.
    assert calls == {"synthesize": 3, "critic": 3}
    assert result.disposition == "failed"
    assert result.state.revision_count == 2
    assert result.state.review_result is not None
    assert result.state.review_result.revision_number == 2


@pytest.mark.parametrize(
    ("decision", "expected_visits", "expected_disposition"),
    [
        ("approved", ["approval", "export"], "completed"),
        # The control group. Same graph, same passing review, one different
        # decision -- and the export handler must never run.
        ("rejected", ["approval"], "failed"),
    ],
)
def test_the_approval_decision_decides_whether_the_export_runs(
    decision: str,
    expected_visits: list[str],
    expected_disposition: str,
) -> None:
    visited: list[str] = []

    async def approval(state: TaskState) -> dict[str, Any]:
        visited.append("approval")
        return {"approval_id": "approval_1", "approval_decision": decision}

    async def export(state: TaskState) -> dict[str, Any]:
        visited.append("export")
        return {}

    handlers = _handlers() | {"approval": approval, "export": export}

    result = asyncio.run(
        LangGraphTaskWorkflow(handlers=handlers).run(
            _state(), thread_id="thread_1", graph_version="v1"
        )
    )

    assert visited == expected_visits
    assert result.state.approval_id == "approval_1"
    assert result.state.approval_decision == decision
    assert result.disposition == expected_disposition
    # A rejection is a deliberate terminal failure with a reason, not an empty
    # pending-node list a caller could read as success.
    assert (result.failure_reason is not None) is (decision == "rejected")


def test_an_ungated_deployment_exports_without_opening_an_approval() -> None:
    """Against the *compiled* v1 graph, which is the only place this can fail.

    ``route_quality_gate`` gained "export" when v1 was taught to read
    ``export_requires_approval``, and the edge list beside it is a separate
    declaration. Every routing test passes either way -- they call the router
    and compare its return value -- while LangGraph resolves that same value
    against the edge list, so a target the router can answer and no edge
    declares is `KeyError: 'export'` inside the graph. v2 shipped that exact
    bug once (`test_general_graph_execution`); running the graph is what keeps
    v1 from shipping it twice.
    """

    visited: list[str] = []

    async def approval(state: TaskState) -> dict[str, Any]:
        visited.append("approval")
        return {"approval_id": "approval_1", "approval_decision": "approved"}

    async def export(state: TaskState) -> dict[str, Any]:
        visited.append("export")
        return {}

    handlers = _handlers() | {"approval": approval, "export": export}

    result = asyncio.run(
        LangGraphTaskWorkflow(handlers=handlers).run(
            _state(export_requires_approval=False),
            thread_id="thread_ungated",
            graph_version="v1",
        )
    )

    assert visited == ["export"]
    assert result.disposition == "completed"
    # Skipped, not auto-answered: no approval was opened, so nothing later
    # reads a decision nobody made.
    assert result.state.approval_id is None
    assert result.state.approval_decision is None
    assert result.failure_reason is None
