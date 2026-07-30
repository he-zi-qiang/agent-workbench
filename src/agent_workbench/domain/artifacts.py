"""References to stored bytes.

Large tool output, uploaded documents, generated reports and compaction
summaries never travel inside messages, events or graph state. They are written
to the artifact store and referenced by id.

An ``ArtifactRef`` deliberately carries no URL, bucket or filesystem path. The
store resolves an id against server-generated object keys; a client-supplied
path is exactly how path traversal and cross-tenant reads enter a system. The
``filename`` field is display metadata only, and is validated as such.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, field_validator

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import VersionedModel

ArtifactKind = Literal[
    "source_document",
    "tool_result",
    "agent_outcome",
    "report",
    "compaction_summary",
    "task_input",
    "evidence_bundle",
]

FILENAME_MAX_LENGTH: Final[int] = 255

MediaType = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z]+/[A-Za-z0-9][A-Za-z0-9.+_-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]


class ArtifactRef(VersionedModel):
    """Immutable pointer to one stored, content-addressed object."""

    artifact_id: Identifier
    tenant_id: Identifier
    kind: ArtifactKind
    media_type: MediaType
    size_bytes: int = Field(ge=0)
    sha256: Sha256
    filename: str | None = None

    @field_validator("sha256")
    @classmethod
    def normalize_digest(cls, value: str) -> str:
        return value.lower()

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or len(cleaned) > FILENAME_MAX_LENGTH:
            raise ValueError("filename must be 1..255 characters")
        if any(ord(character) < 32 or character == "\x7f" for character in cleaned):
            raise ValueError("filename must not contain control characters")
        # Display metadata must not be usable as a location.
        if "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."}:
            raise ValueError("filename must not contain path separators")
        return cleaned


__all__ = [
    "FILENAME_MAX_LENGTH",
    "ArtifactKind",
    "ArtifactRef",
    "MediaType",
    "Sha256",
]
