"""The Worker's recovery decision, case by case.

Architecture baseline section 9.5 lists seven situations a claimed Task can be
in. Each has a test named after what it means rather than after the branch that
implements it, and two more tests check the properties the list as a whole has
to have: it is total over the whole input space, and no input reaches two
answers.

Nothing here touches a database or a graph. That is the reason the decision was
written as a function over values: reaching "the graph was interrupted at an
approval that has since been rejected" through a real Worker would need a lease,
an advisory lock and a graph that can interrupt, and would still only test one
of the seven.
"""

from __future__ import annotations

import itertools
from typing import Any, get_args

import pytest

from agent_workbench.application.task_recovery import (
    RESULTING_STATUS,
    CheckpointPosition,
    ReconciliationAction,
    reconcile,
)
from agent_workbench.domain.task_registry import (
    CANCELLABLE_STATUSES,
    TERMINAL_STATUSES,
    ApprovalDecision,
    TaskStatus,
)
from agent_workbench.domain.tasks import CANONICAL_V1_NODE_IDS

VERSIONS = ("v1", "v2")

ALL_STATUSES: tuple[TaskStatus, ...] = get_args(TaskStatus)
ALL_ACTIONS: tuple[ReconciliationAction, ...] = get_args(ReconciliationAction)
ALL_DECISIONS: tuple[ApprovalDecision | None, ...] = (None, *get_args(ApprovalDecision))


def _position(**overrides: Any) -> CheckpointPosition:
    base: dict[str, Any] = {"graph_version": "v1", "pending_nodes": ("critic",)}
    base.update(overrides)
    return CheckpointPosition(**base)


def _reconcile(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "status": "running",
        "graph_version": "v1",
        "position": _position(),
        "buildable_versions": VERSIONS,
    }
    base.update(overrides)
    return reconcile(**base)


# --------------------------------------------------------------------------
# The seven cases


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_a_task_that_already_reached_a_terminal_fact_is_not_resumed(
    status: TaskStatus,
) -> None:
    """Case 1. A cancelled Task with unfinished work is not work to finish.

    Checked before anything reads the checkpoint, so a late claim on a
    cancelled Task cannot be turned back into a running one by the fact that
    its graph still has nodes left.
    """

    decision = _reconcile(status=status, position=_position(pending_nodes=("critic",)))

    assert decision.action == "propagate_terminal"
    assert not decision.keeps_executing
    # Terminal is already recorded; the decision does not rewrite it.
    assert decision.resulting_status is None


def test_a_graph_version_this_process_cannot_build_waits_for_a_migration() -> None:
    """Case 2, first half: the Worker does not have the graph at all.

    Falling back to the newest registered graph is how a node whose meaning
    changed gets re-entered under its old name.
    """

    decision = _reconcile(graph_version="v9", position=None)

    assert decision.action == "wait_for_migration"
    assert decision.resulting_status == "waiting_migration"
    assert "v9" in decision.detail


def test_a_checkpoint_written_by_another_graph_waits_for_a_migration() -> None:
    """Case 2, second half: the two facts disagree about which graph this is."""

    decision = _reconcile(graph_version="v2", position=_position(graph_version="v1"))

    assert decision.action == "wait_for_migration"
    assert decision.resulting_status == "waiting_migration"
    assert "v1" in decision.detail and "v2" in decision.detail


def test_a_checkpoint_that_never_recorded_its_graph_waits_for_a_migration() -> None:
    """An unlabelled position is not more recoverable than a mismatched one.

    It is what a checkpoint written before the adapter recorded the version
    looks like. Guessing "it must be the version the Task is registered as"
    would be a guess made where a wrong answer costs the most.
    """

    decision = _reconcile(position=_position(graph_version=None))

    assert decision.action == "wait_for_migration"
    assert "does not record" in decision.detail


def test_a_task_already_waiting_for_a_migration_stays_there() -> None:
    """It is not terminal, so it must be refused explicitly rather than fall through.

    A Task parked for a migration decision has a perfectly resumable-looking
    checkpoint. Reading only the checkpoint would put it straight back to work
    and undo the decision somebody is still waiting to make.
    """

    decision = _reconcile(status="waiting_migration", position=_position())

    assert decision.action == "wait_for_migration"
    assert not decision.keeps_executing


def test_a_task_with_no_checkpoint_starts_from_its_own_input() -> None:
    """Case 3. Nothing has run, so there is no position to continue from."""

    decision = _reconcile(position=None)

    assert decision.action == "start"
    assert decision.keeps_executing
    assert decision.resulting_status is None


def test_a_graph_that_finished_settles_the_registry_the_crash_left_behind() -> None:
    """Case 4. The graph completed and the process died before recording it."""

    decision = _reconcile(position=_position(pending_nodes=()))

    assert decision.action == "settle_succeeded"
    assert decision.resulting_status == "succeeded"
    assert not decision.keeps_executing


def test_a_graph_that_reached_a_failed_terminal_state_settles_failed() -> None:
    decision = _reconcile(
        position=_position(
            pending_nodes=(),
            failure_reason="the revision budget was exhausted",
        )
    )

    assert decision.action == "settle_failed"
    assert decision.resulting_status == "failed"
    assert decision.detail == "the revision budget was exhausted"
    assert not decision.keeps_executing


