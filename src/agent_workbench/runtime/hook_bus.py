"""Running the configured hooks over one call, in order, and failing closed.

Hooks run once each, in registration order, and each one sees whatever the
previous one produced. A single pass is deliberate: re-running earlier hooks
after a later rewrite would need a convergence rule, and a rule nobody can
state is a rule nobody can review.

Failure is refusal, not omission. A hook that raises or hangs blocks the call
it was inspecting, because the alternative -- treating a broken hook as
permission -- turns every deployment-supplied safety rule into something that
disappears the moment it has a bug. A hook is also bounded in time for the same
reason the tool it guards is: an unbounded await is how one slow rule stops a
whole run.

The bus only decides what the final arguments are. Whether they may run is
still the gateway's question, asked after this pass, on whatever came out of
it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.hooks import ToolCallHook

DEFAULT_HOOK_TIMEOUT_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class HookBusOutcome:
    """The result of one pass over the hooks."""

    call: ToolCall
    rewritten: bool = False
    blocked_by: str | None = None
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_by is not None


class HookBus:
    """Runs every registered hook over one proposed call."""

    __slots__ = ("_hooks", "_timeout_seconds")

    def __init__(
        self,
        hooks: Sequence[ToolCallHook] = (),
        *,
        timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        names = [hook.name for hook in hooks]
        if len(set(names)) != len(names):
            # Two hooks under one name make an audit line ambiguous about which
            # of them refused a call.
            raise ValueError("hook names must be unique")
        self._hooks = tuple(hooks)
        self._timeout_seconds = timeout_seconds

    def _bound(self, remaining_run_seconds: float | None) -> tuple[float, str]:
        """The smaller of this bus's timeout and what the run has left.

        The source travels with the number. Both outcomes refuse the call, but
        "this hook is slow" and "this run is nearly over" send whoever reads
        the audit line to different places.
        """

        if remaining_run_seconds is None or self._timeout_seconds < (
            remaining_run_seconds
        ):
            return self._timeout_seconds, "its {:g}s timeout"
        return remaining_run_seconds, "the {:g}s the run had left"

    @property
    def is_empty(self) -> bool:
        return not self._hooks

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(hook.name for hook in self._hooks)

    async def before_tool(
        self,
        call: ToolCall,
        context: ExecutionContext,
        *,
        remaining_run_seconds: float | None = None,
    ) -> HookBusOutcome:
        """Run one pass, returning the final call or the hook that stopped it.

        ``remaining_run_seconds`` is what the run has left. Hooks are bounded
        by the smaller of it and their own timeout, and it is re-read for each
        hook: a slow first hook leaves less for the second.
        """

        current = call
        rewritten = False

        for hook in self._hooks:
            bound, described = self._bound(remaining_run_seconds)
            if bound <= 0:
                # No time to start. Refusing is the same answer a hook that
                # ran out of time would get, and for the same reason.
                return HookBusOutcome(
                    call=current,
                    rewritten=rewritten,
                    blocked_by=hook.name,
                    reason="the run had no time left to consult this hook",
                )
            try:
                async with asyncio.timeout(bound):
                    outcome = await hook.before_tool(current, context)
            except TimeoutError:
                return HookBusOutcome(
                    call=current,
                    rewritten=rewritten,
                    blocked_by=hook.name,
                    reason=f"the hook exceeded {described.format(bound)}",
                )
            except Exception as exc:
                # Only the exception type crosses the boundary: hook code is
                # deployment-supplied and its messages are not vetted.
                return HookBusOutcome(
                    call=current,
                    rewritten=rewritten,
                    blocked_by=hook.name,
                    reason=f"the hook raised {type(exc).__name__}",
                )

            if outcome.blocked_reason is not None:
                return HookBusOutcome(
                    call=current,
                    rewritten=rewritten,
                    blocked_by=hook.name,
                    reason=outcome.blocked_reason,
                )

            if outcome.arguments is not None:
                # Only the arguments may change. The tool name and the call id
                # belong to the model's request and to the result answering it.
                current = current.model_copy(update={"arguments": outcome.arguments})
                rewritten = True

        return HookBusOutcome(call=current, rewritten=rewritten)


__all__ = ["DEFAULT_HOOK_TIMEOUT_SECONDS", "HookBus", "HookBusOutcome"]
