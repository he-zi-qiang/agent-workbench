"""The authorization boundary.

One decision function guards every tool call, whoever proposed it. The engine
receives the call and the context that call runs in, and returns one of three
answers; there is no fourth answer and no way to skip the question.

Rewritten arguments are a decision, not a side effect: when an engine returns
``allow_with_modified_input`` the gateway must re-validate the new arguments
against the tool schema and ask again, otherwise a rewrite would be a way past
both checks.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.domain.policies import ExecutionContext, PolicyDecision
from agent_workbench.domain.tools import ToolCall


@runtime_checkable
class PolicyEngine(Protocol):
    """Decides whether one tool call may run."""

    async def decide(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> PolicyDecision: ...


__all__ = ["PolicyEngine"]
