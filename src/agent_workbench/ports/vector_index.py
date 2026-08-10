"""The vector index boundary: a derived copy of what PostgreSQL already knows.

Nothing here is authoritative. Every point can be rebuilt from documents, their
versions and their ACL, and that stays true only while nothing writes a fact to
the index that is not in PostgreSQL first. An index that has become the only
place some fact lives is an index that cannot be dropped, and a derived store
you cannot drop is a primary store nobody designed.

Points carry a stable id derived from the chunk they hold, so re-indexing the
same chunk overwrites rather than duplicates. That is what makes a retried
ingestion safe: at-least-once delivery plus a stable id is idempotent, while
at-least-once plus a generated id is a growing pile of near-duplicates that
retrieval will happily return all of.

Every point also carries the ``source_revision`` it was built from. This port
does not enforce ordering with it, and saying so is the point: Qdrant has no
conditional write, so any check here would be read-then-write and would lose
the race it claims to win. Ordering is the ingestion worker's job -- one writer
per document, holding a lock, re-reading the current snapshot before it writes
(WP05-07). The revision is recorded so that protocol has something to compare,
and so a reader can tell which revision it is looking at.

The payload filter is a narrowing, never an authorization. Tenant and ACL
fields are stored so a query returns less, but what a principal may actually
read is decided against PostgreSQL, before and after this port is consulted.
Treating a derived copy as the authority on permissions is how a stale index
becomes a data leak.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel

# Qdrant names each vector in a collection so dense and sparse can live side by
# side. The dense name is fixed here and mirrored in configuration; a mismatch
# between the two is a collection whose vectors nothing can query.
DENSE_VECTOR_NAME = "dense"

# Sparse lives beside dense in the same collection, under its own name, so that
# both arms range over one set of points and one narrowing. Two collections
# would be two candidate sets that could drift apart -- a point present in one
# and missing from the other is a fusion input nobody can reconstruct. This is
# about the points, not about where the fusion runs: ADR-033 moved that into
# the adapter and left this unchanged.
SPARSE_VECTOR_NAME = "sparse"


class IndexedChunk(DomainModel):
    """One chunk as the index holds it: its vector, and what filters it."""

    chunk_id: Identifier
    document_id: Identifier
    document_version: Identifier
    tenant_id: Identifier
    knowledge_base_id: Identifier
    owner_id: Identifier
    # Who PostgreSQL said could read this document when the point was written.
    # Stored to narrow a query, never to answer one.
    authorized_principals: tuple[Identifier, ...] = ()
    source_revision: int = Field(ge=1)
    text: str
    ordinal: int = Field(ge=0)
    #: The page this chunk begins on, for formats that have pages. ``None``
    #: for the ones that do not -- not 1, which would claim a location nothing
    #: established. A character offset indexes the extracted text rather than
    #: the stored file, so for a PDF this is the only part of a locator that
    #: can actually send a reader to the right place.
    page: int | None = Field(default=None, ge=1)
    vector: tuple[float, ...]
    # Absent when the process has no sparse encoder. A point without them is
    # still dense-searchable, so a collection can hold both while sparse is
    # being rolled out -- and a query that prefetches sparse simply does not
    # match it, rather than failing.
    sparse_indices: tuple[int, ...] = ()
    sparse_values: tuple[float, ...] = ()


class ScoredChunk(DomainModel):
    """A candidate the index returned, with the score it was ranked by."""

    chunk_id: Identifier
    document_id: Identifier
    document_version: Identifier
    tenant_id: Identifier
    knowledge_base_id: Identifier
    source_revision: int = Field(ge=1)
    text: str
    ordinal: int = Field(ge=0)
    #: The page this chunk begins on, for formats that have pages. ``None``
    #: for the ones that do not -- not 1, which would claim a location nothing
    #: established. A character offset indexes the extracted text rather than
    #: the stored file, so for a PDF this is the only part of a locator that
    #: can actually send a reader to the right place.
    page: int | None = Field(default=None, ge=1)
    score: float


@runtime_checkable
class VectorIndexPort(Protocol):
    """Storage and dense search over chunk vectors."""

    async def ensure_collection(self, *, vector_size: int) -> None:
        """Create the collection if it is absent, and refuse a mismatched one.

        Idempotent by design -- every process may call it at startup. A
        collection that already exists with a different vector size is an
        error rather than something to recreate: dropping it would discard an
        index somebody is querying right now.
        """
        ...

    async def upsert(self, chunks: tuple[IndexedChunk, ...]) -> int:
        """Write chunks under their stable ids, returning how many were sent.

        Last write wins. Ordering between revisions belongs to the single
        writer holding the document lock, not here -- see the module
        docstring.
        """
        ...

    async def search(
        self,
        *,
        vector: tuple[float, ...],
        tenant_id: str,
        knowledge_base_id: str,
        authorized_principals: tuple[str, ...],
        limit: int,
    ) -> tuple[ScoredChunk, ...]:
        """Nearest chunks within one tenant and knowledge base.

        ``authorized_principals`` narrows the candidate set; it does not
        authorize anything. The caller re-checks every surviving candidate
        against PostgreSQL before it reaches a context packet or a citation.
        """
        ...

    async def search_hybrid(
        self,
        *,
        vector: tuple[float, ...],
        sparse_indices: tuple[int, ...],
        sparse_values: tuple[float, ...],
        tenant_id: str,
        knowledge_base_id: str,
        authorized_principals: tuple[str, ...],
        limit: int,
        dense_limit: int,
        sparse_limit: int,
    ) -> tuple[ScoredChunk, ...]:
        """Dense and sparse candidates, fused exactly once.

        Once is the constraint, and it is about attribution: a second fusion --
        a relative-score pass applied on top of RRF, or two already-truncated
        lists recombined -- produces an ordering neither retriever produced and
        no evaluation can explain.

        Once used to be guaranteed by keeping both arms inside the engine, so
        that this process never held two ranked lists. ADR-033 gives that up,
        because it bought the wrong guarantee: RRF scores by rank within an
        arm, and the engine ranks candidates it scored equally in whatever
        order it produced them, so the fused scores were not reproducible
        across a re-index. An implementation may now fuse the two arms itself,
        under two conditions -- each arm is a raw retriever result that nothing
        has fused, and the fusion runs over them one time.
        """
        ...

    async def delete_document(self, *, tenant_id: str, document_id: str) -> None:
        """Remove every point belonging to one document."""
        ...


__all__ = [
    "DENSE_VECTOR_NAME",
    "SPARSE_VECTOR_NAME",
    "IndexedChunk",
    "ScoredChunk",
    "VectorIndexPort",
]
