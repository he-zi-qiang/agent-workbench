"""The custom agent runtime: one model-tool loop, owned in one place.

This is the only component in the system that runs ``model -> tool -> result ->
model``. LangGraph advances a long task, LlamaIndex puts knowledge into an
index, and neither of them takes a turn of this loop; a second executor behind
the same protocol is the failure the architecture baseline exists to prevent.

Two properties hold on every path through it.

Every ``tool_call_id`` the model was shown ends with exactly one
``ToolResult``. Unknown tool, denied call, handler exception, timeout,
cancellation mid-batch: each of them produces a result rather than a gap,
because the model is waiting on the id either way and a missing answer is a
conversation that can never continue.

And results are submitted in the model's own call order. **Execution is not
serial**: one step's calls are grouped by ``plan_tool_batches`` and a group of
read-only tools is awaited with ``asyncio.gather``, so completion order and call
order genuinely differ. The alignment is what makes that invisible to the model
-- it sees its own order regardless of which tool finished first.

This paragraph read "Execution is serial here, so the two orders happen to
coincide; the alignment is applied anyway, because the parallel scheduler that
arrives later must not be able to change what the model sees" until 2026-08-31.
That scheduler arrived in PR-009 and the sentence did not follow it, which left
the first thing this file tells a reader describing a runtime that no longer
existed -- and by this repository's own convention a comment is part of the
specification, not a gloss on it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol, runtime_checkable

from agent_workbench.domain.errors import (
    AgentWorkbenchError,
    ErrorInfo,
    OperationCancelledError,
    ToolPairingError,
)
from agent_workbench.domain.events import (
    ContextBuilt,
    ContextCompacted,
    ModelCompleted,
    ModelDelta,
    ModelStarted,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
)
from agent_workbench.domain.events import (
    # Aliased because the domain event shares its name with the port event it
    # is translated from, and this module is the one place both cross: the
    # port kind is what the adapter streamed, the domain kind is what the
    # sink fans out.
    ModelThinkingDelta as DomainModelThinkingDelta,
)
from agent_workbench.domain.identifiers import new_model_call_id
from agent_workbench.domain.messages import (
    Message,
    assistant_message,
    tool_message,
    user_message,
)
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.pricing import ModelPrices
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    BudgetUsage,
    StopReason,
    TokenUsage,
    context_reason_for,
)
from agent_workbench.domain.schema import (
    ANSWER_TEXT_LIMIT,
    BoundedText,
    bounded,
    bounded_thinking,
)
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec, align_results
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.model import (
    ModelEvent,
    ModelFinishReason,
    ModelPort,
    ModelRequest,
    ModelTextDelta,
    ModelThinkingDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)
from agent_workbench.ports.telemetry import (
    MODEL_INPUT_TOKENS,
    MODEL_OUTPUT_TOKENS,
    RUN_COMPLETED,
    RUN_DURATION,
    RUN_FAILED,
    RUN_STARTED,
    RUN_STEPS,
    Attributes,
    NullTelemetry,
    Telemetry,
)
from agent_workbench.runtime.budgets import (
    effective_model_deadline,
    remaining_run_seconds,
)
from agent_workbench.runtime.compaction import (
    conversation_chars,
    plan_compaction,
    render_for_summary,
    scaled_tokens_after,
)
from agent_workbench.runtime.state import RunStateMachine
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway
from agent_workbench.runtime.tool_scheduler import (
    DEFAULT_MAX_PARALLEL_READS,
    plan_tool_batches,
)

DEFAULT_MODEL_LABEL = "scripted-fake"

# Mirrors runtime.model_timeout_seconds. It is the runtime's own envelope for
# any single model call; the adapter still applies the model profile's timeout
# inside it, so the shorter of the two fires first.
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0

# Matches the domain's BoundedText ceiling. Writing a longer answer to the
# artifact store instead of clipping it belongs with context management.
MAX_OUTPUT_TEXT = ANSWER_TEXT_LIMIT
TRUNCATION_MARKER = "… [truncated]"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _clip(text: str) -> str:
    """Bound the answer at the answer's ceiling, not at a preview's.

    Still clipped at its source rather than wherever it is stored, so every
    consumer sees the same answer: trimming it further downstream would publish
    something other than what the provider returned (ADR-035 §3.3).
    """

    if len(text) <= MAX_OUTPUT_TEXT:
        return text
    return text[: MAX_OUTPUT_TEXT - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def render_prompt(system_prompt: str, messages: Sequence[Message]) -> BoundedText:
    """Flatten what is about to be sent to the model into something readable.

    A transcript rather than the provider's wire format: the question this
    answers is "what was the model looking at when it said that", and a reader
    should not have to decode a vendor envelope to see it. Tool results are
    included because they are the largest thing shaping a turn -- omitting them
    would show a prompt that does not explain the answer.

    Only called when ADR-019's `runtime.record_step_inputs` is on.
    """

    sections: list[str] = []
    if system_prompt:
        sections.append(f"[system]\n{system_prompt}")
    for message in messages:
        parts: list[str] = []
        for block in message.content:
            if block.kind == "text":
                parts.append(block.text)
            elif block.kind == "tool_use":
                parts.append(f"→ 调用 {block.tool_name} #{block.tool_call_id}")
            elif block.kind == "tool_result":
                parts.append(f"← {block.tool_call_id} 返回\n{block.text}")
        sections.append(f"[{message.role}]\n" + "\n".join(parts))
    return bounded("\n\n".join(sections))


@dataclass(frozen=True, slots=True)
class _ModelTurn:
    """What one model call produced."""

    text: str
    calls: tuple[ToolCall, ...]
    usage: TokenUsage
    finish: ModelFinishReason | None
    error: ErrorInfo | None
    # Set when the turn failed for a reason the loop must report as something
    # other than a plain error, such as running out of run deadline.
    stop_reason: StopReason | None = None
    # The reasoning that preceded the text, already clipped to the thinking
    # ceiling. Never joins `text`, and never re-enters the ledger the next
    # request is built from -- but that is a decision of ours rather than a
    # requirement of the provider's, which is what the note here used to imply.
    # DeepSeek accepts the next round with the reasoning, without it, and with a
    # truncated copy of it (measured 2026-08-17; ADR-064). Its only durable home
    # is `ModelCompleted.thinking_preview`.
    thinking: str = ""


#: How much of a declared context window a run may fill, when nobody said.
#:
#: Mirrors `runtime.context_soft_limit_ratio`, which is the value every
#: assembly actually passes. It is a default rather than a required argument
#: because this constructor has a dozen call sites in tests that care about the
#: loop and not about context, and 0.75 is the shipped setting -- a test that
#: wanted a different one would say so.
DEFAULT_CONTEXT_SOFT_LIMIT_RATIO: Final[float] = 0.75


@dataclass(slots=True)
class _RunLedger:
    """Everything that accumulates across turns of one run."""

    messages: list[Message]
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    answer: str = ""
    #: How many times each (tool, arguments) pair has been proposed in this run.
    #: Keyed by content rather than by ``tool_call_id``, which is fresh every
    #: turn and so cannot see a model asking the same question twice.
    call_counts: dict[str, int] = field(default_factory=dict[str, int])
    #: How many times each *tool* has been asked anything in this run,
    #: whatever the arguments (ADR-0114). The coarser count `call_counts`
    #: cannot give: a run that asks one tool a fresh question every time never
    #: repeats a signature and still never gets anywhere.
    tool_counts: dict[str, int] = field(default_factory=dict[str, int])
    #: How many batches so far have dispatched something that could change
    #: what a tool would answer -- any call whose risk is not ``read``
    #: (ADR-0116). A write moves a file, a command may move any file, and a
    #: search may put new pages behind the same query; a read moves nothing.
    #: What this counts is not "did the world change" -- nothing here can know
    #: that -- but "did this run do anything that *could* have", which is the
    #: question a repeat has to be judged against.
    world_version: int = 0
    #: What each (tool, arguments) pair last answered, and under which
    #: `world_version` it answered it (ADR-0116). A repeat proposed while the
    #: version is unchanged is answered from here without dispatch: the model
    #: is handed the same result again, marked as the same, rather than made
    #: to spend a dispatch -- and, for a gated tool, a person's click -- to
    #: re-learn what is already in its context.
    answered: dict[str, tuple[int, ToolResult]] = field(
        default_factory=dict[str, tuple[int, ToolResult]]
    )
    #: How many times each signature has been answered from `answered`.
    replayed: dict[str, int] = field(default_factory=dict[str, int])
    #: Whether the tools have been taken off the request for the rest of the
    #: run. Set by the repeat that crossed `MAX_REPLAYS`; read where the
    #: request is built, the same place the spent tool allowance is read.
    tools_withdrawn: bool = False
    #: How many of this run's calls arrived cut off at the provider's output
    #: ceiling (ADR-0118). Each is answered with a refusal that says so; the
    #: one past `MAX_CUT_OFFS` ends the run instead, because two answers that
    #: each said "send it in pieces" were not read.
    cut_offs: int = 0
    #: How many times this run has shortened its own conversation.
    compactions: int = 0
    #: The provider's count for the *last* prompt this run sent, which is the
    #: best evidence of how large the next one will be. Deliberately not
    #: derived from `usage.tokens`: that is cumulative across turns and every
    #: turn re-sends the whole conversation, so it grows roughly with the
    #: square of the turn count and says nothing about one request's size.
    last_input_tokens: int = 0


@runtime_checkable
class _Closable(Protocol):
    """Anything that can be told the caller is finished with it."""

    async def aclose(self) -> None: ...


async def _aclose(stream: AsyncIterator[ModelEvent]) -> None:
    """Close a stream that can be closed, whatever concrete type it is.

    ``aclose`` is the protocol; ``AsyncGenerator`` is only the most common
    thing that satisfies it.
    """

    if isinstance(stream, _Closable):
        await stream.aclose()


#: How many calls cut off at the provider's output ceiling one run may be
#: answered for before it ends (ADR-0118). Two, on the same reasoning as
#: `MAX_REPLAYS`: the first refusal is the model learning that what it sent
#: does not fit, the second is it being told again, and a third identical
#: attempt is a run that will spend its whole budget the same way. The run
#: that motivated this sent one 42 KB page whole and was cut at 8192 tokens
#: -- three runs in a row, because the failure never reached the model.
MAX_CUT_OFFS: Final[int] = 2


def _cut_off_note(tool_name: str, count: int) -> str:
    """What the model is told when its call did not fit (ADR-0118).

    Written for the model, which is the one reader that can act on it: the
    provider's own word ("length") is on the `ModelCompleted` event for the
    operator. It names the tool, says that nothing ran, and says the one
    thing a retry has to change -- the size. It does not name a parameter of
    any particular tool: the runtime holds no vocabulary for files, and the
    tool that accepts pieces says so in its own description, which the
    model is holding.
    """

    return (
        f"your call to {tool_name} was cut off at the model's output ceiling: "
        "the arguments stopped mid-JSON, so nothing ran and nothing was "
        "written. What you were sending does not fit in one call, and sending "
        "it again will not make it fit. Send it in pieces -- several smaller "
        "calls, each a fraction of this one; a file goes in as a first part "
        "and then appended parts, never as a whole -- and keep the reasoning "
        "before a large call short, because it is spent from the same "
        f"ceiling. (cut off {count} of {MAX_CUT_OFFS} answered in this run)"
    )


def _repeated_call_ids(calls: Sequence[ToolCall]) -> tuple[str, ...]:
    """Ids proposed more than once in one turn, in the order first repeated.

    A tool_call_id is what a result answers to. Two calls sharing one leave no
    way to say which result belongs to which, so the turn is not something this
    runtime can execute -- whatever the model meant by it.
    """

    seen: set[str] = set()
    repeated: list[str] = []
    for call in calls:
        if call.tool_call_id in seen and call.tool_call_id not in repeated:
            repeated.append(call.tool_call_id)
        seen.add(call.tool_call_id)
    return tuple(repeated)


#: How many times one (tool, arguments) pair is answered *from the record*
#: before the run's tools are withdrawn (ADR-0116).
#:
#: This replaces a dispatch ceiling. Until 2026-09-13 an identical call was
#: *run* up to three times and refused from the fourth, on the reasoning that
#: asking again is ordinary -- a document read again after a write, a
#: workspace listed before and after. That reasoning was right about the
#: cases and wrong about the mechanism: "after a write" is what made those
#: repeats ordinary, and a count of dispatches cannot see a write. So the
#: ordinary repeat is now told apart by `_RunLedger.world_version` -- a call
#: proposed again after anything non-read ran is a fresh question and is
#: dispatched -- and the other kind, the same question with nothing between,
#: is answered from what it answered last time.
#:
#: Measured 2026-09-12 on the run that motivated this (`docs/status.md`
#: 第八十四批): a coding turn proposed one `python3 -c` command four times in
#: a row, was refused on the fourth, added a comment to the script to make it
#: a new signature, proposed *that* four times, and was stopped after the
#: third refusal with `RunFailed` -- ten approval cards for two commands whose
#: answer was in its context from the first. Under this rule the second and
#: third proposals cost nothing and nobody, and the fourth ends the loop by
#: taking the tools away, which leaves the run with the one move it can still
#: make: write what it has.
#:
#: 2 and not 1, because a single re-serve is cheap and the model that missed
#: an answer once may read it the second time; a model that misses it twice
#: is not going to read it the third time either.
MAX_REPLAYS: Final[int] = 2

#: How many times one run may shorten its own conversation (ADR-081).
#:
#: A backstop rather than a policy. Each compaction removes the middle, so the
#: conversation shrinks and `plan_compaction` eventually refuses on its own --
#: but a run that has done this three times and is still over the line is not
#: going to be rescued by a fourth, and the honest answer at that point is the
#: ceiling it keeps hitting.
MAX_COMPACTIONS_PER_RUN: Final[int] = 3

#: What the summariser is told it is doing.
#:
#: Short, and about fidelity rather than brevity: the failure mode that matters
#: is a summary that reads complete while having dropped the decision the next
#: turn depends on. It says the reader is the same agent because that is true
#: -- the text comes back into this run's own conversation as an assistant
#: message -- and a summariser writing for a stranger writes an abstract.
COMPACTION_PROMPT: Final[str] = """\
You are compacting the earlier part of a coding run so it fits in a smaller
context. What you write goes back to the agent that did this work, as its own
record of what it already did.

