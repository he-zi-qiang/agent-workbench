"""Tool contract: specification, call, result, and the pairing invariant.

``ToolSpec`` is the only description of a tool the runtime understands. Native
handlers, MCP tools and LangChain tools are all converted into it at an adapter
boundary, so the tool gateway has one schema, one risk model and one timeout
rule to enforce rather than three vendor shapes.

The specification is serializable; the handler is not. A handler exists only in
the runtime registry, which keeps ``ToolSpec`` safe to store in a task snapshot
and to send to a model.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import ErrorInfo, ToolPairingError
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.project_files import ProjectRelativePath
from agent_workbench.domain.schema import (
    JsonObject,
    ToolOutputText,
    VersionedModel,
)
from agent_workbench.domain.workspace import WorkspaceName

ToolRisk = Literal["read", "write", "external", "destructive"]
ToolConcurrency = Literal["parallel", "exclusive"]
ToolIdempotency = Literal["safe", "keyed", "unsafe"]
ToolResultStatus = Literal["ok", "error"]

ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]

# A model may propose any string, including one that matches no registered
# tool. The proposal still has to become exactly one ToolResult, so the name is
# bounded and printable rather than well-formed.
ProposedToolName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$"),
]
ToolDescription = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
PermissionScope = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(:[a-z][a-z0-9_]*)*$"),
]


class ToolSpec(VersionedModel):
    """Framework-neutral description of one callable tool."""

    name: ToolName
    description: ToolDescription
    input_schema: JsonObject
    # `output_schema: JsonObject | None` was here until 2026-08-31. It had no
    # producer and no consumer in `src/`, and was frozen into the golden
    # contract -- which is how a field with neither survived being a contract.
    #
    # The one thing that looked like a producer is not this field:
    # `apps/sandbox_mcp/server.py` sets `output_schema` on **MCP's**
    # `types.Tool`, a different attribute on a different type, declared to a
    # different protocol.
    #
    # Deleted rather than enforced, and the alternative is worth writing down
    # because it is the one a reader will think of. The gateway validates
    # *arguments*; validating a *result* against a schema means deciding what
    # the model is shown when a call that ran and returned is then failed on
    # its shape -- a behavioural change to the one thing `tool_executor`
    # promises ("exactly one ToolResult leaves this method"). That is an ADR,
    # and inventing it to give this field a job would be backwards. If output
    # validation ever lands, it brings its own field back with it.
    concurrency: ToolConcurrency
    risk: ToolRisk
    idempotency: ToolIdempotency
    timeout_seconds: int = Field(ge=1, le=3600)
    permission_scopes: tuple[PermissionScope, ...] = ()

    @field_validator("permission_scopes")
    @classmethod
    def normalize_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("input_schema")
    @classmethod
    def require_object_input(cls, value: JsonObject) -> JsonObject:
        # Tool arguments are always a named mapping: positional payloads cannot
        # be validated, policy-checked or re-validated after a hook edit.
        if value.get("type") != "object":
            raise ValueError("input_schema must be a JSON Schema object type")
        return value

    @model_validator(mode="after")
    def validate_risk_consistency(self) -> ToolSpec:
        if self.risk == "read":
            if self.idempotency != "safe":
                raise ValueError("read tools must declare safe idempotency")
            return self
        # Runtime invariant: write, external and destructive tools cross an
        # exclusive barrier instead of running beside anything else.
        if self.concurrency != "exclusive":
            raise ValueError("write, external and destructive tools must be exclusive")
        if not self.permission_scopes:
            raise ValueError(
                "write, external and destructive tools must declare at least "
                "one permission scope"
            )
        return self


class ToolCall(VersionedModel):
    """One tool invocation proposed by a model."""

    tool_call_id: Identifier
    tool_name: ProposedToolName
    arguments: JsonObject = Field(default_factory=dict)
    model_call_id: Identifier | None = None


class ToolResult(VersionedModel):
    """The single, mandatory answer to one ``ToolCall``.

    ``content`` is what the model sees. When output exceeds the inline ceiling
    it is written to the artifact store and ``content`` holds a short summary,
    so a large result cannot silently consume the context budget.

    ``workspace_writes`` is a structured fact rather than a sentence about one
    (ADR-063). Before it, the only machine-readable route to "which file did
    this step produce" was to parse the prose in ``content`` -- three English
    sentences in ``adapters/tools/workspace.py`` and ``sandbox.py`` that no
    test pins -- or to parse ``ToolProposed.argument_preview``, which is
    bounded at 4096 characters and so truncates the name away precisely when
    the written body is large. Naming the names is cheaper than either, and it
    is the tool that knows them.
    """

    tool_call_id: Identifier
    tool_name: ProposedToolName
    status: ToolResultStatus
    content: ToolOutputText = ""
    artifact: ArtifactRef | None = None
    #: Write order, deliberately unsorted -- the opposite of
    #: ``WorkspaceManifest.names()`` one module over, and opposite for the
    #: reason that makes them different questions. A manifest is a *set* of
    #: files, so it sorts: two runs that wrote the same files in a different
    #: order must not read as two different workspaces. This is a *sequence of
    #: writes by one call*. A sandbox script that produced `plot.svg` and then
    #: `data.csv` did them in that order, and sorting would report the reverse
    #: -- attributing to the script an order it never performed.
    #:
    #: Empty for every tool that writes nothing, which is most of them, and
    #: empty on every failure by construction rather than by memory: each write
    #: tool returns before ``session.version`` advances, so there is no name to
    #: forget to clear.
    workspace_writes: tuple[WorkspaceName, ...] = ()
    #: What each of those names resolved to, at the moment it was written
    #: (ADR-088).
    #:
    #: A workspace entry is stored *as an artifact* -- `WorkspaceManifest`
    #: binds every name to an `ArtifactRef` -- and `/v1/artifacts/{id}` already
    #: serves those bytes under the owner check the write itself recorded. So
    #: the console needs no workspace read route to open one; it needs only to
    #: know which artifact the name meant. That is this field, and it is the
    #: whole of what ADR-088 adds.
    #:
    #: Parallel to `workspace_writes` rather than folded into it, for the
    #: reason `project_writes` is parallel too: widening the element type would
    #: make every existing consumer test which of two shapes it received.
    #: Pairing is by `ArtifactRef.filename`, never by index -- a producer that
    #: emitted these in a different order, or emitted one fewer, would
    #: otherwise mislabel a file rather than simply omit it.
    #:
    #: Empty on failure by the same construction that empties `workspace_writes`
    #: on failure: both are produced only after `session.version` advances.
    workspace_write_refs: tuple[ArtifactRef, ...] = ()
    #: The same fact for the other file-shaped side (ADR-086).
    #:
    #: A separate field rather than a wider ``workspace_writes``, and the type
    #: is the reason rather than the taste: ``WorkspaceName`` forbids ``/`` on
    #: purpose (``domain/workspace.py``), so a project path cannot be spelled
    #: in it at all. Widening that type to admit one would remove the property
    #: the flat side buys by refusing paths -- for every caller, to serve a
    #: side that does not need it.
    #:
    #: Two fields also keep a reader honest about which store a name is in.
    #: ``report.html`` in a workspace and ``docs/report.html`` in a project are
    #: reached by different endpoints and shown by different components; a
    #: single list would have made "which one is this" a guess from the
    #: presence of a separator, and a project file written at the root has no
    #: separator either.
    project_writes: tuple[ProjectRelativePath, ...] = ()
    error: ErrorInfo | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    #: Whether the tool cut its own output before handing it over.
    #:
    #: `ToolCompleted.truncated` has said it means exactly this since the event
    #: was written -- "``output_bytes`` stays the truth about size, and
    #: ``truncated`` about the tool's own clipping" -- and until now **nothing
    #: in this repository ever set it**. The field existed, defaulted to
    #: ``False``, and described a mechanism with no producer.
    #:
    #: The consequence was not theoretical. ``delegate_agent`` clips a
    #: sub-agent's report at ``max_report_chars`` and marks the cut *in the
    #: report's own text*, at the end, because the parent model reads the text
    #: and not this metadata. But the event log's copy goes through
    #: ``bounded()`` at 4096 characters, and an 8000-character report's closing
    #: marker is the first thing that falls off -- so a console watching a
    #: delegating Task was shown a half report with the one sign of the cut
    #: removed, which is the precise failure the marker was added to prevent.
    #:
    #: A field rather than a longer ``content``: the marker in the text is for
    #: the model, this is for anything that has to *branch* on the fact, and a
    #: reader that had to regex English prose out of a preview to find out
    #: would be guessing.
    truncated: bool = False

    @model_validator(mode="after")
    def validate_status_and_error(self) -> ToolResult:
        if (self.status == "error") is not (self.error is not None):
            raise ValueError(
                "a failed ToolResult carries an ErrorInfo and a successful one does not"
            )
        return self

    @classmethod
    def succeeded(
        cls,
        call: ToolCall,
        *,
        content: str = "",
        artifact: ArtifactRef | None = None,
        workspace_writes: tuple[WorkspaceName, ...] = (),
        workspace_write_refs: tuple[ArtifactRef, ...] = (),
        project_writes: tuple[ProjectRelativePath, ...] = (),
        duration_ms: int | None = None,
        truncated: bool = False,
    ) -> ToolResult:
        return cls(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            status="ok",
            content=content,
            artifact=artifact,
            workspace_writes=workspace_writes,
            workspace_write_refs=workspace_write_refs,
            project_writes=project_writes,
            duration_ms=duration_ms,
            truncated=truncated,
        )

    @classmethod
    def failed(
        cls,
        call: ToolCall,
        error: ErrorInfo,
        *,
        content: str = "",
        duration_ms: int | None = None,
        truncated: bool = False,
    ) -> ToolResult:
        return cls(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            status="error",
            content=content,
            error=error,
            duration_ms=duration_ms,
            truncated=truncated,
        )

    @classmethod
    def from_exception(
        cls,
        call: ToolCall,
        exc: BaseException,
        *,
        duration_ms: int | None = None,
    ) -> ToolResult:
        """Normalize any handler failure into the mandatory result."""

        return cls.failed(
            call,
            ErrorInfo.from_exception(exc, default_code="tool_failed"),
            duration_ms=duration_ms,
        )


def canonical_arguments(arguments: JsonObject) -> str:
    """Return the one canonical JSON form of a call's arguments.

    There is exactly one canonicalization on purpose. Events record a digest of
    this form instead of the arguments themselves, and the side-effect ledger
    keys a retry on the same form; a second, slightly different canonicalization
    would eventually disagree with the first and turn a retry into a second real
    effect.
    """

    return json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def argument_digest(arguments: JsonObject) -> str:
    """SHA-256 of :func:`canonical_arguments`, as lowercase hex."""

    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def align_results(
    calls: Sequence[ToolCall],
    results: Iterable[ToolResult],
) -> tuple[ToolResult, ...]:
    """Return exactly one result per call, ordered by the original calls.

    This is where two runtime invariants become checkable instead of hoped for:
    every exposed ``tool_call_id`` ends with exactly one result, and the order
    submitted back to the model follows the model's own call order regardless
    of the order in which parallel executions completed.
    """

    by_id: dict[str, ToolResult] = {}
    for result in results:
        if result.tool_call_id in by_id:
            raise ToolPairingError(
                f"duplicate ToolResult for tool_call_id {result.tool_call_id}"
            )
        by_id[result.tool_call_id] = result

    ordered: list[ToolResult] = []
    for call in calls:
        result = by_id.pop(call.tool_call_id, None)
        if result is None:
            raise ToolPairingError(
                f"missing ToolResult for tool_call_id {call.tool_call_id}"
            )
        if result.tool_name != call.tool_name:
            raise ToolPairingError(
                f"ToolResult for {call.tool_call_id} reports tool "
                f"{result.tool_name!r} but the call proposed {call.tool_name!r}"
            )
        ordered.append(result)

    if by_id:
        unmatched = ", ".join(sorted(by_id))
        raise ToolPairingError(f"ToolResult without a matching call: {unmatched}")
    return tuple(ordered)


__all__ = [
    "PermissionScope",
    "ProposedToolName",
    "ToolCall",
    "ToolConcurrency",
    "ToolDescription",
    "ToolIdempotency",
    "ToolName",
    "ToolResult",
    "ToolResultStatus",
    "ToolRisk",
    "ToolSpec",
    "align_results",
    "argument_digest",
    "canonical_arguments",
]
