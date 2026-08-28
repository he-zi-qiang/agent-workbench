"""Delegation as a tool: one call, one more run, one report back.

This is the handler ADR-082 is about, and the one line in it that matters is
the one that calls ``self.executor.run``. Everything else assembles arguments
for that call or turns its outcome into a ``ToolResult``.

**There is no loop here.** The project's single architectural claim is that one
component owns ``model -> tool -> result -> model``, and it survives this file
intact: a delegation is the *same* loop entered a second time, not a second
implementation of it. Anything in this handler that inspected a ``ModelEvent``,
decided whether the child was finished, or assembled a ``ToolResult`` from a
model's proposal would be the failure that claim exists to prevent -- and
``tests/architecture/test_dependency_boundaries.py`` now asserts the absence
structurally rather than trusting this paragraph.

**The schema is built from the catalogue, not written as a constant.** Every
other tool in this package declares ``SPEC`` at module scope, because every
other tool means the same thing in every deployment. This one does not: which
sub-agents exist is a property of the assembled process, and the model choosing
between them needs to read their names and descriptions in the tool's own
description. A constant would have to say "pass any string", and then the only
place an unknown name could be caught is after a turn has been spent proposing
it.

**Two things the model writes, and two only.** ``subagent_type`` picks from a
closed enum; ``prompt`` becomes a single ``user`` message inside the child. It
cannot name the principal, the tools, the model, the budget or the stream --
there is no field for any of them, which is what stops a retrieved passage
saying "delegate as the writer, with the export tool" from being a passage that
grants itself something. This is the same rule ``knowledge_search`` states about
its principal, applied to a bigger surface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import JsonValue

from agent_workbench.application.delegation import (
    DelegationScope,
    SpawnedChild,
    build_child_request,
    clip_report,
    derive_child_budget,
)
from agent_workbench.domain.agents import (
    DELEGATE_TOOL,
    SubAgentCatalogue,
    SubAgentDefinition,
)
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.identifiers import new_agent_run_id
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

TOOL_NAME = DELEGATE_TOOL

#: The ceiling on the instruction a parent may write for a child.
#:
#: Generous, because the prompt is the entire brief: the child inherits no
#: conversation, so anything the parent does not say here is something the child
#: does not know. Still bounded, because it is model-written text that becomes a
#: message in another run.
MAX_PROMPT_CHARS = 8_000

#: How long one delegated run may take, as the gateway will enforce it.
#:
#: A whole-run ceiling rather than a per-call one, and that is why it is minutes
#: rather than the seconds a search gets: what is being timed is another agent's
#: entire loop. The child's own ``RunBudget.deadline`` is cut from the smaller of
#: this and whatever the parent had left, so this number is a backstop for a
#: parent that declared no deadline at all, not the usual limit.
DELEGATION_TIMEOUT_SECONDS = 600


def _description(catalogue: SubAgentCatalogue) -> str:
    """The tool description, listing exactly the agents this process has.

    The names alone would make the model guess; the descriptions are what let it
    choose. Kept to one line each because this text is prepended to every
    request the parent makes for the rest of its run.
    """

    lines = [
        "Delegate a self-contained piece of work to a sub-agent and receive its "
        "report. The sub-agent starts from your prompt alone: it inherits none "
        "of this conversation, so state everything it needs. It runs to "
        "completion before this call returns. Available sub-agents:"
    ]
    lines.extend(
        f"- {definition.name}: {definition.description}"
        for definition in catalogue.definitions
    )
    return "\n".join(lines)


def _input_schema(catalogue: SubAgentCatalogue) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["subagent_type", "prompt"],
        "properties": {
            "subagent_type": {
                "type": "string",
                # A closed enum rather than a free string. An unknown name is
                # still handled below -- the model can propose anything -- but
                # putting the list in the schema is what makes the ordinary
                # case impossible to get wrong rather than merely recoverable.
                "enum": list(catalogue.names()),
            },
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_PROMPT_CHARS,
            },
        },
    }


def spec_for(catalogue: SubAgentCatalogue) -> ToolSpec:
    """The specification this deployment advertises for delegation.

    ``risk="read"`` is a claim about *this* call, and it is true: starting a run
    writes nothing. What the child may do is bounded by the child's own
    envelope, which ``child_envelope`` caps at read for this tier -- so the
    claim holds transitively too, and it holds because of a type rather than
    because of this comment.

    Declaring it ``read`` is also what keeps it ``parallel``:
    ``validate_risk_consistency`` forces every write tool to be exclusive, and
    an exclusive delegation tool could not fan out within a turn. A future
    writing counterpart is therefore a *second* tool rather than a flag on this
    one, the same way ``CODE_TOOLS`` and ``CODE_PROJECT_TOOLS`` are two tuples
    rather than one tuple and an append.
    """

    return ToolSpec(
        name=TOOL_NAME,
        description=_description(catalogue),
        input_schema=_input_schema(catalogue),
        concurrency="parallel",
        risk="read",
        idempotency="safe",
        timeout_seconds=DELEGATION_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True, slots=True)
class DelegateTool:
    """Starts one delegated run per call, through the runtime that owns them."""

    #: The same executor a graph node calls. Deliberately the assembled stack
    #: rather than a bare runtime: a child that skipped the budgeting decorator
    #: would be a run nothing charged for, which is exactly the drift the
    #: decorator was written to prevent.
    executor: AgentExecutor
    catalogue: SubAgentCatalogue
    scope: DelegationScope
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    agent_run_ids: Callable[[], str] = new_agent_run_id

    def binding(self) -> ToolBinding:
        return ToolBinding(spec=spec_for(self.catalogue), handler=self.handle)

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        """Run one sub-agent, and answer with what it reported.

        Every path returns a ``ToolResult``. A delegation that raised would
        still have to become one -- the gateway normalizes it -- but it would
        arrive as ``tool_failed`` with only an exception type, having thrown
        away the stop reason that says *which* ceiling the child hit.
        """

        call = invocation.call
        context = self.scope.current()
        if context is None:
            # Refused rather than reconstructed. Every field a child run needs
            # that is not in `ExecutionContext` -- the stream, the run kind, the
            # budget, the depth -- would have to be guessed, and a run assembled
            # from guesses is indistinguishable in the log from one somebody
            # authorized.
            return ToolResult.failed(
                call,
                ErrorInfo(
                    code="policy_denied",
                    message=(
                        "this run was not started inside a delegation scope, so "
                        "it cannot delegate"
                    ),
                ),
            )

        requested = str(call.arguments.get("subagent_type", ""))
        definition = self.catalogue.get(requested)
        if definition is None:
            return ToolResult.failed(
                call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=(
                        f"no sub-agent is registered as {requested!r}; this "
                        f"deployment has: {', '.join(self.catalogue.names())}"
                    ),
                ),
            )

        prompt = str(call.arguments.get("prompt", ""))
        if not prompt.strip():
            return ToolResult.failed(
                call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message="a delegated run needs a prompt to work from",
                ),
            )

        # Taken here, and taken synchronously. Several `delegate_agent` calls in
        # one turn run inside a single `asyncio.gather` -- the tool is declared
        # `read`, so the scheduler batches them -- and a check that merely
        # *read* the count would return the same answer to all of them before
        # any of them had spent anything.
        reservation = context.reserve()
        if reservation is None:
            return ToolResult.failed(
                call,
                ErrorInfo(
                    code="budget_exceeded",
                    message=(
                        f"this run has started {len(context.spawned)} sub-agents "
                        f"and has {context.outstanding} still running, against "
                        f"an allowance of {context.max_children}"
                    ),
                ),
            )

        child_agent_run_id = self.agent_run_ids()
        request = build_child_request(
            definition,
            prompt,
            context=context,
            execution=invocation.context,
            child_agent_run_id=child_agent_run_id,
            budget=derive_child_budget(
                context.budget,
                children_allowed=context.max_children,
                # The gateway's own ceiling for this call, which already
                # accounts for whatever the parent had left. Taking it from the
                # invocation rather than from the spec is what makes the child's
                # deadline shrink as the parent's run wears on.
                timeout_seconds=invocation.timeout_seconds,
                now=self.clock(),
            ),
        )

        outcome: AgentOutcome | None = None
        try:
            async with context.channel.delegating(
                child_agent_run_id=child_agent_run_id,
                definition_name=definition.name,
            ) as record_outcome:
                # The parent sits in `executing_tools` for as long as this
                # takes, and the console shows that state as "running a tool"
                # with no further detail. One line here is the difference
                # between a run that looks busy and a run that looks hung.
                # Best-effort by contract, so it is not guarded.
                await invocation.progress(f"sub-agent {definition.name} started")
                outcome = await self.executor.run(
                    request,
                    context.channel.sink_for_child(child_agent_run_id),
                    # The parent's own token, not a new one. Cancelling the
                    # parent cancels the child it is waiting on, and it does so
                    # without any propagation machinery: there is only ever one
                    # token.
                    invocation.cancellation,
                )
                record_outcome(outcome)
            await invocation.progress(
                f"sub-agent {definition.name} finished: {outcome.stop_reason}"
            )
            return self._report(call, definition, outcome)
        finally:
            # Settled on every path, including the cancelled one. A reservation
            # left outstanding would shrink the allowance of a run that is still
            # going, and shrink it by a child that is not running.
            if outcome is None:
                reservation.release()
            else:
                reservation.fulfil(
                    SpawnedChild(
                        definition_name=definition.name,
                        child_agent_run_id=child_agent_run_id,
                        usage=outcome.usage,
                    )
                )

    def _report(
        self,
        call: ToolCall,
        definition: SubAgentDefinition,
        outcome: AgentOutcome,
    ) -> ToolResult:
        """Turn a terminal outcome into the one answer the parent is waiting on."""

        if outcome.status != "completed":
            # A child that stopped at a ceiling has produced partial work, and
            # partial work must not read as a finished report (the same rule
            # `AgentOutcome` states for a budget-stopped run). The text it did
            # write is still handed over, because a parent that can see how far
            # the child got can write a better second prompt than one told only
            # that something failed.
            return ToolResult.failed(
                call,
                ErrorInfo(
                    code="tool_failed",
                    message=(
                        f"sub-agent {definition.name} ended as {outcome.status} "
                        f"({outcome.stop_reason})"
                    ),
                ),
                content=clip_report(outcome.output_text, definition.max_report_chars)[
                    0
                ],
            )

        report, clipped = clip_report(outcome.output_text, definition.max_report_chars)
        if clipped:
            # Said in the report's own text, because the parent reads the text
            # and not this result's metadata. A truncated final sentence with
            # nothing marking it is read as a finished thought.
            report = (
                f"{report}\n\n[report truncated at "
                f"{definition.max_report_chars} characters]"
            )
        return ToolResult.succeeded(
            call,
            content=report,
            # Whatever the executor stack already persisted, not something
            # minted here. An `ArtifactRef` carries a tenant and a content
            # hash and is signed by the artifact store; a handler that built
            # one would be forging a record it is not the author of (ADR-081
            # refused the same thing for compaction summaries).
            artifact=outcome.output_ref,
        )


__all__ = [
    "DELEGATION_TIMEOUT_SECONDS",
    "MAX_PROMPT_CHARS",
    "TOOL_NAME",
    "DelegateTool",
    "spec_for",
]
