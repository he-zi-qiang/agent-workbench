"""Serialization primitives shared by every domain object.

Domain objects cross process, storage and protocol boundaries: the same value
becomes a PostgreSQL row, an SSE frame and a LangGraph checkpoint entry. Two
properties therefore matter more than convenience.

Every aggregate that is serialized on its own carries an explicit schema
version, so a consumer never has to guess which contract produced a payload.
And no domain object accepts a field it does not know: an unexpected key means
a producer and a consumer disagree, which is a defect to surface at the
boundary rather than data to silently drop.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

DOMAIN_SCHEMA_VERSION: Final[int] = 1

# Tool arguments, tool output and policy overrides are user- and model-supplied
# structures. They stay JSON values inside the domain; only an adapter is
# allowed to turn them into a vendor object.
JsonObject = dict[str, JsonValue]

# Free text copied into events, errors or model context is always bounded. An
# unbounded string is an unbounded database row, an unbounded SSE frame and an
# unbounded prompt at the same time.
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
BoundedText = Annotated[str, StringConstraints(max_length=4096)]


class DomainModel(BaseModel):
    """Immutable value object with a closed field set."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        # Rejected input is echoed into ValidationError by default. Domain
        # objects carry document text, tool arguments and model output, so the
        # input stays out of the error surface.
        hide_input_in_errors=True,
    )


class VersionedModel(DomainModel):
    """Aggregate that is persisted or transmitted as a standalone payload."""

    schema_version: int = Field(default=DOMAIN_SCHEMA_VERSION, ge=1)

    @field_validator("schema_version")
    @classmethod
    def reject_unsupported_schema_version(cls, value: int) -> int:
        # Fail closed instead of best-effort parsing: a payload written by a
        # different contract version is a migration decision, not a fallback.
        if value != DOMAIN_SCHEMA_VERSION:
            raise ValueError(
                "unsupported domain schema version: expected "
                f"{DOMAIN_SCHEMA_VERSION}, received {value}"
            )
        return value


__all__ = [
    "DOMAIN_SCHEMA_VERSION",
    "BoundedText",
    "DomainModel",
    "JsonObject",
    "ShortText",
    "VersionedModel",
]
