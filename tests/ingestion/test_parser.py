"""What this build will read, and what it refuses rather than mangle."""

from __future__ import annotations

import pytest

from agent_workbench.adapters.ingestion.parser import (
    TextDocumentParser,
    UnsupportedMediaTypeError,
)


def test_plain_text_decodes_unchanged() -> None:
    parsed = TextDocumentParser().parse(
        b"Fusion happens once per query.\n", media_type="text/plain"
    )

    assert parsed.text == "Fusion happens once per query.\n"
    assert parsed.media_type == "text/plain"


def test_markdown_keeps_its_syntax() -> None:
    """Stripping it would shift every offset a citation indexes by."""

    source = "# Retrieval\n\nDense **and** sparse.\n"

    assert (
        TextDocumentParser().parse(source.encode(), media_type="text/markdown").text
        == source
    )


def test_a_charset_parameter_is_tolerated() -> None:
    """Content-Type carries parameters; the media type is what selects a reader."""

    parsed = TextDocumentParser().parse(
        b"hello", media_type="text/plain; charset=utf-8"
    )

    assert parsed.media_type == "text/plain"


def test_a_format_this_build_cannot_read_is_refused() -> None:
    """PDF is a dependency and a class of failure of its own, not a branch here."""

    with pytest.raises(UnsupportedMediaTypeError, match="application/pdf"):
        TextDocumentParser().parse(b"%PDF-1.7\n", media_type="application/pdf")


def test_bytes_that_are_not_utf8_are_refused_rather_than_replaced() -> None:
    """Replacement characters chunk, embed and retrieve like any other text.

    The answer would then be grounded in a passage nobody wrote, and every
    layer downstream would report success.
    """

    with pytest.raises(UnsupportedMediaTypeError, match="UTF-8"):
        TextDocumentParser().parse(b"\xff\xfe\x00 not utf-8", media_type="text/plain")


def test_an_empty_document_is_not_an_error() -> None:
    """An upload may legitimately be blank."""

    assert TextDocumentParser().parse(b"", media_type="text/plain").text == ""
