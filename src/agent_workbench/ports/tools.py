"""Tool execution boundary.

A handler is reached only through the tool gateway, which owns schema
validation, policy evaluation, the timeout and result normalization. Native
handlers, MCP tools and LangChain tools all arrive as the same binding, so
there is exactly one place where a tool can be stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import CancellationToken


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Everything one handler call receives.

    This is a dataclass, not a domain model: it carries a live cancellation
    token and is never serialized, stored or sent to a model.
    """

    call: ToolCall
    context: ExecutionContext
    cancellation: CancellationToken
    timeout_seconds: int


@runtime_checkable
class ToolHandler(Protocol):
    """The executable half of a tool."""

    async def __call__(self, invocation: ToolInvocation) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """A serializable specification joined to its non-serializable handler.

    Only the specification may travel into a task snapshot, an event or a model
    request; the handler exists in the process registry alone.
    """

    spec: ToolSpec
    handler: ToolHandler


@runtime_checkable
class ToolRegistry(Protocol):
    """Lookup of the tools this process is willing to run."""

    def get(self, name: str) -> ToolBinding | None:
        """Return the binding, or ``None`` for an unknown tool.

        An unknown tool is not an exception: the model proposed it, so it must
        still receive exactly one ToolResult explaining the refusal.
        """
        ...

    def specs(self) -> tuple[ToolSpec, ...]:
        """Specifications of every registered tool, in a stable order."""
        ...


__all__ = [
    "ToolBinding",
    "ToolHandler",
    "ToolInvocation",
    "ToolRegistry",
]
