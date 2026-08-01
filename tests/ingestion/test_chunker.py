"""Where the boundaries fall, and what a boundary is measured in.

The offsets are the part with consequences. A citation quotes text by
character range, so a chunk whose recorded range does not index its own text is
a citation pointing at something the reader did not read.
"""

from __future__ import annotations

import pytest

from agent_workbench.adapters.ingestion import ApproximateTokenCounter
from agent_workbench.application.chunking import Chunker

PASSAGE = (
    "Dense retrieval finds passages by meaning. Sparse retrieval finds them by "
    "term overlap. Fusing the two happens once, inside Qdrant, so no ranking "
    "is invented twice."
)


def _chunker(size: int = 8, overlap: int = 2) -> Chunker:
    return Chunker(
        size_tokens=size, overlap_tokens=overlap, counter=ApproximateTokenCounter()
    )


def test_the_pieces_reassemble_into_the_original() -> None:
    """A counter that loses characters would make every offset a guess."""

    counter = ApproximateTokenCounter()

    assert "".join(counter.split(PASSAGE)) == PASSAGE


def test_every_chunk_is_located_by_its_own_offsets() -> None:
    """The property a citation depends on, asserted against the source text."""

    for chunk in _chunker().split(PASSAGE):
        assert chunk.locator.char_start is not None
        assert chunk.locator.char_end is not None
        assert PASSAGE[chunk.locator.char_start : chunk.locator.char_end] == chunk.text


def test_offsets_are_used_rather_than_searching_for_the_text() -> None:
    """A repeated passage would otherwise locate every copy at the first one."""

    repeated = "the same sentence. " * 6
    chunks = _chunker(size=4, overlap=0).split(repeated)

    starts = [chunk.locator.char_start for chunk in chunks]

    assert len(set(starts)) == len(starts)
    for chunk in chunks:
        assert repeated[chunk.locator.char_start : chunk.locator.char_end] == chunk.text


def test_windows_overlap_by_the_configured_amount() -> None:
    """A sentence spanning a boundary has to be retrievable from either side."""

    counter = ApproximateTokenCounter()
    chunks = _chunker(size=8, overlap=3).split(PASSAGE)

    first = counter.split(chunks[0].text)
    second = counter.split(chunks[1].text)

    assert [piece.strip() for piece in first[-3:]] == [
        piece.strip() for piece in second[:3]
    ]


def test_no_window_exceeds_the_configured_size() -> None:
    counter = ApproximateTokenCounter()

    for chunk in _chunker(size=6, overlap=1).split(PASSAGE):
        assert counter.count(chunk.text) <= 6


def test_the_last_window_does_not_repeat_itself() -> None:
    """Once a window reaches the end, further ones are all suffixes of it."""

    chunks = _chunker(size=100, overlap=10).split(PASSAGE)

    assert len(chunks) == 1
    assert chunks[0].text == PASSAGE


def test_ordinals_are_dense_and_in_order() -> None:
    chunks = _chunker(size=5, overlap=1).split(PASSAGE)

    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_an_empty_document_produces_no_chunks() -> None:
    assert _chunker().split("") == ()


def test_overlap_at_least_the_window_is_refused() -> None:
    """The cursor would never advance, so the document would never end."""

    with pytest.raises(ValueError, match="smaller than size_tokens"):
        Chunker(size_tokens=4, overlap_tokens=4, counter=ApproximateTokenCounter())


# --- what a chunking is ------------------------------------------------------


def test_the_counter_is_part_of_the_chunker_identity() -> None:
    """Two chunkings under one name is how a re-index moves every boundary.

    Citations built against the old offsets would then point at text that is
    no longer there, and nothing would report a change.
    """

    approximate = _chunker()
    pretend_real = Chunker(
        size_tokens=8,
        overlap_tokens=2,
        counter=ApproximateTokenCounter(name="xlm-roberta-v1"),
    )

    assert approximate.identity != pretend_real.identity
    assert "approx" in approximate.identity


def test_the_identity_records_the_window_too() -> None:
    assert _chunker(size=8, overlap=2).identity.endswith("-8-2")


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def _pages(*pages: str) -> tuple[str, tuple[int, ...]]:
    """Assemble page text the way the PDF parser does, and its offsets."""

    text = ""
    starts: list[int] = []
    for page in pages:
        starts.append(len(text))
        text += page + "\n"
    return text.rstrip("\n"), tuple(starts)


def test_a_chunk_reports_the_page_it_begins_on() -> None:
    """What a PDF citation is followed by.

    A character offset indexes the extraction, not the stored file, so it
    cannot send a reader anywhere in the original. A page number can.
    """

    chunker = _chunker(size=4, overlap=0)
    text, starts = _pages("alpha beta gamma delta", "epsilon zeta eta theta")

    chunks = chunker.split(text, page_starts=starts)

    assert [chunk.locator.page for chunk in chunks] == [1, 2]


def test_pages_are_numbered_from_one() -> None:
    """Zero-based would make the first page's citation unfollowable."""

    chunker = _chunker(size=8, overlap=0)
    text, starts = _pages("alpha beta")

    assert chunker.split(text, page_starts=starts)[0].locator.page == 1


def test_a_window_spanning_a_boundary_reports_where_it_begins() -> None:
    """The place the passage starts is where a reader should start reading.

    Reporting the last page it touches would send them past the sentence they
    came for.
    """

    chunker = _chunker(size=4, overlap=0)
    # Two tokens per page, so the four-token window covers both.
    text, starts = _pages("alpha beta", "gamma delta")

    chunks = chunker.split(text, page_starts=starts)

    assert len(chunks) == 1
    assert chunks[0].locator.page == 1


def test_a_format_without_pages_reports_no_page() -> None:
    """Not page one. A default of 1 would claim a location nothing established."""

    chunker = _chunker(size=4, overlap=0)

    chunks = chunker.split("alpha beta gamma delta epsilon zeta")

    assert chunks
    assert all(chunk.locator.page is None for chunk in chunks)


def test_character_offsets_are_unchanged_by_page_tracking() -> None:
    """The control: adding a page must not move where a chunk says it is."""

    chunker = _chunker(size=4, overlap=0)
    text, starts = _pages("alpha beta gamma delta", "epsilon zeta eta theta")

    without = chunker.split(text)
    with_pages = chunker.split(text, page_starts=starts)

    assert [c.locator.char_start for c in without] == [
        c.locator.char_start for c in with_pages
    ]
    assert [c.text for c in without] == [c.text for c in with_pages]
