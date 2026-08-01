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

A decision that requires human approval is not a decision to run. Until the
approval boundary exists -- a durable request, a recorded human answer, a run
that can pause and be resumed by whichever worker picks it up -- a call that
needs one is refused here. Treating "allow, pending approval" as "allow" is how
a write tool performs an irreversible effect that nobody agreed to, and no
amount of later approval machinery can undo an effect already dispatched.

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
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.errors import (
    AgentWorkbenchError,
    ErrorCode,
    ErrorInfo,
    ToolInputInvalidError,
    UnknownToolError,
)
from agent_workbench.domain.events import (
    PermissionRequested,
    PermissionResolved,
    ToolCompleted,
    ToolFailed,
    ToolProposed,
    ToolStarted,
)
from agent_workbench.domain.policies import ExecutionContext, PolicyDecision
from agent_workbench.domain.tools import (
    ToolCall,
    ToolResult,
    ToolRisk,
    ToolSpec,
    argument_digest,
    canonical_arguments,
)
from agent_workbench.ports.cancellation import CancellationToken
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

# The failures that carry no answer. For an external write these are the ones
# where the request may have landed after the deadline passed, so they become a
# human's problem rather than a retry's. Every other failure was reported by the
# handler itself and is recorded as what it says it is.
AMBIGUOUS_DISPATCH_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {"tool_timeout", "cancelled", "budget_exceeded"}
)


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
    ) -> None:
        if max_policy_rounds < 1:
            raise ValueError("max_policy_rounds must be positive")
        if policy_timeout_seconds <= 0:
            raise ValueError("policy_timeout_seconds must be positive")
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

    def advertise(self, names: Sequence[str]) -> tuple[ToolSpec, ...]:
        """Specifications for the tools a run may use.

        Raises when a run asks for a tool this process does not register: the
        model must never be shown a tool the gateway would refuse to resolve.
        """

        specs: list[ToolSpec] = []
        for name in names:
            binding = self._registry.get(name)
            if binding is None:
                raise UnknownToolError(
                    f"the run requested a tool this process does not register: {name}"
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
    ) -> PreparedCall | ToolResult:
        """Decide the call, re-checking any arguments the policy rewrites."""

        call = prepared.call
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
                # Checked before either allow branch, so a rewrite cannot carry
                # an approval requirement past this point either.
                return await self._await_approval(
                    prepared.binding,
                    call,
                    reason_code=decision.reason_code,
                    sink=sink,
                )

            if decision.effect == "allow":
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
    ) -> ToolResult:
        """Record that a human decision is needed, and refuse until it exists.

        The request is emitted before the refusal so the audit trail says what
        was actually wanted: not that the call was forbidden, but that nobody
        was there to permit it. When the approval boundary lands, this is the
        point that pauses the run instead of answering it.
        """

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
        if isinstance(reauthorized, ErrorInfo) or reauthorized.effect == "deny":
            denial = (
                reauthorized
                if isinstance(reauthorized, ErrorInfo)
                else ErrorInfo(
                    code="policy_denied",
                    message=f"denied before dispatch: {reauthorized.reason_code}",
                )
            )
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
                artifact=result.artifact,
            )
        )


__all__ = [
    "DEFAULT_MAX_ARGUMENT_BYTES",
    "DEFAULT_MAX_POLICY_ROUNDS",
    "PreparedCall",
    "ToolGateway",
]
