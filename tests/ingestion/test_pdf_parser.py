"""Reading a PDF, and refusing the ones that would index into nothing.

The parser's own docstring used to argue PDF out of scope: a dependency, plus
encrypted files, scans with no text layer, and producers that disagree about
where a page ends. Supporting it means answering each of those in code rather
than behind a media type check, so each has a test here.

The fixtures are real PDFs assembled byte by byte rather than produced by a
writer library. That keeps the test dependency-free, and it makes the page
boundaries something this file states rather than something it discovers --
which is the property under test.
"""

from __future__ import annotations

import io

import pytest

from agent_workbench.adapters.ingestion.parser import (
    PAGE_SEPARATOR,
    PDF_MEDIA_TYPE,
    UnreadableDocumentError,
    UnsupportedMediaTypeError,
    parse,
)
from tests.support.pdf import build_pdf

PAGES = (
    "Reciprocal rank fusion runs inside the database.",
    "PostgreSQL is the authority on document permissions.",
    "An outbox claim is a lease with an expiry.",
)


# --------------------------------------------------------------------------
# What it reads
# --------------------------------------------------------------------------


def test_a_pdf_becomes_its_pages_in_order() -> None:
    parsed = parse(build_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert parsed.media_type == PDF_MEDIA_TYPE
    assert parsed.text == PAGE_SEPARATOR.join(PAGES)


def test_page_starts_index_the_extracted_text() -> None:
    """The offsets have to land on the text, not near it.

    This is what a page number on a citation is built from, so the assertion
    is that slicing at each start returns that page -- not that the count of
    offsets happens to match the count of pages.
    """

    parsed = parse(build_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert len(parsed.page_starts) == len(PAGES)
    for index, start in enumerate(parsed.page_starts):
        assert parsed.text[start : start + len(PAGES[index])] == PAGES[index]


def test_the_first_page_starts_at_zero() -> None:
    parsed = parse(build_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert parsed.page_starts[0] == 0


def test_extraction_is_deterministic() -> None:
    """Chunk boundaries are derived from this text, and boundaries decide ids.

    An extractor that reordered anything between runs would move every chunk
    id in the index without the document having changed.
    """

    first = parse(build_pdf(PAGES), media_type=PDF_MEDIA_TYPE)
    second = parse(build_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert first.text == second.text
    assert first.page_starts == second.page_starts


def test_a_single_page_pdf_still_reports_its_page() -> None:
    parsed = parse(build_pdf((PAGES[0],)), media_type=PDF_MEDIA_TYPE)

    assert parsed.page_starts == (0,)


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_a_pdf_with_no_text_layer_is_refused() -> None:
    """What a scan looks like from here: extraction succeeds and yields nothing.

    Accepting it would index a document that can never be retrieved, with
    every layer downstream reporting success.
    """

    with pytest.raises(UnreadableDocumentError, match="no text layer"):
        parse(build_pdf(("", "")), media_type=PDF_MEDIA_TYPE)


def test_an_encrypted_pdf_is_refused_rather_than_opened() -> None:
    """Not attempted with an empty password.

    A file that opens under "" is still one somebody chose to encrypt, and
    indexing it would put its contents somewhere the encryption says they
    should not be.
    """

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(build_pdf(PAGES))).pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    encrypted = io.BytesIO()
    writer.write(encrypted)

    with pytest.raises(UnreadableDocumentError, match="encrypted"):
        parse(encrypted.getvalue(), media_type=PDF_MEDIA_TYPE)


@pytest.mark.parametrize(
    "content",
    [b"", b"not a pdf at all", b"%PDF-1.4\nbut then nothing useful\n"],
)
def test_bytes_that_are_not_a_pdf_are_refused_not_raised_through(
    content: bytes,
) -> None:
    """An ingestion worker must not take a library traceback for a bad upload."""

    with pytest.raises(UnreadableDocumentError):
        parse(content, media_type=PDF_MEDIA_TYPE)


def test_a_pdf_declared_as_text_is_still_refused_for_not_being_utf8() -> None:
    """The declared type is what selects the reader, and it was wrong here."""

    with pytest.raises(UnsupportedMediaTypeError, match="not valid UTF-8"):
        parse(build_pdf(PAGES), media_type="text/plain")


def test_an_unsupported_type_names_pdf_among_what_is_supported() -> None:
    with pytest.raises(UnsupportedMediaTypeError) as refusal:
        parse(b"<html></html>", media_type="text/html")

    assert PDF_MEDIA_TYPE in str(refusal.value)


# --------------------------------------------------------------------------
# What the other formats keep
# --------------------------------------------------------------------------


@pytest.mark.parametrize("media_type", ["text/plain", "text/markdown"])
def test_a_format_without_pages_reports_none(media_type: str) -> None:
    """Empty rather than a made-up single page.

    "This document has no pages" and "nobody looked" must not be the same
    value, because the chunker decides whether to emit a page number from it.
    """

    parsed = parse(b"# Title\n\nBody text.", media_type=media_type)

    assert parsed.page_starts == ()
