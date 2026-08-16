"""The properties that must hold on *both* graphs, asserted over both.

ADR-031 §3 names the price of a second graph outright: "recovery, approval,
events and budget are cross-cutting, and every new one has to hold on both
graphs -- and missing one graph is very easy to do". This file is the place
that would notice.

Every test here is parameterised over the graph registry rather than written
twice. That is the whole design: a third graph added to ``GRAPH_DEFINITIONS``
without a handler set, without a canonical node tuple, or without its own
terminal-failure wording fails these tests on the day it is registered, not on
the day a Task recovers through it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_workbench.adapters.langgraph.workflow import (
    GRAPH_DEFINITIONS,
    LangGraphTaskWorkflow,
)
from agent_workbench.adapters.testing import FailpointController, InjectedFaultError
from agent_workbench.domain.tasks import (
    CANONICAL_V1_NODE_IDS,
    CANONICAL_V2_NODE_IDS,
    TaskNodeId,
    TaskState,
)
from agent_workbench.workflows import general_graph, research_graph
from agent_workbench.workflows.demo_handlers import (
    build_demo_handlers,
    build_demo_v1_handlers,
    build_demo_v2_handlers,
)
from agent_workbench.workflows.task_handlers import (
    V1_HANDLER_NODES,
    V2_HANDLER_NODES,
)

#: Every graph this repository ships, with the three things each has to declare
#: separately. Keyed by the version the checkpoint records, which is what a
#: recovery has in hand.
GRAPHS = {
    "v1": (research_graph, CANONICAL_V1_NODE_IDS, V1_HANDLER_NODES),
    "v2_general": (general_graph, CANONICAL_V2_NODE_IDS, V2_HANDLER_NODES),
}

VERSIONS = sorted(GRAPHS)

DEMO_HANDLERS = {
    "v1": build_demo_v1_handlers,
    "v2_general": build_demo_v2_handlers,
}


def _state(**overrides: object) -> TaskState:
    base: dict[str, object] = {
        "task_id": "task_1",
        "objective": "Do the thing the objective describes.",
    }
    base.update(overrides)
    return TaskState.model_validate(base)


# --- every graph is registered as completely as every other ------------------


def test_the_registry_holds_exactly_the_graphs_this_repository_declares() -> None:
    """The control for everything below: the parameter list is not a subset.

    Each test here runs over ``GRAPHS``, so a graph registered in the adapter
    and forgotten here would be tested by nothing at all -- which is the exact
    shape of the mistake this file exists to catch.
    """

    assert set(GRAPH_DEFINITIONS) == set(GRAPHS)


@pytest.mark.parametrize("version", VERSIONS)
def test_each_graph_declares_exactly_the_nodes_its_canonical_tuple_names(
    version: str,
) -> None:
    """``declared_nodes()``, run on both (ADR-031 §3).

    The tuple is durable checkpoint metadata and the function is what the graph
    says it can route. A node reachable but not named is one a recovery reads
    off a checkpoint and cannot classify; a node named but unreachable is a
    promise nothing keeps.
    """

    module, canonical, _ = GRAPHS[version]

    assert module.declared_nodes() == frozenset(canonical)
    assert len(canonical) == len(set(canonical))


@pytest.mark.parametrize("version", VERSIONS)
def test_each_graph_starts_and_ends_at_a_node_it_declares(version: str) -> None:
    module, _, _ = GRAPHS[version]

    assert module.ENTRY_NODE in module.declared_nodes()
    assert module.TERMINAL_NODE in module.declared_nodes()


@pytest.mark.parametrize("version", VERSIONS)
def test_every_conditional_node_of_every_graph_is_one_it_can_route(
    version: str,
) -> None:
    module, _, _ = GRAPHS[version]

    assert module.declared_nodes() >= module.CONDITIONAL_NODES


@pytest.mark.parametrize("version", VERSIONS)
def test_every_node_of_every_graph_has_a_handler_or_is_graph_control(
    version: str,
) -> None:
    """The missing-handler failure is silent, which is why this is asserted.

    An unsupplied node is not an error in the adapter -- it defaults to a
    pass-through. A graph whose working node had no handler would run end to
    end, do nothing, and report success.
    """

    module, canonical, handler_nodes = GRAPHS[version]
    unhandled = set(canonical) - set(handler_nodes)

    # Only a node that exists to route may lack one. A working node without a
    # handler is the silent case above; a routing node with one would be a
    # supervisor that had acquired a prompt.
    assert unhandled <= set(module.CONDITIONAL_NODES)
    # And ``approval`` is always among them: it interrupts, and interrupting is
    # the framework's, so the composition root supplies that node instead.
    assert "approval" in unhandled


@pytest.mark.parametrize("version", VERSIONS)
def test_every_node_of_every_graph_has_a_demo_handler(version: str) -> None:
    """Including ``approval``: the demo graph answers its own gate.

    Without this a demo Worker would run a submitted Task of the other shape
    through pass-throughs and report success, which is indistinguishable from
    a Task that worked.
    """

    _, _, handler_nodes = GRAPHS[version]
    expected = set(handler_nodes) | {"approval"}

    assert set(DEMO_HANDLERS[version]()) == expected
    # And the combined set a demo Worker is actually built from covers both.
    assert expected <= set(build_demo_handlers())


# --- recovery: each graph's terminal wording is its own ----------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_each_graph_reports_its_own_terminal_failure_reason(version: str) -> None:
    """Registered per version, because the wrong one does not raise.

    Both graphs stop on the same two facts -- a reviewer that ran out of
    revisions, and a human who said no -- and each words them for its own
    nodes. A reader fixed on one graph would put v1's sentence on a v2 Task,
    and every test that only checks *whether* a Task failed would pass.
    """

    module, _, _ = GRAPHS[version]

    assert GRAPH_DEFINITIONS[version].terminal_failure_reason is (
        module.terminal_failure_reason
    )


def test_the_two_graphs_word_an_exhausted_budget_differently() -> None:
    """The control for the test above: swapping them would be observable."""

    exhausted = _state(
        draft_ref="draft_1",
        revision_count=2,
        max_revisions=2,
        review_result={
            "decision": "revise",
            "reviewed_draft_ref": "draft_1",
            "revision_number": 2,
            "summary": "Still not there.",
            "issues": ("It still fails.",),
            "score": 20,
        },
    )
    v1_reason = research_graph.terminal_failure_reason(exhausted)
    v2_reason = general_graph.terminal_failure_reason(exhausted)

    assert v1_reason is not None
    assert v2_reason is not None
    assert v1_reason != v2_reason
    assert "critic" in v1_reason
    assert "work node" in v2_reason


# --- the graphs stay apart ---------------------------------------------------


def test_the_two_graphs_share_no_node_beyond_the_three_they_mean_to() -> None:
    """Named rather than counted, so a fourth shared id has to be a decision.

    Sharing a node id is sharing an operator's reading of a timeline and a
    recovery's reading of a checkpoint. Three of them are deliberate: framing
    the objective, the human gate and the one approved write mean the same
    thing in either graph.
    """

    shared = research_graph.declared_nodes() & general_graph.declared_nodes()

    assert shared == frozenset({"understand", "approval", "export"})


@pytest.mark.parametrize("version", VERSIONS)
def test_no_graph_routes_into_a_node_outside_the_shared_union(version: str) -> None:
    """``TaskNodeId`` is one union across both graphs, so this is checkable."""

    module, _, _ = GRAPHS[version]
    known = set(TaskNodeId.__args__)  # type: ignore[attr-defined]

    assert module.declared_nodes() <= known


# --- the fault injector reaches both graphs' nodes ---------------------------


#: How many node invocations one clean demo run of each graph performs. v1's
#: ten are its eight handler-backed nodes plus the two routing nodes LangGraph
#: still runs as nodes; v2's five are its whole tuple.
EXECUTED_NODE_COUNT = {"v1": 10, "v2_general": 5}


@pytest.mark.parametrize("version", VERSIONS)
def test_the_fault_injector_wraps_every_node_of_whichever_graph_runs(
    version: str,
) -> None:
    """A reliability window that only exists on one graph is worse than none.

    Counted, not sampled. This wrapping *replaces* the supplied handler
    mapping, so a node missing from the wrapper's list is not merely
    un-instrumented -- its handler is dropped and becomes a pass-through. The
    first version of the wrapper iterated v1's tuple alone, and a sampled
    assertion (one armed fault fired) would still pass under that: the fault
    fires at ``understand``, which both graphs share, while v2's own nodes
    run as nothing. An unarmed controller records a hit per wrapped node and
    stops nothing, so the count is exact and the run is the clean one.
    """

    controller = FailpointController(frozenset({"after_node_before_checkpoint"}))
    workflow = LangGraphTaskWorkflow(
        handlers=DEMO_HANDLERS[version](),
        fault_injector=controller,
    )

    async def scenario() -> Any:
        return await workflow.run(
            _state(), thread_id=f"thread_{version}", graph_version=version
        )

    result = asyncio.run(scenario())

    assert result.disposition == "interrupted" or result.state.draft_ref is not None
    assert len(controller.hits) == EXECUTED_NODE_COUNT[version]


@pytest.mark.parametrize("version", VERSIONS)
def test_an_armed_fault_stops_either_graph_before_its_first_checkpoint(
    version: str,
) -> None:
    """The armed half: the recorded window is one a test can actually use."""

    controller = FailpointController(frozenset({"after_node_before_checkpoint"}))
    controller.arm("after_node_before_checkpoint", mode="raise")
    workflow = LangGraphTaskWorkflow(
        handlers=DEMO_HANDLERS[version](),
        fault_injector=controller,
    )

    async def scenario() -> None:
        await workflow.run(
            _state(), thread_id=f"thread_armed_{version}", graph_version=version
        )

    with pytest.raises(InjectedFaultError):
        asyncio.run(scenario())


@pytest.mark.parametrize("version", VERSIONS)
def test_an_injector_free_worker_still_runs_every_node_of_every_graph(
    version: str,
) -> None:
    """The control: the same handlers, unarmed, reach the terminal node.

    Without this the test above would pass on a graph whose handlers had all
    been replaced by pass-throughs -- the injected fault fires either way.
    """

    async def scenario() -> Any:
        return await LangGraphTaskWorkflow(handlers=DEMO_HANDLERS[version]()).run(
            _state(wants_report=True),
            thread_id=f"thread_clean_{version}",
            graph_version=version,
        )

    result = asyncio.run(scenario())

    assert result.disposition == "completed"
    # Every graph's working node produced something an approval could gate and
    # an export could render.
    assert result.state.draft_ref is not None
    assert result.state.approval_decision == "approved"


# --- the export gate answers the same question on either graph ---------------


def _recording_handlers(version: str, visited: list[str]) -> dict[str, Any]:
    """The graph's own demo handlers, each noting that it ran.

    Which nodes ran is the whole question here, and the demo handlers record
    only an opaque outcome ref -- so the node identity has to come from the
    wrapper rather than from the resulting state.
    """

    def record(node: str, handler: Any) -> Any:
        async def run(state: TaskState) -> Any:
            visited.append(node)
            return await handler(state)

        return run

    return {
        node: record(node, handler)
        for node, handler in DEMO_HANDLERS[version]().items()
    }


@pytest.mark.parametrize("version", VERSIONS)
def test_every_graph_skips_the_approval_when_the_deployment_ungates_export(
    version: str,
) -> None:
    """`workflow.export_requires_approval` is one setting, so it must mean one thing.

    This is the invariant that was missing, and its absence is what let the two
    graphs disagree for as long as they did: ADR-038 taught v2's ``route_review``
    to read the field, ADR-048 flipped the shipped default to ``false``, and
    v1's ``route_quality_gate`` went on routing to ``approval`` unconditionally
    the whole time. Every v1 test passed, because none of them named the field.

    Asserted against the compiled graph rather than the routers, so a graph that
    can answer "export" without declaring the edge fails here too.
    """

    visited: list[str] = []

    async def scenario() -> Any:
        return await LangGraphTaskWorkflow(
            handlers=_recording_handlers(version, visited)
        ).run(
            _state(wants_report=True, export_requires_approval=False),
            thread_id=f"thread_ungated_{version}",
            graph_version=version,
        )

    result = asyncio.run(scenario())

    assert result.disposition == "completed"
    assert "export" in visited
    assert "approval" not in visited
    # Skipped, not auto-approved. The distinction is the point: an approval row
    # nobody answered would leave a decision downstream code could read as
    # consent, and the audit trail would record one that never happened.
    assert result.state.approval_id is None
    assert result.state.approval_decision is None


@pytest.mark.parametrize("version", VERSIONS)
def test_every_graph_still_stops_at_the_approval_when_the_gate_is_on(
    version: str,
) -> None:
    """The control for the test above, on the same compiled graphs.

    Without it, "both graphs export without an approval" could be satisfied by
    two graphs that had stopped honouring the gate at all -- which is the
    opposite failure and just as silent.
    """

    visited: list[str] = []

    async def scenario() -> Any:
        return await LangGraphTaskWorkflow(
            handlers=_recording_handlers(version, visited)
        ).run(
            _state(wants_report=True, export_requires_approval=True),
            thread_id=f"thread_gated_{version}",
            graph_version=version,
        )

    result = asyncio.run(scenario())

    assert result.disposition == "completed"
    assert visited.index("approval") < visited.index("export")
    assert result.state.approval_decision == "approved"


# --- recovery decides identically over either graph's checkpoints ------------


@pytest.mark.parametrize("version", VERSIONS)
def test_reconciliation_reaches_every_decision_for_either_graph(
    version: str,
) -> None:
    """`reconcile` over a v2 Task, branch for branch with v1.

    The function is deliberately graph-blind -- it reads a position, never a
    shape -- and this is the test that keeps it so: every action a Worker can
    take on a v1 Task is reachable for a v2 Task through the same two facts.
    A branch that inspected the version for anything but buildability would
    break here first.
    """

    from agent_workbench.application.task_recovery import reconcile
    from agent_workbench.ports.task_workflow import CheckpointPosition

    both = frozenset(GRAPHS)

    no_checkpoint = reconcile(
        status="running",
        graph_version=version,
        position=None,
        buildable_versions=both,
    )
    assert no_checkpoint.action == "start"

    unfinished = reconcile(
        status="running",
        graph_version=version,
        position=CheckpointPosition(
            graph_version=version,
            pending_nodes=("work",) if version == "v2_general" else ("synthesize",),
        ),
        buildable_versions=both,
    )
    assert unfinished.action == "resume"

    waiting = reconcile(
        status="running",
        graph_version=version,
        position=CheckpointPosition(
            graph_version=version,
            pending_nodes=("approval",),
            awaiting_approval_id="apr_1",
        ),
        buildable_versions=both,
    )
    assert waiting.action == "wait_for_approval"
    assert waiting.approval_id == "apr_1"

    decided = reconcile(
        status="running",
        graph_version=version,
        position=CheckpointPosition(
            graph_version=version,
            pending_nodes=("approval",),
            awaiting_approval_id="apr_1",
        ),
        buildable_versions=both,
        approval_decision="approved",
    )
    assert decided.action == "resume_with_approval"

    finished = reconcile(
        status="running",
        graph_version=version,
        position=CheckpointPosition(graph_version=version),
        buildable_versions=both,
    )
    assert finished.action == "settle_succeeded"

    failed = reconcile(
        status="running",
        graph_version=version,
        position=CheckpointPosition(
            graph_version=version,
            failure_reason="the reviewer ran out of patience",
        ),
        buildable_versions=both,
    )
    assert failed.action == "settle_failed"
    assert failed.detail == "the reviewer ran out of patience"


@pytest.mark.parametrize("version", VERSIONS)
def test_a_worker_deployed_without_one_graph_parks_that_graphs_tasks(
    version: str,
) -> None:
    """The migration branch, from both directions.

    Deploying a Worker that builds only one graph is a legitimate state; that
    Worker substituting the graph it does have is not. Each version's Task
    must park on a Worker built without it -- including v1's on a
    v2-only Worker, which is the direction nobody would think to try.
    """

    from agent_workbench.application.task_recovery import reconcile

    other_only = frozenset(GRAPHS) - {version}

    parked = reconcile(
        status="running",
        graph_version=version,
        position=None,
        buildable_versions=other_only,
    )

    assert parked.action == "wait_for_migration"
    assert version in parked.detail
