"""What one search may hand the model, and what happens when it is too much.

This tool could not return an ordinary result. Passages render into
``ToolResult.content``, which shared ``BoundedText`` with everything a model
*writes* -- 4096 characters. This project chunks at 512 tokens and the tool's
own default ``top_k`` is 8, so an ordinary result rendered to 16,732 characters
and the value could not be constructed at all. The call failed with a validation
error rather than returning less, and whether a search survived depended on how
long the matched passages happened to be. A chat eval measured it: two questions
lost, one of them running out of steps with an empty answer while the model
narrated "the knowledge search tool is failing with a validation error on every
attempt".

Two things fix it and both are asserted here: tool output has its own ceiling,
and the tool has a budget under it that drops whole passages rather than
clipping them.
"""

from __future__ import annotations

import json

import pytest

from agent_workbench.adapters.tools.knowledge_search import (
    MAX_CONTENT_CHARS,
    MAX_TOP_K,
    _render,
)
from agent_workbench.domain.context import ContextChunk, ContextPacket
from agent_workbench.domain.tools import ToolResult

#: The per-chunk ceiling the domain already enforces. Read from the model
#: rather than restated, so this test follows it if it moves.
CHUNK_TEXT_LIMIT = 32_768

#: What this project actually produces: 512-token chunks, about 2000 characters.
REAL_CHUNK_CHARS = 2_000


def _packet(count: int, chars: int) -> ContextPacket:
    return ContextPacket(
        chunks=tuple(
            ContextChunk(
                chunk_id=f"chk_{index:032x}",
                document_id=f"doc_{index}",
                document_version="v1",
                tenant_id="tenant_a",
                text="x" * chars,
                score=0.9,
            )
            for index in range(count)
        )
    )


def _result(rendered: str) -> ToolResult:
    return ToolResult(
        tool_call_id="toolu_01",
        tool_name="knowledge_search",
        status="ok",
        content=rendered,
    )


# --------------------------------------------------------------------------
# The sizes that used to fail
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("top_k", "chars"),
    [
        (3, REAL_CHUNK_CHARS),
        (8, 1_200),
        (8, REAL_CHUNK_CHARS),
        (MAX_TOP_K, REAL_CHUNK_CHARS),
    ],
)
def test_an_ordinary_result_can_be_returned(top_k: int, chars: int) -> None:
    """Each of these raised a validation error before.

    The largest is the tool's own maximum: ``top_k`` at its ceiling, of chunks
    the size this project's chunker actually emits. If that cannot be returned,
    the tool cannot answer a question at its own documented limits.
    """

    rendered = _render(_packet(top_k, chars))

    result = _result(rendered)
    assert json.loads(result.content)["chunks"]


def test_the_whole_result_survives_when_it_fits() -> None:
    """Nothing is dropped, and nothing claims anything was."""

    payload = json.loads(_render(_packet(8, REAL_CHUNK_CHARS)))

    assert len(payload["chunks"]) == 8
    assert "note" not in payload


# --------------------------------------------------------------------------
# What happens when it does not fit
# --------------------------------------------------------------------------


def test_passages_are_dropped_whole_rather_than_clipped() -> None:
    """A half-passage is evidence that says what the document does not.

    The citation fence checks that a cited chunk was *shown*, not that the
    sentence the model leaned on survived the cut -- so a clipped passage would
    be cited under the id of the whole thing.
    """

    chars = CHUNK_TEXT_LIMIT
    payload = json.loads(_render(_packet(4, chars)))

    assert payload["chunks"], "at least one passage must come back"
    for entry in payload["chunks"]:
        assert len(entry["text"]) == chars, "a returned passage is never partial"


def test_what_was_dropped_is_reported() -> None:
    """A model that knows its evidence is partial can search again.

    One that does not will answer as though it had everything, and the count it
    would need to notice is a count it never saw.
    """

    payload = json.loads(_render(_packet(4, CHUNK_TEXT_LIMIT)))

    omitted = 4 - len(payload["chunks"])
    assert omitted > 0
    assert "note" in payload
    assert str(omitted) in payload["note"]


def test_an_oversized_result_still_fits_the_tool_output_ceiling() -> None:
    """The budget is the operative limit; the type is the backstop."""

    rendered = _render(_packet(MAX_TOP_K, CHUNK_TEXT_LIMIT))

    assert len(rendered) <= MAX_CONTENT_CHARS
    assert _result(rendered) is not None


def test_the_highest_ranked_passages_are_the_ones_kept() -> None:
    """Retrieval already ordered them. Dropping from the tail respects that."""

    packet = _packet(4, CHUNK_TEXT_LIMIT)
    payload = json.loads(_render(packet))

    kept = [entry["chunk_id"] for entry in payload["chunks"]]
    assert kept == [chunk.chunk_id for chunk in packet.chunks[: len(kept)]]


# --------------------------------------------------------------------------
# The relationship that makes one branch dead
# --------------------------------------------------------------------------


def test_one_passage_always_fits() -> None:
    """Why ``_render``'s empty-result branch is unreachable, stated as a test.

    A chunk cannot exceed ``ChunkText``'s ceiling, and the budget is above it,
    so the first passage always survives. Lowering the budget below that would
    bring the branch back to life silently; this fails first.
    """

    assert MAX_CONTENT_CHARS > CHUNK_TEXT_LIMIT

    payload = json.loads(_render(_packet(1, CHUNK_TEXT_LIMIT)))

    assert len(payload["chunks"]) == 1


def test_an_empty_packet_still_says_so() -> None:
    """Unchanged, and the reason it reads as it does is unchanged too.

    "Nothing is there" and "nothing is there for you" are deliberately the same
    answer, so this wording must not start distinguishing them.
    """

    payload = json.loads(_render(ContextPacket()))

    assert payload["chunks"] == []
    assert payload["note"] == "no readable passages matched"
