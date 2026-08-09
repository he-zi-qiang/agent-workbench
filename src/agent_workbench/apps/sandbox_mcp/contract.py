"""Closed, bounded input contract for ``run_python`` (ADR-029 §3.1, §3.3).

Files in, files out. The schema carries a script and a list of named byte
blobs, and nothing else: no path, no tenant, no owner, no artifact identifier,
no workspace version. A process that cannot name a tenant cannot write under
one, which is the same property ADR-026 gave the Word renderer and the reason
both servers are safe to run beside the Worker rather than inside it.

The name rule is a deliberate copy of the workspace's, not an import of it.
``domain.workspace`` is exactly the thing ADR-029 §3.1 says this process does
not know about, so the constraint is restated here with its own justification:
these names become filenames inside the container, and a name that can spell a
separator is a name that can escape the working directory.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Final, cast

from pydantic import TypeAdapter, ValidationError

from agent_workbench.domain.errors import ToolInputInvalidError
from agent_workbench.domain.schema import JsonObject, JsonValue
from agent_workbench.runtime.schema_validation import validate_arguments

#: A model-authored program. Large enough for a real transformation script,
#: small enough that it cannot be used as a data channel -- bulk data belongs
#: in ``inputs``, where it is counted against the input ceilings.
MAX_SCRIPT_CHARS: Final[int] = 40_000

MAX_INPUT_FILES: Final[int] = 32
MAX_INPUT_FILE_BYTES: Final[int] = 4 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES: Final[int] = 16 * 1024 * 1024

#: Flat, printable, no separator of any kind, first character alphanumeric so
#: neither ``.`` nor ``..`` is spellable. Same shape as a workspace name; see
#: the module docstring for why it is restated rather than imported.
NAME_PATTERN: Final[str] = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"
MAX_NAME_LENGTH: Final[int] = 128


def base64_length(byte_count: int) -> int:
    """How many base64 characters ``byte_count`` bytes occupy, with padding."""

    return 4 * ((byte_count + 2) // 3)


_NAME_SCHEMA: Final[JsonObject] = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_NAME_LENGTH,
    "pattern": NAME_PATTERN,
    "description": (
        "A flat file name. No directories and no path separators; the file "
        "appears in the script's working directory under exactly this name."
    ),
}

RUN_PYTHON_INPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "title": "Sandboxed Python execution request",
    "description": (
        "A Python script plus the files it reads. The script runs in a "
        "throwaway container with no network access and no state from any "
        "previous call. No path, URL, tenant, owner, or artifact field is "
        "accepted."
    ),
    "properties": {
        "script": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SCRIPT_CHARS,
            "description": (
                "Python 3 source. It runs with the input files in its working "
                "directory; files it writes there are returned as outputs."
            ),
        },
        "inputs": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_INPUT_FILES,
            "description": "Files placed in the working directory before the run.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": _NAME_SCHEMA,
                    "content_base64": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": base64_length(MAX_INPUT_FILE_BYTES),
                        "description": "The file's bytes, base64-encoded.",
                    },
                },
                "required": ["name", "content_base64"],
            },
        },
    },
    "required": ["script"],
    "additionalProperties": False,
}

RUN_PYTHON_OUTPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "title": "Sandboxed Python execution result",
    "properties": {
        "exit_code": {"type": "integer"},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "outputs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": _NAME_SCHEMA,
                    "content_base64": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                },
                "required": ["name", "content_base64", "size_bytes"],
            },
        },
    },
    "required": ["exit_code", "stdout", "stderr", "outputs"],
    "additionalProperties": False,
}

_JSON_OBJECT: Final[TypeAdapter[JsonObject]] = TypeAdapter(JsonObject)


class SandboxInputError(ValueError):
    """A request failed the public schema or a cross-field invariant."""


@dataclass(frozen=True, slots=True)
class SandboxFile:
    name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    script: str
    inputs: tuple[SandboxFile, ...]


def parse_run_request(arguments: object) -> SandboxRequest:
    """Validate and decode one untrusted MCP argument object.

    Error messages identify the failing location and never echo script text or
    file content: they travel into protocol results, operator logs and back
    into the model's own context.
    """

    try:
        payload = _JSON_OBJECT.validate_python(arguments, strict=True)
    except ValidationError as error:
        raise SandboxInputError("arguments must contain only JSON values") from error

    try:
        validate_arguments(RUN_PYTHON_INPUT_SCHEMA, payload)
    except ToolInputInvalidError as error:
        raise SandboxInputError(str(error)) from None

    script = cast(str, payload["script"])
    if not script.strip():
        raise SandboxInputError("arguments.script: must contain non-whitespace text")

    raw_inputs = cast(list[JsonValue], payload.get("inputs") or [])
    inputs = tuple(
        _parse_file(cast(dict[str, JsonValue], item), index)
        for index, item in enumerate(raw_inputs)
    )

    seen: set[str] = set()
    for index, file in enumerate(inputs):
        if file.name in seen:
            # Two entries for one name would leave which bytes land on disk to
            # dictionary order. Refused rather than resolved: a caller that
            # meant one of them should say which.
            raise SandboxInputError(f"arguments.inputs[{index}].name: is a duplicate")
        seen.add(file.name)

    total = sum(len(file.content) for file in inputs)
    if total > MAX_TOTAL_INPUT_BYTES:
        raise SandboxInputError(
            f"arguments.inputs exceeds the {MAX_TOTAL_INPUT_BYTES}-byte total limit"
        )
    return SandboxRequest(script=script, inputs=inputs)


def _parse_file(value: dict[str, JsonValue], index: int) -> SandboxFile:
    prefix = f"arguments.inputs[{index}]"
    name = cast(str, value["name"])
    encoded = cast(str, value["content_base64"])
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise SandboxInputError(f"{prefix}.content_base64: is not valid base64") from (
            error
        )
    if len(content) > MAX_INPUT_FILE_BYTES:
        raise SandboxInputError(
            f"{prefix}.content_base64: decodes to more than "
            f"{MAX_INPUT_FILE_BYTES} bytes"
        )
    return SandboxFile(name=name, content=content)


__all__ = [
    "MAX_INPUT_FILES",
    "MAX_INPUT_FILE_BYTES",
    "MAX_NAME_LENGTH",
    "MAX_SCRIPT_CHARS",
    "MAX_TOTAL_INPUT_BYTES",
    "NAME_PATTERN",
    "RUN_PYTHON_INPUT_SCHEMA",
    "RUN_PYTHON_OUTPUT_SCHEMA",
    "SandboxFile",
    "SandboxInputError",
    "SandboxRequest",
    "base64_length",
    "parse_run_request",
]
