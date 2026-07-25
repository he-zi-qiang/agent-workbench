"""The state machine table, checked against the baseline diagram."""

from __future__ import annotations

from typing import get_args

import pytest

from agent_workbench.domain.runs import RunState
from agent_workbench.runtime.state import (
    ALLOWED_TRANSITIONS,
    INITIAL_STATE,
    TERMINAL_STATES,
    InvalidStateTransition,
    RunStateMachine,
)

HAPPY_PATH: tuple[RunState, ...] = (
    "model_streaming",
    "validating_tools",
    "authorizing",
    "executing_tools",
    "recording_results",
    "model_streaming",
    "completed",
)


def test_every_declared_run_state_appears_in_the_table() -> None:
    assert set(get_args(RunState)) == set(ALLOWED_TRANSITIONS)


def test_every_target_state_is_itself_a_known_state() -> None:
    targets = {state for targets in ALLOWED_TRANSITIONS.values() for state in targets}

    assert targets <= set(ALLOWED_TRANSITIONS)


def test_a_run_starts_by_building_context() -> None:
    machine = RunStateMachine()

    assert machine.state == INITIAL_STATE
    assert machine.is_terminal is False


def test_the_tool_round_path_is_legal() -> None:
    machine = RunStateMachine()

    for state in HAPPY_PATH:
        machine.to(state)

    assert machine.state == "completed"
    assert machine.is_terminal is True
    assert machine.history == (INITIAL_STATE, *HAPPY_PATH)


def test_a_run_may_skip_authorization_when_no_tool_is_known() -> None:
    """Unknown tools are answered without ever reaching the policy engine."""

    machine = RunStateMachine()
    machine.to("model_streaming")
    machine.to("validating_tools")
    machine.to("recording_results")

    assert machine.state == "recording_results"


def test_a_denied_batch_never_reaches_execution() -> None:
    machine = RunStateMachine()
    machine.to("model_streaming")
    machine.to("validating_tools")
    machine.to("authorizing")
    machine.to("recording_results")

    assert machine.state == "recording_results"


def test_execution_cannot_be_entered_without_authorization() -> None:
    machine = RunStateMachine()
    machine.to("model_streaming")
    machine.to("validating_tools")

    with pytest.raises(InvalidStateTransition, match="validating_tools -> "):
        machine.to("executing_tools")


def test_results_cannot_be_recorded_before_a_model_turn() -> None:
    machine = RunStateMachine()

    with pytest.raises(InvalidStateTransition):
        machine.to("recording_results")


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_a_terminal_state_accepts_nothing_further(terminal: RunState) -> None:
    machine = RunStateMachine()
    machine.to("model_streaming")
    machine.to(terminal)

    assert machine.is_terminal is True
    with pytest.raises(InvalidStateTransition):
        machine.to("model_streaming")


@pytest.mark.parametrize(
    "state",
    sorted(set(ALLOWED_TRANSITIONS) - TERMINAL_STATES),
)
def test_every_non_terminal_state_can_fail_or_be_cancelled(state: RunState) -> None:
    """A budget or a cancellation can arrive in any phase, not only streaming."""

    assert {"failed", "cancelled"} <= ALLOWED_TRANSITIONS[state]


def test_compaction_is_reachable_but_leads_back_into_the_loop() -> None:
    machine = RunStateMachine()
    machine.to("model_streaming")
    machine.to("validating_tools")
    machine.to("recording_results")
    machine.to("compacting")
    machine.to("model_streaming")

    assert machine.state == "model_streaming"
