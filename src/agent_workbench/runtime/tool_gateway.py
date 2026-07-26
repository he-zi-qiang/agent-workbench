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

It also owns the audit trail for a call. Proposal, permission, start,
completion and failure are emitted here, so the events cannot disagree with
what the gateway actually did. Argument bodies never appear in them -- a size
and a digest do.

The provenance of rewritten arguments belongs to the side-effect ledger, where
a retry has to key on exactly what ran. ``PermissionResolved`` records that a
rewrite happened; recording what it produced arrives with that ledger.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from agent_workbench.domain.errors import (
    AgentWorkbenchError,
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
from agent_workbench.domain.policies import ExecutionContext
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
        max_argument_bytes: int = DEFAULT_MAX_ARGUMENT_BYTES,
        max_policy_rounds: int = DEFAULT_MAX_POLICY_ROUNDS,
    ) -> None:
        if max_policy_rounds < 1:
            raise ValueError("max_policy_rounds must be positive")
        # Every registered schema is checked once, here. A tool whose schema
        # this validator cannot enforce stops the process now rather than
        # silently accepting anything later.
        for spec in registry.specs():
            assert_schema_supported(spec.input_schema, origin=f"tool {spec.name}")

        self._registry = registry
        self._policy = policy
        self._executor = executor if executor is not None else ToolExecutor()
        self._hooks = hooks if hooks is not None else HookBus()
        self._max_argument_bytes = max_argument_bytes
        self._max_policy_rounds = max_policy_rounds

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

        outcome = await self._hooks.before_tool(call, context)
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
    ) -> PreparedCall | ToolResult:
        """Decide the call, re-checking any arguments the policy rewrites."""

        call = prepared.call
        for _ in range(self._max_policy_rounds):
            decision = await self._policy.decide(call, context)
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
