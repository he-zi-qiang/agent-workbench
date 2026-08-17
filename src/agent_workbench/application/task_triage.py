"""Deciding a submission's shape before there is a submission (ADR-036).

One toolless structured run answers two questions the create form used to ask
a human every time: which pipeline fits this objective, and should the result
end in a downloadable file. The verdict is a *proposal* -- the client turns it
into an explicit submission, so nothing downstream of ``POST /v1/tasks``
changes shape, freezes differently, or compares idempotency against a value a
model produced.

The three outcomes are deliberately asymmetric in what they cost:

* ``decided`` costs nothing further -- the client submits the proposal;
* ``ask`` is returned only when the *graph* is uncertain, because a wrong
  graph runs the entire wrong pipeline (ADR-031's own argument) while a wrong
  ``wants_report`` costs one resubmission. Uncertainty about the file
  therefore resolves to ``False`` here rather than to a second question:
  approval rejection is terminal (both graphs fail the Task), so hedging
  toward ``True`` would force "approve or fail" on somebody who may never
  have wanted a file;
* ``default`` is every failure -- timeout, provider error, unreadable output
  -- and means "submit exactly what you would have submitted before this
  service existed". Triage must never be the reason a Task cannot be opened.

Decoding shares ADR-034's boundary via ``workflows.structured_output``: only
a framing failure earns one corrective turn; an answer that parses but says
something the contract does not allow is a claim the model made and got
wrong, and is not re-asked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from agent_workbench.application.tasks import TaskGraphChoice
from agent_workbench.domain.identifiers import Identifier, new_agent_run_id, new_id
from agent_workbench.domain.messages import Message, user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import AgentRunRequest, RunBudget, TraceContext
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.workflows.structured_output import (
    StructuredOutputError,
    StructuredOutputFramingError,
    json_object,
    restatement_messages,
)

TriageStatus = Literal["decided", "ask", "default"]

#: The stream prefix for triage runs. The events land in a per-call in-memory
#: log and are discarded with it: a triage run is pre-submission by
#: definition, so there is no Task timeline to write to and no session a
#: subscriber could follow. What survives is the verdict, and the verdict's
#: durable record is the ``intent`` block on ``TaskSubmitted``.
TRIAGE_STREAM_PREFIX: Final[str] = "triage"

#: One completion, no tools. The corrective turn, when earned, is a second
#: run under the same ceiling rather than a wider first one.
_TRIAGE_BUDGET: Final[RunBudget] = RunBudget(max_steps=1, max_tool_calls=1)

_TRIAGE_CONTRACT: Final[str] = (
    "You classify one task objective before submission. Return exactly one "
    "JSON object and no Markdown, prose, or code fence: "
    '{"graph":"research|general|unsure","wants_report":true|false|"unsure",'
    '"reason":"...","question":"..."|null}. '
    '"research" runs a retrieval-grounded research-report pipeline: two '
    "researchers gather evidence, a writer synthesizes a cited report, a "
    "critic reviews it. Choose it when the objective asks for an "
    "investigation, comparison, survey, or evidence-backed write-up. "
    '"general" runs one tool-holding agent that decides its own steps and is '
    "reviewed on whether the goal is met. Choose it when the objective asks "
    "to do, produce, convert, fix, compute, or organize something. "
    'Answer "unsure" for graph only when the objective genuinely reads both '
    'ways; then "question" must ask the submitter which they meant, in the '
    "objective's own language, in one sentence. Otherwise question is null. "
    '"wants_report" is whether the submitter wants a downloadable document '
    'file as the deliverable; "unsure" is allowed. "reason" states your '
    "graph decision in one short sentence in the objective's own language."
)


class _TriageVerdict(BaseModel):
    """Exactly what the contract permits the model to say."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph: Literal["research", "general", "unsure"]
    wants_report: bool | Literal["unsure"]
    reason: str = Field(max_length=500)
    question: str | None = Field(default=None, max_length=500)


_VERDICT: Final[TypeAdapter[_TriageVerdict]] = TypeAdapter(_TriageVerdict)


@dataclass(frozen=True, slots=True)
class TriageResult:
    """One of the three outcomes, with only that outcome's fields set."""

    status: TriageStatus
    graph: TaskGraphChoice | None = None
    wants_report: bool | None = None
    reason: str | None = None
    question: str | None = None


DEFAULT_RESULT: Final[TriageResult] = TriageResult(status="default")

#: Shown when the model was unsure but did not phrase a usable question.
FALLBACK_QUESTION: Final[str] = "这个任务是要一份有依据的调研报告，还是直接把事做完？"


