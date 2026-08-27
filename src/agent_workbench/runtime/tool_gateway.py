"""The single place a tool call can be stopped.

Native handlers, MCP tools and LangChain tools all arrive as the same binding
and pass through here, so there is one implementation of the question "may this
run, with these arguments, right now" instead of one per integration.

The gateway enforces the order the baseline requires: a handler runs only after
its final arguments have passed schema validation *and* an authorization
decision. "Final" is the load-bearing word, and two things can change what the
arguments are.

Hooks run first, before authorization, and may rewrite or block. Whatever they
produce is validated again, because a hook that could edit arguments after the
check would be a way around it.

A policy may then answer ``allow_with_modified_input``, and that rewrite is
validated and re-submitted for a decision for the same reason. That loop is
bounded: an engine that keeps rewriting is refused rather than run.

A decision that requires human approval is not a decision to run. Treating
"allow, pending approval" as "allow" is how a write tool performs an
irreversible effect that nobody agreed to, and no amount of later approval
machinery can undo an effect already dispatched.

So the call is held until somebody permits it, and refused if nobody does. The
holding needs somewhere to ask -- an approval gate, supplied only by a
deployment where the answer can reach the coroutine that is waiting. Where none
is supplied the call is refused exactly as it always was, and that is the
honest answer rather than a stub: a run whose approvals are recorded in another
process would be waiting for something that cannot arrive, holding whatever it
holds for as long as it waited. Which of the two a deployment gets is decided
at assembly, once, and never per call.

Everything the gateway consults is bounded and fails closed. A policy engine
that hangs used to hang the run, and one that raised sent its exception -- and
whatever a backend put in the message -- straight out through the caller, so a
run got an exception instead of a terminal outcome. Both now become an ordinary
refusal, and only the exception's type name crosses the boundary. The bound is
the smaller of the gateway's own timeout and whatever the run has left, because
a deadline that inner work can outlive is not a deadline.

It also owns the audit trail for a call. Proposal, permission, start,
completion and failure are emitted here, so the events cannot disagree with
what the gateway actually did. Argument bodies never appear in them -- a size
and a digest do.

The provenance of rewritten arguments belongs to the side-effect ledger, where
a retry has to key on exactly what ran. ``PermissionResolved`` records that a
rewrite happened; recording what it produced arrives with that ledger.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, get_args

from agent_workbench.domain.errors import (
    AgentWorkbenchError,
    ErrorCode,
    ErrorInfo,
    PolicyDeniedError,
    ToolInputInvalidError,
    UnknownToolError,
)
from agent_workbench.domain.events import (
    APPROVAL_PREVIEW_LIMIT,
    ApprovalDecidedBy,
    ApprovalDecision,
    PermissionRequested,
    PermissionResolved,
    RunPaused,
    ToolApprovalDecided,
    ToolCompleted,
    ToolFailed,
    ToolProposed,
    ToolStarted,
)
from agent_workbench.domain.identifiers import new_approval_id
from agent_workbench.domain.policies import ExecutionContext, PolicyDecision
from agent_workbench.domain.schema import bounded
from agent_workbench.domain.tools import (
    ToolCall,
    ToolResult,
    ToolRisk,
    ToolSpec,
    argument_digest,
    canonical_arguments,
)
from agent_workbench.ports.approval_gate import InteractiveApprovalGate
from agent_workbench.ports.cancellation import CancellationToken, NullCancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.policy import PolicyEngine
from agent_workbench.ports.tool_executions import (
    ToolExecutionIntent,
    ToolExecutionLedger,
    ToolExecutionNotWritableError,
    ToolOperationConflictError,
)
from agent_workbench.ports.tools import ToolBinding, ToolRegistry
from agent_workbench.runtime.hook_bus import HookBus
from agent_workbench.runtime.schema_validation import (
    assert_schema_supported,
    validate_arguments,
)
from agent_workbench.runtime.tool_executor import ToolExecutor

# Mirrors policy.max_tool_argument_bytes; bootstrap will pass the configured
# value once it projects settings into the runtime.
DEFAULT_MAX_ARGUMENT_BYTES: Final[int] = 65_536

# One rewrite is a clamp; a second is a negotiation. Beyond that the engine and
# the tool disagree, and the call is refused rather than retried forever.
DEFAULT_MAX_POLICY_ROUNDS: Final[int] = 3

# A policy engine is deployment-supplied code on the path of every call. It
# gets a bound of its own so that forgetting to pass the run's remaining time
# still leaves one, rather than leaving none.
DEFAULT_POLICY_TIMEOUT_SECONDS: Final[float] = 5.0

# How long a call may be held waiting for a person, when nothing else bounds it
# sooner. Minutes rather than seconds because the other side of this wait is
# somebody reading a proposed command and deciding, and it is capped anyway by
# whatever the run has left -- which, for the only run kind that supplies a
# gate, is required to exist.
DEFAULT_APPROVAL_TIMEOUT_SECONDS: Final[float] = 300.0

# The two vocabularies an approval gate may answer in, as sets rather than as
# types: the gate is deployment-supplied, so what it returns is data to be
# checked and not an annotation to be believed.
APPROVAL_DECISIONS: Final[frozenset[str]] = frozenset(get_args(ApprovalDecision))
APPROVAL_DECIDERS: Final[frozenset[str]] = frozenset(get_args(ApprovalDecidedBy))

# What the second authorization, taken one line from an external effect, calls
# each answer that is not a plain allow. A rewrite is spelled out rather than
# folded into "denied", because an operator reading it has a different thing to
# go and look at.
_REDECISION_REFUSALS: Final[Mapping[str, str]] = {
    "deny": "denied",
    "allow_with_modified_input": "no longer permitted with these arguments",
}

# The failures that carry no answer. For an external write these are the ones
# where the request may have landed after the deadline passed, so they become a
# human's problem rather than a retry's. Every other failure was reported by the
# handler itself and is recorded as what it says it is.
AMBIGUOUS_DISPATCH_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {"tool_timeout", "cancelled", "budget_exceeded"}
)


#: What a preview says when it is not all of it.
_TRUNCATED: Final[str] = "...[truncated]"


def _approval_preview(canonical: str) -> str:
    """The arguments as far as they fit, and a mark when they did not.

    A preview cut without a sign is worse than a short one: the person
    approving reads it as the whole request and cannot tell that the tail --
    the redirect, the second path, the ``--force`` -- was removed by a length
    limit rather than absent from the call.

    The identity of the arguments is untouched by this. ``ToolProposed``
    carries a digest taken over the whole of them and their true length, so a
    reader who needs to know exactly what ran still has both.
    """

    if len(canonical) <= APPROVAL_PREVIEW_LIMIT:
        return canonical
    return canonical[: APPROVAL_PREVIEW_LIMIT - len(_TRUNCATED)] + _TRUNCATED


def _discard_outcome(
    task: asyncio.Task[tuple[ApprovalDecision, ApprovalDecidedBy]],
) -> None:
    """Retrieve whatever an abandoned task ended with, and drop it.

    ``Task.exception()`` is what marks the failure as seen; without the call,
    asyncio's default handler logs it -- traceback, message and all -- when
    the task is garbage collected.
    """

    if not task.cancelled():
        task.exception()


@dataclass(frozen=True, slots=True)
class PreparedCall:
    """A call that has cleared one stage and may enter the next."""

    binding: ToolBinding
    call: ToolCall


class ToolGateway:
    """Validates, authorizes, runs and records one tool call at a time."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        executor: ToolExecutor | None = None,
        hooks: HookBus | None = None,
        ledger: ToolExecutionLedger | None = None,
        max_argument_bytes: int = DEFAULT_MAX_ARGUMENT_BYTES,
        max_policy_rounds: int = DEFAULT_MAX_POLICY_ROUNDS,
        policy_timeout_seconds: float = DEFAULT_POLICY_TIMEOUT_SECONDS,
        record_step_inputs: bool = False,
        approvals: InteractiveApprovalGate | None = None,
        approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        if max_policy_rounds < 1:
            raise ValueError("max_policy_rounds must be positive")
        if policy_timeout_seconds <= 0:
            raise ValueError("policy_timeout_seconds must be positive")
        if approval_timeout_seconds <= 0:
            raise ValueError("approval_timeout_seconds must be positive")
        # Every registered schema is checked once, here. A tool whose schema
        # this validator cannot enforce stops the process now rather than
        # silently accepting anything later.
        for spec in registry.specs():
            assert_schema_supported(spec.input_schema, origin=f"tool {spec.name}")

        # A registry holding a ledgered tool without a ledger to record it in
        # is a deployment that would dispatch external effects nothing accounts
        # for. Refused at assembly rather than per call: a process that cannot
        # honour the protocol should not start, rather than start and refuse
        # one tool at a time once somebody is depending on it.
        if ledger is None:
            unrecorded = sorted(
                spec.name
                for spec in registry.specs()
                if (binding := registry.get(spec.name)) is not None
                and binding.operation_key is not None
            )
            if unrecorded:
                raise ValueError(
                    "these tools record external effects but no ledger was "
                    f"supplied: {', '.join(unrecorded)}"
                )

        self._registry = registry
        self._policy = policy
        self._ledger = ledger
        self._executor = executor if executor is not None else ToolExecutor()
        self._hooks = hooks if hooks is not None else HookBus()
        self._max_argument_bytes = max_argument_bytes
        self._max_policy_rounds = max_policy_rounds
        self._policy_timeout_seconds = policy_timeout_seconds
        # ADR-019. Off unless a deployment opted in; the digest and byte count
        # below are emitted either way, so nothing downstream depends on this.
        self._record_step_inputs = record_step_inputs
        # Optional, and its absence is a statement rather than a gap: a
        # deployment that cannot deliver an answer to a parked coroutine must
        # keep refusing these calls instead of waiting for one that cannot
        # come. See ``ports/approval_gate.py``.
        self._approvals = approvals
        self._approval_timeout_seconds = approval_timeout_seconds

    def knows(self, name: str) -> bool:
        """Whether this process registers a tool by that name.

        Exists so a caller can tell "no such tool" from "not this run's tool"
        without reaching into the registry. The two are different answers and
        deserve different error codes: the first is a name the model invented,
        the second is a profile that does not carry something it needed.
        """

        return self._registry.get(name) is not None

    def advertise(self, names: Sequence[str]) -> tuple[ToolSpec, ...]:
        """Specifications for the tools a run may use.

        Raises when a run asks for a tool this process does not register: the
        model must never be shown a tool the gateway would refuse to resolve.

        And raises when a run asks for a *ledgered* one. A ledgered tool is an
        external effect that cannot be undone, and the ledger's protection --
        one effect per ``operation_key`` -- is only as good as the key, which
        the tool derives from the arguments and the context it is handed. That
        derivation is answerable for a call a deterministic node issued; it has
        nothing to be answerable with for a call a model invented, because
        nothing in a run distinguishes "the same intent, replayed" from "a new
        intent that happens to look identical" (ADR-075). So the rule is
        positional rather than a judgement: a ledgered effect is *issued* by a
        node that meant it, the way ``export_artifact`` is, and is never put in
        front of a model as a choice.

        Nothing in this repository is refused by this today -- no profile names
        a ledgered tool -- and that is the intended shape. It replaces a
        guardrail that used to exist by accident: until the trace carried a
        lease epoch, every ledgered tool a model proposed was refused deeper
        down for want of a fence, which looked like a decision and was an
        omission.

        Two different refusals, and they keep two different codes on purpose.
        A name this process does not register is `unknown_tool` -- the model,
        or the profile, asked for something that is not here. A name it does
        register but may not offer is `policy_denied`: the tool exists, the
        deployment built it, and a rule says it is not the model's to call.
        Reading `unknown_tool` for the second one would send whoever is
        debugging it to look for a missing registration.
        """

        specs: list[ToolSpec] = []
        for name in names:
            binding = self._registry.get(name)
            if binding is None:
                raise UnknownToolError(
                    f"the run requested a tool this process does not register: {name}"
                )
            if binding.operation_key is not None:
                raise PolicyDeniedError(
                    "this tool records an external effect and is issued by a "
                    f"graph node, never offered to a model: {name}"
                )
            specs.append(binding.spec)
        return tuple(specs)

    async def propose(self, call: ToolCall, *, sink: EventSink) -> None:
        """Record what the model asked for, before anything judges it."""

        canonical = canonical_arguments(call.arguments)
        await sink.emit(
            ToolProposed(
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                argument_bytes=len(canonical.encode("utf-8")),
                argument_sha256=argument_digest(call.arguments),
                # The canonical form, so what the reader sees is the same string
                # the digest was taken over rather than a re-serialization of it.
                argument_preview=bounded(canonical) if self._record_step_inputs else "",
                risk=self._risk_of(call),
            )
        )

    async def prepare(
        self,
        call: ToolCall,
        *,
        context: ExecutionContext,
        sink: EventSink,
        remaining_run_seconds: float | None = None,
    ) -> PreparedCall | ToolResult:
        """Resolve the tool, check its arguments, and let hooks shape them."""

        binding = self._registry.get(call.tool_name)
        if binding is None:
            return await self.refuse(
                call,
                ErrorInfo(
                    code="unknown_tool",
                    message=f"no tool named {call.tool_name}",
                ),
                sink=sink,
            )

        rejected = self._check(binding, call)
        if rejected is not None:
            return await self.refuse(call, rejected, sink=sink)

        if self._hooks.is_empty:
            return PreparedCall(binding=binding, call=call)

        outcome = await self._hooks.before_tool(
            call, context, remaining_run_seconds=remaining_run_seconds
        )
        if outcome.blocked:
            return await self.refuse(
                call,
                ErrorInfo(
                    code="policy_denied",
                    message=f"blocked by hook {outcome.blocked_by}: {outcome.reason}",
                ),
                sink=sink,
            )

        if outcome.rewritten:
            # Whatever a hook produced is input like any other, and is checked
            # like any other.
            rejected = self._check(binding, outcome.call)
            if rejected is not None:
                return await self.refuse(call, rejected, sink=sink)

        return PreparedCall(binding=binding, call=outcome.call)

    def _check(self, binding: ToolBinding, call: ToolCall) -> ErrorInfo | None:
        """Size and schema, applied to whatever the arguments currently are."""

        size = len(canonical_arguments(call.arguments).encode("utf-8"))
        if size > self._max_argument_bytes:
            return ErrorInfo(
                code="invalid_tool_input",
                message=(
                    f"arguments are {size} bytes, over the "
                    f"{self._max_argument_bytes} byte ceiling"
                ),
            )
        return self._validate(binding, call)

    async def authorize(
        self,
        prepared: PreparedCall,
        *,
        context: ExecutionContext,
        sink: EventSink,
        remaining_run_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
    ) -> PreparedCall | ToolResult:
        """Decide the call, re-checking any arguments the policy rewrites.

        ``cancellation`` is optional and defaults to a token that never fires,
        so the two adapter callers keep working -- but a caller that omits it
        while an approval gate is configured is asking this phase to hold a
        run that can no longer be stopped.
        """

        stop = cancellation if cancellation is not None else NullCancellationToken()
        call = prepared.call
        approval_reason: str | None = None
        for _ in range(self._max_policy_rounds):
            verdict = await self._decide(call, context, remaining_run_seconds)
            if isinstance(verdict, ErrorInfo):
                return await self.refuse(call, verdict, sink=sink)
            decision = verdict
            await sink.emit(
                PermissionResolved(
                    tool_call_id=call.tool_call_id,
                    effect=decision.effect,
                    reason_code=decision.reason_code,
                )
            )

            if decision.effect == "deny":
                return await self.refuse(
                    call,
                    ErrorInfo(
                        code="policy_denied",
                        message=f"denied: {decision.reason_code}",
                    ),
                    sink=sink,
                )

            if decision.requires_approval:
                # Remembered here, asked below. A decision may require approval
                # *and* rewrite the arguments, and asking at this point would
                # put one call in front of a human and dispatch another: the
                # rewrite happens after, and a later round can rewrite again.
                #
                # Sticky, because a requirement is not lifted by a subsequent
                # round that neglects to repeat it -- that is precisely the
                # rewrite carrying a call past its own approval requirement.
                approval_reason = decision.reason_code

            if decision.effect == "allow":
                # The arguments have stopped moving, so this is the first
                # moment the question can be about what will actually run.
                if approval_reason is not None:
                    refusal = await self._await_approval(
                        prepared.binding,
                        call,
                        reason_code=approval_reason,
                        sink=sink,
                        remaining_run_seconds=remaining_run_seconds,
                        cancellation=stop,
                    )
                    if refusal is not None:
                        return refusal
                return PreparedCall(binding=prepared.binding, call=call)

            # allow_with_modified_input: the rewritten arguments are not yet
            # trusted. They go back through the *whole* check -- size as well
            # as schema -- and through the policy engine before anything runs
            # on them. Re-running only the schema let a rewrite deliver
            # arguments the original call could never have carried, which made
            # the byte ceiling something a policy could opt out of.
            rewritten = call.model_copy(
                update={"arguments": decision.modified_input or {}}
            )
            invalid = self._check(prepared.binding, rewritten)
            if invalid is not None:
                return await self.refuse(call, invalid, sink=sink)
            call = rewritten

        return await self.refuse(
            call,
            ErrorInfo(
                code="policy_denied",
                message=(
                    "the policy engine kept rewriting the arguments after "
                    f"{self._max_policy_rounds} rounds"
                ),
            ),
            sink=sink,
        )

    async def _decide(
        self,
        call: ToolCall,
        context: ExecutionContext,
        remaining_run_seconds: float | None,
    ) -> PolicyDecision | ErrorInfo:
        """Ask the policy engine, bounded, and never let it throw.

        A policy engine is deployment-supplied code sitting on the path of
        every call. Letting it hang held the run past its own deadline, and
        letting it raise sent the exception -- and whatever a backend wrote
        into the message, which has included a DSN -- out through a caller that
        was promised a terminal outcome. Both are refusals here, and only the
        exception's type name crosses the boundary.
        """

        bound = self._policy_timeout_seconds
        if remaining_run_seconds is not None:
            bound = min(bound, remaining_run_seconds)
        if bound <= 0:
            return ErrorInfo(
                code="policy_denied",
                message="the run had no time left to reach a policy decision",
            )

        try:
            async with asyncio.timeout(bound):
                return await self._policy.decide(call, context)
        except TimeoutError:
            return ErrorInfo(
                code="policy_denied",
                message=f"the policy engine exceeded its {bound:g}s bound",
            )
        except AgentWorkbenchError as exc:
            # Our own errors already carry a vetted message.
            return exc.to_error_info()
        except Exception as exc:
            return ErrorInfo(
                code="policy_denied",
                message=f"the policy engine raised {type(exc).__name__}",
            )

    async def _await_approval(
        self,
        binding: ToolBinding,
        call: ToolCall,
        *,
        reason_code: str,
        sink: EventSink,
        remaining_run_seconds: float | None,
        cancellation: CancellationToken,
    ) -> ToolResult | None:
        """Get the call permitted, or answer it. ``None`` means permitted.

        Two shapes, chosen by whether this deployment has anywhere to ask.

        With no gate it records the request and refuses, which is what this
        did before there was such a thing as asking. The request is emitted
        first either way, so the audit trail says what was actually wanted:
        not that the call was forbidden, but that nobody was there to permit
        it.

        With a gate it holds the call. The bound is the smaller of this
        gateway's own allowance and whatever the run has left, the same
        composition the policy engine gets, and it is raced against the run's
        cancellation -- otherwise a cancelled run would keep waiting for a
        human who is no longer being shown anything, for the whole bound, once
        per call that needs one.
        """

        if self._approvals is None:
            await sink.emit(
                PermissionRequested(
                    tool_call_id=call.tool_call_id,
                    required_scopes=binding.spec.permission_scopes,
                    risk=binding.spec.risk,
                )
            )
            return await self.refuse(
                call,
                ErrorInfo(
                    code="approval_required",
                    message=(
                        f"{call.tool_name} requires human approval "
                        f"({reason_code}), and no approval facility exists yet"
                    ),
                ),
                sink=sink,
            )

        approval_id = new_approval_id()
        canonical = canonical_arguments(call.arguments)
        # Computed once and used twice -- on the event below and in the
        # question handed to the gate. Deriving it separately at each site
        # would let the record of what was asked and the thing a person read
        # drift apart under any future change to the bound or the marker, and
        # the whole point of the field is that those two are the same string.
        preview = _approval_preview(canonical)
        await sink.emit(
            PermissionRequested(
                tool_call_id=call.tool_call_id,
                required_scopes=binding.spec.permission_scopes,
                risk=binding.spec.risk,
                approval_id=approval_id,
                # Written because somebody is about to be asked, and the
                # arguments are what they would be consenting to. The
                # canonical form, so what is shown is the string the digest
                # was taken over rather than a re-serialization of it.
                approval_preview=preview,
            )
        )
        await sink.emit(RunPaused(reason="approval", approval_id=approval_id))

        # No ``bound <= 0`` arm, and its absence is checked rather than
        # assumed: a run with nothing left never reaches here, because the
        # decision that raised the approval requirement is taken under the same
        # remaining time and ``_decide`` refuses first. Writing one anyway
        # would be a branch no test could enter and every reader would believe.
        bound = self._approval_timeout_seconds
        if remaining_run_seconds is not None:
            bound = min(bound, remaining_run_seconds)

        decision, decided_by, detail = await self._ask(
            binding,
            call,
            approval_id=approval_id,
            preview=preview,
            bound=bound,
            cancellation=cancellation,
        )
        if decision != "deny":
            await sink.emit(
                ToolApprovalDecided(
                    tool_call_id=call.tool_call_id,
                    approval_id=approval_id,
                    decision=decision,
                    decided_by=decided_by,
                )
            )
            return None

        return await self._decided(
            call,
            sink=sink,
            approval_id=approval_id,
            decided_by=decided_by,
            message=f"{call.tool_name} was not permitted ({reason_code}): {detail}",
        )

    async def _ask(
        self,
        binding: ToolBinding,
        call: ToolCall,
        *,
        approval_id: str,
        preview: str,
        bound: float,
        cancellation: CancellationToken,
    ) -> tuple[ApprovalDecision, ApprovalDecidedBy, str]:
        """Race the gate against the run's cancellation, bounded either way.

        The gate is deployment-supplied code holding an open question, so it
        gets what the policy engine gets: a bound it cannot exceed, and no way
        to send its own words back -- only its exception's type name. Every
        way of not getting an answer is a refusal here. "We could not
        establish that this was permitted" and "this was permitted" are the
        two results that must never collapse into each other.
        """

        assert self._approvals is not None
        answer = asyncio.ensure_future(
            self._approvals.request(
                approval_id=approval_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                argument_digest=argument_digest(call.arguments),
                approval_preview=preview,
                risk=binding.spec.risk,
                required_scopes=binding.spec.permission_scopes,
                timeout_seconds=bound,
            )
        )
        stopped = asyncio.ensure_future(cancellation.wait_cancelled())
        try:
            async with asyncio.timeout(bound):
                await asyncio.wait(
                    (answer, stopped), return_when=asyncio.FIRST_COMPLETED
                )
        except TimeoutError:
            pass
        finally:
            # Read before cancelling: the cancellation below would otherwise be
            # what decides whether the gate is recorded as having answered.
            answered = answer.done()
            was_cancelled = stopped.done()
            # Both, always. The loser is parked on something that may never
            # arrive, and one abandoned waiter per approval is a leak; for the
            # gate it is also how it learns to take the question down.
            if not answered:
                answer.cancel()
                # And somebody has to look at how it ended. A gate that fails
                # while tearing its question down -- the cleanup this port
                # obliges it to do -- ends with an exception nobody retrieves,
                # and asyncio prints those in full. Scrubbing the message out
                # of the refusal and letting the event loop log it is not
                # scrubbing it.
                answer.add_done_callback(_discard_outcome)
            stopped.cancel()

        if answered:
            try:
                decision, decided_by = answer.result()
            except asyncio.CancelledError:
                return "deny", "cancelled", "the run stopped while it waited"
            except Exception as exc:
                return (
                    "deny",
                    "gate_failed",
                    f"the approval gate raised {type(exc).__name__}",
                )
            recognised = (
                decision in APPROVAL_DECISIONS and decided_by in APPROVAL_DECIDERS
            )
            if not recognised:
                # Guarding the shape and trusting the values would be the worse
                # half of a guard. An unrecognised word is not a permission,
                # and it must not become one by failing to match "deny" -- this
                # repository already holds a second, differently-worded
                # approval vocabulary (``domain/task_registry.ApprovalDecision``
                # is "approved"/"rejected"), so the first gate somebody adapts
                # from the existing approvals API answers off-contract.
                #
                # The value itself does not cross: it reached this process from
                # deployment-supplied code, exactly like an exception message.
                return (
                    "deny",
                    "gate_failed",
                    "the approval gate answered outside its contract",
                )
            return decision, decided_by, f"the decision was {decision}"
        if was_cancelled:
            return "deny", "cancelled", "the run stopped while it waited"
        return "deny", "timeout", f"nobody answered within its {bound:g}s bound"

    async def _decided(
        self,
        call: ToolCall,
        *,
        sink: EventSink,
        approval_id: str,
        decided_by: ApprovalDecidedBy,
        message: str,
    ) -> ToolResult:
        """Record the refusal, then answer the call in its terms.

        The event comes first for the same reason the request did: a refusal
        is not self-describing, and "nobody answered" is a fact about the run
        that the ``ToolFailed`` alone cannot carry.

        One error code for all of them. ``ErrorCode`` is a closed set and each
        member is a contract with every reader of every stream; "who decided"
        is what ``decided_by`` is for, and a code per outcome would put the
        same distinction in two places that can disagree. A run that was
        cancelled is the exception -- it is not a policy story at all, and
        ``cancelled`` is already the word the rest of the runtime uses for it.
        """

        await sink.emit(
            ToolApprovalDecided(
                tool_call_id=call.tool_call_id,
                approval_id=approval_id,
                decision="deny",
                decided_by=decided_by,
            )
        )
        code: ErrorCode = "cancelled" if decided_by == "cancelled" else "policy_denied"
        return await self.refuse(
            call,
            ErrorInfo(code=code, message=message),
            sink=sink,
        )

    async def invoke(
        self,
        prepared: PreparedCall,
        *,
        context: ExecutionContext,
        cancellation: CancellationToken,
        sink: EventSink,
        run_budget_seconds: float | None = None,
    ) -> ToolResult:
        """Run an authorized call and record however it ended."""

        if prepared.binding.operation_key is not None:
            return await self._invoke_ledgered(
                prepared,
                context=context,
                cancellation=cancellation,
                sink=sink,
                run_budget_seconds=run_budget_seconds,
            )
        return await self._dispatch(
            prepared,
            context=context,
            cancellation=cancellation,
            sink=sink,
            run_budget_seconds=run_budget_seconds,
        )

    async def _dispatch(
        self,
        prepared: PreparedCall,
        *,
        context: ExecutionContext,
        cancellation: CancellationToken,
        sink: EventSink,
        run_budget_seconds: float | None = None,
    ) -> ToolResult:
        await sink.emit(
            ToolStarted(
                tool_call_id=prepared.call.tool_call_id,
                tool_name=prepared.call.tool_name,
            )
        )
        result = await self._executor.execute(
            prepared.binding,
            prepared.call,
            context=context,
            cancellation=cancellation,
            run_budget_seconds=run_budget_seconds,
            # The same sink that just emitted `ToolStarted` and is about to
            # emit `ToolCompleted`, so a call's progress lands between its own
            # two bookends rather than on some other stream. It is passed here
            # rather than held by the executor because the executor is built
            # once per process and a sink belongs to one run (ADR-068).
            sink=sink,
        )
        await self._record(result, sink=sink)
        return result

    async def _invoke_ledgered(
        self,
        prepared: PreparedCall,
        *,
        context: ExecutionContext,
        cancellation: CancellationToken,
        sink: EventSink,
        run_budget_seconds: float | None,
    ) -> ToolResult:
        """The side-effect protocol, in the order the baseline fixes it.

        Record the intent, re-check the authorization that has to hold at the
        moment of an irreversible act, dispatch, then report. The second check
        is not a duplicate of ``authorize``: an ACL or a tool registry may have
        tightened while this call queued behind an exclusive barrier, and
        tightening is required to take effect at the *next* authorization
        boundary. This is that boundary.
        """

        call = prepared.call
        if (
            self._ledger is None
            or context.task_id is None
            or context.lease_epoch is None
        ):
            # A ledgered tool with nowhere to record is not a tool to run
            # unrecorded. Refusing costs an operation; dispatching would cost an
            # effect nothing can later account for.
            return await self.refuse(
                call,
                ErrorInfo(
                    code="policy_denied",
                    message=(
                        f"{call.tool_name} records external effects, and this run "
                        "has no ledger, task or lease to record them against"
                    ),
                ),
                sink=sink,
            )

        assert prepared.binding.operation_key is not None  # narrowed by the caller
        operation_key = prepared.binding.operation_key(call, context)
        try:
            record = await self._ledger.record_intent(
                ToolExecutionIntent(
                    task_id=context.task_id,
                    operation_key=operation_key,
                    tool_name=call.tool_name,
                    canonical_request_hash=argument_digest(call.arguments),
                    lease_epoch=context.lease_epoch,
                    agent_run_id=context.agent_run_id,
                    tool_call_id=call.tool_call_id,
                    policy_identity=context.policy_identity,
                )
            )
        except ToolOperationConflictError:
            return await self.refuse(
                call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=(
                        f"operation {operation_key} was already recorded with "
                        "different arguments"
                    ),
                ),
                sink=sink,
            )
        except ToolExecutionNotWritableError as error:
            return await self.refuse(
                call,
                ErrorInfo(
                    code="policy_denied",
                    message=(
                        f"{call.tool_name} cannot record an intent: "
                        f"{type(error).__name__}"
                    ),
                ),
                sink=sink,
            )

        if not record.may_dispatch:
            # Somebody already did this. Answering from the ledger is the whole
            # reason it exists: the alternative is performing the effect twice
            # and calling the second one a retry.
            return await self.refuse(
                call,
                ErrorInfo(
                    code="tool_failed",
                    message=(
                        f"operation {operation_key} is already {record.status}; "
                        "it will not be performed again"
                    ),
                    retryable=False,
                ),
                sink=sink,
            )

        reauthorized = await self._decide(call, context, run_budget_seconds)
        # Only ``allow``, exactly, may dispatch. This second decision is taken
        # one line from performing an external effect, and the two answers that
        # used to fall through here are both "not with these arguments":
        #
        # ``requires_approval`` -- the policy changed under a call that was
        # already permitted. Asking again is the wrong repair: the question was
        # answered, and re-opening it would let a policy that flaps prompt a
        # human twice for one call.
        #
        # ``allow_with_modified_input`` -- the engine is saying these arguments
        # are not the ones it will permit, and dispatching them anyway is the
        # one reading of that answer it cannot mean. The rewrite is not applied
        # here either: the arguments in hand are the ones the ledger recorded
        # an intent for, and substituting others at dispatch would make the
        # recorded intent describe a call that never happened.
        if isinstance(reauthorized, ErrorInfo) or reauthorized.effect != "allow":
            denial = (
                reauthorized
                if isinstance(reauthorized, ErrorInfo)
                else ErrorInfo(
                    code="policy_denied",
                    message=(
                        f"{_REDECISION_REFUSALS[reauthorized.effect]} before "
                        f"dispatch: {reauthorized.reason_code}"
                    ),
                )
            )
        elif reauthorized.requires_approval:
            denial = ErrorInfo(
                code="policy_denied",
                message=(
                    "approval was required again before dispatch: "
                    f"{reauthorized.reason_code}"
                ),
            )
        else:
            denial = None

        if denial is not None:
            # Recorded as failed, not left intended: nothing was dispatched, and
            # that *is* knowledge. Leaving it open would send a human to
            # reconcile an effect that provably never happened.
            await self._settle_quietly(
                context.task_id,
                operation_key,
                context.lease_epoch,
                succeeded=False,
                detail=denial.message,
            )
            return await self.refuse(call, denial, sink=sink)

        result = await self._dispatch(
            prepared,
            context=context,
            cancellation=cancellation,
            sink=sink,
            run_budget_seconds=run_budget_seconds,
        )
        await self._report(
            result,
            task_id=context.task_id,
            operation_key=operation_key,
            lease_epoch=context.lease_epoch,
        )
        return result

    async def _report(
        self,
        result: ToolResult,
        *,
        task_id: str,
        operation_key: str,
        lease_epoch: int,
    ) -> None:
        """Say what happened, or say that nobody knows.

        The line between the two is which failures carry an answer. A handler
        that returned an error answered; a call that timed out or was cancelled
        did not, and for an external write "no answer" does not mean "no
        effect" -- the request may be in flight at the moment the deadline
        passes. Those two become a human's problem rather than a retry's.

        This is a rule about the codes the executor produces, and it is only as
        good as a handler's own error reporting: an adapter that converts its
        own post-send timeout into an ordinary failure would be recorded as
        knowledge it does not have. That is worth stating plainly rather than
        hiding behind the word "failed".
        """

        if result.status != "error":
            await self._settle_quietly(
                task_id,
                operation_key,
                lease_epoch,
                succeeded=True,
                # What the effect produced, when it produced something nameable.
                # A crash between this write and the caller's own checkpoint
                # otherwise leaves an operation that provably succeeded and no
                # way to reach what it made -- and the only remaining options
                # are performing the effect a second time or handing a person a
                # row that says an object exists somewhere. An id, not a
                # payload: this table is read by operators.
                detail=(
                    None if result.artifact is None else result.artifact.artifact_id
                ),
            )
            return
        code = result.error.code if result.error is not None else "tool_failed"
        if code in AMBIGUOUS_DISPATCH_CODES:
            with suppress(ToolExecutionNotWritableError):
                await self._ledger.mark_for_reconciliation(  # type: ignore[union-attr]
                    task_id=task_id,
                    operation_key=operation_key,
                    lease_epoch=lease_epoch,
                    detail=f"no answer was received: {code}",
                )
            return
        await self._settle_quietly(
            task_id,
            operation_key,
            lease_epoch,
            succeeded=False,
            detail=result.error.message if result.error is not None else None,
        )

    async def _settle_quietly(
        self,
        task_id: str,
        operation_key: str,
        lease_epoch: int,
        *,
        succeeded: bool,
        detail: str | None,
    ) -> None:
        """Report the outcome, and never turn a failure to report into one.

        A ledger write that is refused means the lease moved underneath this
        call. The Worker that replaced it owns the operation now, and the row it
        will read is the truthful one -- ``intended`` -- so losing this write
        loses nothing. Raising here would replace a recorded tool result with an
        exception the run was not promised.
        """

        if self._ledger is None:  # pragma: no cover - guarded by the caller
            return
        with suppress(ToolExecutionNotWritableError):
            await self._ledger.record_result(
                task_id=task_id,
                operation_key=operation_key,
                lease_epoch=lease_epoch,
                succeeded=succeeded,
                detail=detail,
            )

    async def refuse(
        self,
        call: ToolCall,
        error: ErrorInfo,
        *,
        sink: EventSink,
    ) -> ToolResult:
        """Answer a call that must not run, so its id is never left open."""

        result = ToolResult.failed(call, error)
        await self._record(result, sink=sink)
        return result

    def _validate(self, binding: ToolBinding, call: ToolCall) -> ErrorInfo | None:
        try:
            validate_arguments(binding.spec.input_schema, call.arguments)
        except ToolInputInvalidError as exc:
            return exc.to_error_info()
        except AgentWorkbenchError as exc:
            # An unsupported schema slipped past assembly: fail the call rather
            # than run a handler nothing checked.
            return exc.to_error_info()
        return None

    def _risk_of(self, call: ToolCall) -> ToolRisk | None:
        binding = self._registry.get(call.tool_name)
        return binding.spec.risk if binding is not None else None

    async def _record(self, result: ToolResult, *, sink: EventSink) -> None:
        if result.status == "error" and result.error is not None:
            await sink.emit(
                ToolFailed(
                    tool_call_id=result.tool_call_id,
                    error=result.error,
                    duration_ms=result.duration_ms or 0,
                )
            )
            return
        await sink.emit(
            ToolCompleted(
                tool_call_id=result.tool_call_id,
                duration_ms=result.duration_ms or 0,
                output_bytes=len(result.content.encode("utf-8")),
                # Behind the same flag as `argument_preview` in `propose`
                # above, deliberately the same one: a deployment that declined
                # to record what a tool was asked has not agreed to record what
                # it answered. `bounded` may shorten again on top of whatever
                # the tool already clipped, which is why `output_bytes` and
                # `truncated` both stay -- they describe the tool's output, and
                # this describes what was kept of it.
                output_preview=(
                    bounded(result.content) if self._record_step_inputs else ""
                ),
                artifact=result.artifact,
                # Deliberately *not* behind the flag one line above, and the
                # asymmetry is the decision (ADR-063). That flag governs
                # content -- text a deployment may have declined to copy into
                # its event log. A filename is a structured fact about what the
                # call did, in the same standing as `tool_name` and
                # `output_bytes`: the principal who made the call can already
                # list the workspace, so recording the name reveals nothing the
                # flag was written to withhold. Gating it would delete the
                # field precisely where previews are off, i.e. everywhere it is
                # the only machine-readable answer left.
                workspace_writes=result.workspace_writes,
                # Same standing, same argument, other store (ADR-086).
                project_writes=result.project_writes,
            )
        )


__all__ = [
    "DEFAULT_MAX_ARGUMENT_BYTES",
    "DEFAULT_MAX_POLICY_ROUNDS",
    "PreparedCall",
    "ToolGateway",
]
