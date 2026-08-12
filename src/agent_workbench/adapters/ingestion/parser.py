"""Getting plain text out of an uploaded document.

Three formats: UTF-8 text, Markdown, and PDF. The first two need no library.
The third is a dependency and a whole class of failure of its own, and this
module refuses each of those failures by name rather than letting them through
as text:

* an encrypted file has no readable content until somebody supplies a password,
  and this build has nowhere to put one;
* a scanned page has no text layer at all, so extraction "succeeds" and returns
  nothing -- which would chunk into nothing, embed into nothing, and index a
  document that can never be retrieved while every layer reported success;
* a producer that lays text out in columns or tables returns it in an order
  nobody wrote. That one cannot be refused, only stated: what is extracted is
  what the producer's content stream says, in its order.

Markdown is deliberately not rendered. Stripping syntax would shift every
character offset, and a citation's offsets have to index the text that was
actually chunked, or "here is where I got that" points somewhere else.

PDF makes that same point sharper, and it is why pages are tracked. The
extracted text is not the stored bytes -- a character offset into it indexes the
extraction, not the file -- so an offset alone cannot send a reader to the right
place in the original. A page number can. ``ParsedDocument`` therefore reports
where each page begins in the extracted text, and the chunker turns that into
the page a chunk sits on. Formats without pages report nothing and keep the
offsets they already had.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

from agent_workbench.adapters.documents.docx import (
    DocxTooLargeError,
    extract_docx_preview,
)
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

PDF_MEDIA_TYPE = "application/pdf"

#: What a browser sends for a .docx. The long one is the real registered
#: type; the short alias turns up from some uploaders and from `file`-style
#: sniffers, and refusing it would refuse a file this build can read.
DOCX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }
)

#: Every media type this build reads, for the refusal message and for callers
#: that want to check before uploading.
SUPPORTED_MEDIA_TYPES = frozenset(
    {*TEXT_MEDIA_TYPES, PDF_MEDIA_TYPE, *DOCX_MEDIA_TYPES}
)

#: What separates one page's text from the next. The page boundaries are
#: carried structurally in ``page_starts`` rather than by being recoverable
#: from the text, so this only has to be something that reads sanely.
PAGE_SEPARATOR = "\n"


class UnsupportedMediaTypeError(ToolInputInvalidError):
    """The document is in a format this build cannot read."""


class UnreadableDocumentError(ToolInputInvalidError):
    """The format is supported but this particular file yields no text.

    Separate from ``UnsupportedMediaTypeError`` because the answers differ: one
    means "send a different kind of file", the other means "this PDF is
    encrypted, or is a scan, and needs OCR this build does not do".
    """


class TextDocumentParser:
    """Reads UTF-8 text, Markdown and PDF, and refuses everything else."""

    def parse(self, content: bytes, *, media_type: str) -> ParsedDocument:
        """Decode a document, or refuse it.

        Raises ``UnsupportedMediaTypeError`` for a format this build cannot
        read and for bytes that are not valid UTF-8, and
        ``UnreadableDocumentError`` for a supported format that yields nothing.
        """

        return parse(content, media_type=media_type)


def parse(content: bytes, *, media_type: str) -> ParsedDocument:
    """Decode a document, or refuse it."""

    base = media_type.split(";", 1)[0].strip().lower()
    if base == PDF_MEDIA_TYPE:
        return _parse_pdf(content)
    if base in DOCX_MEDIA_TYPES:
        return _parse_docx(content, media_type=base)
    if base not in TEXT_MEDIA_TYPES:
        raise UnsupportedMediaTypeError(
            f"this build reads {', '.join(sorted(SUPPORTED_MEDIA_TYPES))}, not {base!r}"
        )

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedMediaTypeError(
            f"the document declared {base!r} but is not valid UTF-8"
        ) from exc

    return ParsedDocument(text=text, media_type=base)


def _parse_docx(content: bytes, *, media_type: str) -> ParsedDocument:
    """Read a .docx through the one implementation this build has.

    It calls the same extractor the preview panel calls, deliberately. There is
    exactly one place in this repository that opens a ``.docx``, and its
    preflight -- entry count, expanded size, compression ratio -- is what keeps
    a zip bomb from turning an upload into a multi-hundred-megabyte allocation
    inside an ingestion worker. A second reader here would be a second place to
    forget those three gates, and the symptom would surface far from the cause.

    ``max_chars=None`` is the one thing that differs from the preview. The
    panel stops at forty thousand characters because a reader who wants the
    whole report wants the file; this path is about to chunk and index the
    document, and a ceiling here would index a fraction of it while every layer
    reported success.

    **No page starts, and that is a property of the format rather than an
    omission.** A .docx stores no pagination -- where a page breaks is decided
    by whichever renderer lays it out, with its fonts and its paper size, so
    two readers of one file legitimately disagree. Citations into a Word
    document therefore fall back to character offsets, which
    ``ParsedDocument`` already expresses by leaving ``page_starts`` empty.
    """

    try:
        extracted = extract_docx_preview(content, max_chars=None)
    except DocxTooLargeError as oversized:
        # Its own refusal, not "unsupported": the format is one this build
        # reads, and the answer is a smaller file rather than a different kind.
        raise UnreadableDocumentError(str(oversized)) from oversized
    except (KeyError, ValueError, zipfile.BadZipFile) as unreadable:
        # python-docx raises through several families for a file that is not a
        # Word package -- a renamed .doc, a truncated upload, a zip whose
        # document part is missing. All of them mean the same thing here.
        raise UnreadableDocumentError(
            f"this .docx could not be read: {type(unreadable).__name__}"
        ) from unreadable

    if not extracted.text.strip():
        # Same shape as the scanned-PDF refusal above, and for the same reason:
        # a document with no extractable text chunks into nothing, embeds into
        # nothing, and is indexed as something that can never be retrieved
        # while every layer reports success.
        raise UnreadableDocumentError(
            "this .docx has no extractable text; it may hold only images"
        )

    return ParsedDocument(text=extracted.text, media_type=media_type)


def _parse_pdf(content: bytes) -> ParsedDocument:
    """Extract a PDF's text layer, page by page, or refuse the file.

    Page starts are recorded as the text is assembled rather than searched for
    afterwards: the separator also occurs inside a page's own text, so finding
    the boundaries again would be ambiguous -- and the boundaries are the one
    thing here a citation depends on.
    """

    from pypdf import PdfReader

    # ``PyPdfError`` is the library's root: PdfReadError, PdfStreamError,
    # EmptyFileError and the rest all derive from it. Catching the base rather
    # than a list means a malformed file this build has not met yet is still a
    # refusal rather than a traceback out of an ingestion worker.
    from pypdf.errors import PyPdfError

    # BytesIO rather than a path: the bytes came from the artifact store, and
    # a PDF is a format with an appetite for external references. Handing the
    # reader something with no path, no name and no file descriptor is the
    # cheapest way to be sure it resolves none of them.
    try:
        reader = PdfReader(BytesIO(content))
    except PyPdfError as exc:
        raise UnreadableDocumentError(f"this PDF could not be read: {exc}") from exc

    if reader.is_encrypted:
        # Refused rather than attempted with an empty password. A file that
        # opens under "" is still a file somebody chose to encrypt, and
        # indexing it would put its contents somewhere the encryption says
        # they should not be.
        raise UnreadableDocumentError(
            "this PDF is encrypted; decrypt it before uploading"
        )

    pages: list[str] = []
    starts: list[int] = []
    offset = 0
    for page in reader.pages:
        try:
            extracted = page.extract_text() or ""
        except PyPdfError as exc:  # pragma: no cover - producer-specific
            raise UnreadableDocumentError(
                f"a page of this PDF could not be read: {exc}"
            ) from exc
        starts.append(offset)
        pages.append(extracted)
        offset += len(extracted) + len(PAGE_SEPARATOR)

    text = PAGE_SEPARATOR.join(pages)
    if not text.strip():
        # Extraction succeeded and produced nothing, which is what a scan looks
        # like. Accepting it would index a document that can never be
        # retrieved, with every layer downstream reporting success.
        raise UnreadableDocumentError(
            "this PDF has no text layer; it is probably a scan and needs OCR, "
            "which this build does not do"
        )

    return ParsedDocument(
        text=text,
        media_type=PDF_MEDIA_TYPE,
        page_starts=tuple(starts),
    )


__all__ = [
    "PAGE_SEPARATOR",
    "PDF_MEDIA_TYPE",
    "SUPPORTED_MEDIA_TYPES",
    "TEXT_MEDIA_TYPES",
    "TextDocumentParser",
    "UnreadableDocumentError",
    "UnsupportedMediaTypeError",
    "parse",
]
