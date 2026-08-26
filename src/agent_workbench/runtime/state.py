"""The runtime's state machine, as a table rather than a diagram.

The architecture baseline draws this machine; encoding it here makes it
executable. Every phase change goes through :meth:`RunStateMachine.to`, so a
loop that skipped authorization or recorded results it never executed would
fail immediately and loudly instead of quietly producing a plausible run.

The table is a superset of the drawn diagram in one respect: the diagram shows
termination from model streaming, while in practice a run can fail or be
cancelled from any non-terminal phase -- a budget is checked before a turn
starts, and cancellation is observed between tool calls. Those edges are listed
explicitly rather than left implicit.

``compacting`` is entered by ``ClaudeLikeAgentRuntime`` when a run is over its
context soft limit and ``runtime.context_compaction_enabled`` is on (ADR-0081).
It was reachable and unused from the baseline until 2026-08-25; the sentence
here used to add that "a test asserts the current runtime never enters it",
which was not true of any test in the repository -- the claim outlived whatever
had once checked it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final

from agent_workbench.domain.errors import AgentWorkbenchError, ErrorCode
from agent_workbench.domain.runs import RunState

INITIAL_STATE: Final[RunState] = "building_context"


def _states(*states: RunState) -> frozenset[RunState]:
    return frozenset(states)


TERMINAL_STATES: Final[frozenset[RunState]] = _states(
    "completed",
    "failed",
    "cancelled",
)

_TERMINATION: Final[frozenset[RunState]] = _states("failed", "cancelled")

ALLOWED_TRANSITIONS: Final[Mapping[RunState, frozenset[RunState]]] = {
    "building_context": _states("model_streaming") | _TERMINATION,
    "model_streaming": _states("validating_tools", "completed") | _TERMINATION,
    "validating_tools": _states("authorizing", "recording_results") | _TERMINATION,
    "authorizing": _states("executing_tools", "recording_results") | _TERMINATION,
    "executing_tools": _states("recording_results") | _TERMINATION,
    "recording_results": _states("model_streaming", "compacting") | _TERMINATION,
    "compacting": _states("model_streaming") | _TERMINATION,
    "completed": _states(),
    "failed": _states(),
    "cancelled": _states(),
}


class InvalidStateTransition(AgentWorkbenchError):
    """The loop attempted a phase change the baseline does not allow."""

    code: ClassVar[ErrorCode] = "internal_error"


class RunStateMachine:
    """Tracks one run's phase and refuses illegal changes."""

    __slots__ = ("_history",)

    def __init__(self) -> None:
        self._history: list[RunState] = [INITIAL_STATE]

    @property
    def state(self) -> RunState:
        return self._history[-1]

    @property
    def history(self) -> tuple[RunState, ...]:
        """Every phase this run has entered, in order."""

        return tuple(self._history)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to(self, state: RunState) -> None:
        """Enter ``state``, or raise if the baseline forbids the edge."""

        if state not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransition(
                f"illegal run state transition: {self.state} -> {state}"
            )
        self._history.append(state)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "INITIAL_STATE",
    "TERMINAL_STATES",
    "InvalidStateTransition",
    "RunStateMachine",
]