Keep: what was asked, what was decided and why, which files were read or
changed and what is now in them, what failed and what the failure said, and
anything the next step depends on. Keep exact names -- paths, identifiers,
error strings -- because they are what the next tool call will be built from.

Drop: repetition, the full text of files, and anything already superseded.

Write it as a plain account in the past tense. Do not add advice, do not
speculate about what to do next, and do not describe anything the transcript
does not show.\
"""


def _replay_note(tool_name: str, *, withdrawn: bool) -> str:
    """The sentence a result answered from the record carries (ADR-0116).

    Written for the model that is repeating itself, so it says three things
    and no more: that this is the same call, that nothing has happened since
    which could change its answer, and what to do instead. It does not say
    "stop" for the reason `_nudge` does not -- except on the repeat that took
    the tools away, where "stop" is no longer advice but a description of
    the next request, and the model is told so it writes a report rather
    than a fourth proposal.
    """

    same = (
        f"\n\n[This is the same {tool_name} call as before, with the same "
        "arguments, and nothing has run since that could change its answer, "
        "so this is that answer again rather than a new one."
    )
    if withdrawn:
        return same + (
            " It has now been repeated past the point of being useful: the "
            "tools are withdrawn for the rest of this run. Write your report "
            "now, from what you have, and say what could not be determined.]"
        )
    return same + (
        " Do not call it again: act on what it says, or report what could "
        "not be determined.]"
    )


#: Every this-many calls to *one tool*, its result carries a sentence saying so
#: (ADR-0114).
#:
#: A nudge, not a refusal, and not a ceiling -- the two things above are the
#: ceiling and the refusal, and they answer a different question. They see a
#: run asking the *same* question again. What they cannot see is a run asking
#: one tool a *different* question every time in pursuit of an answer that
#: tool does not have: measured 2026-09-12, a coding turn made 74 `project_grep`
#: calls against one file, each pattern different from the last, bisecting a
#: string's length with regex quantifiers ("at most 96 ... 89 ... 79 ... 69
#: ... 59 ... 49 ... 47 ... 49"), two per step for thirty steps, and ended on
#: `max_steps` with the answer wrong. No two of those calls shared a
#: signature, so `MAX_IDENTICAL_CALLS` never fired, and the step ceiling that
#: did fire reads to the person watching as the model being incapable.
#:
#: 25, and the number is calibrated rather than guessed. Over every run in the
#: local event log that day, the busiest single tool of a run that *completed*
#: had a median of 2 calls and a 90th percentile of 11; the runs that looped
#: had 31, 42, 74, 98, 106 and 114. The nudge lands after the healthy tail and
#: before the pathology has spent half its budget, and it lands again at
#: every multiple so a run that shrugs off the first one is told again rather
#: than once.
#:
#: Why a sentence in the result and not a refusal: the call may be legitimate
#: -- a wide exploration of a large tree is thirty greps -- and a refusal of
#: legitimate work is the mistake the identical-call breaker was careful not
#: to make (its bar sits above re-reading). A sentence costs a false positive
#: nothing but the sentence. It is the shape Claude Code uses for the same
#: situation: a reminder attached to a tool result, from the harness, in the
#: harness's own voice.
SAME_TOOL_NUDGE_EVERY: Final[int] = 25


def _nudge(tool_name: str, count: int) -> str:
    """The sentence appended to the ``count``-th result of ``tool_name``.

    Written to be read by the model that is looping, so it names the count, the
    tool, and the two honest ways out -- act on what is known, or report what
    could not be determined. It deliberately does not say "stop": the run may
    be right to continue, and a harness cannot know. What it can know is the
    count, and the count is the fact worth handing over.
    """

    return (
        f"\n\n[This is call {count} of {tool_name} in this run. If the last "
        "several were narrowing one question, this tool is not going to answer "
        "it: write down what you know and act on it, or report what could not "
        "be determined.]"
    )


def _call_signature(call: ToolCall) -> str:
    """Identify a call by what it asks, not by the id it was asked under.

    Arguments are serialized with sorted keys so that two calls a model wrote
    in a different key order are recognised as the one question they are.
    """

    arguments = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
    return f"{call.tool_name}\x00{arguments}"


class ClaudeLikeAgentRuntime:
    """Runs one agent's model-tool loop to a terminal outcome."""

    def __init__(
        self,
        *,
        model: ModelPort,
        gateway: ToolGateway,
        policy_identity: str,
        model_label: str = DEFAULT_MODEL_LABEL,
        model_timeout_seconds: float | None = DEFAULT_MODEL_TIMEOUT_SECONDS,
        max_parallel_read_tools: int = DEFAULT_MAX_PARALLEL_READS,
        clock: Callable[[], datetime] | None = None,
        model_call_ids: Callable[[], str] | None = None,
        telemetry: Telemetry | None = None,
        record_step_inputs: bool = False,
        prices: ModelPrices | None = None,
        context_window_tokens: int | None = None,
        context_soft_limit_ratio: float = DEFAULT_CONTEXT_SOFT_LIMIT_RATIO,
        compaction_enabled: bool = False,
        compact_model_label: str | None = None,
    ) -> None:
        self._model = model
        # Tools are reached only through the gateway: resolving, validating,
        # authorizing and running them is one component's job, not the loop's.
        self._gateway = gateway
        # The operator label paired with the fingerprint of the rules it claims
        # to describe. It is recorded with every decision this run makes.
        self._policy_identity = policy_identity
        self._model_label = model_label
        self._model_timeout_seconds = model_timeout_seconds
        # Mirrors runtime.max_parallel_read_tools. Exclusive tools ignore it:
        # they are always a group of one.
        self._max_parallel_read_tools = max_parallel_read_tools
        self._clock = clock if clock is not None else _utc_now
        self._model_call_ids = (
            model_call_ids if model_call_ids is not None else new_model_call_id
        )
        # ADR-019. Puts the prompt on the run's own event stream when a
        # deployment asked for it. Independent of `telemetry`, which never
        # carries a body.
        self._record_step_inputs = record_step_inputs
        # What this deployment's model can hold, and how much of it a run may
        # fill (ADR-0080). `None` is a real state and not "unlimited": it is a
        # process that cannot tell a long turn from a short one, and whose
        # over-long turns die as `provider_error: HTTP 400` -- a message that
        # sends whoever is reading the transcript to the model adapter, which
        # is the one place the problem is not.
        self._context_window_tokens = context_window_tokens
        self._context_soft_limit_ratio = context_soft_limit_ratio
        # ADR-081. Off by default, and the staging is the point: ADR-080 made
        # an over-long run stop and say so, and this decides whether it tries
        # to survive instead. A deployment that has not yet seen a real
        # `context_limit` has nothing to tune this against -- honest failure
        # first, remedy second.
        self._compaction_enabled = compaction_enabled
        # The `[model.compact]` profile's `model_id`, because ADR-081 makes one
        # call per run under a profile that is not this runtime's own, and the
        # adapter dispatches on `ModelRequest.model_profile`. Recording that
        # call under the main label would be an event that names a model the
        # request never reached -- the failure the comment beside `model_label`
        # in `apps/api/dependencies.py` calls the one thing an event log may
        # not do. `None` falls back to the main label: the honest state for a
        # deployment that never enabled compaction, not a claim that the two
        # profiles point at the same model. They frequently do not --
        # `config.code-local.toml` and `config.demo-local.toml` both run
        # `deepseek-v4-flash` as main against `deepseek-chat` as compact.
        self._compact_model_label = compact_model_label
        # Defaults to recording nothing. A deployment without a collector is
        # not a deployment that behaves differently, so this is the absence of
        # one rather than a degraded mode.
        self._telemetry = telemetry if telemetry is not None else NullTelemetry()
        # What this profile's model charges, when the deployment said. Absent
        # is a real state rather than "free": every spend stays zero, and a run
        # that asked for a cost ceiling is refused instead of being given one
        # that cannot fire.
        self._prices = prices

    def _priced(self, usage: TokenUsage) -> int:
        """Micro-USD for one turn, or zero where this process has no prices.

        Zero, not an estimate. A guessed rate would put a number on the event
        stream that reads exactly like a measured one.
        """

        return 0 if self._prices is None else self._prices.cost_micro_usd(usage)

    async def run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        """Run the loop, and record what it did.

        A wrapper rather than instrumentation at each ``return``: the loop has
        several terminal paths and the one that would get missed is whichever
        is added next. Everything here is derived from the outcome the loop
        already produces, so recording cannot disagree with what happened.
        """

        started = self._clock()
        attributes: Attributes = {
            "run_kind": request.run_kind,
            "model_profile": request.model_profile,
        }
        self._telemetry.count(RUN_STARTED, attributes=attributes)
        with self._telemetry.span("agent.run", attributes=attributes):
            outcome = await self._run(request, emit, cancellation)

        elapsed = (self._clock() - started).total_seconds() * 1000
        settled: Attributes = {
            **attributes,
            "status": outcome.status,
            "stop_reason": outcome.stop_reason or "",
        }
        self._telemetry.record(RUN_DURATION, elapsed, attributes=settled)
        self._telemetry.record(RUN_STEPS, outcome.usage.steps, attributes=settled)
        self._telemetry.record(
            MODEL_INPUT_TOKENS, outcome.usage.tokens.input_tokens, attributes=settled
        )
        self._telemetry.record(
            MODEL_OUTPUT_TOKENS, outcome.usage.tokens.output_tokens, attributes=settled
        )
        self._telemetry.count(
            RUN_COMPLETED if outcome.status == "completed" else RUN_FAILED,
            attributes=settled,
        )
        return outcome

    async def _run(
        self,
        request: AgentRunRequest,
        emit: EventSink,
        cancellation: CancellationToken,
    ) -> AgentOutcome:
        # The frozen protocol names this parameter `emit`; everything below
        # reads better calling the object what it is.
        sink = emit
        machine = RunStateMachine()
        ledger = _RunLedger(messages=list(request.messages))
        context = self._execution_context(request)

        await sink.emit(
            RunStarted(
                run_kind=request.run_kind,
                model_profile=request.model_profile,
                tool_names=request.tool_names,
                budget=request.budget,
            )
        )
        if request.context is not None:
            await sink.emit(
                ContextBuilt(
                    chunk_count=len(request.context.chunks),
                    citation_count=len(request.context.citations),
                    token_estimate=request.context.token_estimate,
                    retrieval_trace_id=request.context.retrieval_trace_id,
                )
            )

        if request.budget.max_cost_micro_usd is not None and self._prices is None:
            # The ceiling is enforceable only where this process was told what
            # its model charges. Unpriced, ``cost_micro_usd`` would stay at
            # zero for the whole run and the ceiling would never fire -- so it
            # is refused, on the same reasoning that refused every cost ceiling
            # before a pricer existed: a limit that cannot be enforced must not
            # be accepted as one. What changed is that this is now a statement
            # about one deployment's configuration rather than about the
            # runtime, and the message has to send the reader to the config
            # rather than to the backlog.
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                ErrorInfo(
                    code="invalid_tool_input",
                    message=(
                        "max_cost_micro_usd was requested, but no prices are "
                        f"configured for model profile {self._model_label!r}, "
                        "so a cost ceiling cannot be enforced"
                    ),
                ),
                ledger,
            )

        try:
            advertised = self._gateway.advertise(request.tool_names)
        except AgentWorkbenchError as exc:
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                exc.to_error_info(),
                ledger,
            )

        while True:
            if cancellation.cancelled:
                return await self._cancelled(request, sink, machine, ledger)

            # Ceilings are consulted before a turn starts. A budget that only
            # triggers after the spend is not a ceiling, and an unbounded loop
            # is exactly what a model proposing tools forever would produce.
            #
            # `halt_reason_for`, not `stop_reason_for`: the tool ceiling is not
            # a reason to end a run. Every limit asked about here leaves the run
            # with nothing further it could do; a spent tool allowance leaves it
            # with an answer to write. `max_steps` still bounds the loop, and it
            # is what bounds it -- one step is spent per iteration, so no run can
            # circle here forever on a closed toolbox.
            halted = request.budget.halt_reason_for(
                ledger.usage,
                now=self._clock(),
            )
            if halted is not None:
                return await self._failed(
                    request,
                    sink,
                    machine,
                    halted,
                    ErrorInfo(
                        code="budget_exceeded",
                        message=f"the run stopped at its ceiling: {halted}",
                    ),
                    ledger,
                )

            # The one ceiling that is not the submitter's (ADR-0080). Asked
            # here, in the same breath as the budget, because it answers the
            # same question -- may this run take another turn -- from the other
            # side: not "has it spent what it was given" but "will what it is
            # about to send fit".
            #
            # The numbers go in the message. Without them the operator reading
            # a stopped run has a stop reason and no way to tell a model that
            # needs a bigger window from a run that read too many files, and
            # the previous answer to this situation -- `provider_error: HTTP
            # 400` from the adapter, whose body is deliberately never read --
            # sent them to the one component that was working correctly.
            over_context = context_reason_for(
                ledger.last_input_tokens,
                window_tokens=self._context_window_tokens,
                soft_limit_ratio=self._context_soft_limit_ratio,
            )
            if over_context is not None:
                shortened = await self._compacted(
                    request, sink, machine, ledger, cancellation
                )
                if shortened is not None:
                    # Shortened instead of stopped (ADR-081). The estimate is
                    # carried forward rather than cleared: this used to set 0,
                    # on the reasoning that the measurement describing the old
                    # conversation no longer applied to the new one. True, and
                    # it disarmed ADR-080's ceiling for the next request --
                    # measured, a run whose kept tail held the 60 KB tool
                    # result then compacted three times, reported saving 0.03%
                    # each time, and sent three requests at 70,000 tokens
                    # against a 64,000-token window, ending in the HTTP 400
                    # ADR-080 exists to stop transcribing.
                    ledger.last_input_tokens = shortened
                    over_context = None
            if cancellation.cancelled:
                # The compaction call is the one model call in this loop whose
                # result does not pass through `_terminal_for_turn`, whose
                # first question is whether the run was cancelled. A cancelled
                # summariser returns empty text and no error, which
                # `_compacted` reports as "could not shorten" -- and the run
                # would then be filed under `context_limit`, blaming the
                # model's window for a stop somebody asked for.
                return await self._cancelled(request, sink, machine, ledger)
            if over_context is not None:
                assert self._context_window_tokens is not None
                return await self._failed(
                    request,
                    sink,
                    machine,
                    over_context,
                    ErrorInfo(
                        code="budget_exceeded",
                        message=(
                            "the run stopped before its next request would "
                            "have outgrown the model's context: the last "
                            f"prompt was {ledger.last_input_tokens} tokens of "
                            f"a {self._context_window_tokens}-token window, "
                            f"past the {self._context_soft_limit_ratio} soft "
                            "limit"
                        ),
                    ),
                    ledger,
                )

            # Recomputed each turn, because what the model may reach changes
            # within a run. Once the allowance is gone the tools come off the
            # request entirely rather than staying on it to be refused: a model
            # that can still see `web_search` proposes `web_search`, and then
            # the only thing left to do with the proposal is turn it away and
            # kill the run holding the results that proposal was meant to
            # improve on. Measured on the chat fallback -- two successful
            # searches, 5.5KB of results, a third proposal, and an answer that
            # said it could not search. Taking the tool away asks the question
            # the run is actually able to answer: "write what you have".
            # And the same shape for a run that has repeated one call past
            # `MAX_REPLAYS` (ADR-0116): the tools come off, the question the
            # run can still answer is asked. `tools_withdrawn` is read here,
            # beside the allowance, because it is the same decision made for
            # a different reason -- this run has nothing left to learn from
            # its tools, whether because it spent them or because it stopped
            # reading what they said.
            specs = (
                ()
                if ledger.tools_withdrawn
                or request.budget.tool_allowance_spent(ledger.usage)
                else advertised
            )

            machine.to("model_streaming")
            turn = await self._stream_model(
                request,
                sink,
                ledger,
                specs,
                cancellation,
            )
            ledger.usage = ledger.usage.merged(
                BudgetUsage(
                    steps=1,
                    tokens=turn.usage,
                    cost_micro_usd=self._priced(turn.usage),
                )
            )
            # Carried beside the merge rather than derived from it, for the
            # reason the field's own comment gives: the merged figure is a sum
            # over turns and this is one turn's prompt.
            ledger.last_input_tokens = turn.usage.input_tokens

            terminal = await self._terminal_for_turn(
                request,
                sink,
                machine,
                cancellation,
                turn,
                ledger,
            )
            if terminal is not None:
                return terminal

            terminal = await self._run_tool_batch(
                request,
                sink,
                machine,
                cancellation,
                turn,
                ledger,
                context=context,
            )
            if terminal is not None:
                return terminal

            if cancellation.cancelled:
                return await self._cancelled(request, sink, machine, ledger)

    def _execution_context(self, request: AgentRunRequest) -> ExecutionContext:
        return ExecutionContext(
            principal=request.principal,
            envelope=request.envelope,
            agent_run_id=request.trace.agent_run_id,
            policy_identity=self._policy_identity,
            task_id=request.trace.task_id,
            workflow_thread_id=request.trace.workflow_thread_id,
            graph_node_id=request.trace.graph_node_id,
            # Forwarded rather than left None, which is what it was until
            # 2026-08-23. The omission read as a deliberate narrowing and was
            # not one: it silently made the side-effect ledger unreachable from
            # the tool loop, because `_invoke_ledgered` refuses a call whose
            # context cannot name an epoch. Nothing noticed, because the only
            # ledgered tool in the repository is issued by a deterministic node
            # that builds its own context (`_tool_execution_context`) and
            # supplies the epoch there.
            #
            # Restoring it also removes an accidental guardrail: with an epoch
            # present, a ledgered tool placed in a profile would now dispatch on
            # nothing but a model's say-so. `ToolGateway.advertise` refuses to
            # offer one, which is the guardrail on purpose rather than by
            # omission (ADR-075).
            lease_epoch=request.trace.lease_epoch,
        )

    async def _stream_model(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        ledger: _RunLedger,
        specs: tuple[ToolSpec, ...],
        cancellation: CancellationToken,
    ) -> _ModelTurn:
        deadline = effective_model_deadline(
            envelope_seconds=self._model_timeout_seconds,
            run_deadline=request.budget.deadline,
            now=self._clock(),
        )
        if deadline.expired:
            # No time left to start: reported before a model call is paid for.
            return _ModelTurn(
                text="",
                calls=(),
                usage=TokenUsage(),
                finish="error",
                error=deadline.to_error(),
                stop_reason=deadline.stop_reason(),
            )

        model_call_id = self._model_call_ids()
        await sink.emit(
            ModelStarted(
                model_call_id=model_call_id,
                model_profile=request.model_profile,
                model_id=self._label_for(request.model_profile),
                prompt_preview=(
                    render_prompt(request.system_prompt, ledger.messages)
                    if self._record_step_inputs
                    else ""
                ),
            )
        )

        stream = self._model.stream(
            ModelRequest(
                model_profile=request.model_profile,
                system_prompt=request.system_prompt,
                messages=tuple(ledger.messages),
                tools=specs,
                thinking=request.thinking,
            )
        )
        try:
            async with asyncio.timeout(deadline.seconds):
                turn = await self._consume(stream, sink, model_call_id, cancellation)
        except TimeoutError:
            return _ModelTurn(
                text="",
                calls=(),
                usage=TokenUsage(),
                finish="error",
                error=deadline.to_error(),
                stop_reason=deadline.stop_reason(),
            )
        except OperationCancelledError:
            return _ModelTurn("", (), TokenUsage(), "cancelled", None)
        except Exception as exc:
            # An adapter fault is a run outcome, not the caller's exception.
            return _ModelTurn(
                text="",
                calls=(),
                usage=TokenUsage(),
                finish="error",
                error=ErrorInfo.from_exception(exc, default_code="provider_error"),
            )

        if turn.finish is not None and turn.finish != "cancelled":
            await sink.emit(
                ModelCompleted(
                    model_call_id=model_call_id,
                    finish_reason=turn.finish,
                    usage=turn.usage,
                    text=turn.text,
                    thinking_preview=turn.thinking,
                    tool_call_ids=tuple(call.tool_call_id for call in turn.calls),
                )
            )
        return turn

    async def _consume(
        self,
        stream: AsyncIterator[ModelEvent],
        sink: EventSink,
        model_call_id: str,
        cancellation: CancellationToken,
    ) -> _ModelTurn:
        """Drain one model stream, stopping early if the run was cancelled."""

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[ToolCall] = []
        tokens = TokenUsage()
        finish: ModelFinishReason | None = None
        error: ErrorInfo | None = None

        try:
            async for event in stream:
                if cancellation.cancelled:
                    # Observed at the next event boundary. A stream that goes
                    # quiet instead is bounded by the deadline above.
                    return _ModelTurn("", (), tokens, "cancelled", None)
                if isinstance(event, ModelTextDelta):
                    text_parts.append(event.text)
                    await sink.emit(
                        ModelDelta(model_call_id=model_call_id, text=event.text)
                    )
                elif isinstance(event, ModelThinkingDelta):
                    # An explicit arm, never the else below: the else consumes
                    # events as stream completion, and a reasoning slice read
                    # as "the stream ended" would truncate every thinking
                    # turn at its first thought.
                    thinking_parts.append(event.text)
                    await sink.emit(
                        DomainModelThinkingDelta(
                            model_call_id=model_call_id, text=event.text
                        )
                    )
                elif isinstance(event, ModelToolCallProposed):
                    calls.append(event.call)
                elif isinstance(event, ModelUsageReported):
                    tokens = event.usage
                else:
                    finish = event.finish_reason
                    error = event.error
                    if event.usage.total:
                        tokens = event.usage
        finally:
            # Closing the stream is how cancellation and deadlines reach the
            # adapter, and through it whatever connection it holds open. The
            # port promises an AsyncIterator, not an AsyncGenerator, so asking
            # for the concrete type meant an adapter that returned any other
            # closable iterator was simply never closed -- and a leak of that
            # shape shows up as exhausted connections under load, far from the
            # line that caused it.
            await _aclose(stream)

        return _ModelTurn(
            _clip("".join(text_parts)),
            tuple(calls),
            tokens,
            finish,
            error,
            # On the thinking ceiling and cut from the middle, not the end: the
            # full chain already streamed as transient deltas and the durable
            # record describes rather than copies (ADR-061), but what it keeps
            # has to include the conclusion. `bounded()` would keep the opening
            # and drop the sentence that says what the model decided to do
            # (ADR-064).
            thinking=bounded_thinking("".join(thinking_parts)),
        )

    async def _terminal_for_turn(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        cancellation: CancellationToken,
        turn: _ModelTurn,
        ledger: _RunLedger,
    ) -> AgentOutcome | None:
        """Map one model turn onto a terminal outcome, or ``None`` to continue."""

        if turn.finish == "cancelled" or cancellation.cancelled:
            return await self._cancelled(request, sink, machine, ledger)

        if turn.finish is None:
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                ErrorInfo(
                    code="provider_error",
                    message="the model stream ended without a completion event",
                ),
                ledger,
            )

        if turn.error is not None or turn.finish == "error":
            return await self._failed(
                request,
                sink,
                machine,
                turn.stop_reason or "error",
                turn.error
                or ErrorInfo(
                    code="provider_error",
                    message="the model reported an error without describing it",
                ),
                ledger,
            )

        if turn.finish == "max_tokens" and not any(call.cut_off for call in turn.calls):
            # A cut-off answer must not reach its caller looking complete.
            # A cut-off *call* is different (ADR-0118): the adapter handed it
            # on marked, and the batch below answers it with a refusal the
            # model reads, so the run goes on with what it had worked out.
            return await self._failed(
                request,
                sink,
                machine,
                "token_budget",
                ErrorInfo(
                    code="budget_exceeded",
                    message="the model stopped at its output token ceiling",
                ),
                ledger,
            )

        # Checked here, after this turn's tokens are in the ledger and before
        # either continuing or completing. The top-of-loop check runs before a
        # turn and so cannot see what that turn spent; without this, a run that
        # blew its token ceiling reported "completed", and one that blew it
        # while proposing tools went on to run them.
        exceeded = request.budget.overrun_reason_for(ledger.usage, now=self._clock())
        if exceeded is not None:
            return await self._failed(
                request,
                sink,
                machine,
                exceeded,
                ErrorInfo(
                    code="budget_exceeded",
                    message=f"the run passed its ceiling: {exceeded}",
                ),
                ledger,
            )

        if turn.calls:
            # The model wants to continue, so the question changes from "did
            # this overrun?" to "may it start more work?" -- and the answer to
            # the second is no once the allowance is used up. Dispatching here
            # would buy side effects for results the last step has no reader
            # for. A turn that finished instead is completed below: spending
            # the allowance exactly is not overspending it.
            spent = request.budget.stop_reason_for(ledger.usage, now=self._clock())
            if spent is not None:
                return await self._failed(
                    request,
                    sink,
                    machine,
                    spent,
                    ErrorInfo(
                        code="budget_exceeded",
                        message=f"the run stopped at its ceiling: {spent}",
                    ),
                    ledger,
                )

            if ledger.tools_withdrawn:
                # The request carried no tools, and the model proposed one
                # anyway. A provider that honours the request cannot produce
                # this; a scripted double and a model that hallucinates a
                # call both can, and neither is a run that can go on. The
                # message names the cause -- the repeats -- rather than the
                # symptom, which is what the person reading the stop wants.
                return await self._failed(
                    request,
                    sink,
                    machine,
                    "error",
                    ErrorInfo(
                        code="tool_failed",
                        message=(
                            "the run kept proposing calls it had already made: "
                            "its tools were withdrawn after "
                            f"{sum(ledger.replayed.values())} repeats were "
                            "answered from the record, and it proposed another"
                        ),
                    ),
                    ledger,
                )

            duplicated = _repeated_call_ids(turn.calls)
            if duplicated:
                # Checked before anything is prepared, authorized or run. The
                # pairing rule that catches this otherwise runs after the
                # handlers, which meant a model repeating an id got its tool
                # executed once per repetition and the run then died on the
                # bookkeeping. A malformed proposal must cost nothing.
                return await self._failed(
                    request,
                    sink,
                    machine,
                    "error",
                    ErrorInfo(
                        code="provider_error",
                        message=(
                            "the model proposed the same tool_call_id more "
                            f"than once: {', '.join(duplicated)}"
                        ),
                    ),
                    ledger,
                )
            return None

        if turn.finish == "tool_use":
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                ErrorInfo(
                    code="provider_error",
                    message="the model finished for tool use but proposed no call",
                ),
                ledger,
            )

        ledger.answer = turn.text
        machine.to("completed")
        await sink.emit(RunCompleted(stop_reason="completed", usage=ledger.usage))
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="completed",
            stop_reason="completed",
            output_text=turn.text,
            usage=ledger.usage,
        )

    async def _run_tool_batch(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        cancellation: CancellationToken,
        turn: _ModelTurn,
        ledger: _RunLedger,
        *,
        context: ExecutionContext,
    ) -> AgentOutcome | None:
        """Take one batch of proposed calls through the gateway's phases."""

        machine.to("validating_tools")
        for call in turn.calls:
            await self._gateway.propose(call, sink=sink)

        # The ceiling is spent before the batch runs, not counted after it. A
        # turn proposing more calls than remain used to run all of them and
        # report the overrun afterwards, which is an accounting entry, not a
        # limit: the side effects had already happened.
        allowance = max(request.budget.max_tool_calls - ledger.usage.tool_calls, 0)
        admitted = turn.calls[:allowance]

        # Re-read for each phase: the run's deadline bounds the policy engine
        # and the hooks exactly as it bounds the tools they guard, and a slow
        # phase leaves less for the next.
        def remaining() -> float | None:
            return remaining_run_seconds(request.budget.deadline, now=self._clock())

        results: list[ToolResult] = []
        for call in turn.calls[allowance:]:
            results.append(
                await self._gateway.refuse(
                    call,
                    ErrorInfo(
                        code="budget_exceeded",
                        message=(
                            "the run reached its tool-call ceiling before this "
                            "call was dispatched"
                        ),
                    ),
                    sink=sink,
                )
            )

        # A call the provider cut off at its output ceiling is answered, not
        # run (ADR-0118). Its arguments are not what the model meant, and the
        # one place the model reads is its tool result -- so the refusal goes
        # there, in this run, while the level layout and the plan it had
        # worked out are still in its context. Answered before the counting
        # below, and that order matters: the record is keyed by (tool,
        # arguments) and a cut-off call has none, so two of them would be
        # "the same question" and the second replayed with a note about
        # repeating itself. The one past `MAX_CUT_OFFS` ends the run instead.
        cut_off = [call for call in admitted if call.cut_off]
        if cut_off:
            ledger.cut_offs += len(cut_off)
            if ledger.cut_offs > MAX_CUT_OFFS:
                return await self._failed(
                    request,
                    sink,
                    machine,
                    "token_budget",
                    ErrorInfo(
                        code="budget_exceeded",
                        message=(
                            "the model was cut off at its output ceiling inside "
                            f"a call to {cut_off[-1].tool_name} {ledger.cut_offs} "
                            f"times in this run: {MAX_CUT_OFFS} times it was told "
                            "that what it sent does not fit in one call and to "
                            "send it in pieces, and it sent the whole again"
                        ),
                    ),
                    ledger,
                )
            answered_so_far = ledger.cut_offs - len(cut_off)
            for offset, call in enumerate(cut_off, start=1):
                results.append(
                    await self._gateway.refuse(
                        call,
                        ErrorInfo(
                            code="invalid_tool_input",
                            message=_cut_off_note(
                                call.tool_name, answered_so_far + offset
                            ),
                            retryable=False,
                        ),
                        sink=sink,
                    )
                )
            admitted = tuple(call for call in admitted if not call.cut_off)

        # What this run was offered, compared against what it took.
        #
        # The offer is already an intersection -- `permitted_tools` narrows the
        # profile by the Task's envelope, so a sub-agent can never hold more
        # authority than the Task it belongs to. What it was not, until now, is
        # binding: the Policy Gateway resolves a proposed name against the whole
        # registry and then asks the *Task-wide* envelope whether it permits the
        # binding. A name the Task allows but this node was never offered --
        # another audience's tool, in a Worker that registered both -- passed
        # that check, because the envelope is the Task's and the offer is the
        # node's, and nothing compared the two.
        offered = frozenset(request.tool_names)

        # A call the run has already made, with nothing run since that could
        # change its answer, is answered from the record before it is
        # prepared (ADR-0116). Running it again spends budget -- and, for a
        # gated tool, a person's attention -- to re-learn what the run already
        # holds. Measured first on a research node that fetched one URL eight
        # times because every sub-page redirected to the same place, and then
        # on a coding turn that proposed one approved command four times in a
        # row: the refusal that used to stand here stopped the first and made
        # the second worse, because a refusal is a new answer the model can
        # react to by changing a comment, and a re-served result is not.
        repeatable: list[ToolCall] = []
        # Which of this batch's calls is the 25th, 50th, ... of its tool, and
        # which number it is (ADR-0114). Decided here, while counting, because
        # a batch may hold two calls of one tool -- the observed loop proposed
        # exactly two per step -- and the sentence belongs on the one that
        # crossed the line, not on both.
        nudged: dict[str, int] = {}
        for call in admitted:
            # Counted before anything else can skip the rest of the loop, and
            # that ordering is the whole guard. A refusal is cheap in tokens
            # and free in effects, so a model that keeps proposing the same
            # refused call costs nothing per call and everything per run: it
            # burns turns until the step ceiling. The circuit breaker only
            # works if every admitted call reaches it, including the ones the
            # checks below are about to turn away.
            signature = _call_signature(call)
            seen_before = ledger.call_counts.get(signature, 0)
            ledger.call_counts[signature] = seen_before + 1
            # The per-tool count, kept in the same breath and for the same
            # reason: a call the checks below refuse is still the model asking
            # this tool once more, and a count that skipped refusals would let
            # a run alternate refused and fresh calls without ever reaching a
            # multiple.
            asked = ledger.tool_counts.get(call.tool_name, 0) + 1
            ledger.tool_counts[call.tool_name] = asked
            if asked % SAME_TOOL_NUDGE_EVERY == 0:
                nudged[call.tool_call_id] = asked
            recorded = ledger.answered.get(signature)
            if recorded is not None and recorded[0] == ledger.world_version:
                # The same question, and nothing has happened since it was
                # answered. Answered from the record, whatever the tool is: a
                # read that would return the same bytes, a command whose
                # world has not moved, a refusal that would be refused again.
                # `seen_before` is not consulted -- the record is the fact
                # that it was seen, and the version is the fact that nothing
                # since could have changed what it said.
                replays = ledger.replayed.get(signature, 0) + 1
                ledger.replayed[signature] = replays
                withdrawn = replays > MAX_REPLAYS
                if withdrawn:
                    ledger.tools_withdrawn = True
                results.append(
                    await self._gateway.replay(
                        call,
                        recorded[1],
                        note=_replay_note(call.tool_name, withdrawn=withdrawn),
                        sink=sink,
                    )
                )
                continue
            if call.tool_name not in offered and self._gateway.knows(call.tool_name):
                # `knows` and not just the offer, so the two failures keep their
                # own names. A tool this process never registered is still an
                # `unknown_tool`, answered further down by the gateway that owns
                # that vocabulary; only a tool that exists and was withheld from
                # *this* run is a policy refusal, and the difference is what an
                # operator reads to tell a hallucinated name from a profile that
                # is missing something it needs.
                results.append(
                    await self._gateway.refuse(
                        call,
                        ErrorInfo(
                            code="policy_denied",
                            message=(
                                f"{call.tool_name} was not offered to this run. "
                                "Use one of the tools you were given."
                            ),
                        ),
                        sink=sink,
                    )
                )
                continue
            repeatable.append(call)

        prepared: list[PreparedCall] = []
        for call in repeatable:
            outcome = await self._gateway.prepare(
                call,
                context=context,
                sink=sink,
                remaining_run_seconds=remaining(),
            )
            if isinstance(outcome, ToolResult):
                results.append(outcome)
            else:
                prepared.append(outcome)

        authorized: list[PreparedCall] = []
        if prepared:
            machine.to("authorizing")
            for index, candidate in enumerate(prepared):
                if cancellation.cancelled:
                    # Authorization is serial and one of these calls may have
                    # just spent minutes held for a human. A cancel that
                    # arrived during that wait must not then be spent asking
                    # about the rest of the batch, one bounded wait at a time.
                    # They still owe the model an answer, so they are refused
                    # rather than dropped.
                    results.extend(
                        await self._refuse_cancelled(tuple(prepared[index:]), sink=sink)
                    )
                    break
                outcome = await self._gateway.authorize(
                    candidate,
                    context=context,
                    sink=sink,
                    remaining_run_seconds=remaining(),
                    cancellation=cancellation,
                )
                if isinstance(outcome, ToolResult):
                    results.append(outcome)
                else:
                    authorized.append(outcome)

        if authorized:
            machine.to("executing_tools")
            for group in plan_tool_batches(
                authorized,
                max_parallel=self._max_parallel_read_tools,
            ):
                if cancellation.cancelled:
                    # The run is ending, but these ids were already shown to
                    # the model and still owe an answer. Groups already in
                    # flight keep their real results.
                    results.extend(await self._refuse_cancelled(group, sink=sink))
                    continue
                results.extend(
                    await self._run_group(
                        group,
                        request=request,
                        context=context,
                        cancellation=cancellation,
                        sink=sink,
                    )
                )

        machine.to("recording_results")
        try:
            aligned = align_results(turn.calls, results)
        except ToolPairingError as exc:
            # Unreachable by way of duplicate ids, which are refused before
            # dispatch. It stays because the caller was promised a terminal
            # outcome: a graph node needs something it can record and route on,
            # and a traceback is neither. An invariant this runtime broke is
            # still this runtime's to report.
            return await self._failed(
                request,
                sink,
                machine,
                "error",
                exc.to_error_info(),
                ledger,
            )
        # What the tools answered, before the runtime says anything on top of
        # it. The record a repeat is served from is taken here rather than
        # after the nudge below, so a replayed result never carries a "this is
        # call 25" sentence that was true of a different call.
        answered = tuple(zip(turn.calls, aligned, strict=True))
        if nudged:
            # Appended to the *message*, after the event was emitted from the
            # unaltered result, and the difference between the two is exactly
            # this sentence. `ToolCompleted.output_bytes` describes what the
            # tool answered, which is what an operator reading the log wants
            # to know about the tool; what the model was told on top of that
            # is the runtime's doing and is recorded here, in the runtime.
            #
            # Successful results only. An error result carries its text in
            # `error`, and `ToolResultBlock.from_tool_result` renders that
            # *only when `content` is empty* -- so appending to a refusal would
            # replace "invalid_tool_input: the snippet appears 0 times" with
            # the nudge and lose the refusal. The count still advanced, so the
            # next multiple lands on whatever that call is.
            aligned = tuple(
                result.model_copy(
                    update={
                        "content": result.content
                        + _nudge(result.tool_name, nudged[result.tool_call_id])
                    }
                )
                if result.tool_call_id in nudged and result.status == "ok"
                else result
                for result in aligned
            )
        ledger.messages.append(assistant_message(text=turn.text, tool_calls=turn.calls))
        ledger.messages.append(tool_message(aligned))
        # Only the admitted calls are charged. A call refused *because* the
        # ceiling was reached must not itself consume the ceiling, or the
        # ledger would report spending more than the budget allowed.
        ledger.usage = ledger.usage.merged(BudgetUsage(tool_calls=len(admitted)))

        # The record a later repeat is answered from (ADR-0116), written
        # *after* this batch's version bump so that a call's own effect never
        # counts against its own repeat: `run pytest` proposed twice in a row
        # is answered from the record the second time, while `run pytest`,
        # `edit`, `run pytest` is dispatched twice, because the edit sits
        # between them. Everything answered this batch is recorded --
        # replays too, so the count of replays keeps a record to count
        # against -- except a failure that says it may be retried, which is
        # the one answer a model is right to ask for again.
        if any(
            self._gateway.risk_of(candidate.call.tool_name) != "read"
            for candidate in authorized
        ):
            ledger.world_version += 1
        for call, result in answered:
            if result.error is not None and result.error.retryable:
                continue
            ledger.answered[_call_signature(call)] = (ledger.world_version, result)
        return None

    async def _run_group(
        self,
        group: tuple[PreparedCall, ...],
        *,
        request: AgentRunRequest,
        context: ExecutionContext,
        cancellation: CancellationToken,
        sink: EventSink,
    ) -> tuple[ToolResult, ...]:
        """Run one group, concurrently when it holds more than one call."""

        # Recomputed per group: a slow group leaves less for the next, and the
        # run's deadline bounds every one of them.
        budget = remaining_run_seconds(request.budget.deadline, now=self._clock())

        async def invoke(prepared: PreparedCall) -> ToolResult:
            return await self._gateway.invoke(
                prepared,
                context=context,
                cancellation=cancellation,
                sink=sink,
                run_budget_seconds=budget,
            )

        if len(group) == 1:
            return (await invoke(group[0]),)
        return tuple(await asyncio.gather(*(invoke(prepared) for prepared in group)))

    async def _refuse_cancelled(
        self,
        group: tuple[PreparedCall, ...],
        *,
        sink: EventSink,
    ) -> tuple[ToolResult, ...]:
        return tuple(
            [
                await self._gateway.refuse(
                    prepared.call,
                    ErrorInfo(
                        code="cancelled",
                        message="the run was cancelled before this call ran",
                    ),
                    sink=sink,
                )
                for prepared in group
            ]
        )

    def _label_for(self, profile: str) -> str:
        """Which model this call actually reaches.

        `ModelStarted.model_id` names the model that answered, not the one this
        process was configured around.
        """

        if profile == "compact" and self._compact_model_label is not None:
            return self._compact_model_label
        return self._model_label

    async def _compacted(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        ledger: _RunLedger,
        cancellation: CancellationToken,
    ) -> int | None:
        """Shorten this run's conversation, or answer that it could not be.

        Answers the estimated size of the shortened conversation, in the units
        the ceiling is expressed in, so the caller can carry a real number into
        the next turn instead of clearing the one that stopped it.

        ``None`` on every path that did not produce a shorter conversation
        *with* an account of what was removed, and the caller then stops the
        run the way ADR-080 stopped it before this existed. There is no middle
        outcome on purpose: dropping messages without a summary would leave the
        model continuing from a history with a hole in it that neither it nor
        the reader could see.

        The summary is produced by an ordinary model call -- same `ModelPort`,
        same deadline, same cancellation, same `ModelStarted`/`ModelCompleted`
        pair -- because a private "just call the provider" helper would put a
        request outside this run's own accounting, and the recorded cost would
        stop matching the bill.
        """

        if not self._compaction_enabled:
            return None
        if ledger.compactions >= MAX_COMPACTIONS_PER_RUN:
            # A backstop, not a policy. Each compaction removes the middle, so
            # the conversation shrinks and `plan_compaction` eventually refuses
            # -- but a run that has shortened itself three times and is still
            # over the line is not going to be rescued by a fourth, and the
            # honest answer is the ceiling it keeps hitting.
            return None
        plan = plan_compaction(ledger.messages)
        if plan is None:
            return None

        machine.to("compacting")
        before_chars = conversation_chars(ledger.messages)
        tokens_before = ledger.last_input_tokens
        # A separate ledger, so the transcript being summarised cannot be
        # confused with the conversation being repaired: `_stream_model` reads
        # its messages from whichever ledger it is handed.
        asking = _RunLedger(messages=[user_message(render_for_summary(plan.removed))])
        turn = await self._stream_model(
            request.model_copy(
                update={
                    # The profile that exists for exactly this (ADR-081 §2.1);
                    # `[model.compact]` has been configurable since the schema
                    # had a `[model]` section.
                    "model_profile": "compact",
                    "system_prompt": COMPACTION_PROMPT,
                    # A summariser has nothing to reason about, and a run that
                    # pinned thinking off for its own turns did not ask to pay
                    # for it here.
                    "thinking": False,
                }
            ),
            sink,
            asking,
            (),
            cancellation,
        )
        # Metered even when it fails: the call was made and the provider
        # charged for it, and a run whose recorded spend omits its failed
        # compaction is a run whose cost ceiling is wrong by that much.
        #
        # `steps` stays at zero. A step is a turn of the agent loop, and the
        # loop did not advance -- counting this would let a run near its step
        # ceiling be halted by the thing that was trying to save it.
        ledger.usage = ledger.usage.merged(
            BudgetUsage(
                tokens=turn.usage,
                cost_micro_usd=self._priced(turn.usage),
            )
        )
        summary = turn.text.strip()
        if turn.error is not None or not summary:
            return None

        shortened = plan.rebuilt(summary)
        tokens_after = scaled_tokens_after(
            tokens_before,
            chars_before=before_chars,
            chars_after=conversation_chars(shortened),
        )
        # The number that says whether this worked, checked instead of merely
        # published. `plan_compaction` cuts on message *count*, so the span it
        # removed can be four short turns while the tail it kept holds the
        # 60 KB tool result that is the actual problem. Returning success there
        # cleared the caller's ceiling for a conversation that had not got
        # smaller -- measured, three compactions reporting 0.03% saved, three
        # requests sent at 70,000 tokens against a 64,000-token window, and the
        # HTTP 400 ADR-080 exists to stop transcribing.
        #
        # Refusing here costs the summariser call that was already made. That
        # is the right trade: the alternative spends it *and* the two requests
        # after it, and ends somewhere nobody can attribute.
        if (
            context_reason_for(
                tokens_after,
                window_tokens=self._context_window_tokens,
                soft_limit_ratio=self._context_soft_limit_ratio,
            )
            is not None
        ):
            return None

        ledger.messages = shortened
        ledger.compactions += 1
        await sink.emit(
            ContextCompacted(
                removed_message_count=len(plan.removed),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                # No artifact. `ArtifactRef` carries a tenant and a content
                # digest and is minted only by the artifact store under
                # `tenant_filter_required`; this runtime holds no store and no
                # principal, so a ref built here would be a fabricated one.
                summary_ref=None,
            )
        )
        return tokens_after

    async def _failed(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        stop_reason: StopReason,
        error: ErrorInfo,
        ledger: _RunLedger,
    ) -> AgentOutcome:
        machine.to("failed")
        await sink.emit(
            RunFailed(error=error, stop_reason=stop_reason, usage=ledger.usage)
        )
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="failed",
            stop_reason=stop_reason,
            output_text=ledger.answer,
            error=error,
            usage=ledger.usage,
        )

    async def _cancelled(
        self,
        request: AgentRunRequest,
        sink: EventSink,
        machine: RunStateMachine,
        ledger: _RunLedger,
    ) -> AgentOutcome:
        machine.to("cancelled")
        await sink.emit(RunCancelled(usage=ledger.usage))
        return AgentOutcome(
            agent_run_id=request.trace.agent_run_id,
            status="cancelled",
            stop_reason="cancelled",
            output_text=ledger.answer,
            usage=ledger.usage,
        )


__all__ = [
    "DEFAULT_MODEL_LABEL",
    "MAX_OUTPUT_TEXT",
    "TRUNCATION_MARKER",
    "ClaudeLikeAgentRuntime",
]
