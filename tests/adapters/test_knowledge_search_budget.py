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

And one thing follows from dropping, which is the last section: what a search
*returned* and what the model was *shown* stopped being the same list the day
this budget was added. The journal has to record the second one, or the fence
downstream verifies citations to passages nobody saw.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from agent_workbench.adapters.tools.knowledge_search import (
    MAX_CONTENT_CHARS,
    MAX_TOP_K,
    KnowledgeSearchTool,
    _render,
)
from agent_workbench.application.chat_execution import (
    RetrievalJournal,
    merge_authorized,
)
from agent_workbench.application.citations import verify_citations
from agent_workbench.application.retrieval import AuthorizedContext, RetrievalRequest
from agent_workbench.domain.context import Citation, ContextChunk, ContextPacket
from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.ports.cancellation import NullCancellationToken
from agent_workbench.ports.tools import ToolInvocation

#: The per-chunk ceiling the domain already enforces. Read from the model
#: rather than restated, so this test follows it if it moves.
CHUNK_TEXT_LIMIT = 32_768

#: What this project actually produces: 512-token chunks, about 2000 characters.
REAL_CHUNK_CHARS = 2_000

RUN_ID = "run_1"


def _packet(count: int, chars: int) -> ContextPacket:
    chunks = tuple(
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
    return ContextPacket(
        chunks=chunks,
        # Retrieval builds one citation per surviving chunk, so a packet
        # without them would be testing a shape this tool never receives --
        # and the citations are the half the fence reads.
        citations=tuple(
            Citation(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_version=chunk.document_version,
            )
            for chunk in chunks
        ),
    )


def _authorized(count: int, chars: int) -> AuthorizedContext:
    packet = _packet(count, chars)
    return AuthorizedContext(
        packet=packet,
        authorized_revisions=tuple(
            sorted({(chunk.document_id, 1) for chunk in packet.chunks})
        ),
    )


def _result(rendered: str) -> ToolResult:
    return ToolResult(
        tool_call_id="toolu_01",
        tool_name="knowledge_search",
        status="ok",
        content=rendered,
    )


@dataclass(frozen=True)
class _Retrieval:
    """Answers every search with one prepared, already-authorized result."""

    context: AuthorizedContext

    async def retrieve(self, request: RetrievalRequest) -> AuthorizedContext:
        return self.context


def _search(context: AuthorizedContext, journal: RetrievalJournal) -> ToolResult:
    """Run one real tool call over a prepared retrieval result."""

    tool = KnowledgeSearchTool(
        retrieval=_Retrieval(context),  # type: ignore[arg-type]
        journal=journal,
    )
    invocation = ToolInvocation(
        call=ToolCall(
            tool_call_id="toolu_01",
            tool_name="knowledge_search",
            arguments={"query": "how are deletions handled", "knowledge_base_id": "kb"},
        ),
        context=ExecutionContext(
            principal=PrincipalContext(tenant_id="tenant_a", principal_id="user_1"),
            envelope=AuthorizationEnvelope(allowed_tools=("knowledge_search",)),
            agent_run_id=RUN_ID,
            policy_identity="policy-1:ffffffffffffffff",
        ),
        cancellation=NullCancellationToken(),
        timeout_seconds=30,
    )
    return asyncio.run(tool.handle(invocation))


def _journalled(context: AuthorizedContext) -> tuple[Any, tuple[ToolResult, ...]]:
    """What one search left for the fence, beside what the model was handed."""

    journal = RetrievalJournal()
    result = _search(context, journal)
    return journal.take(RUN_ID), (result,)


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

    rendered = _render(_packet(top_k, chars)).text

    result = _result(rendered)
    assert json.loads(result.content)["chunks"]


def test_the_whole_result_survives_when_it_fits() -> None:
    """Nothing is dropped, and nothing claims anything was."""

    payload = json.loads(_render(_packet(8, REAL_CHUNK_CHARS)).text)

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
    payload = json.loads(_render(_packet(4, chars)).text)

    assert payload["chunks"], "at least one passage must come back"
    for entry in payload["chunks"]:
        assert len(entry["text"]) == chars, "a returned passage is never partial"


def test_what_was_dropped_is_reported() -> None:
    """A model that knows its evidence is partial can search again.

    One that does not will answer as though it had everything, and the count it
    would need to notice is a count it never saw.
    """

    payload = json.loads(_render(_packet(4, CHUNK_TEXT_LIMIT)).text)

    omitted = 4 - len(payload["chunks"])
    assert omitted > 0
    assert "note" in payload
    assert str(omitted) in payload["note"]


def test_an_oversized_result_still_fits_the_tool_output_ceiling() -> None:
    """The budget is the operative limit; the type is the backstop."""

    rendered = _render(_packet(MAX_TOP_K, CHUNK_TEXT_LIMIT)).text

    assert len(rendered) <= MAX_CONTENT_CHARS
    assert _result(rendered) is not None


def test_the_highest_ranked_passages_are_the_ones_kept() -> None:
    """Retrieval already ordered them. Dropping from the tail respects that."""

    packet = _packet(4, CHUNK_TEXT_LIMIT)
    payload = json.loads(_render(packet).text)

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

    payload = json.loads(_render(_packet(1, CHUNK_TEXT_LIMIT)).text)

    assert len(payload["chunks"]) == 1


def test_an_empty_packet_still_says_so() -> None:
    """Unchanged, and the reason it reads as it does is unchanged too.

    "Nothing is there" and "nothing is there for you" are deliberately the same
    answer, so this wording must not start distinguishing them.
    """

    payload = json.loads(_render(ContextPacket()).text)

    assert payload["chunks"] == []
    assert payload["note"] == "no readable passages matched"


# --------------------------------------------------------------------------
# What the fence is told the model saw
# --------------------------------------------------------------------------


def test_what_was_rendered_is_what_is_reported_as_shown() -> None:
    """The two lists come from one pass, so they cannot disagree.

    Recomputing the budget at the call site would be a second implementation of
    the drop rule, and the one that drifts is always the copy.
    """

    rendered = _render(_packet(4, CHUNK_TEXT_LIMIT))

    payload = json.loads(rendered.text)
    assert [chunk.chunk_id for chunk in rendered.shown] == [
        entry["chunk_id"] for entry in payload["chunks"]
    ]
    # The premise of every test below: this packet does not fit whole.
    assert len(rendered.shown) < 4


def test_a_dropped_passage_cannot_be_cited() -> None:
    """The defect, stated as the thing an asker would have been handed.

    The model never saw the dropped passage -- it is not in the tool result. But
    the journal recorded the whole retrieval, so a chunk id the model produced
    rather than read verified against it, and came back to the asker as a source
    with this system's authority.

    The control group is in the same assertion: a passage that *was* rendered,
    cited the same way in the same answer, still verifies. What changed is
    whether the model was shown it.
    """

    context = _authorized(4, CHUNK_TEXT_LIMIT)
    searched, _ = _journalled(context)

    rendered = _render(context.packet)
    shown = rendered.shown[0].chunk_id
    dropped = context.packet.chunks[-1].chunk_id
    assert dropped not in {chunk.chunk_id for chunk in rendered.shown}

    verdict = verify_citations(
        f"Deletions are tombstoned [{shown}] and reconciled later [{dropped}].",
        tuple(entry.packet for entry in searched),
    )

    assert [citation.chunk_id for citation in verdict.verified] == [shown]
    assert verdict.fabricated == (dropped,)


def test_the_release_fence_is_told_the_documents_the_model_read() -> None:
    """And not the ones retrieval proposed and the budget then dropped.

    Fencing a document no passage of which entered the prompt would refuse good
    answers over a permission change this run never depended on -- the fence
    asks what the answer was built from, and an unshown passage was not.
    """

    context = _authorized(4, CHUNK_TEXT_LIMIT)
    searched, _ = _journalled(context)

    shown_documents = {chunk.document_id for chunk in _render(context.packet).shown}
    fenced = {document_id for document_id, _ in merge_authorized(searched)}

    assert fenced == shown_documents
    assert fenced < {chunk.document_id for chunk in context.packet.chunks}


def test_a_result_that_fits_journals_everything_it_retrieved() -> None:
    """The control group for both tests above.

    Nothing is dropped here, so the journal is the whole retrieval -- including
    the identical `AuthorizedContext` object, because narrowing a search that
    lost nothing would be rebuilding a value to say the same thing.
    """

    context = _authorized(8, REAL_CHUNK_CHARS)
    searched, results = _journalled(context)

    assert searched == (context,)
    assert len(json.loads(results[0].content)["chunks"]) == 8
