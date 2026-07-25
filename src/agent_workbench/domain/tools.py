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
from agent_workbench.domain.schema import (
    BoundedText,
    JsonObject,
    VersionedModel,
)

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
    output_schema: JsonObject | None = None
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
    """

    tool_call_id: Identifier
    tool_name: ProposedToolName
    status: ToolResultStatus
    content: BoundedText = ""
    artifact: ArtifactRef | None = None
    error: ErrorInfo | None = None
    duration_ms: int | None = Field(default=None, ge=0)

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
        duration_ms: int | None = None,
    ) -> ToolResult:
        return cls(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            status="ok",
            content=content,
            artifact=artifact,
            duration_ms=duration_ms,
        )

    @classmethod
    def failed(
        cls,
        call: ToolCall,
        error: ErrorInfo,
        *,
        content: str = "",
        duration_ms: int | None = None,
    ) -> ToolResult:
        return cls(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            status="error",
            content=content,
            error=error,
            duration_ms=duration_ms,
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
