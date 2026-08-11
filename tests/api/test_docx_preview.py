"""Reading back a .docx this project itself rendered.

Every fixture here is produced by ``render_document`` rather than hand-built or
checked in as bytes. That is the whole point of the pairing: the preview exists
to show the reader what a Task actually produced, so the thing it is tested
against has to be what a Task actually produces. A hand-made document would let
the renderer change its styles -- which it does, for Chinese thesis format --
and leave this suite green while the console showed a wall of unstyled text.
"""

from __future__ import annotations

import io
import os
import re
import zipfile

import pytest

from agent_workbench.apps.api.docx_preview import (
    MAX_PREVIEW_CHARS,
    MAX_PREVIEW_COMPRESSION_RATIO,
    MAX_PREVIEW_EXPANDED_BYTES,
    DocxTooLargeError,
    extract_docx_preview,
    preflight_docx,
)
from agent_workbench.apps.word_mcp.contract import (
    DocumentRequest,
    DocumentSection,
    SimpleTable,
)
from agent_workbench.apps.word_mcp.renderer import render_document


def _document(*sections: DocumentSection, title: str = "季度报告") -> bytes:
    return render_document(
        DocumentRequest(title=title, subtitle=None, sections=sections)
    )


def _section(
    heading: str,
    *paragraphs: str,
    bullets: tuple[str, ...] = (),
    table: SimpleTable | None = None,
) -> DocumentSection:
    return DocumentSection(
        heading=heading,
        paragraphs=paragraphs,
        bullets=bullets,
        table=table,
    )


def test_a_rendered_document_reads_back_as_its_own_text() -> None:
    preview = extract_docx_preview(
        _document(_section("背景", "第一段正文。", "第二段正文。"))
    )

    assert "背景" in preview.text
    assert "第一段正文。" in preview.text
    assert "第二段正文。" in preview.text
    assert preview.truncated is False


def test_paragraphs_keep_the_order_the_document_has_them_in() -> None:
    preview = extract_docx_preview(
        _document(
            _section("第一节", "甲"),
            _section("第二节", "乙"),
        )
    )

    assert preview.text.index("甲") < preview.text.index("乙")


def test_a_table_is_read_in_place_rather_than_after_the_prose() -> None:
    """The bug this walks the body's XML children to avoid.

    ``document.paragraphs`` and ``document.tables`` each return their own kind
    in order, so reading one then the other puts every table after every
    paragraph -- a report whose table sits in section 1 would show it below the
    conclusion. The control is the paragraph that follows it.
    """

    preview = extract_docx_preview(
        _document(
            _section(
                "数据",
                "表格之前。",
                table=SimpleTable(headers=("指标", "值"), rows=(("营收", "12"),)),
            ),
            _section("结论", "表格之后。"),
        )
    )

    # Located by the rendered *table row*, not by its cell text. Cell text also
    # exists as a paragraph inside the table, so `index("指标")` finds that copy
    # -- which sits in the right place even when the traversal is wrong, and
    # made an earlier version of this assertion survive the exact bug it names.
    lines = preview.text.splitlines()
    table_at = next(i for i, line in enumerate(lines) if line.startswith("| 指标 |"))
    before_at = next(i for i, line in enumerate(lines) if line == "表格之前。")
    after_at = next(i for i, line in enumerate(lines) if line == "表格之后。")

    assert before_at < table_at < after_at
    assert preview.table_count == 1


def test_a_table_becomes_a_markdown_table_the_console_can_render() -> None:
    preview = extract_docx_preview(
        _document(
            _section(
                "数据",
                table=SimpleTable(
                    headers=("指标", "值"),
                    rows=(("营收", "12"), ("成本", "8")),
                ),
            )
        )
    )

    assert "| 指标 | 值 |" in preview.text
    assert "| --- | --- |" in preview.text
    assert "| 营收 | 12 |" in preview.text


def test_a_pipe_inside_a_cell_does_not_break_the_row_it_sits_in() -> None:
    preview = extract_docx_preview(
        _document(
            _section(
                "数据",
                table=SimpleTable(headers=("键", "值"), rows=(("a|b", "1"),)),
            )
        )
    )

    assert r"a\|b" in preview.text
    # Escaped, so the row still has exactly the two cells it was given. Split
    # on unescaped pipes -- which is what a Markdown renderer does, and the
    # thing an unescaped cell would break.
    row = next(line for line in preview.text.splitlines() if "a\\|b" in line)
    cells = [cell for cell in re.split(r"(?<!\\)\|", row) if cell.strip()]
    assert cells == [" a\\|b ", " 1 "]


def test_a_long_document_is_cut_and_says_so() -> None:
    """Truncation is reported, never silent.

    A preview that stops early and presents itself as complete is worse than no
    preview: the reader draws conclusions from a document they have only part
    of, with nothing on screen to suggest it.
    """

    long_paragraph = "长" * 2_000
    preview = extract_docx_preview(
        _document(_section("很长的一节", *([long_paragraph] * 40)))
    )

    assert preview.truncated is True
    assert len(preview.text) <= MAX_PREVIEW_CHARS + len(long_paragraph)


