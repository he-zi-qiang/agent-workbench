"""Reading a .docx on the way into the index.

Every fixture here is **built by hand as OOXML bytes**, not produced by this
project's own ``render_document``. That is the entire point of the file. The
existing docx evidence is a closed loop -- ``tests/adapters/test_docx.py`` says
so in its own docstring -- so it can only ever show that this build reads what
this build writes. A file that came out of Word, WPS or Google Docs differs in
style vocabulary, numbering and run splitting. What that costs is measured
here rather than guessed at -- and the first thing it corrected was a
limitation this file originally asserted and that turned out not to exist.

The packages below are minimal on purpose: content types, one relationship,
one ``word/document.xml``. python-docx opens them, which is the property under
test -- the reader must not depend on parts our own writer happens to emit.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from agent_workbench.adapters.documents.docx import (
    MAX_PREVIEW_CHARS,
    extract_docx_preview,
)
from agent_workbench.adapters.ingestion.parser import (
    UnreadableDocumentError,
    UnsupportedMediaTypeError,
    parse,
)

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_DOCUMENT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_WORDPROCESSINGML = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
)


def _paragraph(text: str, *, style: str | None = None) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{properties}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _styles(*definitions: tuple[str, str]) -> str:
    """A styles part mapping style ids to their names.

    Carried because a package without one resolves no style at all, so a test
    built on a bare document would report "not a heading" for everything and
    prove nothing about heading detection. Real Word files always have it.
    """

    entries = "".join(
        f'<w:style w:type="paragraph" w:styleId="{style_id}">'
        f'<w:name w:val="{name}"/></w:style>'
        for style_id, name in definitions
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:styles {_WORDPROCESSINGML}>{entries}</w:styles>"
    )


def _docx(*paragraphs: str, styles: str | None = None) -> bytes:
    """A minimal, valid Word package carrying exactly these paragraphs."""

    body = "".join(paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_WORDPROCESSINGML}><w:body>{body}<w:sectPr/></w:body>"
        "</w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", _CONTENT_TYPES)
        package.writestr("_rels/.rels", _ROOT_RELS)
        package.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELS)
        package.writestr("word/document.xml", document)
        package.writestr("word/styles.xml", styles or _styles())
    return buffer.getvalue()


def test_a_word_file_from_another_producer_is_read_into_text() -> None:
    content = _docx(
        _paragraph("Annual Report", style="Titre1"),
        _paragraph("Revenue grew by twelve percent."),
        _paragraph("汇率波动是主要风险。"),
    )

    parsed = parse(content, media_type=DOCX_MEDIA_TYPE)

    assert parsed.media_type == DOCX_MEDIA_TYPE
    assert "Revenue grew by twelve percent." in parsed.text
    assert "汇率波动是主要风险。" in parsed.text
    # No pagination, and not because nobody looked: a .docx stores none. Where
    # a page breaks is decided by whichever renderer lays the file out, so two
    # readers of one document legitimately disagree, and a citation into one
    # falls back to character offsets.
    assert parsed.page_starts == ()


def test_a_localised_word_keeps_its_outline() -> None:
    """Measured, not assumed -- and it corrected what I first wrote here.

    The obvious worry about reading files this project did not write is that
    French Word calls Heading 1 ``Titre1`` and German calls it
    ``Überschrift 1``, so the reader (which matches on ``"Heading "``) would
    drop every outline level. That worry is wrong, and the reason is in the
    file format: the *style id* is localised but ``w:name`` is not -- every
    locale writes the built-in name ``heading 1``. python-docx resolves the
    name, so the reader sees what it expects.

    The first version of this test asserted the opposite and passed, because
    its package had no styles part and therefore resolved nothing at all. That
    is worth leaving in the record: it is a test that would have shipped a
    false limitation into the documentation.
    """

    french = parse(
        _docx(
            _paragraph("Rapport annuel", style="Titre1"),
            styles=_styles(("Titre1", "heading 1")),
        ),
        media_type=DOCX_MEDIA_TYPE,
    )
    german = parse(
        _docx(
            _paragraph("Risiken", style="berschrift2"),
            styles=_styles(("berschrift2", "heading 2")),
        ),
        media_type=DOCX_MEDIA_TYPE,
    )

    assert french.text == "# Rapport annuel"
    assert german.text == "## Risiken"


def test_a_template_style_that_is_not_a_heading_stays_prose() -> None:
    """The control for the test above.

    A genuinely custom style -- the kind a corporate template defines -- is not
    a heading and must not become one. Without this, "headings are recognised"
    would also be satisfied by a reader that marked every styled paragraph.
    """

    parsed = parse(
        _docx(
            _paragraph("Annual Report", style="ReportTitle"),
            styles=_styles(("ReportTitle", "Report Title")),
        ),
        media_type=DOCX_MEDIA_TYPE,
    )

    assert parsed.text == "Annual Report"


def test_ingestion_reads_the_whole_document_rather_than_a_preview_of_it() -> None:
    """The property the ceiling parameter exists for.

    The panel beside a run stops at ``MAX_PREVIEW_CHARS``; ingestion must not,
    because the document is about to be chunked and indexed and a ceiling here
    would index a fraction of it while every layer reported success. Both
    halves run against the same bytes, so this measures the two callers'
    ceilings rather than the size of the fixture.
    """

    line = "This sentence exists to make the document longer than one preview."
    paragraphs = [_paragraph(f"{index} {line}") for index in range(900)]
    content = _docx(*paragraphs)

    parsed = parse(content, media_type=DOCX_MEDIA_TYPE)
    previewed = extract_docx_preview(content)

    assert len(parsed.text) > MAX_PREVIEW_CHARS
    assert parsed.text.startswith("0 ")
    assert "899 " in parsed.text
    # The control: the same bytes through the preview path stop early and say
    # so. If this went green while the assertion above went red, the ceiling
    # would simply have been removed for everybody.
    assert previewed.truncated is True
    assert len(previewed.text) < len(parsed.text)
    # Close to the ceiling rather than exactly under it. The running total
    # counts paragraph lengths while the returned text also carries the "\n\n"
    # joins, so a preview overshoots by two characters per paragraph. That is
    # pre-existing and harmless for a panel; it is asserted loosely here rather
    # than tightened, because tightening it would be this test quietly changing
    # what the preview means.
    assert len(previewed.text) < MAX_PREVIEW_CHARS * 1.1


def test_a_word_file_with_no_text_is_refused_rather_than_indexed_empty() -> None:
    with pytest.raises(UnreadableDocumentError):
        parse(_docx(_paragraph("   ")), media_type=DOCX_MEDIA_TYPE)


def test_something_that_is_not_a_word_package_is_refused() -> None:
    """A renamed file, a truncated upload, a zip of something else.

    python-docx raises through several unrelated families for these; all of
    them mean "this is not a document", and the caller needs one answer.
    """

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("hello.txt", "not a word package")

    with pytest.raises(UnreadableDocumentError):
        parse(buffer.getvalue(), media_type=DOCX_MEDIA_TYPE)


def test_a_media_type_this_build_does_not_read_is_still_refused() -> None:
    """The control for the accept list.

    Adding .docx must not have turned the parser into one that accepts
    anything; a format with no reader still has to be refused by name.
    """

    with pytest.raises(UnsupportedMediaTypeError):
        parse(_docx(_paragraph("hello")), media_type="application/zip")
