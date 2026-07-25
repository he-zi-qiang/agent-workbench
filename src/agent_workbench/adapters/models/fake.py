"""A scripted model.

Agent correctness comes from the protocol and the state machine, not from a
model's occasional good behaviour, so the deterministic model is a first-class
implementation rather than a test afterthought. It is what lets continuous
integration assert "two read tools ran in parallel and their results were
submitted in call order" without paying a provider or tolerating flakiness.

Settings admit ``provider = "fake"`` only when the environment is ``test``, so
this adapter cannot become a production model by configuration accident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.runs import TokenUsage
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.model import (
    ModelEvent,
    ModelFinishReason,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)

DEFAULT_DELTA_SIZE = 16


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One model reply, spelled out in advance."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: ModelFinishReason | None = None
    error: ErrorInfo | None = None

    def resolved_finish_reason(self) -> ModelFinishReason:
        if self.finish_reason is not None:
            return self.finish_reason
        if self.error is not None:
            return "error"
        return "tool_use" if self.tool_calls else "stop"


class FakeModel:
    """Replays scripted turns as a provider-neutral event stream."""

    def __init__(
        self,
        turns: Sequence[ScriptedTurn],
        *,
        delta_size: int = DEFAULT_DELTA_SIZE,
        repeat_last: bool = False,
    ) -> None:
        if delta_size < 1:
            raise ValueError("delta_size must be positive")
        self._turns = tuple(turns)
        self._delta_size = delta_size
        # Runaway-loop tests need a model that keeps proposing tools forever;
        # every other test wants an exhausted script to be visible.
        self._repeat_last = repeat_last
        self._calls = 0
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """Every request received, for assertions about what was sent."""

        return tuple(self._requests)

    @property
    def call_count(self) -> int:
        return self._calls

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self._requests.append(request)
        index = self._calls
        self._calls += 1

        turn = self._turn_at(index)
        if turn is None:
            yield ModelStreamCompleted(
                finish_reason="error",
                error=ErrorInfo(
                    code="provider_error",
                    message=f"scripted model exhausted after {index} calls",
                ),
            )
            return

        for offset in range(0, len(turn.text), self._delta_size):
            yield ModelTextDelta(text=turn.text[offset : offset + self._delta_size])

        # Tool calls are emitted whole. A partially parsed call must never
        # reach validation or policy.
        for call in turn.tool_calls:
            yield ModelToolCallProposed(call=call)

        yield ModelUsageReported(usage=turn.usage)
        yield ModelStreamCompleted(
            finish_reason=turn.resolved_finish_reason(),
            usage=turn.usage,
            error=turn.error,
        )

    def _turn_at(self, index: int) -> ScriptedTurn | None:
        if index < len(self._turns):
            return self._turns[index]
        if self._repeat_last and self._turns:
            return self._turns[-1]
        return None


__all__ = [
    "DEFAULT_DELTA_SIZE",
    "FakeModel",
    "ScriptedTurn",
]
