"""Reading a .docx back as text, so the UI can show one instead of describing it.

A rendered document was the one Task output the console could not display. Text
and JSON previewed inline; a .docx -- the artifact most Tasks are actually asked
for -- rendered as "这个类型只能下载查看", which tells the reader everything
about the file except what it says.

Extraction lives on the server rather than in the browser, and the reason is not
convenience. A .docx is a zip of XML, so previewing one client-side means
shipping a zip reader and an XML parser to every page load in order to re-derive
text this process can already produce -- ``python-docx`` is a core dependency
because the Word MCP server renders with it. The same library reads.

**This is a preview, not a conversion.** It recovers the document's text in
reading order and drops everything that made it a document: styles, images,
headers, page geometry. That is the correct trade for a panel beside the run --
somebody who needs the document downloads it, and the download is unchanged
bytes. What this must never do is look like more than it is, which is why the
response says how much it dropped rather than presenting a silent excerpt as the
whole file.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final, cast

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

#: Bounded for the same reason every other preview here is: this is one panel
#: beside a run, and a reader who wants a 200-page report wants the file.
MAX_PREVIEW_CHARS: Final[int] = 40_000

#: How a heading becomes Markdown. python-docx reports built-in heading styles
#: as "Heading 1".."Heading 9"; anything else is a paragraph, including the
#: custom styles the project's own renderer applies for Chinese thesis format.
_HEADING_PREFIX: Final[str] = "Heading "
_MAX_HEADING_LEVEL: Final[int] = 6


@dataclass(frozen=True, slots=True)
class DocxPreview:
    """A document's text, and an honest account of what is missing from it."""

    text: str
    #: True when `text` stops before the document does. The UI says so; a
    #: truncated preview presented as complete is worse than no preview.
    truncated: bool
    #: Counted rather than rendered. A reader who sees "3 张表格" knows to open
    #: the file; one who sees the surrounding prose with the tables silently
    #: absent has no way to know anything is missing.
    table_count: int


def _paragraph_markdown(paragraph: Paragraph) -> str:
    """One paragraph as a line of Markdown, or "" for an empty one."""

    text = paragraph.text.strip()
    if not text:
        return ""
    # `style.name` is `str | None` in the stubs but reaches here as `Unknown`
    # through the element this Paragraph was constructed from. Narrowed once,
    # so the heading arithmetic below is checked normally.
    named = cast("object", paragraph.style.name if paragraph.style is not None else "")
    style = named if isinstance(named, str) else ""
    if style.startswith(_HEADING_PREFIX):
        suffix = style[len(_HEADING_PREFIX) :].strip()
        if suffix.isdigit():
            level = min(int(suffix), _MAX_HEADING_LEVEL)
            return f"{'#' * level} {text}"
    if style == "Title":
        return f"# {text}"
    return text


def _table_markdown(table: Table) -> str:
    """One table as a Markdown table, header row taken from the first row.

    Cell text is flattened to a single line and pipes are escaped: a newline or
    an unescaped pipe inside a cell breaks the row it sits in, which turns one
    malformed cell into a mangled table.
    """

    rows = [
        [cell.text.replace("\n", " ").replace("|", "\\|").strip() for cell in row.cells]
        for row in table.rows
    ]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header, *body = padded
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _body_children(document: Any) -> Iterator[Any]:
    """The body's own XML children, in document order.

    The one untyped-library boundary in this module, isolated to a function so
    the extraction below stays fully checked. python-docx ships ``py.typed``
    but annotates only its high-level surface; the lxml elements underneath are
    ``Any``, and walking them is the only way to see paragraphs and tables
    interleaved as the document has them.
    """

    return cast("Iterator[Any]", document.element.body.iterchildren())


def extract_docx_preview(content: bytes) -> DocxPreview:
    """The document's text in reading order, as Markdown.

    Paragraphs and tables are walked through the body's own XML children rather
    than through ``document.paragraphs`` and ``document.tables`` separately.
    Those two properties each return their own kind in order, but reading them
    one after the other puts every table after every paragraph -- so a report
    whose table sits in section 2 shows it after the conclusion, which is not
    what the document says.
    """

    document = Document(io.BytesIO(content))
    pieces: list[str] = []
    tables = 0
    total = 0
    truncated = False

    for child in _body_children(document):
        tag = str(cast("object", child.tag))
        if tag.endswith("}p"):
            piece = _paragraph_markdown(Paragraph(child, document))
        elif tag.endswith("}tbl"):
            tables += 1
            piece = _table_markdown(Table(child, document))
        else:
            # sectPr and friends. Structure rather than content.
            continue
        if not piece:
            continue
        if total + len(piece) > MAX_PREVIEW_CHARS:
            truncated = True
            break
        pieces.append(piece)
        total += len(piece)

    return DocxPreview(
        text="\n\n".join(pieces),
        truncated=truncated,
        table_count=tables,
    )


__all__ = ["MAX_PREVIEW_CHARS", "DocxPreview", "extract_docx_preview"]
