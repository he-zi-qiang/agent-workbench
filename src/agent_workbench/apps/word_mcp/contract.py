"""Closed, bounded input contract for ``render_document``.

The MCP process accepts document *content*, never storage coordinates.  In
particular, paths, URLs, tenant identifiers, owners and artifact identifiers
are absent from the schema.  Artifact ownership remains the caller's concern
at the existing MCP result-to-ArtifactStore boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from pydantic import TypeAdapter, ValidationError

from agent_workbench.domain.errors import ToolInputInvalidError
from agent_workbench.domain.schema import JsonObject, JsonValue
from agent_workbench.runtime.schema_validation import validate_arguments

MAX_TOTAL_CHARACTERS: Final[int] = 30_000
MAX_SECTIONS: Final[int] = 12
MAX_PARAGRAPHS_PER_SECTION: Final[int] = 8
MAX_BULLETS_PER_SECTION: Final[int] = 12
MAX_TABLE_COLUMNS: Final[int] = 6
MAX_TABLE_ROWS: Final[int] = 20

_STRING_100: Final[JsonObject] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 100,
}
_STRING_500: Final[JsonObject] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 500,
}

RENDER_DOCUMENT_INPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "title": "Word document request",
    "description": (
        "Bounded structured content for an in-memory Word document. "
        "No path, URL, tenant, owner, or artifact field is accepted."
    ),
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Document title.",
        },
        "subtitle": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "description": "Optional subtitle shown in the memo masthead.",
        },
        "sections": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_SECTIONS,
            "description": "Ordered document sections.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "heading": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 160,
                    },
                    "paragraphs": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_PARAGRAPHS_PER_SECTION,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2_000,
                        },
                    },
                    "bullets": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_BULLETS_PER_SECTION,
                        "items": _STRING_500,
                    },
                    "table": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "headers": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": MAX_TABLE_COLUMNS,
                                "items": _STRING_100,
                            },
                            "rows": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": MAX_TABLE_ROWS,
                                "items": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": MAX_TABLE_COLUMNS,
                                    "items": _STRING_500,
                                },
                            },
                        },
                        "required": ["headers", "rows"],
                    },
                },
                "required": ["heading"],
            },
        },
    },
    "required": ["title", "sections"],
    "additionalProperties": False,
}

_JSON_OBJECT: Final[TypeAdapter[JsonObject]] = TypeAdapter(JsonObject)


class WordDocumentInputError(ValueError):
    """A request failed the public schema or a cross-field invariant."""


@dataclass(frozen=True, slots=True)
class SimpleTable:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class DocumentSection:
    heading: str
    paragraphs: tuple[str, ...]
    bullets: tuple[str, ...]
    table: SimpleTable | None


@dataclass(frozen=True, slots=True)
class DocumentRequest:
    title: str
    subtitle: str | None
    sections: tuple[DocumentSection, ...]


def parse_document_request(arguments: object) -> DocumentRequest:
    """Validate and normalize one untrusted MCP argument object.

    Error messages identify only the failing location and never echo document
    text.  Leading/trailing whitespace is normalized so retries produce the
    same package for semantically identical inputs.
    """

    try:
        payload = _JSON_OBJECT.validate_python(arguments, strict=True)
    except ValidationError as error:
        raise WordDocumentInputError(
            "arguments must contain only JSON values"
        ) from error

    try:
        validate_arguments(RENDER_DOCUMENT_INPUT_SCHEMA, payload)
    except ToolInputInvalidError as error:
        raise WordDocumentInputError(str(error)) from None

    title = _nonblank(cast(str, payload["title"]), "arguments.title")
    raw_subtitle = payload.get("subtitle")
    subtitle = (
        _nonblank(cast(str, raw_subtitle), "arguments.subtitle")
        if raw_subtitle is not None
        else None
    )
    raw_sections = cast(list[JsonValue], payload["sections"])
    sections = tuple(
        _parse_section(cast(dict[str, JsonValue], item), index)
        for index, item in enumerate(raw_sections)
    )
    request = DocumentRequest(title=title, subtitle=subtitle, sections=sections)
    if _character_count(request) > MAX_TOTAL_CHARACTERS:
        raise WordDocumentInputError(
            f"arguments exceeds the {MAX_TOTAL_CHARACTERS}-character document limit"
        )
    return request


def _parse_section(value: dict[str, JsonValue], index: int) -> DocumentSection:
    prefix = f"arguments.sections[{index}]"
    heading = _nonblank(cast(str, value["heading"]), f"{prefix}.heading")
    paragraphs = _strings(value.get("paragraphs"), f"{prefix}.paragraphs")
    bullets = _strings(value.get("bullets"), f"{prefix}.bullets")
    raw_table = value.get("table")
    table = (
        _parse_table(cast(dict[str, JsonValue], raw_table), f"{prefix}.table")
        if raw_table is not None
        else None
    )
    if not paragraphs and not bullets and table is None:
        raise WordDocumentInputError(
            f"{prefix}: expected paragraphs, bullets, or table content"
        )
    return DocumentSection(
        heading=heading,
        paragraphs=paragraphs,
        bullets=bullets,
        table=table,
    )


def _parse_table(value: dict[str, JsonValue], path: str) -> SimpleTable:
    headers = _strings(value["headers"], f"{path}.headers")
    raw_rows = cast(list[JsonValue], value["rows"])
    rows = tuple(
        _strings(row, f"{path}.rows[{index}]") for index, row in enumerate(raw_rows)
    )
    for index, row in enumerate(rows):
        if len(row) != len(headers):
            raise WordDocumentInputError(
                f"{path}.rows[{index}]: expected {len(headers)} cells"
            )
    return SimpleTable(headers=headers, rows=rows)


def _strings(value: JsonValue | None, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(
        _nonblank(cast(str, item), f"{path}[{index}]")
        for index, item in enumerate(cast(list[JsonValue], value))
    )


def _nonblank(value: str, path: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise WordDocumentInputError(f"{path}: must contain non-whitespace text")
    return normalized


def _character_count(request: DocumentRequest) -> int:
    values = [request.title]
    if request.subtitle is not None:
        values.append(request.subtitle)
    for section in request.sections:
        values.extend((section.heading, *section.paragraphs, *section.bullets))
        if section.table is not None:
            values.extend(section.table.headers)
            values.extend(cell for row in section.table.rows for cell in row)
    return sum(len(value) for value in values)


__all__ = [
    "MAX_TOTAL_CHARACTERS",
    "RENDER_DOCUMENT_INPUT_SCHEMA",
    "DocumentRequest",
    "DocumentSection",
    "SimpleTable",
    "WordDocumentInputError",
    "parse_document_request",
]
