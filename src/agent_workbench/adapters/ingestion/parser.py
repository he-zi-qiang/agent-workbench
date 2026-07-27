"""Getting plain text out of an uploaded document.

Only the two formats that need no library: UTF-8 text and Markdown. PDF is a
dependency and a whole class of failure of its own -- encrypted files, scanned
pages with no text layer, producers that disagree about where a page ends --
and pretending it belongs in the same function as ``bytes.decode`` would hide
all of that behind a media type check.

Markdown is deliberately not rendered. Stripping syntax would shift every
character offset, and a citation's offsets have to index the bytes that were
actually stored, or "here is where I got that" points somewhere else. What the
parser does instead is refuse what it cannot read, so a document never becomes
chunks of replacement characters that embed into nonsense and retrieve
convincingly.
"""

from __future__ import annotations

from agent_workbench.domain.errors import ToolInputInvalidError
from agent_workbench.ports.ingestion import ParsedDocument

# Media types this parser can turn into text without guessing.
TEXT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
    }
)


class UnsupportedMediaTypeError(ToolInputInvalidError):
    """The document is in a format this build cannot read."""


class TextDocumentParser:
    """Reads UTF-8 text and Markdown, and refuses everything else."""

    def parse(self, content: bytes, *, media_type: str) -> ParsedDocument:
        """Decode a document, or refuse it.

        Raises ``UnsupportedMediaTypeError`` for a format this build cannot
        read, and for bytes that are not valid UTF-8.
        """

        return parse(content, media_type=media_type)


def parse(content: bytes, *, media_type: str) -> ParsedDocument:
    """Decode a document, or refuse it."""

    base = media_type.split(";", 1)[0].strip().lower()
    if base not in TEXT_MEDIA_TYPES:
        raise UnsupportedMediaTypeError(
            f"this build reads {', '.join(sorted(TEXT_MEDIA_TYPES))}, not {base!r}"
        )

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedMediaTypeError(
            f"the document declared {base!r} but is not valid UTF-8"
        ) from exc

    return ParsedDocument(text=text, media_type=base)


__all__ = [
    "TEXT_MEDIA_TYPES",
    "TextDocumentParser",
    "UnsupportedMediaTypeError",
    "parse",
]