@dataclass(frozen=True, slots=True)
class TaskTriageService:
    """Propose a submission's shape, or say honestly that it could not."""

    executor: AgentExecutor
    #: Bounds the whole call including the corrective turn. Submission paths
    #: wait on this, so the ceiling is a promise to the create form, not a
    #: model courtesy.
    timeout_seconds: float
    #: Where this call's run events go, keyed by the minted stream id. Wired
    #: by the composition layer -- the application layer names no event-log
    #: adapter -- and expected to hand back a sink whose events are
    #: discardable. See TRIAGE_STREAM_PREFIX for why nothing durable listens.
    sink_for: Callable[[Identifier], EventSink]

    async def triage(
        self,
        principal: PrincipalContext,
        *,
        objective: str,
        knowledge_base_selected: bool = False,
        attachment_names: tuple[str, ...] = (),
    ) -> TriageResult:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._triage(
                    principal,
                    objective=objective,
                    knowledge_base_selected=knowledge_base_selected,
                    attachment_names=attachment_names,
                )
        except TimeoutError:
            return DEFAULT_RESULT
        except Exception:  # triage must never be why a Task cannot be opened
            return DEFAULT_RESULT

    async def _triage(
        self,
        principal: PrincipalContext,
        *,
        objective: str,
        knowledge_base_selected: bool,
        attachment_names: tuple[str, ...],
    ) -> TriageResult:
        stream_id = new_id(TRIAGE_STREAM_PREFIX)
        sink = self.sink_for(stream_id)
        question = user_message(
            _prompt(
                objective=objective,
                knowledge_base_selected=knowledge_base_selected,
                attachment_names=attachment_names,
            )
        )

        outcome = await self.executor.run(
            self._request(principal, stream_id=stream_id, messages=(question,)),
            sink,
            NullCancellationToken(),
        )
        if outcome.status != "completed":
            return DEFAULT_RESULT
        try:
            return _result(_decode(outcome.output_text))
        except StructuredOutputFramingError:
            pass
        except StructuredOutputError:
            return DEFAULT_RESULT

        # The one corrective turn a framing failure earns (ADR-034): the
        # unreadable answer replayed as the model's own turn, and the ask.
        corrected = await self.executor.run(
            self._request(
                principal,
                stream_id=stream_id,
                messages=(question, *restatement_messages(outcome.output_text)),
            ),
            sink,
            NullCancellationToken(),
        )
        if corrected.status != "completed":
            return DEFAULT_RESULT
        try:
            return _result(_decode(corrected.output_text))
        except StructuredOutputError:
            return DEFAULT_RESULT

    def _request(
        self,
        principal: PrincipalContext,
        *,
        stream_id: str,
        messages: tuple[Message, ...],
    ) -> AgentRunRequest:
        return AgentRunRequest(
            trace=TraceContext(agent_run_id=new_agent_run_id()),
            run_kind="chat",
            stream_id=stream_id,
            principal=principal,
            # Deny-shaped for the same reason the ungrounded chat run's is:
            # a classifier holds no tools, and no evidence is not more
            # freedom.
            envelope=AuthorizationEnvelope(),
            system_prompt=_TRIAGE_CONTRACT,
            messages=messages,
            budget=_TRIAGE_BUDGET,
            # Off, and this is the shape that needs it most (ADR-061). Nothing
            # displays a classifier's reasoning, the caller holds a ten-second
            # client deadline, and the output budget is sized for one small
            # JSON object -- reasoning would spend that budget and that clock
            # on text no reader will ever see, and a truncated verdict falls
            # back to the default silently.
            thinking=False,
        )


def _prompt(
    *,
    objective: str,
    knowledge_base_selected: bool,
    attachment_names: tuple[str, ...],
) -> str:
    lines = [f"Objective:\n{objective}"]
    lines.append(
        "A knowledge base is attached."
        if knowledge_base_selected
        else "No knowledge base is attached."
    )
    if attachment_names:
        names = ", ".join(attachment_names[:8])
        lines.append(f"Files being uploaded with it: {names}")
    return "\n\n".join(lines)


def _decode(text: str) -> _TriageVerdict:
    json_object(text)
    try:
        return _VERDICT.validate_json(text, strict=True)
    except ValidationError as error:
        raise StructuredOutputError("triage output has an invalid shape") from error


def _result(verdict: _TriageVerdict) -> TriageResult:
    if verdict.graph == "unsure":
        question = verdict.question if verdict.question else FALLBACK_QUESTION
        return TriageResult(status="ask", question=question)
    return TriageResult(
        status="decided",
        graph=verdict.graph,
        # "unsure" resolves to False, never to a second question. See the
        # module docstring for why the hedge points this way.
        wants_report=verdict.wants_report is True,
        reason=verdict.reason,
    )


__all__ = [
    "DEFAULT_RESULT",
    "FALLBACK_QUESTION",
    "TaskTriageService",
    "TriageResult",
    "TriageStatus",
]
