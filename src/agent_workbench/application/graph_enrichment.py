"""The second pass: read one document's chunks for entities and edges.

Separate from ``IngestionService`` on purpose, and the separation is a failure
domain rather than a layering preference. The first pass holds the document
lock and ends by declaring the version indexed; it must not be able to fail
because a model timed out. This pass runs afterwards, from its own outbox
event, and everything it does is additive: a document is searchable whether or
not its graph was ever built (ADR-037 §2.6).

Chunks are re-derived rather than carried. A chunk id is a hash over
``index_identity``, the document version and the ordinal, so parsing and
chunking the same bytes with the same chunker lands on exactly the ids the
vector index holds. That is what lets an outbox row stay small: it names a
document version, not a payload of text.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_workbench.application.graph_extraction import GraphExtractionService
from agent_workbench.application.ingestion import IngestionService
from agent_workbench.domain.knowledge_graph import normalize_entity_name
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.knowledge_graph import KnowledgeGraphStore


@dataclass(frozen=True, slots=True)
class EnrichmentReport:
    """What one document's second pass did, in numbers a run can be judged on.

    ``unreadable_chunks`` is the honest counter: a chunk the extractor could
    not read stores nothing, exactly like a chunk that names nothing, and a
    report that folded the two together would let a broken provider look like
    a boring corpus.
    """

    chunks: int = 0
    entities: int = 0
    relations: int = 0
    unreadable_chunks: int = 0


@dataclass(frozen=True, slots=True)
class GraphEnrichmentService:
    """Turn one indexed document version into entities, mentions and edges."""

    ingestion: IngestionService
    extraction: GraphExtractionService
    store: KnowledgeGraphStore
    graph_identity: str

    async def enrich(
        self,
        principal: PrincipalContext,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        document_id: str,
        document_version: str,
        media_type: str,
        content: bytes,
    ) -> EnrichmentReport:
        parsed = self.ingestion.parser.parse(content, media_type=media_type)
        chunks = self.ingestion.chunker.split(
            parsed.text, page_starts=parsed.page_starts
        )
        if not chunks:
            return EnrichmentReport()

        report = EnrichmentReport()
        for chunk in chunks:
            result = await self.extraction.extract(principal, text=chunk.text)
            if not result.extracted:
                report = _plus(report, chunks=1, unreadable=1)
                continue

            extraction = result.extraction
            if not extraction.entities:
                report = _plus(report, chunks=1)
                continue

            # Relations are filtered in the domain, where the merge key is
            # defined: an edge to something this chunk did not name would need
            # an entity nobody mentioned, and inventing it would be this code
            # making the claim.
            relations = extraction.relations_with_known_entities()
            stored = await self.store.record_chunk(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                document_version=document_version,
                chunk_id=self.ingestion.chunk_id(document_version, chunk.ordinal),
                graph_identity=self.graph_identity,
                entities=tuple(
                    (entity.normalized_name, entity.entity_type, entity.name)
                    for entity in extraction.entities
                ),
                relations=tuple(
                    (
                        normalize_entity_name(relation.subject),
                        normalize_entity_name(relation.object),
                        relation.description,
                    )
                    for relation in relations
                ),
            )
            report = _plus(
                report,
                chunks=1,
                entities=len(stored),
                relations=len(relations),
            )
        return report


def _plus(
    report: EnrichmentReport,
    *,
    chunks: int = 0,
    entities: int = 0,
    relations: int = 0,
    unreadable: int = 0,
) -> EnrichmentReport:
    return EnrichmentReport(
        chunks=report.chunks + chunks,
        entities=report.entities + entities,
        relations=report.relations + relations,
        unreadable_chunks=report.unreadable_chunks + unreadable,
    )


__all__ = ["EnrichmentReport", "GraphEnrichmentService"]