def test_an_undecided_approval_releases_the_worker_instead_of_waiting() -> None:
    """Case 5. Nothing is executing, so nothing should be holding a lease.

    Blocking here would keep an advisory lock, a lease and a guard connection
    open for as long as a human takes to answer.
    """

    decision = _reconcile(
        position=_position(
            pending_nodes=("approval",), awaiting_approval_id="approval_1"
        ),
        approval_decision=None,
    )

    assert decision.action == "wait_for_approval"
    assert decision.resulting_status == "waiting_approval"
    assert not decision.keeps_executing
    assert decision.approval_id == "approval_1"


@pytest.mark.parametrize("outcome", ["approved", "rejected"])
def test_a_decided_approval_resumes_the_same_thread(outcome: ApprovalDecision) -> None:
    """Case 6. Both outcomes resume: a rejection is a path through the graph.

    The approval id travels with the decision because the node re-reads the
    authoritative decision itself rather than trusting what it was handed.
    """

    decision = _reconcile(
        position=_position(
            pending_nodes=("approval",), awaiting_approval_id="approval_1"
        ),
        approval_decision=outcome,
    )

    assert decision.action == "resume_with_approval"
    assert decision.keeps_executing
    assert decision.approval_id == "approval_1"
    assert outcome in decision.detail


def test_a_checkpoint_with_work_left_resumes_without_resubmitting_input() -> None:
    """Case 7, and the ordinary one."""

    decision = _reconcile(position=_position(pending_nodes=("critic", "synthesize")))

    assert decision.action == "resume"
    assert decision.keeps_executing
    assert decision.resulting_status is None


# --------------------------------------------------------------------------
# Properties of the list, rather than of one entry in it


def test_every_input_combination_reaches_exactly_one_action() -> None:
    """Totality. A situation with no answer is a Worker that hangs.

    The whole cross product is small enough to enumerate, so it is enumerated
    rather than sampled.
    """

    reached: set[ReconciliationAction] = set()
    combinations = itertools.product(
        ALL_STATUSES,
        VERSIONS,
        (*VERSIONS, None),
        ((), ("critic",), ("approval",)),
        (None, "approval_1"),
        (None, "the revision budget was exhausted"),
        ALL_DECISIONS,
    )
    for (
        status,
        registered,
        written,
        pending,
        approval,
        failure,
        decision,
    ) in combinations:
        if approval is not None and not pending:
            continue  # refused by CheckpointPosition, and asserted separately
        if failure is not None and pending:
            continue  # refused by CheckpointPosition, and asserted separately
        if failure is not None and approval is not None:
            continue  # refused by CheckpointPosition, and asserted separately
        for position in (
            None,
            CheckpointPosition(
                graph_version=written,
                pending_nodes=pending,
                awaiting_approval_id=approval,
                failure_reason=failure,
            ),
        ):
            outcome = reconcile(
                status=status,
                graph_version=registered,
                position=position,
                buildable_versions=VERSIONS,
                approval_decision=decision,
            )
            assert outcome.action in ALL_ACTIONS
            reached.add(outcome.action)

    # And every action is reachable: one that is not would be a branch no
    # situation produces, which is a branch that is wrong or dead.
    assert reached == set(ALL_ACTIONS)


def test_a_position_cannot_be_finished_and_awaiting_an_approval_at_once() -> None:
    """The invariant that makes two of the seven cases mutually exclusive."""

    with pytest.raises(ValueError, match="pending nodes"):
        CheckpointPosition(
            graph_version="v1", pending_nodes=(), awaiting_approval_id="approval_1"
        )


def test_no_action_resumes_a_graph_the_registry_has_finished_with() -> None:
    """Whatever the checkpoint says, a terminal Registry ends the Task."""

    for status in TERMINAL_STATUSES:
        for pending in ((), ("critic",), ("approval",)):
            decision = _reconcile(
                status=status,
                position=_position(pending_nodes=pending),
                approval_decision="approved",
            )
            assert not decision.keeps_executing


def test_every_action_declares_what_it_does_to_the_registry() -> None:
    """A new action without an entry here would silently leave status alone."""

    assert set(RESULTING_STATUS) == set(ALL_ACTIONS)


def test_the_statuses_named_here_are_the_ones_the_lifecycle_defines() -> None:
    """Guards against a status appearing in one of the two sets and nowhere else."""

    assert set(ALL_STATUSES) >= TERMINAL_STATUSES
    assert set(ALL_STATUSES) >= CANCELLABLE_STATUSES
    # Cancellable and terminal do not overlap: cancelling a finished Task is
    # not a transition, it is a late request that must fail.
    assert not (TERMINAL_STATUSES & CANCELLABLE_STATUSES)
    # `waiting_migration` is in neither: it is not executing and not finished.
    assert set(ALL_STATUSES) - TERMINAL_STATUSES - CANCELLABLE_STATUSES == {
        "waiting_migration"
    }


def test_pending_nodes_are_the_canonical_v1_node_ids() -> None:
    """The decision reads node ids that the graph declaration owns."""

    decision = _reconcile(position=_position(pending_nodes=CANONICAL_V1_NODE_IDS))

    assert decision.action == "resume"
    assert str(len(CANONICAL_V1_NODE_IDS)) in decision.detail
