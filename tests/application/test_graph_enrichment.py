"""The second pass over one document, and what it counts.

The property worth pinning is that chunk ids are *re-derived*: the pass reads
the same bytes with the same chunker and must land on exactly the ids the
vector index already holds, because a nomination pointing at an id nothing
indexed can never be authorized and never reaches a reader.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_workbench.adapters.embedding.fake import DeterministicEmbedder
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.ingestion.approximate_counter import (
    ApproximateTokenCounter,
)
from agent_workbench.adapters.ingestion.parser import TextDocumentParser
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.graph_enrichment import GraphEnrichmentService
from agent_workbench.application.graph_extraction import GraphExtractionService
from agent_workbench.application.ingestion import IngestionService
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.event_log import EventScope
from agent_workbench.ports.knowledge_graph import StoredEntity
from agent_workbench.runtime.fake_executor import FakeAgentExecutor

PRINCIPAL = PrincipalContext(tenant_id="tenant_a", principal_id="user_1")
IDENTITY = "deepseek-chat+v1+fake"
TEXT = b"Team Marlin carries the Cinder rotation."


class _RecordingStore:
    """Accepts writes and remembers the chunk ids they claimed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record_chunk(self, **kwargs: Any) -> tuple[StoredEntity, ...]:
        self.calls.append(kwargs)
        return tuple(
            StoredEntity(
                entity_id=f"ent_{index}",
                normalized_name=normalized,
                entity_type=kind,
                display_name=display,
            )
            for index, (normalized, kind, display) in enumerate(kwargs["entities"])
        )

    async def forget_document(self, **kwargs: Any) -> int:  # pragma: no cover
        return 0

    async def nominations_for_entities(self, **kwargs: Any):  # pragma: no cover
        return ()

    async def nominations_for_relations(self, **kwargs: Any):  # pragma: no cover
        return ()


def _ingestion() -> IngestionService:
    return IngestionService(
        parser=TextDocumentParser(),
        chunker=Chunker(
            size_tokens=512, overlap_tokens=64, counter=ApproximateTokenCounter()
        ),
        embedder=DeterministicEmbedder(),
        index=_NullIndex(),
    )


class _NullIndex:
    async def ensure_collection(self, **kwargs: Any) -> None:  # pragma: no cover
        return None

    async def upsert(self, chunks: Any) -> int:
        return len(chunks)

    async def search(self, **kwargs: Any):  # pragma: no cover
        return ()

    async def search_sparse(self, **kwargs: Any):  # pragma: no cover
        return ()

    async def search_hybrid(self, **kwargs: Any):  # pragma: no cover
        return ()

    async def delete_document(self, **kwargs: Any) -> None:  # pragma: no cover
        return None


def _service(respond: Any, store: _RecordingStore) -> GraphEnrichmentService:
    return GraphEnrichmentService(
        ingestion=_ingestion(),
        extraction=GraphExtractionService(
            executor=FakeAgentExecutor(respond=respond),
            timeout_seconds=5.0,
            sink_for=lambda stream_id: ScopedEventSink(
                log=InMemoryEventLog(),
                scope=EventScope(stream_id=stream_id, run_id=stream_id),
            ),
        ),
        store=store,
        graph_identity=IDENTITY,
    )


def _enrich(service: GraphEnrichmentService, content: bytes = TEXT):
    return asyncio.run(
        service.enrich(
            PRINCIPAL,
            tenant_id="tenant_a",
            knowledge_base_id="kb_main",
            document_id="doc_teams",
            document_version="ver_1",
            media_type="text/markdown",
            content=content,
        )
    )


_GOOD = json.dumps(
    {
        "entities": [
            {"name": "Team Marlin", "entity_type": "team"},
            {"name": "Cinder rotation", "entity_type": "rotation"},
        ],
        "relations": [
            {
                "subject": "Team Marlin",
                "object": "Cinder rotation",
                "description": "Team Marlin carries the Cinder rotation.",
            }
        ],
    }
)


def test_the_chunk_id_matches_what_ingestion_would_have_indexed() -> None:
    """Re-derivation, asserted against the first pass's own computation.

    A nomination pointing at an id nothing indexed can never be authorized and
    never reaches a reader, so this is the property the whole design rests on.
    """

    store = _RecordingStore()
    service = _service(lambda request: _GOOD, store)

    _enrich(service)

    expected = service.ingestion.chunk_id("ver_1", 0)
    assert [call["chunk_id"] for call in store.calls] == [expected]
    assert store.calls[0]["graph_identity"] == IDENTITY


def test_relations_are_normalized_to_the_merge_key_before_storage() -> None:
    store = _RecordingStore()
    _enrich(_service(lambda request: _GOOD, store))

    subject, object_, _ = store.calls[0]["relations"][0]
    assert (subject, object_) == ("team marlin", "cinder rotation")


def test_an_unreadable_chunk_is_counted_apart_from_an_empty_one() -> None:
    """Both store nothing; only one is a fact about the corpus."""

    unreadable = _enrich(_service(lambda request: "not json", _RecordingStore()))
    empty = _enrich(
        _service(lambda request: json.dumps({"entities": []}), _RecordingStore())
    )

    assert unreadable.unreadable_chunks == 1
    assert unreadable.entities == 0
    assert empty.unreadable_chunks == 0
    assert empty.chunks == 1


def test_an_empty_document_writes_nothing_and_reports_nothing() -> None:
    store = _RecordingStore()
    report = _enrich(_service(lambda request: _GOOD, store), content=b"")

    assert store.calls == []
    assert report == type(report)()
