"""Export an approved draft as the Task's one report artifact.

This is v1's only deterministic write node, and the only tool whose effects
reach the side-effect ledger. Everything else a Task runs is a read.

The write is irreversible in the sense that matters here: the artifact store is
not this database, so storing the report cannot be rolled back with the
transaction that records having stored it. That gap is exactly what the ledger
exists to survive -- intent, dispatch, result -- and it is why this tool carries
an operation key while ``knowledge_search`` and ``external_search`` do not.

The key names the Task, not the draft. One Task exports once: the graph reaches
this node only after a passing review and a human approval, and a resume
re-derives the same key from the same checkpoint. What was exported lives in the
arguments instead, so exporting a *different* draft under the same key is
refused by the ledger as the conflict it is, rather than performed as a second
export.

The rendered bytes are a pure function of the arguments and the draft: no
timestamp, no run id, no worker name. Two attempts at one export therefore agree
on their digest, which is what lets a test assert that a retry did not produce a
second, differently-shaped report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from agent_workbench.domain.errors import ErrorInfo, NotFoundError
from agent_workbench.domain.identifiers import ID_MAX_LENGTH
from agent_workbench.domain.policies import ExecutionContext
from agent_workbench.domain.tools import ToolCall, ToolResult, ToolSpec
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

TOOL_NAME: Final[str] = "export_artifact"

#: The exported report is the draft plus a short header. A draft larger than
#: this is a synthesis that already went wrong, and reading it whole to copy it
#: is the one place this tool would hold an unbounded object in memory.
MAX_DRAFT_BYTES: Final[int] = 4 * 1024 * 1024

EXPORT_MEDIA_TYPE: Final[str] = "text/markdown"
EXPORT_FILENAME: Final[str] = "report.md"

_IDENTIFIER_SCHEMA: Final[dict[str, JsonValue]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": ID_MAX_LENGTH,
}

INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["draft_ref"],
    "properties": {
        "draft_ref": _IDENTIFIER_SCHEMA,
        # Part of the request rather than context, because it is part of what
        # makes this export the one that was authorized. A second approval over
        # the same draft is a different operation, and the ledger's canonical
        # request hash is where that difference has to be visible.
        #
        # Optional since the gate became optional
        # (`workflow.export_requires_approval`). Where there is no gate the
        # distinction it draws does not exist -- there is no second approval
        # over the same draft -- so the key is simply absent and the hash is
        # over the draft alone. Absent, never a placeholder: a synthetic id
        # here would be indistinguishable in the ledger from a real approval.
        "approval_id": _IDENTIFIER_SCHEMA,
    },
}

SPEC = ToolSpec(
    name=TOOL_NAME,
    description="Export the approved draft as this Task's final report artifact.",
    input_schema=INPUT_SCHEMA,
    concurrency="exclusive",
    risk="write",
    # `keyed`, not `unsafe`: repeating this call is safe precisely because the
    # ledger answers the repeat instead of performing it. Declaring it `safe`
    # is rejected by ToolBinding, and rightly -- a safe tool needs no ledger.
    idempotency="keyed",
    timeout_seconds=30,
    permission_scopes=("artifact:export",),
)


class ExportUnavailableError(RuntimeError):
    """The draft this export names could not be read under the Task's identity."""


def operation_key_for(call: ToolCall, context: ExecutionContext) -> str:
    """Name the export by its Task.

    Deliberately not derived from ``tool_call_id``: a resumed graph builds a new
    call for the same intent, and keying on that would make every resume a fresh
    export. Deliberately not derived from the draft either -- a changed draft
    must collide with this key so the ledger can refuse it, not slip past under
    a name of its own.
    """

    del call
    if context.task_id is None:  # pragma: no cover - the handler refuses first
        raise ValueError("export_artifact has no Task to key its operation by")
    return f"export:{context.task_id}"


@dataclass(frozen=True, slots=True)
class ExportArtifactTool:
    """Render and store the report, under the Task's own identity."""

    artifacts: ArtifactStore

    def binding(self) -> ToolBinding:
        return ToolBinding(
            spec=SPEC, handler=self.handle, operation_key=operation_key_for
        )

    async def handle(self, invocation: ToolInvocation) -> ToolResult:
        arguments = invocation.call.arguments
        draft_ref = arguments.get("draft_ref")
        approval_id = arguments.get("approval_id")
        if not isinstance(draft_ref, str) or not draft_ref:
            return _invalid(invocation, "draft_ref must be a non-empty string")
        # Absent is allowed -- an ungated deployment has no approval to name.
        # Present-but-empty is not: that is a caller that meant to supply one.
        if approval_id is not None and (
            not isinstance(approval_id, str) or not approval_id
        ):
            return _invalid(invocation, "approval_id must be a non-empty string")
        if invocation.context.task_id is None:
            return _invalid(invocation, "export_artifact is available only in a Task")

        principal = invocation.context.principal
        invocation.cancellation.raise_if_cancelled()
        try:
            # Tenant and principal come from the execution context. The schema
            # carries no identity fields, so nothing in a model's arguments can
            # select another owner's draft or write into their namespace.
            draft = await self.artifacts.get(
                tenant_id=principal.tenant_id,
                artifact_id=draft_ref,
                principal_id=principal.principal_id,
            )
        except NotFoundError:
            # One answer for "no such draft", "another tenant's" and "not
            # yours", because the store already refuses to tell those apart.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="not_found",
                    message="the draft this export names is not readable",
                ),
            )
        if len(draft) > MAX_DRAFT_BYTES:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="output_too_large",
                    message="the draft exceeds the exportable size",
                ),
            )

        invocation.cancellation.raise_if_cancelled()
        content = render_report(
            task_id=invocation.context.task_id,
            approval_id=approval_id,
            draft_ref=draft_ref,
            draft=draft,
        )
        reference = await self.artifacts.put(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            kind="report",
            media_type=EXPORT_MEDIA_TYPE,
            content=content,
            filename=EXPORT_FILENAME,
        )
        return ToolResult.succeeded(
            invocation.call,
            artifact=reference,
            content=f"report exported as {reference.artifact_id}",
        )


def render_report(
    *, task_id: str, approval_id: str | None, draft_ref: str, draft: bytes
) -> bytes:
    """Build the report bytes, deterministically.

    The draft is decoded permissively and re-encoded: it was written as UTF-8 by
    the synthesis node, and a byte that says otherwise is a corrupt draft, not a
    reason to refuse an export a human has already approved.

    With no approval the header says so, rather than dropping the line. A
    report that simply omits "Approved by" reads the same as one from before
    this field existed; a report that says the export was not gated tells its
    reader what the document is, which is the entire job of a provenance
    header.
    """

    body = draft.decode("utf-8", errors="replace")
    approval_line = (
        f"- Approved by: {approval_id}\n"
        if approval_id is not None
        else "- Approved by: not required by this deployment\n"
    )
    header = (
        "# Task report\n\n"
        f"- Task: {task_id}\n"
        f"{approval_line}"
        f"- Draft: {draft_ref}\n\n"
        "---\n\n"
    )
    return (header + body).encode("utf-8")


def _invalid(invocation: ToolInvocation, message: str) -> ToolResult:
    return ToolResult.failed(
        invocation.call,
        ErrorInfo(code="invalid_tool_input", message=message),
    )


__all__ = [
    "EXPORT_FILENAME",
    "EXPORT_MEDIA_TYPE",
    "INPUT_SCHEMA",
    "MAX_DRAFT_BYTES",
    "SPEC",
    "TOOL_NAME",
    "ExportArtifactTool",
    "ExportUnavailableError",
    "operation_key_for",
    "render_report",
]
