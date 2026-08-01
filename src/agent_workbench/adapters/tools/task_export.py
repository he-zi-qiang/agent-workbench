"""Adapt the Task's export node to the one policy-gated tool runtime.

The node does not call the handler. It builds a call and drives it through the
same gateway a model-proposed tool goes through, so schema validation, policy,
the timeout, the audit events and the side-effect ledger are the same code on
both paths. A deterministic node with its own private route to a handler would
be a second tool runtime with none of those.

Two answers come back from a gateway that refused. Only one of them is a
failure. "This operation already succeeded" is the ledger doing its job on a
resume, and the export it is talking about is the one this Task wants; the
recovery is to read which artifact it produced, not to produce another.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.adapters.tools.export_artifact import (
    TOOL_NAME,
    operation_key_for,
)
from agent_workbench.domain.identifiers import Identifier, new_tool_call_id
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.tools import ToolCall, argument_digest
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.event_log import EventSink
from agent_workbench.ports.tool_executions import ToolExecutionLedger
from agent_workbench.runtime.tool_gateway import PreparedCall, ToolGateway


class ExportRefusedError(RuntimeError):
    """The export did not happen and no earlier attempt performed it either.

    Raised rather than returned. A Task that was approved and then could not
    export has not succeeded, and the node's caller turns this into a failed
    Task -- which is the honest outcome and the one a person can act on.
    """


class ExportUnrecoverableError(RuntimeError):
    """An earlier attempt exported, and what it produced cannot be named.

    Distinct from a refusal because the responses differ: nothing should be
    exported again, and somebody has to reconcile a report that exists with a
    Task that cannot point at it.
    """


@dataclass(frozen=True, slots=True)
class GatewayReportExport:
    """Perform the Task's single export, or recover the one already performed."""

    gateway: ToolGateway
    ledger: ToolExecutionLedger

    async def export(
        self,
        *,
        draft_ref: Identifier,
        approval_id: Identifier,
        execution: ExecutionContext,
        sink: EventSink,
        cancellation: CancellationToken,
    ) -> Identifier:
        if execution.task_id is None:
            raise ExportRefusedError("export requires a Task execution context")
        cancellation.raise_if_cancelled()
        call = ToolCall(
            tool_call_id=new_tool_call_id(),
            tool_name=TOOL_NAME,
            arguments={"draft_ref": draft_ref, "approval_id": approval_id},
        )
        await self.gateway.propose(call, sink=sink)
        prepared = await self.gateway.prepare(call, context=execution, sink=sink)
        if isinstance(prepared, PreparedCall):
            prepared = await self.gateway.authorize(
                prepared, context=execution, sink=sink
            )
        if isinstance(prepared, PreparedCall):
            result = await self.gateway.invoke(
                prepared,
                context=execution,
                cancellation=cancellation,
                sink=sink,
            )
        else:
            result = prepared

        if result.status == "ok" and result.artifact is not None:
            return result.artifact.artifact_id

        # Not "did the call fail" but "did this Task already export *this*".
        # Asking the ledger rather than parsing the refusal's message keeps the
        # recovery from depending on wording the gateway is free to change.
        recovered = await self._recover(call, execution)
        if recovered is not None:
            return recovered
        detail = result.error.message if result.error is not None else "no result"
        raise ExportRefusedError(f"export_artifact did not export: {detail}")

    async def _recover(
        self, call: ToolCall, execution: ExecutionContext
    ) -> Identifier | None:
        """The artifact a settled export produced, if it is the one asked for."""

        if execution.task_id is None:  # pragma: no cover - guarded by the caller
            return None
        record = await self.ledger.get(
            task_id=execution.task_id,
            operation_key=operation_key_for(call, execution),
        )
        if record is None or record.status != "succeeded":
            return None
        if record.canonical_request_hash != argument_digest(call.arguments):
            # Same key, different request. The row describes an export of
            # something else, and handing its artifact back would report that
            # what this caller asked for had been done. That is the conflict the
            # ledger raised, and it stays a conflict here.
            return None
        if not record.outcome_detail:
            # It happened, and the row does not say what it made. Retrying would
            # export a second report; reporting success would name nothing.
            raise ExportUnrecoverableError(
                "an earlier export succeeded without recording its artifact"
            )
        return record.outcome_detail


__all__ = [
    "ExportRefusedError",
    "ExportUnrecoverableError",
    "GatewayReportExport",
]
