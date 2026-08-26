"""Rebuilding the run tree from events that were already written.

There is no stored tree, so every property here is a property of the *rebuild*.
The ones worth pinning are all about runs the function must not drop, because
dropping is the failure mode a read model has: a tree that omits a node reads as
work that never happened rather than as work whose record is incomplete.

The input is always a **page**, never "the stream". A caller can hold the middle
of one, a child whose parent scrolled off the top, or a parent whose children
have not written yet. All three have to produce a tree.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent_workbench.application.run_tree import build_run_tree
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.events import (
    AgentCompleted,
    AgentDelegated,
    EventEnvelope,
    EventPayload,
    RunBudget,
    RunCompleted,
    RunFailed,
    RunStarted,
    TaskSubmitted,
    TaskSucceeded,
)
from agent_workbench.domain.runs import BudgetUsage, TokenUsage

STREAM = "stream_1"
PARENT = "run_parent"
CHILD = "run_child"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

_BUDGET = RunBudget(max_steps=12, max_tool_calls=8)


def _envelope(run_id: str, payload: EventPayload, sequence: int) -> EventEnvelope:
    return EventEnvelope.for_payload(
        payload,
        stream_id=STREAM,
        run_id=run_id,
        timestamp=NOW,
        sequence=sequence,
    )


def _started(run_id: str, sequence: int) -> EventEnvelope:
    return _envelope(
        run_id,
        RunStarted(run_kind="task", model_profile="main", budget=_BUDGET),
        sequence,
    )


def _usage(tokens: int) -> BudgetUsage:
    return BudgetUsage(steps=2, tokens=TokenUsage(input_tokens=tokens))


class TestOneDelegationIsATreeOfTwo:
    def test_the_child_is_a_child_and_the_parent_is_a_root(self) -> None:
        tree = build_run_tree(
            STREAM,
            [
                _started(PARENT, 1),
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    2,
                ),
                _started(CHILD, 3),
                _envelope(
                    CHILD, RunCompleted(stop_reason="completed", usage=_usage(100)), 4
                ),
                _envelope(
                    PARENT,
                    AgentCompleted(
                        child_agent_run_id=CHILD,
                        status="completed",
                        stop_reason="completed",
                        usage=_usage(100),
                    ),
                    5,
                ),
                _envelope(
                    PARENT, RunCompleted(stop_reason="completed", usage=_usage(300)), 6
                ),
            ],
        )

        assert len(tree.roots) == 1
        root = tree.roots[0]
        assert root.run_id == PARENT
        assert root.parent_run_id is None
        assert root.definition_name is None
        assert [child.run_id for child in root.children] == [CHILD]

        child = root.children[0]
        assert child.parent_run_id == PARENT
        assert child.definition_name == "analyst"
        assert child.status == "completed"
        assert child.usage.tokens.input_tokens == 100

    def test_the_parents_own_spend_is_its_own(self) -> None:
        """The tree does not sum children into their parent.

        The parent's `RunCompleted.usage` is what *it* spent; a total is a thing
        a caller can compute and a thing this must not silently pre-compute,
        because the parent's own budget never saw the child's tokens (ADR-082
        §5) and a summed number here would suggest otherwise.
        """

        tree = build_run_tree(
            STREAM,
            [
                _started(PARENT, 1),
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    2,
                ),
                _envelope(
                    PARENT,
                    AgentCompleted(
                        child_agent_run_id=CHILD,
                        status="completed",
                        stop_reason="completed",
                        usage=_usage(100),
                    ),
                    3,
                ),
                _envelope(
                    PARENT, RunCompleted(stop_reason="completed", usage=_usage(300)), 4
                ),
            ],
        )

        assert tree.roots[0].usage.tokens.input_tokens == 300


class TestNothingIsDroppedWhenTheRecordIsIncomplete:
    def test_a_child_that_never_completed_is_shown_as_running(self) -> None:
        """A crashed Worker leaves exactly this. Omitting it would make a crash
        look like work that was never attempted."""

        tree = build_run_tree(
            STREAM,
            [
                _started(PARENT, 1),
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    2,
                ),
                _started(CHILD, 3),
            ],
        )

        child = tree.roots[0].children[0]
        assert child.status == "running"

    def test_a_child_announced_and_silent_is_a_node_not_a_gap(self) -> None:
        """``AgentDelegated`` is written before the child's first event, so
        between those two writes the child exists and has said nothing."""

        tree = build_run_tree(
            STREAM,
            [
                _started(PARENT, 1),
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    2,
                ),
            ],
        )

        child = tree.roots[0].children[0]
        assert child.status == "unknown"
        assert child.definition_name == "analyst"

    def test_a_child_whose_parent_scrolled_off_the_top_is_a_root_of_this_page(
        self,
    ) -> None:
        """The honest answer for a caller holding the middle of a stream. The
        alternative is dropping the run entirely, which is worse."""

        tree = build_run_tree(
            STREAM,
            [
                _started(CHILD, 30),
                _envelope(
                    CHILD, RunCompleted(stop_reason="completed", usage=_usage(50)), 31
                ),
            ],
        )

        assert [root.run_id for root in tree.roots] == [CHILD]

    def test_a_failed_child_keeps_its_status_from_its_own_event(self) -> None:
        tree = build_run_tree(
            STREAM,
            [
                _started(PARENT, 1),
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    2,
                ),
                _started(CHILD, 3),
                _envelope(
                    CHILD,
                    RunFailed(
                        error=ErrorInfo(code="budget_exceeded", message="ceiling"),
                        stop_reason="max_steps",
                        usage=_usage(80),
                    ),
                    4,
                ),
            ],
        )

        assert tree.roots[0].children[0].status == "failed"

    def test_the_parents_account_fills_in_for_a_child_not_on_this_page(self) -> None:
        """``AgentCompleted`` carries the same numbers the child reported. It is
        second-hand and used only where the first-hand record is absent."""

        tree = build_run_tree(
            STREAM,
            [
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    2,
                ),
                _envelope(
                    PARENT,
                    AgentCompleted(
                        child_agent_run_id=CHILD,
                        status="cancelled",
                        stop_reason="cancelled",
                        usage=_usage(70),
                    ),
                    3,
                ),
            ],
        )

        child = tree.roots[0].children[0]
        assert child.status == "cancelled"
        assert child.usage.tokens.input_tokens == 70


class TestNotEveryRunIdInAStreamIsARun:
    def test_a_tasks_own_lifecycle_events_do_not_become_a_run(self) -> None:
        """Found by running it against a real Task.

        ``TaskSubmitted`` / ``TaskClaimed`` / ``TaskSucceeded`` are written
        under the *task* id, so before this rule every Task grew a phantom root
        that was `unknown` forever and had spent nothing. The tree claims to
        show runs, and those are not one.
        """

        tree = build_run_tree(
            STREAM,
            [
                _envelope(
                    "task_1",
                    TaskSubmitted(graph_version="v2_general", input_ref="input_1"),
                    1,
                ),
                _started(PARENT, 2),
                _envelope(
                    PARENT, RunCompleted(stop_reason="completed", usage=_usage(10)), 3
                ),
                _envelope(
                    "task_1",
                    TaskSucceeded(task_id="task_1", epoch=1, attempt=1),
                    4,
                ),
            ],
        )

        assert [root.run_id for root in tree.roots] == [PARENT]

    def test_a_run_that_only_delegated_is_still_a_run(self) -> None:
        """The other direction, and the reason attestation is not simply
        "saw a RunStarted": a page that begins after a run started still holds
        that run, and the delegation it wrote is the proof.
        """

        tree = build_run_tree(
            STREAM,
            [
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    9,
                ),
            ],
        )

        assert [root.run_id for root in tree.roots] == [PARENT]
        assert [child.run_id for child in tree.roots[0].children] == [CHILD]


class TestAFanOutKeepsTheOrderItHappenedIn:
    def test_children_are_listed_in_the_order_they_were_announced(self) -> None:
        """Not sorted by id and not sorted by finish time: either would reorder
        a fan-out relative to the transcript it is read beside."""

        events = [_started(PARENT, 1)]
        for index, name in enumerate(("gamma", "alpha", "beta")):
            events.append(
                _envelope(
                    PARENT,
                    AgentDelegated(
                        child_agent_run_id=f"run_{name}", profile_name="analyst"
                    ),
                    2 + index,
                )
            )

        tree = build_run_tree(STREAM, events)

        assert [child.run_id for child in tree.roots[0].children] == [
            "run_gamma",
            "run_alpha",
            "run_beta",
        ]

    def test_every_node_is_reachable_by_flattening(self) -> None:
        events = [_started(PARENT, 1)]
        for index in range(3):
            events.append(
                _envelope(
                    PARENT,
                    AgentDelegated(
                        child_agent_run_id=f"run_child_{index}",
                        profile_name="analyst",
                    ),
                    2 + index,
                )
            )

        tree = build_run_tree(STREAM, events)

        assert len(tree.nodes()) == 4
        assert tree.get("run_child_1") is not None
        assert tree.get("nobody") is None


class TestASecondGenerationIsNested:
    def test_a_grandchild_hangs_off_its_own_parent(self) -> None:
        """The tree has to be a tree, not two levels flattened into one."""

        grandchild = "run_grandchild"
        tree = build_run_tree(
            STREAM,
            [
                _started(PARENT, 1),
                _envelope(
                    PARENT,
                    AgentDelegated(child_agent_run_id=CHILD, profile_name="analyst"),
                    2,
                ),
                _started(CHILD, 3),
                _envelope(
                    CHILD,
                    AgentDelegated(
                        child_agent_run_id=grandchild, profile_name="researcher"
                    ),
                    4,
                ),
                _started(grandchild, 5),
            ],
        )

        assert len(tree.roots) == 1
        child = tree.roots[0].children[0]
        assert [node.run_id for node in child.children] == [grandchild]
        assert len(tree.nodes()) == 3
