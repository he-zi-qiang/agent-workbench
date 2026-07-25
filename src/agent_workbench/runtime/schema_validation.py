"""A deliberately small JSON Schema subset, validated in-process.

Tool input schemas are authored in this repository, and the runtime needs to
check maybe eight keywords against them. A full JSON Schema implementation
would be a large dependency for that, so this module implements the subset and
-- the part that matters -- refuses any schema that reaches beyond it.

Silent under-validation is the failure mode worth designing against. A
validator that ignored ``oneOf`` would report every call as valid while
enforcing nothing, so unsupported keywords are rejected when the gateway is
built, not skipped when a call arrives. Adding a keyword is then a deliberate
change here rather than an accident in a tool definition.

Error messages name the path and the expectation, never the value. Tool
arguments carry user text, document ids and search queries, and these messages
travel into events, into operator logs and back into the model's own context.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import ClassVar, Final

from pydantic import JsonValue

from agent_workbench.domain.errors import (
    AgentWorkbenchError,
    ErrorCode,
    ToolInputInvalidError,
)
from agent_workbench.domain.schema import JsonObject

SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        # structure
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        # value constraints
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        # documentation only, ignored during validation
        "title",
        "description",
        "default",
        "examples",
    }
)

SUPPORTED_TYPES: Final[frozenset[str]] = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


class UnsupportedToolSchema(AgentWorkbenchError):
    """A tool declares a schema this validator cannot enforce."""

    code: ClassVar[ErrorCode] = "internal_error"


def _is_integer(value: object) -> bool:
    # bool is a subclass of int; a flag is not a number here.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


TYPE_PREDICATES: Final[Mapping[str, Callable[[JsonValue], bool]]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _int_of(schema: JsonObject, key: str) -> int | None:
    value = schema.get(key)
    return value if _is_integer(value) and isinstance(value, int) else None


def _float_of(schema: JsonObject, key: str) -> float | None:
    value = schema.get(key)
    if _is_number(value) and isinstance(value, int | float):
        return float(value)
    return None


def assert_schema_supported(schema: JsonObject, *, origin: str) -> None:
    """Reject a schema that uses anything this module cannot enforce.

    Called once per registered tool when the gateway is assembled, so an
    unsupported schema stops the process instead of quietly weakening every
    call to that tool.
    """

    _assert_supported(schema, origin=origin, path="")


def _assert_supported(schema: JsonObject, *, origin: str, path: str) -> None:
    where = f"{origin}{f' at {path}' if path else ''}"
    unsupported = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unsupported:
        raise UnsupportedToolSchema(
            f"{where} uses unsupported JSON Schema keywords: " + ", ".join(unsupported)
        )

    declared = schema.get("type")
    if declared is not None:
        if not isinstance(declared, str):
            raise UnsupportedToolSchema(f"{where} must declare a single type")
        if declared not in SUPPORTED_TYPES:
            raise UnsupportedToolSchema(f"{where} declares unknown type {declared}")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise UnsupportedToolSchema(f"{where} has a malformed properties table")
        for name, child in properties.items():
            if not isinstance(child, dict):
                raise UnsupportedToolSchema(f"{where} property {name} is not a schema")
            _assert_supported(
                child,
                origin=origin,
                path=f"{path}.{name}" if path else name,
            )

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise UnsupportedToolSchema(f"{where} items must be a single schema")
        _assert_supported(items, origin=origin, path=f"{path}[]")


def validate_arguments(schema: JsonObject, arguments: JsonObject) -> None:
    """Raise :class:`ToolInputInvalidError` if the arguments do not fit."""

    _validate(arguments, schema, path="arguments")


def _fail(path: str, expectation: str) -> None:
    raise ToolInputInvalidError(f"{path}: {expectation}")


def _validate(value: JsonValue, schema: JsonObject, *, path: str) -> None:
    declared = schema.get("type")
    if isinstance(declared, str) and not TYPE_PREDICATES[declared](value):
        _fail(path, f"expected {declared}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        _fail(path, "is not one of the permitted values")

    if isinstance(value, str):
        _validate_string(value, schema, path=path)
    elif _is_number(value) and isinstance(value, int | float):
        _validate_number(float(value), schema, path=path)

    if isinstance(value, dict):
        _validate_object(value, schema, path=path)
    elif isinstance(value, list):
        _validate_array(value, schema, path=path)


def _validate_string(value: str, schema: JsonObject, *, path: str) -> None:
    minimum = _int_of(schema, "minLength")
    if minimum is not None and len(value) < minimum:
        _fail(path, f"is shorter than {minimum} characters")

    maximum = _int_of(schema, "maxLength")
    if maximum is not None and len(value) > maximum:
        _fail(path, f"is longer than {maximum} characters")

    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        _fail(path, "does not match the required pattern")


def _validate_number(value: float, schema: JsonObject, *, path: str) -> None:
    minimum = _float_of(schema, "minimum")
    if minimum is not None and value < minimum:
        _fail(path, f"is below the minimum of {minimum}")

    maximum = _float_of(schema, "maximum")
    if maximum is not None and value > maximum:
        _fail(path, f"is above the maximum of {maximum}")


def _validate_object(
    value: dict[str, JsonValue],
    schema: JsonObject,
    *,
    path: str,
) -> None:
    properties = schema.get("properties")
    table: dict[str, JsonValue] = properties if isinstance(properties, dict) else {}

    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in value:
                _fail(f"{path}.{name}", "is required")

    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(value) - set(table))
        if unexpected:
            _fail(path, "has unexpected properties: " + ", ".join(unexpected))

    for name, child_value in value.items():
        child_schema = table.get(name)
        if isinstance(child_schema, dict):
            _validate(child_value, child_schema, path=f"{path}.{name}")


def _validate_array(value: list[JsonValue], schema: JsonObject, *, path: str) -> None:
    minimum = _int_of(schema, "minItems")
    if minimum is not None and len(value) < minimum:
        _fail(path, f"has fewer than {minimum} items")

    maximum = _int_of(schema, "maxItems")
    if maximum is not None and len(value) > maximum:
        _fail(path, f"has more than {maximum} items")

    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            _validate(item, items, path=f"{path}[{index}]")


__all__ = [
    "SUPPORTED_KEYWORDS",
    "SUPPORTED_TYPES",
    "UnsupportedToolSchema",
    "assert_schema_supported",
    "validate_arguments",
]
