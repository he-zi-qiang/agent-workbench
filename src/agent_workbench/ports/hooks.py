"""The hook boundary: inspecting and shaping a call before it is judged.

A hook is deployment-supplied code that sees every proposed tool call and may
leave it alone, rewrite its arguments, or block it. That makes hooks a way to
add local rules -- redact an argument, pin a tenant, refuse a path -- without
changing the runtime.

It also makes them a way to smuggle input past validation, which is why the
contract is narrow. A hook returns a decision; it does not mutate the call it
was given. Anything it rewrites goes back through schema validation and
authorization before it can run, and the only thing it may rewrite is the
arguments: the tool name and the call id belong to the model's request and to
the result that has to answer it.

A hook outcome is process-local, like a tool invocation. It is never persisted
or sent to a model; what reaches the event log is the effect -- a refused call
records why, and a rewritten one is decided again on its new arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """What one hook decided about one call."""

    arguments: JsonObject | None = None
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if self.arguments is not None and self.blocked_reason is not None:
            raise ValueError("a hook either rewrites a call or blocks it, not both")

    @property
    def blocks(self) -> bool:
        return self.blocked_reason is not None

    @property
    def rewrites(self) -> bool:
        return self.arguments is not None

    @classmethod
    def unchanged(cls) -> HookOutcome:
        return cls()

    @classmethod
    def rewrite(cls, arguments: JsonObject) -> HookOutcome:
        return cls(arguments=arguments)

    @classmethod
    def block(cls, reason: str) -> HookOutcome:
        return cls(blocked_reason=reason)


@runtime_checkable
class ToolCallHook(Protocol):
    """Deployment-supplied inspection of one proposed tool call."""

    name: str

    async def before_tool(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> HookOutcome:
        """Decide what should happen to ``call``.

        Implementations must not perform side effects that the run cannot undo:
        a hook runs before authorization, so the call it inspects may still be
        refused.
        """
        ...


__all__ = ["HookOutcome", "ToolCallHook"]