def test_a_document_that_fits_is_not_marked_truncated() -> None:
    """The control for the test above. Without it, an extractor that reported
    `truncated=True` unconditionally would pass."""

    preview = extract_docx_preview(_document(_section("短", "一句话。")))

    assert preview.truncated is False


def test_a_document_with_no_tables_counts_none() -> None:
    """The control for `table_count`, which the console uses to tell the reader
    what the preview left out."""

    preview = extract_docx_preview(_document(_section("背景", "只有正文。")))

    assert preview.table_count == 0


def _zip_of(members: dict[str, bytes]) -> bytes:
    """A deflated archive built from raw members, for the refusals below.

    Hand-built rather than rendered, and that is the one place in this file
    where it has to be: the point of these cases is a package the renderer
    would never produce, so a fixture that went through it could not express
    them.
    """

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_a_package_that_expands_past_the_ceiling_is_refused_before_parsing() -> None:
    """The gap the compressed-size ceiling never closed.

    A .docx is a zip, so what reading one costs is its *expanded* size, while
    the caller's limit is on the stored bytes. This archive is well inside any
    stored-size limit and asks for far more than that once opened; the refusal
    has to happen before python-docx is handed the bytes, because by then the
    allocation has already been made.

    Built to sit *under* the ratio limit on purpose -- a megabyte of
    incompressible data pads the stored size -- so that what it exercises is
    the absolute ceiling and not the ratio check below it.
    """

    padding = os.urandom(1024 * 1024)
    bomb = _zip_of(
        {
            "word/media/noise.bin": padding,
            "word/document.xml": b"\0" * (150 * 1024 * 1024),
        }
    )
    expanded = len(padding) + 150 * 1024 * 1024

    assert expanded > MAX_PREVIEW_EXPANDED_BYTES
    assert expanded < len(bomb) * MAX_PREVIEW_COMPRESSION_RATIO
    with pytest.raises(DocxTooLargeError):
        preflight_docx(bomb)


def test_the_preview_itself_refuses_a_bomb_and_not_only_the_preflight() -> None:
    """The wiring, which is the half that actually protects the route.

    A preflight nothing calls is a function, not a ceiling. Every caller of
    `extract_docx_preview` has to inherit it -- including the ones that do not
    know it exists.
    """

    bomb = _zip_of({"word/document.xml": b"\0" * (400 * 1024 * 1024)})

    with pytest.raises(DocxTooLargeError):
        extract_docx_preview(bomb)


def test_a_small_package_that_is_nothing_but_expansion_is_refused() -> None:
    """Under the absolute ceiling, over the ratio.

    Split across several members on purpose: a ratio measured per entry would
    let one bomb be delivered as ten, so it is measured against the whole
    stored object.
    """

    piece = b"\0" * (6 * 1024 * 1024)
    spread = _zip_of({f"word/part{index}.xml": piece for index in range(6)})
    expanded = 6 * len(piece)

    assert expanded < MAX_PREVIEW_EXPANDED_BYTES
    assert expanded > len(spread) * MAX_PREVIEW_COMPRESSION_RATIO
    with pytest.raises(DocxTooLargeError):
        preflight_docx(spread)


def test_a_package_with_too_many_entries_is_refused() -> None:
    many = _zip_of({f"word/media/image{index}.bin": b"x" for index in range(600)})

    with pytest.raises(DocxTooLargeError):
        preflight_docx(many)


def test_understating_the_declared_size_yields_no_extra_bytes() -> None:
    """Why reading the declared sizes is enough, asserted rather than assumed.

    The preflight trusts ``ZipInfo.file_size``, which the archive supplies
    about itself, so the whole design rests on a bomb being unable to
    understate it. This rewrites the declared size to zero and pins what
    ``zipfile`` then does: it stops the member at the declared length and fails
    the CRC. python-docx reads through the same ``zipfile``, so it is bounded
    by the same number the preflight checked -- no extra bytes are reachable by
    lying, only an unreadable file.
    """

    bomb = bytearray(_zip_of({"word/document.xml": b"\0" * (64 * 1024 * 1024)}))
    # The uncompressed size sits in both the local header and the central
    # directory record; zero it wherever the real value appears.
    real = (64 * 1024 * 1024).to_bytes(4, "little")
    assert real in bomb
    patched = bytes(bomb).replace(real, (0).to_bytes(4, "little"))

    with (
        zipfile.ZipFile(io.BytesIO(patched)) as archive,
        pytest.raises(zipfile.BadZipFile),
    ):
        archive.read("word/document.xml")

    # And the preview says "unreadable", not "here is your document". The
    # exact type is python-docx's business; what matters is that nothing is
    # returned as a readable document.
    with pytest.raises((ValueError, KeyError, zipfile.BadZipFile)):
        extract_docx_preview(patched)


def test_an_ordinary_rendered_document_passes_the_preflight() -> None:
    """The control. Without it, a preflight that refused everything would pass
    every test above, and the preview would be gone rather than bounded."""

    document = _document(
        _section("背景", "第一段正文。" * 200),
        _section("结论", "结论段落。" * 200),
    )

    preflight_docx(document)
    assert extract_docx_preview(document).text != ""
