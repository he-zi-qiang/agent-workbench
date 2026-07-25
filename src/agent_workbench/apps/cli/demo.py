"""The deterministic demonstration run.

Every input is fixed or supplied on the command line, the clock is frozen and
identifiers are counters, so the same command produces byte-identical output
every time. That is what lets a golden file guard this vertical slice, instead
of a test that merely asserts something was printed.

The demo intentionally runs on the fake stack and does not load settings. It
touches no database, no vector store and no provider, so requiring a DSN to see
a scripted model answer would be ceremony rather than safety. Real dependency
injection from validated settings arrives with the first real adapter, in
``bootstrap/container.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count

from agent_workbench.adapters.agents import SingleTurnAgentExecutor
from agent_workbench.adapters.events import ObservingEventSink, ScopedEventSink
from agent_workbench.adapters.models.fake import ScriptedTurn
from agent_workbench.adapters.testing import fake_stack
from agent_workbench.apps.cli.rendering import Renderer
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TokenUsage,
    TraceContext,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventLogPort, EventScope

DEMO_CLOCK = datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)
DEMO_PROMPT = "Who owns hybrid fusion?"
DEMO_REPLY = (
    "Qdrant owns hybrid fusion: one Query API call fuses the dense and "
    "sparse hits with RRF."
)
DEMO_CORPUS: Mapping[str, str] = {
    "doc_1": "Qdrant performs one dense and sparse fusion per query.",
}
DEMO_TOOL_NAMES = ("read_document", "text_statistics")
DEMO_TOOL_ARGUMENTS: Mapping[str, JsonObject] = {
    "read_document": {"document_id": "doc_1"},
    "text_statistics": {"text": DEMO_REPLY},
}
DEMO_TOOL_CALL_ID = "toolu_demo_1"
# Scripted rather than measured: token accounting has to travel from the model
# adapter through the events into the outcome, and a run reporting zero tokens
# would let that path break unnoticed.
DEMO_USAGE = TokenUsage(input_tokens=118, output_tokens=24, cache_read_tokens=960)

DEMO_RUN_ID = "run_demo"
DEMO_STREAM_ID = "stream_demo"
DEMO_TENANT_ID = "tenant_demo"
DEMO_PRINCIPAL_ID = "user_demo"
DEMO_SYSTEM_PROMPT = "Answer from the local corpus only."


def _frozen_clock() -> datetime:
    return DEMO_CLOCK


def _sequential_ids(prefix: str) -> Callable[[], str]:
    counter = count(1)

    def next_id() -> str:
        return f"{prefix}_{next(counter):04d}"

    return next_id


@dataclass(frozen=True, slots=True)
class DemoRun:
    """An assembled run, ready to execute."""

    executor: AgentExecutor
    request: AgentRunRequest
    log: EventLogPort
    scope: EventScope
    cancellation: CancellationToken


def build_demo(
    *,
    prompt: str = DEMO_PROMPT,
    reply: str = DEMO_REPLY,
    propose_tool: str | None = None,
    max_steps: int = 4,
    max_tool_calls: int = 8,
) -> DemoRun:
    """Wire the fake stack into one scripted run.

    ``propose_tool`` scripts the model into proposing a tool call. The
    single-turn executor owns no tool loop, so the run must fail loudly rather
    than drop the call -- which is the seam the custom runtime fills next.
    """

    tool_calls: tuple[ToolCall, ...] = ()
    if propose_tool is not None:
        tool_calls = (
            ToolCall(
                tool_call_id=DEMO_TOOL_CALL_ID,
                tool_name=propose_tool,
                arguments=DEMO_TOOL_ARGUMENTS.get(propose_tool, {}),
            ),
        )

    stack = fake_stack(
        turns=[ScriptedTurn(text=reply, tool_calls=tool_calls, usage=DEMO_USAGE)],
        corpus=DEMO_CORPUS,
        clock=_frozen_clock,
        event_ids=_sequential_ids("evt_demo"),
    )
    executor = SingleTurnAgentExecutor(
        model=stack.model,
        registry=stack.registry,
        clock=_frozen_clock,
        model_call_ids=_sequential_ids("mc_demo"),
    )
    request = AgentRunRequest(
        trace=TraceContext(agent_run_id=DEMO_RUN_ID),
        run_kind="chat",
        stream_id=DEMO_STREAM_ID,
        principal=PrincipalContext(
            principal_id=DEMO_PRINCIPAL_ID,
            tenant_id=DEMO_TENANT_ID,
        ),
        envelope=AuthorizationEnvelope(
            allowed_tools=DEMO_TOOL_NAMES,
            max_tool_risk="read",
        ),
        budget=RunBudget(max_steps=max_steps, max_tool_calls=max_tool_calls),
        system_prompt=DEMO_SYSTEM_PROMPT,
        messages=(user_message(prompt),),
        tool_names=DEMO_TOOL_NAMES,
    )
    return DemoRun(
        executor=executor,
        request=request,
        log=stack.events,
        scope=EventScope(stream_id=DEMO_STREAM_ID, run_id=DEMO_RUN_ID),
        cancellation=stack.cancellation,
    )


async def execute(demo: DemoRun, renderer: Renderer, *, prompt: str) -> AgentOutcome:
    """Run the demo, streaming live events and then replaying the durable log."""

    renderer.start(prompt)
    sink = ObservingEventSink(
        inner=ScopedEventSink(log=demo.log, scope=demo.scope),
        observer=renderer.on_event,
    )
    outcome = await demo.executor.run(demo.request, sink, demo.cancellation)
    # The timeline comes from replay, not from what was just observed: if the
    # durable log cannot reconstruct the run, neither can a reconnecting client.
    durable = await demo.log.read(demo.scope.stream_id)
    renderer.finish(outcome, durable)
    return outcome


__all__ = [
    "DEMO_CLOCK",
    "DEMO_CORPUS",
    "DEMO_PROMPT",
    "DEMO_REPLY",
    "DEMO_RUN_ID",
    "DEMO_STREAM_ID",
    "DEMO_TOOL_NAMES",
    "DemoRun",
    "build_demo",
    "execute",
]
