"""Where a citation says the passage came from.

The page a PDF chunk sits on is computed at ingestion, stored on the point and
read back by the index -- and then has one more boundary to cross before anybody
can use it. ``_packet`` builds the chunks the model is shown and the citations
the reader follows, and a page that stops here is a page nothing downstream can
report.

That last hop is why this file exists: a sabotage that dropped the page at this
exact line passed every other test, including the one that reads it back out of
Qdrant.
"""

from __future__ import annotations

from agent_workbench.application.retrieval import _packet
from agent_workbench.ports.vector_index import ScoredChunk


def _chunk(**overrides: object) -> ScoredChunk:
    fields: dict[str, object] = {
        "chunk_id": "chk_1",
        "document_id": "doc_1",
        "document_version": "ver_1",
        "tenant_id": "tenant_a",
        "knowledge_base_id": "kb_1",
        "source_revision": 1,
        "text": "Reciprocal rank fusion runs inside the database.",
        "ordinal": 0,
        "score": 0.9,
    }
    fields.update(overrides)
    return ScoredChunk.model_validate(fields)


def test_a_citation_carries_the_page_its_chunk_came_from() -> None:
    """The end of the chain. A page that stops at the index is a page nobody
    can follow."""

    packet = _packet((_chunk(page=4),))

    assert packet.citations[0].locator.page == 4


def test_the_shown_chunk_carries_it_too() -> None:
    """Both lists are built together, and both are read by different readers:
    the model sees the chunk, a person follows the citation."""

    packet = _packet((_chunk(page=4),))

    assert packet.chunks[0].locator.page == 4


def test_a_chunk_from_a_format_without_pages_reports_none() -> None:
    """Absent, not one. Page one is a location nothing established."""

    packet = _packet((_chunk(),))

    assert packet.chunks[0].locator.page is None
    assert packet.citations[0].locator.page is None


def test_the_paragraph_is_still_the_ordinal() -> None:
    """The control: adding a page must not displace the position already there.

    Markdown and plain text have no pages, so the ordinal is the only locator
    they ever had.
    """

    packet = _packet((_chunk(ordinal=7, page=2),))

    assert packet.citations[0].locator.paragraph == 7
    assert packet.citations[0].locator.page == 2


def test_each_chunk_keeps_its_own_page() -> None:
    """One page for all of them would look right on a single-chunk packet."""

    packet = _packet(
        (
            _chunk(chunk_id="chk_1", ordinal=0, page=1),
            _chunk(chunk_id="chk_2", ordinal=1, page=3),
        )
    )

    assert [citation.locator.page for citation in packet.citations] == [1, 3]
