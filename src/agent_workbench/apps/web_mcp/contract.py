"""Closed, bounded input contracts for ``fetch_page`` and ``download_document``.

Both tools take a URL and nothing else that names storage: no path, no tenant,
no owner, no artifact identifier. That is the same shape ADR-026 gave the Word
renderer and it holds for the same reason -- a process that cannot name a tenant
cannot write under one, so the only thing this server can do with what it reads
is hand it back.

The schemas use only the keywords ``runtime.schema_validation`` can enforce.
That is not politeness towards the gate: a tool this repository ships that
failed its own gate would be dropped from the registry at Worker startup, which
is a comical way to lose a capability and exactly what would happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from pydantic import TypeAdapter, ValidationError

from agent_workbench.domain.errors import ToolInputInvalidError
from agent_workbench.domain.schema import JsonObject
from agent_workbench.runtime.schema_validation import validate_arguments

#: Matches the ceiling the evidence store already applies to a URL. A URL is the
#: one field that must never be shortened to fit -- a cut URL is a different
#: address, not a shorter one.
MAX_URL_CHARS: Final[int] = 2048

#: What one ``fetch_page`` may put into the model's context, and the most a
#: caller may ask for. The default is generous because pages carry far more
#: navigation than content, and the readable part often sits below it.
DEFAULT_PAGE_CHARS: Final[int] = 20_000
MIN_PAGE_CHARS: Final[int] = 500
MAX_PAGE_CHARS: Final[int] = 50_000

#: What ``download_document`` will carry back. Set below the 10 MiB default of
#: ``policy.max_tool_result_bytes`` so the refusal happens here, with a reason
#: naming the limit, rather than at the adapter boundary as an opaque
#: "result too large" after the whole file has already crossed the wire.
MAX_DOCUMENT_BYTES: Final[int] = 8 * 1024 * 1024

#: Bytes read before the ceiling is enforced. A server that streams forever
#: would otherwise be bounded only by the reader's patience.
MAX_PAGE_BYTES: Final[int] = 4 * 1024 * 1024

_URL_SCHEMA: Final[JsonObject] = {
    "type": "string",
    "minLength": 8,
    "maxLength": MAX_URL_CHARS,
    # Scheme only. Everything else about the destination -- that it resolves,
    # and that it resolves somewhere publicly routable -- is decided by the
    # address guard at request time, because it cannot be decided from text.
    "pattern": r"^https?://",
    "description": "An absolute http:// or https:// URL.",
}

FETCH_PAGE_INPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "title": "Readable page text request",
    "description": (
        "Read one web page and return its readable text. No path, tenant, "
        "owner, or artifact field is accepted."
    ),
    "properties": {
        "url": _URL_SCHEMA,
        "max_chars": {
            "type": "integer",
            "minimum": MIN_PAGE_CHARS,
            "maximum": MAX_PAGE_CHARS,
            "default": DEFAULT_PAGE_CHARS,
            "description": "How much readable text to return.",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

DOWNLOAD_DOCUMENT_INPUT_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "title": "Document download request",
    "description": (
        "Download one file by URL and return its bytes. No path, tenant, "
        "owner, or artifact field is accepted."
    ),
    "properties": {"url": _URL_SCHEMA},
    "required": ["url"],
    "additionalProperties": False,
}

_JSON_OBJECT: Final[TypeAdapter[JsonObject]] = TypeAdapter(JsonObject)


class WebRequestInputError(ValueError):
    """A request failed the public schema."""


@dataclass(frozen=True, slots=True)
class FetchPageRequest:
    url: str
    max_chars: int


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    url: str


def parse_fetch_page_request(arguments: object) -> FetchPageRequest:
    payload = _validated(arguments, FETCH_PAGE_INPUT_SCHEMA)
    raw = payload.get("max_chars")
    return FetchPageRequest(
        url=cast(str, payload["url"]).strip(),
        max_chars=cast(int, raw) if isinstance(raw, int) else DEFAULT_PAGE_CHARS,
    )


def parse_download_request(arguments: object) -> DownloadRequest:
    payload = _validated(arguments, DOWNLOAD_DOCUMENT_INPUT_SCHEMA)
    return DownloadRequest(url=cast(str, payload["url"]).strip())


def _validated(arguments: object, schema: JsonObject) -> JsonObject:
    """Validate one untrusted MCP argument object against ``schema``.

    Error messages name the failing location and never echo the URL: they
    travel into protocol results, operator logs and back into the model's own
    context, and a URL is the part an injected instruction wants confirmed.
    """

    try:
        payload = _JSON_OBJECT.validate_python(arguments, strict=True)
    except ValidationError as error:
        raise WebRequestInputError("arguments must contain only JSON values") from error
    try:
        validate_arguments(schema, payload)
    except ToolInputInvalidError as error:
        raise WebRequestInputError(str(error)) from None
    return payload


__all__ = [
    "DEFAULT_PAGE_CHARS",
    "DOWNLOAD_DOCUMENT_INPUT_SCHEMA",
    "FETCH_PAGE_INPUT_SCHEMA",
    "MAX_DOCUMENT_BYTES",
    "MAX_PAGE_BYTES",
    "MAX_PAGE_CHARS",
    "MAX_URL_CHARS",
    "MIN_PAGE_CHARS",
    "DownloadRequest",
    "FetchPageRequest",
    "WebRequestInputError",
    "parse_download_request",
    "parse_fetch_page_request",
]
