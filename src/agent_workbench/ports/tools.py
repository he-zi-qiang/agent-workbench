"""Tool execution boundary.

A handler is reached only through the tool gateway, which owns schema
validation, policy evaluation, the timeout and result normalization. Native
handlers, MCP tools and LangChain tools all arrive as the same binding, so
there is exactly one place where a tool can be stopped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.cancellation import CancellationToken

#: How a tool derives the stable business key one of its operations is known by.
#:
#: It takes the *final* call -- after hooks and after any policy rewrite -- and
#: the context, and must return the same key for two attempts at the same
#: intent. Deriving it from ``tool_call_id`` would defeat the whole point: a
#: retried model turn mints a new one, so every retry would look like new work.
OperationKeyFor = Callable[[ToolCall, ExecutionContext], str]


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
    # Present exactly when the tool's effects are recorded in the side-effect
    # ledger. The gateway takes a tool through intent/dispatch/result if and
    # only if this is here, so "is this operation ledgered" has one answer that
    # lives beside the handler rather than in the gateway's opinion of it.
    operation_key: OperationKeyFor | None = None

    def __post_init__(self) -> None:
        # A ledgered read is a contradiction rather than a redundancy: `safe`
        # says the call may be repeated freely, and the ledger exists precisely
        # to stop something being repeated. One of the two is wrong, and this
        # refuses to guess which.
        #
        # The reverse is deliberately *not* checked. `idempotency` describes
        # what a tool is -- which the scheduler and retry reasoning read --
        # while an operation key describes whether this deployment records its
        # effects. A `keyed` tool with no key is simply one nothing is
        # recording yet, and that is a real and temporary state.
        if self.operation_key is not None and self.spec.idempotency == "safe":
            raise ValueError(
                f"{self.spec.name} declares safe idempotency, so it cannot also "
                "record side effects in the execution ledger"
            )


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
    "OperationKeyFor",
    "ToolBinding",
    "ToolHandler",
    "ToolInvocation",
    "ToolRegistry",
]
