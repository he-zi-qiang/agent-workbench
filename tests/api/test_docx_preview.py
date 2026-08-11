"""Reading back a .docx this project itself rendered.

Every fixture here is produced by ``render_document`` rather than hand-built or
checked in as bytes. That is the whole point of the pairing: the preview exists
to show the reader what a Task actually produced, so the thing it is tested
against has to be what a Task actually produces. A hand-made document would let
the renderer change its styles -- which it does, for Chinese thesis format --
and leave this suite green while the console showed a wall of unstyled text.
"""

from __future__ import annotations

import re

from agent_workbench.apps.api.docx_preview import (
    MAX_PREVIEW_CHARS,
    extract_docx_preview,
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
