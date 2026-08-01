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

PAGES = (
    "Reciprocal rank fusion runs inside the database.",
    "PostgreSQL is the authority on document permissions.",
    "An outbox claim is a lease with an expiry.",
)


def _pdf(pages: tuple[str, ...]) -> bytes:
    """One Helvetica text run per page, in a valid cross-referenced file."""

    streams = []
    for text in pages:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        streams.append(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode())

    count = len(pages)
    page_ids = [3 + index for index in range(count)]
    content_ids = [3 + count + index for index in range(count)]
    font_id = 3 + 2 * count

    objects: list[tuple[int, bytes]] = [(1, b"<< /Type /Catalog /Pages 2 0 R >>")]
    kids = " ".join(f"{identifier} 0 R" for identifier in page_ids).encode()
    objects.append((2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % count))
    for page_id, content_id in zip(page_ids, content_ids, strict=True):
        objects.append(
            (
                page_id,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (font_id, content_id),
            )
        )
    for content_id, stream in zip(content_ids, streams, strict=True):
        objects.append(
            (
                content_id,
                b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            )
        )
    objects.append((font_id, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))

    # The binary marker real producers emit right after the header, so a
    # consumer treats the file as binary. It also makes this fixture what a
    # PDF actually is: not valid UTF-8, which one of the tests below depends
    # on and which a pure-ASCII fixture would quietly falsify.
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number, body in objects:
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start_xref = len(out)
    size = max(offsets) + 1
    out += b"xref\n0 %d\n" % size + b"0000000000 65535 f \n"
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets.get(number, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        size,
        start_xref,
    )
    return bytes(out)


# --------------------------------------------------------------------------
# What it reads
# --------------------------------------------------------------------------


def test_a_pdf_becomes_its_pages_in_order() -> None:
    parsed = parse(_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert parsed.media_type == PDF_MEDIA_TYPE
    assert parsed.text == PAGE_SEPARATOR.join(PAGES)


def test_page_starts_index_the_extracted_text() -> None:
    """The offsets have to land on the text, not near it.

    This is what a page number on a citation is built from, so the assertion
    is that slicing at each start returns that page -- not that the count of
    offsets happens to match the count of pages.
    """

    parsed = parse(_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert len(parsed.page_starts) == len(PAGES)
    for index, start in enumerate(parsed.page_starts):
        assert parsed.text[start : start + len(PAGES[index])] == PAGES[index]


def test_the_first_page_starts_at_zero() -> None:
    parsed = parse(_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert parsed.page_starts[0] == 0


def test_extraction_is_deterministic() -> None:
    """Chunk boundaries are derived from this text, and boundaries decide ids.

    An extractor that reordered anything between runs would move every chunk
    id in the index without the document having changed.
    """

    first = parse(_pdf(PAGES), media_type=PDF_MEDIA_TYPE)
    second = parse(_pdf(PAGES), media_type=PDF_MEDIA_TYPE)

    assert first.text == second.text
    assert first.page_starts == second.page_starts


def test_a_single_page_pdf_still_reports_its_page() -> None:
    parsed = parse(_pdf((PAGES[0],)), media_type=PDF_MEDIA_TYPE)

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
        parse(_pdf(("", "")), media_type=PDF_MEDIA_TYPE)


def test_an_encrypted_pdf_is_refused_rather_than_opened() -> None:
    """Not attempted with an empty password.

    A file that opens under "" is still one somebody chose to encrypt, and
    indexing it would put its contents somewhere the encryption says they
    should not be.
    """

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(_pdf(PAGES))).pages:
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
        parse(_pdf(PAGES), media_type="text/plain")


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
