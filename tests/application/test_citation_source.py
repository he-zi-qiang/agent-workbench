"""Reading a citation is a new read, and the order of the four steps is why.

What these pin is not "the text comes back". It is that a stored citation buys
the caller **nothing** except the right to have the question asked again:
PostgreSQL decides readability now, the index read is narrowed exactly as a
search is, and a revision that has moved on refuses. A turn is a durable record
of what was answered, never a standing permit to read what it was answered from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_workbench.application.citation_source import (
    CitationSourceReader,
    CitedPassageUnavailableError,
)
from agent_workbench.domain.context import Citation
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.ports.documents import ReadableDocument
from agent_workbench.ports.vector_index import ScoredChunk

TENANT = "tenant_a"
OWNER = "user_1"
SESSION = "session_1"
TURN = "turn_1"
KB = "kb_1"
CHUNK = "chunk_1"
DOCUMENT = "doc_1"
VERSION = "docv_1"


@dataclass
class StubTurn:
    """Only the shape `CitationSourceReader` reads off a stored turn."""

    citations: tuple[Citation, ...]

    @property
    def result(self) -> Any:
        if not self.citations:
            # A running turn has no result at all, and a withheld one's is
            # scrubbed. Both reach the reader as "no citations to match".
            return None
        return _Result(citations=self.citations)


@dataclass(frozen=True, slots=True)
class _Result:
    citations: tuple[Citation, ...]


@dataclass
class StubConversations:
    turn_record: StubTurn
    seen: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    refuse: bool = False

    async def turn(
        self, *, session_id: str, tenant_id: str, principal_id: str, turn_id: str
    ) -> Any:
        self.seen.append(
            {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "turn_id": turn_id,
            }
        )
        if self.refuse:
            raise NotFoundError("chat turn not found")
        return self.turn_record


@dataclass
class StubDocuments:
    readable: bool = True
    revision: int = 3
    seen: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def readable_versions(
        self, *, tenant_id: str, principal_id: str, document_ids: tuple[str, ...]
    ) -> tuple[ReadableDocument, ...]:
        self.seen.append(
            {
                "tenant_id": tenant_id,
                "principal_id": principal_id,
                "document_ids": document_ids,
            }
        )
        if not self.readable:
            return ()
        return (
            ReadableDocument(
                document_id=document_ids[0],
                knowledge_base_id=KB,
                source_revision=self.revision,
            ),
        )


@dataclass
class StubIndex:
    revision: int = 3
    present: bool = True
    seen: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def fetch(
        self,
        *,
        chunk_ids: tuple[str, ...],
        tenant_id: str,
        knowledge_base_id: str,
        authorized_principals: tuple[str, ...],
    ) -> tuple[ScoredChunk, ...]:
        self.seen.append(
            {
                "chunk_ids": chunk_ids,
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "authorized_principals": authorized_principals,
            }
        )
        if not self.present:
            return ()
        return (
            ScoredChunk(
                chunk_id=chunk_ids[0],
                document_id=DOCUMENT,
                document_version=VERSION,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                source_revision=self.revision,
                text="被引用的那一段原文。",
                ordinal=7,
                page=3,
                score=1.0,
            ),
        )


def _cited(chunk_id: str = CHUNK) -> Citation:
    return Citation(
        chunk_id=chunk_id,
        document_id=DOCUMENT,
        document_version=VERSION,
    )


def _reader(**overrides: Any) -> tuple[CitationSourceReader, dict[str, Any]]:
    parts: dict[str, Any] = {
        "conversations": StubConversations(StubTurn((_cited(),))),
        "documents": StubDocuments(),
        "index": StubIndex(),
    }
    parts.update(overrides)
    return CitationSourceReader(**parts), parts


async def _read(reader: CitationSourceReader, chunk_id: str = CHUNK) -> Any:
    return await reader.passage(
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
        turn_id=TURN,
        chunk_id=chunk_id,
    )


@pytest.mark.anyio
async def test_a_cited_passage_comes_back_with_where_it_sits() -> None:
    reader, _ = _reader()

    passage = await _read(reader)

    assert passage.text == "被引用的那一段原文。"
    assert passage.ordinal == 7
    # A page for a format that has one; the locator is not invented where there
    # is none, which is why this travels as `int | None` rather than defaulting.
    assert passage.page == 3


@pytest.mark.anyio
async def test_a_chunk_this_turn_never_cited_is_not_readable_through_it() -> None:
    # The whole reason the turn is read first. Without this check the endpoint
    # is a general "fetch me any chunk id" read wearing a turn id as
    # decoration, and the turn id would be doing no work at all.
    reader, parts = _reader()

    with pytest.raises(NotFoundError):
        await _read(reader, chunk_id="chunk_somebody_elses")

    # And it stopped there: no document lookup, no index read.
    assert parts["documents"].seen == []
    assert parts["index"].seen == []


@pytest.mark.anyio
async def test_a_revoked_grant_refuses_even_though_the_citation_is_real() -> None:
    # The case this whole module exists to get right. The answer genuinely
    # cited this chunk; whether the asker may still read it is a question only
    # PostgreSQL can answer, and it is answered now rather than replayed from
    # the turn.
    reader, parts = _reader(documents=StubDocuments(readable=False))

    with pytest.raises(CitedPassageUnavailableError):
        await _read(reader)

    assert parts["index"].seen == []


@pytest.mark.anyio
async def test_a_passage_at_another_revision_is_refused() -> None:
    # A point from an older revision may sit in the index while ingestion
    # catches up. Serving it because the document is still readable would show
    # text the current PostgreSQL snapshot no longer describes.
    reader, _ = _reader(
        documents=StubDocuments(revision=4), index=StubIndex(revision=3)
    )

    with pytest.raises(CitedPassageUnavailableError):
        await _read(reader)


@pytest.mark.anyio
async def test_a_future_revision_is_refused_too() -> None:
    # Equality rather than `>=`: the derived store running ahead of its
    # authority is not a state to serve from either.
    reader, _ = _reader(
        documents=StubDocuments(revision=3), index=StubIndex(revision=9)
    )

    with pytest.raises(CitedPassageUnavailableError):
        await _read(reader)


@pytest.mark.anyio
async def test_a_point_missing_from_the_index_is_unavailable_not_absent() -> None:
    reader, _ = _reader(index=StubIndex(present=False))

    with pytest.raises(CitedPassageUnavailableError):
        await _read(reader)


@pytest.mark.anyio
async def test_the_index_read_is_narrowed_on_every_axis_a_search_is() -> None:
    # A read whose ids came from a trusted place is still a read of the index,
    # and one that quietly stopped narrowing would be a way to reach points a
    # query cannot.
    reader, parts = _reader()

    await _read(reader)

    assert parts["index"].seen == [
        {
            "chunk_ids": (CHUNK,),
            "tenant_id": TENANT,
            # Not taken from the request: it comes from the document the turn
            # named, which is why this route is mounted under a turn at all.
            "knowledge_base_id": KB,
            "authorized_principals": (OWNER,),
        }
    ]


@pytest.mark.anyio
async def test_a_turn_with_no_result_has_nothing_to_serve() -> None:
    # A running turn, and a withheld one whose result was scrubbed. Neither
    # gets its own message: telling them apart would tell a prober which turns
    # exist in which state.
    reader, _ = _reader(conversations=StubConversations(StubTurn(())))

    with pytest.raises(NotFoundError):
        await _read(reader)


@pytest.mark.anyio
async def test_a_turn_in_somebody_elses_session_never_reaches_the_corpus() -> None:
    reader, parts = _reader(
        conversations=StubConversations(StubTurn((_cited(),)), refuse=True)
    )

    with pytest.raises(NotFoundError):
        await _read(reader)

    assert parts["documents"].seen == []
    assert parts["index"].seen == []
