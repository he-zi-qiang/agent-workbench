"""Storing what a chunk named, and finding chunks through it (ADR-037).

Two operations that look symmetric and are not. Writing is per chunk and
merges entities inside a knowledge base, so two documents naming one thing
become one entry point. Reading returns *chunk references*, never entities:
what the graph produces is a nomination that the retrieval service then
authorizes by document, exactly as it authorizes a dense or sparse candidate.

Nothing here returns text or scores an answer. The arms embed entity names and
relationship descriptions elsewhere; this port is the join from whatever those
matched back to the chunks the claim was read from.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel


class ChunkNomination(DomainModel):
    """One chunk the graph proposes, and the provenance that permits it.

    ``document_id`` travels because the caller authorizes by document. A
    nomination that carried only a chunk id would force retrieval to look the
    document up again, and a nomination that carried a *merged* entity's
    documents would be exactly the thing ADR-037 refuses: evidence does not
    merge.
    """

    chunk_id: Identifier
    document_id: Identifier
    document_version: Identifier
    #: How close the query was to the entity name or relationship description
    #: that produced this nomination. Carried so the arm can be ordered before
    #: fusion; it is not comparable with a dense or sparse score and is never
    #: compared with one -- RRF reads ranks, not scores.
    score: float


class StoredEntity(DomainModel):
    """An entity as it exists after merging, with its own id."""

    entity_id: Identifier
    normalized_name: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    display_name: str = Field(min_length=1)


@runtime_checkable
class KnowledgeGraphStore(Protocol):
    """Write what a chunk named; read the chunks an entity or edge points at."""

    async def record_chunk(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version: str,
        chunk_id: str,
        graph_identity: str,
        entities: tuple[tuple[str, str, str], ...],
        relations: tuple[tuple[str, str, str], ...],
    ) -> tuple[StoredEntity, ...]:
        """Merge these entities and store this chunk's mentions and edges.

        ``entities`` is ``(normalized_name, entity_type, display_name)`` and
        ``relations`` is ``(subject_normalized, object_normalized,
        description)``. Normalisation happens in the domain, not here: two
        callers normalising differently would merge differently, and the merge
        key is the one thing that must have a single definition.

        Idempotent per chunk. Re-extracting a version must not accumulate
        mentions, so a repeated call for the same ``(entity, chunk)`` or the
        same ``(subject, object, chunk)`` leaves one row -- which is what makes
        a retried outbox event safe.

        Returns the merged entities so a caller can embed their names knowing
        which id each belongs to.
        """
        ...

    async def forget_document(self, *, tenant_id: str, document_id: str) -> int:
        """Drop every mention and edge read from this document.

        Returns how many rows went. Entities are left standing even when their
        last mention goes: an entity with no mentions nominates nothing, so it
        is inert, and deleting it would race a concurrent extraction that has
        just merged onto it.
        """
        ...

    async def expand_from_seeds(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        graph_identity: str,
        seed_chunk_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[ChunkNomination, ...]:
        """The chunks the seeds' entities also appear in, minus the seeds.

        This is the arm ADR-037 §2.7 replaced §2.1 with, and the direction is
        the whole finding: on the measured failures the bridge entity is named
        in the document the other arms already found (7/7) and never in the
        query (0/7), so an arm that started from the query could not reach it.

        Seeds are excluded from the result. They are already in the other arms'
        output, and returning them would hand a chunk a second rank in a
        fusion that counts ranks -- inflating exactly the documents that needed
        no help.

        ``score`` on a nomination is how many distinct seed entities reached
        it. Not a similarity: nothing here embeds anything. It orders the arm,
        which is all RRF reads.
        """
        ...

    async def nominations_for_entities(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        graph_identity: str,
        scored_entity_ids: tuple[tuple[str, float], ...],
        limit: int,
    ) -> tuple[ChunkNomination, ...]:
        """The chunks these entities were read from.

        ``graph_identity`` narrows in the query rather than being checked
        after: rows written by a different extractor are not comparable with
        these, and a nomination that mixed two identities would let a
        re-extraction change retrieval silently (ADR-037 §2.5).
        """
        ...

    async def nominations_for_relations(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        graph_identity: str,
        scored_relation_ids: tuple[tuple[str, float], ...],
        limit: int,
    ) -> tuple[ChunkNomination, ...]:
        """The chunks these relationships were read from."""
        ...


__all__ = ["ChunkNomination", "KnowledgeGraphStore", "StoredEntity"]
