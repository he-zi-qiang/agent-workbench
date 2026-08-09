"""The direct shape, and the web tool it may reach (ADR-023).

`AnswerModeSelector` sends a turn here whenever the asker named no knowledge
base -- which is the mode the console opens in. Before ADR-023 that made it the
only evidence-free turn in the system that could not reach the web: asking about
today's news required first attaching an unrelated knowledge base and waiting
for retrieval to miss, and the answer that came back was a model guessing from
memory about a date it does not have.

The tests below drive the same three things `test_routed_chat.py` does -- the
prompt the model was handed, the `grounded` flag, and the citations -- because
those are what must not move. Reaching the web buys this shape a better answer;
it does not buy it the right to claim the answer is verified.

The provider-less case is the control, and it is not a formality: ADR-021 §4
says an unconfigured deployment must not gain a tool, so "exactly what it was"
is a property with a test rather than an intention in a comment.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent_workbench.application.chat_execution import (
    UNGROUNDED_SYSTEM_PROMPT,
    WEB_DIRECT_SYSTEM_PROMPT,
    WEB_FALLBACK_SYSTEM_PROMPT,
    ChatRequest,
    UngroundedExecution,
    WebSearchJournal,
)
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    BudgetUsage,
    RunBudget,
    TokenUsage,
)

TENANT = "tenant_a"
PRINCIPAL = "user_reader"


class _Executor:
    """Records the request it was handed, and answers with fixed text."""

    def __init__(self, text: str = "an answer") -> None:
        self._text = text
        self.requests: list[Any] = []

    async def run(self, request: Any, sink: Any, cancellation: Any) -> AgentOutcome:
        self.requests.append(request)
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=self._text,
            usage=BudgetUsage(
                steps=1,
                tool_calls=0,
                tokens=TokenUsage(input_tokens=4, output_tokens=4),
            ),
        )


class _UnfinishedExecutor:
    """A run that ends without an answer, exactly as the real one ends.

    ``output_text`` is empty and that is the point: the runtime records prose
    only on a turn that finished *without* proposing a tool, so a loop that
    spent its whole ceiling searching has nothing partial to hand back. The only
    thing that can be delivered is a second, toolless model call.
    """

    def __init__(
        self,
        *,
        status: str = "failed",
        stop_reason: str = "max_steps",
        error: ErrorInfo | None = None,
    ) -> None:
        self._status = status
        self._stop_reason = stop_reason
        self._error = error
        self.requests: list[Any] = []

    async def run(self, request: Any, sink: Any, cancellation: Any) -> AgentOutcome:
        self.requests.append(request)
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status=self._status,  # pyright: ignore[reportArgumentType]
            stop_reason=self._stop_reason,  # pyright: ignore[reportArgumentType]
            output_text="",
            error=self._error,
            usage=BudgetUsage(
                steps=3,
                tool_calls=2,
                tokens=TokenUsage(input_tokens=4, output_tokens=4),
            ),
        )


def _exhausted() -> _UnfinishedExecutor:
    """The measured failure: every step spent searching, nothing written."""

    return _UnfinishedExecutor(
        status="failed",
        stop_reason="max_steps",
        error=ErrorInfo(
            code="budget_exceeded",
            message="the run stopped at its ceiling: max_steps",
        ),
    )


class _Sink:
    def __init__(self) -> None:
        self.emitted: list[Any] = []

    async def emit(self, payload: Any, **kwargs: Any) -> Any:
        self.emitted.append(payload)


def _request(*, run_id: str = "run_direct_1") -> ChatRequest:
    """A direct turn: no knowledge base, and the mode says so explicitly."""

    return ChatRequest(
        session_id="ses_1",
        question="今天丹东天气怎么样",
        principal=PrincipalContext(tenant_id=TENANT, principal_id=PRINCIPAL),
        knowledge_base_id=None,
        idempotency_key="key-1",
        answer_mode="direct",
        run_id=run_id,
    )


def _run(
    *,
    toolless: _Executor | None = None,
    web_executor: Any | None = None,
    journal: WebSearchJournal | None = None,
) -> tuple[Any, _Executor, _Sink]:
    executor = toolless if toolless is not None else _Executor("from memory")
    sink = _Sink()
    execution = UngroundedExecution(
        executor=executor,  # pyright: ignore[reportArgumentType]
        budget=RunBudget(max_steps=1, max_tool_calls=1),
        web_executor=web_executor,  # pyright: ignore[reportArgumentType]
        web_budget=None
        if web_executor is None
        else RunBudget(max_steps=3, max_tool_calls=2),
        web_tool_names=() if web_executor is None else ("web_search",),
        # A fresh journal per execution unless the test wants to inspect one --
        # the default factory would otherwise hand every case the same object.
        web_journal=journal if journal is not None else WebSearchJournal(),
    )
    produced = asyncio.run(
        execution.produce(
            _request(),
            history=(),
            sink=sink,  # pyright: ignore[reportArgumentType]
            cancellation=None,  # pyright: ignore[reportArgumentType]
        )
    )
    return produced, executor, sink


# --- the control: no provider, nothing changes ------------------------------


def test_without_a_provider_the_direct_shape_is_exactly_what_it_was() -> None:
    """ADR-021 §4: an unconfigured deployment does not gain a tool.

    This is the control for every test below it. If the merge had made the web
    run unconditional, this is what would say so -- and it would say so as a
    deployment that never configured a provider suddenly calling one.
    """

    produced, executor, _sink = _run()

    assert len(executor.requests) == 1
    request = executor.requests[0]
    assert request.system_prompt == UNGROUNDED_SYSTEM_PROMPT
    assert request.tool_names == ()
    assert request.envelope.allowed_tools == ()
    assert produced.outcome.output_text == "from memory"


def test_without_a_provider_the_direct_shape_still_claims_nothing() -> None:
    produced, _executor, _sink = _run()

    assert produced.grounded is False
    assert produced.citations == ()
    assert produced.authorized_revisions == ()


# --- with a provider: the tool is offered, and only that tool ---------------


def test_the_direct_shape_offers_the_web_tool_and_nothing_else() -> None:
    """The turn the console opens in can now reach the web.

    Both `tool_names` and the envelope, because they are different things: the
    envelope says what policy would permit, `tool_names` is what the model is
    actually shown. Setting only the envelope authorizes a tool the model never
    sees, which is the bug ADR-021 records in the routed branch.
    """

    web = _Executor("cloudy, 22C to 32C")
    produced, toolless, _sink = _run(web_executor=web)

    assert len(web.requests) == 1
    request = web.requests[0]
    assert request.tool_names == ("web_search",)
    assert request.envelope.allowed_tools == ("web_search",)
    assert request.envelope.max_tool_risk == "external"
    # A chat turn has no approval node to reach, so a gate here could only ever
    # say no -- measured, three proposals, three denials, then the run died.
    assert request.envelope.approval_required_risks == ()
    # The toolless executor was never asked: the web run answered.
    assert toolless.requests == []
    assert produced.outcome.output_text == "cloudy, 22C to 32C"


def test_the_direct_shape_is_not_told_a_knowledge_base_missed() -> None:
    """The one sentence that differs, and the reason ADR-023 kept two.

    A direct turn names no knowledge base. Handing it the routed fallback's
    prompt would tell the model that a corpus it was never given had nothing for
    it -- a claim about a retrieval that did not happen, in the one place the
    model actually reads.
    """

    web = _Executor()
    _run(web_executor=web)

    prompt = web.requests[0].system_prompt
    assert prompt == WEB_DIRECT_SYSTEM_PROMPT
    assert prompt != WEB_FALLBACK_SYSTEM_PROMPT
    assert "knowledge base did not cover" not in prompt
    # The rules themselves are shared verbatim, so the ceiling in assembly is
    # the ceiling for this sentence too.
    assert "twice at most" in prompt


def test_a_searched_direct_turn_is_still_not_grounded() -> None:
    """ADR-021 §3 survives the merge.

    `grounded` means "rests on authorized revisions the release fence
    re-checks". A fetched page has no revision, no ACL and nothing to re-check,
    so reading three of them does not upgrade the answer. What is withheld is
    the guarantee; the URLs stay on the event stream.
    """

    produced, _toolless, _sink = _run(web_executor=_Executor("read the web"))

    assert produced.grounded is False
    assert produced.citations == ()
    assert produced.authorized_revisions == ()


# --- and it still always delivers an answer ---------------------------------


def test_a_direct_turn_that_spent_its_ceiling_searching_still_answers() -> None:
    """ADR-021 §6, now load-bearing for the console's default mode.

    Before the fallback existed, a turn that searched returned *less* than one
    that never did: `budget_exceeded` reached the client as HTTP 502. Bringing
    the tool to the direct shape would have brought that failure with it.
    """

    web = _exhausted()
    produced, toolless, _sink = _run(web_executor=web)

    assert len(web.requests) == 1
    # The toolless answer was available before the tool was offered, and a dead
    # tool loop does not make it unavailable.
    assert len(toolless.requests) == 1
    assert toolless.requests[0].system_prompt == UNGROUNDED_SYSTEM_PROMPT
    assert produced.outcome.status == "completed"
    assert produced.outcome.output_text == "from memory"


def test_a_direct_turn_that_answered_is_not_asked_a_second_time() -> None:
    """The retry is for a run that produced nothing, not for every web run."""

    produced, toolless, _sink = _run(web_executor=_Executor("from the web"))

    assert toolless.requests == []
    assert produced.outcome.output_text == "from the web"


def test_a_cancelled_direct_turn_stays_cancelled() -> None:
    """Cancellation means the caller left.

    Spending another model call on an answer nobody is waiting for is the
    opposite of what it asked for, and it would report a turn the user cancelled
    as a completed one.
    """

    web = _UnfinishedExecutor(status="cancelled", stop_reason="cancelled")
    produced, toolless, _sink = _run(web_executor=web)

    assert toolless.requests == []
    assert produced.outcome.status == "cancelled"


def test_a_degraded_direct_turn_leaves_no_web_verdict_behind() -> None:
    """The journal is emptied by the run that wrote it, failure included.

    Otherwise a run that searched, died and then answered from memory leaves its
    `record` in place for whichever turn asks next -- reporting a memory answer
    as one that had read the web.
    """

    journal = WebSearchJournal()
    journal.record("run_direct_1")

    produced, _toolless, _sink = _run(web_executor=_exhausted(), journal=journal)

    assert produced.outcome.status == "completed"
    assert journal.take("run_direct_1") is False
