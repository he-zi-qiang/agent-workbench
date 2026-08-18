"""Serving the passage behind one citation, as a read that happens now.

The console could show *which* chunks an answer cited and nothing else about
them: `Citation` carries a `chunk_id`, a `document_id`, a `document_version` and
a locator, and its optional `quote` field is never assigned anywhere in this
repository. So a reader who wanted to check a claim had exactly one route --
open the knowledge base and search for it by hand -- and the citations, which
exist to make checking cheap, made it no cheaper than having none.

**A cited passage is evidence, not a product.** It existed before the turn that
cited it and it belongs to the document afterwards, which is why this is a read
path rather than anything to do with artifacts: Chat gets no artifact container,
no workspace and no download from this module.

The order of the four steps below is the whole security argument and it is not
interchangeable. In particular, the turn is read **first**, to answer a question
that must not be taken from the requester: *did this answer actually cite this
chunk*. Reading the chunk first and checking afterwards would make the endpoint
a general "fetch me any chunk id" read wearing a turn id as decoration.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.ports.conversation_store import ChatTurnStore
from agent_workbench.ports.documents import DocumentStore
from agent_workbench.ports.vector_index import VectorIndexPort


@dataclass(frozen=True, slots=True)
class CitedPassage:
    """The stored text a citation points at, with where it sits."""

    chunk_id: str
    document_id: str
    document_version: str
    text: str
    ordinal: int
    #: Absent for every format without pages, which is most of a Markdown
    #: corpus. Never defaulted to 1 -- that would claim a location nothing
    #: established.
    page: int | None


class CitedPassageUnavailableError(NotFoundError):
    """The citation is real, and its passage cannot be served right now.

    Separate from a plain ``NotFoundError`` so the route can say something true
    to the reader. Three situations reach it and they are all the same shape --
    the answer named this chunk, and the current state of the corpus does not
    let this principal read it:

    * the document's ACL no longer covers this principal;
    * the document has been re-ingested since, so the index holds a different
      revision than PostgreSQL now describes;
    * the point is gone from the index while ingestion catches up.

    All three are correct outcomes rather than faults, and the console has to
    present them as "you cannot read this any more", never as "the citation is
    broken". The distinction matters because the second and third are transient
    and the first is a decision somebody made.
    """


class CitationSourceUnavailableError(RuntimeError):
    """Somebody asked to read a cited passage and this deployment has no index.

    Not a 404 and not a 403: the citation may be entirely real, the caller may
    read its document, and nothing about the request is wrong -- this process
    was assembled without a vector index (``--without-chat``, or a deployment
    that never configured Qdrant). A 404 here would send the reader looking for
    a mistake in their own data; a 503 says the fix is somewhere else.
    """


@dataclass(frozen=True, slots=True)
class CitationSourceReader:
    """Reads one cited passage, re-deciding authorization from scratch."""

    conversations: ChatTurnStore
    documents: DocumentStore
    index: VectorIndexPort

    async def passage(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        chunk_id: str,
    ) -> CitedPassage:
        """The text behind one citation of one turn, if it may be read now."""

        # 1. The ledger says whether this turn cited this chunk. Ownership of
        #    the session is checked inside, and a turn in somebody else's
        #    session is a `NotFoundError` indistinguishable from a missing one.
        turn = await self.conversations.turn(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            turn_id=turn_id,
        )
        # A withheld turn's result is scrubbed and a running one has none, so
        # both fall out here without a status check: there are no citations to
        # match against, and inventing a different message for each would tell
        # a prober which turns exist in which state.
        cited = next(
            (
                citation
                for citation in (turn.result.citations if turn.result else ())
                if citation.chunk_id == chunk_id
            ),
            None,
        )
        if cited is None:
            raise NotFoundError("citation not found")

        # 2. PostgreSQL decides readability, right now. This is the
        #    authorization -- the same call `retrieval.py` makes, for the same
        #    stated reason: the index carries a copy of the ACL that is only as
        #    fresh as the last re-index, and a revoked grant that has not
        #    reached it yet is exactly the case worth catching.
        #
        #    It is also the only place a `knowledge_base_id` can come from. The
        #    turn does not carry one (it lives in the request, not in
        #    `ConversationSession` and not in `chat_turns`), and
        #    `VectorIndexPort.fetch` requires it -- which is why this route is
        #    mounted under a turn instead of as a bare `GET /v1/chunks/{id}`.
        readable = await self.documents.readable_versions(
            tenant_id=tenant_id,
            principal_id=principal_id,
            document_ids=(cited.document_id,),
        )
        document = next(iter(readable), None)
        if document is None:
            raise CitedPassageUnavailableError("cited document is not readable")

        # 3. The index read, narrowed on every axis a search is narrowed on.
        chunks = await self.index.fetch(
            chunk_ids=(chunk_id,),
            tenant_id=tenant_id,
            knowledge_base_id=document.knowledge_base_id,
            authorized_principals=(principal_id,),
        )
        chunk = next(iter(chunks), None)
        if chunk is None:
            raise CitedPassageUnavailableError("cited passage is not in the index")

        # 4. Revision equality, exactly as the answer path requires it. A point
        #    from an older revision may sit in the index while ingestion
        #    catches up, and serving it because the document is still readable
        #    would show text the current PostgreSQL snapshot no longer
        #    describes. Equality rather than `>=` also rejects an impossible
        #    "future" point instead of letting the derived store run ahead of
        #    its authority.
        if chunk.source_revision != document.source_revision:
            raise CitedPassageUnavailableError("cited passage is at another revision")

        return CitedPassage(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            document_version=chunk.document_version,
            text=chunk.text,
            ordinal=chunk.ordinal,
            page=chunk.page,
        )


__all__ = [
    "CitationSourceReader",
    "CitationSourceUnavailableError",
    "CitedPassage",
    "CitedPassageUnavailableError",
]
