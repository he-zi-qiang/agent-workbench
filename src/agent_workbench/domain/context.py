"""Retrieved evidence and the citations grounded in it.

Fixed two-step retrieval and the agentic ``knowledge_search`` tool produce the
same ``ContextPacket``, so citation rendering, evaluation and the context
budget have one implementation instead of two that drift.

Everything in a packet is untrusted data. Retrieved text may contain
instructions aimed at the model; it can never raise permissions, select tools
or override system policy. Authorization is decided against PostgreSQL ACL
state before and after retrieval, never by the retrieved content itself.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel, VersionedModel

ChunkText = Annotated[str, StringConstraints(max_length=32_768)]
QuoteText = Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class SourceLocator(DomainModel):
    """Where a chunk sits inside its source document.

    A citation the reader cannot follow is not a citation, so retrieval keeps
    page and paragraph positions from ingestion rather than reconstructing them
    at answer time.
    """

    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=0)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_character_range(self) -> SourceLocator:
        if (self.char_start is None) is not (self.char_end is None):
            raise ValueError("char_start and char_end are set together")
        if (
            self.char_start is not None
            and self.char_end is not None
            and self.char_end < self.char_start
        ):
            raise ValueError("char_end must not precede char_start")
        return self


class ContextChunk(DomainModel):
    """One retrieved passage, bound to the document version it came from."""

    chunk_id: Identifier
    document_id: Identifier
    document_version: Identifier
    tenant_id: Identifier
    text: ChunkText
    locator: SourceLocator = SourceLocator()
    score: float | None = None


class Citation(DomainModel):
    """A claim's pointer back into the evidence."""

    chunk_id: Identifier
    document_id: Identifier
    document_version: Identifier
    locator: SourceLocator = SourceLocator()
    quote: QuoteText | None = None


class ContextPacket(VersionedModel):
    """Evidence handed to one model call, with its citations."""

    chunks: tuple[ContextChunk, ...] = ()
    citations: tuple[Citation, ...] = ()
    retrieval_trace_id: Identifier | None = None
    token_estimate: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_citations_are_grounded(self) -> ContextPacket:
        chunks_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        if len(chunks_by_id) != len(self.chunks):
            raise ValueError("chunk_id must be unique inside a ContextPacket")

        for citation in self.citations:
            chunk = chunks_by_id.get(citation.chunk_id)
            # A citation that points outside the packet cannot be verified
            # against current ACL state before the answer is committed.
            if chunk is None:
                raise ValueError(
                    f"citation references chunk {citation.chunk_id} which is "
                    "not part of this packet"
                )
            if (
                citation.document_id != chunk.document_id
                or citation.document_version != chunk.document_version
            ):
                raise ValueError(
                    f"citation for chunk {citation.chunk_id} disagrees with "
                    "the chunk's document version"
                )
        return self

    def tenant_ids(self) -> frozenset[str]:
        """Tenants represented in this packet; a mixed packet is a leak."""

        return frozenset(chunk.tenant_id for chunk in self.chunks)


__all__ = [
    "ChunkText",
    "Citation",
    "ContextChunk",
    "ContextPacket",
    "QuoteText",
    "SourceLocator",
]
