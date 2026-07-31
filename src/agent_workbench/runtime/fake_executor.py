"""A deterministic stand-in for the agent runtime.

Shipped rather than kept in the tests, because the thing it makes possible is
not a test: a Task can be driven end to end -- submitted, claimed, run through
the graph, checkpointed, resumed after a crash -- with no provider, no key and
no cost. That is how the recovery paths are exercised in CI and how the whole
pipeline can be demonstrated offline.

It is not a second tool loop, and it must never become one. The frozen
invariant is that exactly one component owns the model-tool loop, which is why
``runtime.executor`` is a single-valued ``Literal`` and stays that way: this is
selected by handing it to a node, not by naming it in configuration. A fake
that could be switched on in production would be an answer nobody asked a model
for, served as though somebody had.

So it does the two things the boundary actually promises and nothing else: it
returns a *terminal* outcome, and it observes cancellation. Everything a real
run varies -- text, artifact, usage -- is a function of the request, so the same
request produces the same outcome and a test can assert on it without
scripting one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.messages import Message, TextBlock
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    TokenUsage,
)
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink

#: What one fake step costs. Not zero: a run that charged nothing would let
#: every budget check pass, and the budget paths would then be untested exactly
#: where they are cheapest to test.
FAKE_STEPS = 1
FAKE_TOKENS = TokenUsage(input_tokens=100, output_tokens=50)
FAKE_COST_MICRO_USD = 500


@dataclass(slots=True)
class FakeAgentExecutor:
    """Answer without a model, deterministically, and record what was asked."""

    # Injectable so a test can make the fake's answer depend on the request
    # without subclassing it. The default is the useful one.
    respond: Callable[[AgentRunRequest], str] = field(
        default_factory=lambda: _echo_objective
    )
    requests: list[AgentRunRequest] = field(
        default_factory=lambda: list[AgentRunRequest]()
    )

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        self.requests.append(request)
        if cancellation.cancelled:
            # Terminal, not an exception. The caller is a graph node that has
            # to record and route on the result either way -- which is the
            # contract's own rule, and the one most easily forgotten by a
            # double that only ever succeeds.
            return AgentOutcome(
                agent_run_id=request.trace.agent_run_id,
                status="cancelled",
                stop_reason="cancelled",
                usage=_usage(),
            )

        text = self.respond(request)
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=text,
            output_ref=_artifact(request, text),
            usage=_usage(),
        )


def _echo_objective(request: AgentRunRequest) -> str:
    """The last thing the node asked, back again.

    Enough for a graph to make progress and for a reader to see which node
    produced which artifact; deliberately not enough to be mistaken for a
    model's answer.
    """

    latest = _text_of(request.messages[-1]) if request.messages else ""
    return f"[fake] {latest}".strip()


def _text_of(message: Message) -> str:
    # A message is a sequence of blocks, and only the text ones say anything a
    # fake could echo. Narrowed by type rather than by a "kind" string, so a
    # block that grows a `text` attribute meaning something else cannot start
    # being repeated back.
    return " ".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    ).strip()


def _usage() -> BudgetUsage:
    return BudgetUsage(
        steps=FAKE_STEPS,
        tool_calls=0,
        tokens=FAKE_TOKENS,
        cost_micro_usd=FAKE_COST_MICRO_USD,
    )


def _artifact(request: AgentRunRequest, text: str) -> ArtifactRef:
    body = text.encode("utf-8")
    return ArtifactRef(
        artifact_id=f"art_fake_{request.trace.agent_run_id}",
        tenant_id=request.principal.tenant_id,
        kind="agent_outcome",
        media_type="text/markdown",
        size_bytes=len(body),
        # A real digest of the real bytes. A constant here would make every
        # fake artifact look like every other one to anything that dedupes.
        sha256=hashlib.sha256(body).hexdigest(),
    )


__all__ = [
    "FAKE_COST_MICRO_USD",
    "FAKE_STEPS",
    "FAKE_TOKENS",
    "FakeAgentExecutor",
]
