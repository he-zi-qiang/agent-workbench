"""The second pass, end to end: index a document, then extract its graph.

Everything here runs against real PostgreSQL and real Qdrant, because the
properties are about ordering and transactions: the extraction request has to
be committed with the revision that produced it, and it has to be claimed by
the same machinery the first pass uses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

from agent_workbench.adapters.artifacts.local import LocalArtifactStore
from agent_workbench.adapters.embedding.fake import DeterministicEmbedder
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.ingestion.approximate_counter import (
    ApproximateTokenCounter,
)
from agent_workbench.adapters.ingestion.parser import TextDocumentParser
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.adapters.persistence.documents import PostgresDocumentStore
from agent_workbench.adapters.persistence.knowledge_graph import (
    PostgresKnowledgeGraphStore,
)
from agent_workbench.adapters.persistence.outbox import PostgresOutbox
from agent_workbench.adapters.vector.qdrant import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.graph_enrichment import GraphEnrichmentService
from agent_workbench.application.graph_extraction import GraphExtractionService
from agent_workbench.application.ingestion import IngestionService
from agent_workbench.apps.ingestion_worker.identity import restore_document_owner
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime.fake_executor import FakeAgentExecutor
from agent_workbench.workers.ingestion import IngestionWorker

DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
SIZE = 8
IDENTITY = "fake-extractor+v1+deterministic"

TABLES = (
    "kg_relations, kg_mentions, kg_entities, artifacts, upload_intents, "
    "document_acl, document_versions, documents, outbox_events"
)


def restore_document_owner_positional(tenant_id: str, owner_id: str) -> Any:
    """The composition-side adapter, as the worker receives it."""

    return restore_document_owner(tenant_id=tenant_id, owner_id=owner_id)


_EXTRACTION = json.dumps(
    {
        "entities": [{"name": "Team Marlin", "entity_type": "team"}],
        "relations": [],
    }
)


def _env() -> tuple[str, str]:
    dsn = os.environ.get(DSN_ENV_VAR)
    url = os.environ.get(QDRANT_URL_ENV_VAR)
    if not dsn:
        pytest.skip(f"{DSN_ENV_VAR} is not set")
    if not url:
        pytest.skip(f"{QDRANT_URL_ENV_VAR} is not set")
    return dsn, url


class _Harness:
    def __init__(self, engine: Any, index: QdrantVectorIndex, root: Path) -> None:
        self.engine = engine
        self.store = PostgresDocumentStore(engine)
        self.artifacts = LocalArtifactStore(root)
        self.graph = PostgresKnowledgeGraphStore(engine)
        self.calls: list[str] = []
        ingestion = IngestionService(
            parser=TextDocumentParser(),
            chunker=Chunker(
                size_tokens=64, overlap_tokens=8, counter=ApproximateTokenCounter()
            ),
            embedder=DeterministicEmbedder(dimension=SIZE),
            index=index,
        )
        self.ingestion = ingestion

        def respond(request: Any) -> str:
            self.calls.append("extract")
            return _EXTRACTION

        self.enrichment = GraphEnrichmentService(
            ingestion=ingestion,
            extraction=GraphExtractionService(
                executor=FakeAgentExecutor(respond=respond),
                timeout_seconds=5.0,
                sink_for=lambda stream_id: ScopedEventSink(
                    log=InMemoryEventLog(),
                    scope=EventScope(stream_id=stream_id, run_id=stream_id),
                ),
            ),
            store=self.graph,
            graph_identity=IDENTITY,
        )

    def worker(self, *, with_graph: bool) -> IngestionWorker:
        return IngestionWorker(
            engine=self.engine,
            outbox=PostgresOutbox(self.engine),
            ingestion=self.ingestion,
            artifacts=self.artifacts,
            worker_id="worker_1",
            enrichment=self.enrichment if with_graph else None,
            principal_for=(restore_document_owner_positional if with_graph else None),
        )

    async def upload(self, body: bytes) -> None:
        digest = hashlib.sha256(body).hexdigest()
        stored = await self.artifacts.put(
            tenant_id=TENANT,
            owner_id=OWNER,
            kind="source_document",
            media_type="text/plain",
            content=body,
        )
        intent = await self.store.create_upload(
            upload_id=f"upl_{uuid.uuid4().hex}",
            tenant_id=TENANT,
            owner_id=OWNER,
            declared_size_bytes=len(body),
            declared_sha256=digest,
            media_type="text/plain",
        )
        await self.store.commit_version(
            upload_id=intent.upload_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            document_id="doc_1",
            knowledge_base_id=KB,
            version_id=f"ver_{uuid.uuid4().hex}",
            artifact_id=stored.artifact_id,
            content_sha256=digest,
        )

    async def queued_kinds(self) -> tuple[str, ...]:
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT kind FROM outbox_events WHERE acked_at IS NULL "
                        "ORDER BY sequence"
                    )
                )
            ).all()
        return tuple(str(row[0]) for row in rows)

    async def mention_count(self) -> int:
        async with self.engine.connect() as connection:
            return int(
                (
                    await connection.execute(text("SELECT count(*) FROM kg_mentions"))
                ).scalar_one()
            )


def _run(scenario: Callable[[_Harness], Awaitable[Any]]) -> Any:
    dsn, url = _env()
    collection = f"test_{uuid.uuid4().hex}"

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        client = AsyncQdrantClient(url=url)
        with TemporaryDirectory() as root:
            try:
                async with engine.begin() as connection:
                    await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
                index = QdrantVectorIndex(client, collection=collection)
                await index.ensure_collection(vector_size=SIZE)
                return await scenario(_Harness(engine, index, Path(root)))
            finally:
                try:
                    await client.delete_collection(collection)
                finally:
                    await client.close()
                    await engine.dispose()

    return asyncio.run(execute())


def test_indexing_enqueues_the_extraction_and_the_second_drain_runs_it() -> None:
    """Two passes, and the graph only exists after the second one."""

    async def scenario(harness: _Harness) -> tuple[Any, ...]:
        await harness.upload(b"Team Marlin carries the Cinder rotation.")
        worker = harness.worker(with_graph=True)

        first = await worker.drain()
        after_first = (await harness.queued_kinds(), await harness.mention_count())

        second = await worker.drain()
        after_second = (await harness.queued_kinds(), await harness.mention_count())
        return first, after_first, second, after_second

    first, after_first, second, after_second = _run(scenario)

    assert first.indexed == 1
    # The request is queued by the pass that indexed, not by a scheduler.
    assert after_first == (("graph_extraction_requested",), 0)
    assert second.indexed == 1
    assert after_second[0] == ()
    assert after_second[1] > 0


def test_without_enrichment_nothing_is_ever_enqueued() -> None:
    """The control. A deployment that builds no graph pays nothing for it --
    not even a queued row somebody has to drain."""

    async def scenario(harness: _Harness) -> Any:
        await harness.upload(b"Team Marlin carries the Cinder rotation.")
        await harness.worker(with_graph=False).drain()
        return await harness.queued_kinds(), harness.calls

    queued, calls = _run(scenario)

    assert queued == ()
    assert calls == []


def test_a_stale_request_is_declined_when_another_worker_moved_ahead() -> None:
    """The interleaving the check exists for, constructed rather than hoped for.

    With one worker the extraction request has a lower sequence than the
    indexing event that would supersede it, so it is normally claimed first
    and runs legitimately -- the test below covers that. This one advances
    ``last_applied_revision`` directly, which is what a second worker
    finishing a newer version looks like from here.

    Declining costs nothing: the newer version queued a request of its own.
    """

    async def scenario(harness: _Harness) -> Any:
        await harness.upload(b"Team Marlin carries the Cinder rotation.")
        worker = harness.worker(with_graph=True)
        await worker.drain()  # indexes v1, queues its extraction

        async with harness.engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE documents SET last_applied_revision = "
                    "last_applied_revision + 1 WHERE document_id = 'doc_1'"
                )
            )

        result = await worker.drain()
        return result, await harness.mention_count(), harness.calls

    result, mentions, calls = _run(scenario)

    assert result.superseded == 1
    assert mentions == 0
    # And no model call was spent on it.
    assert calls == []


def test_a_new_version_gets_its_own_extraction() -> None:
    """The ordinary single-worker path: each indexed version queues, and runs,
    an extraction of its own."""

    async def scenario(harness: _Harness) -> Any:
        await harness.upload(b"Team Marlin carries the Cinder rotation.")
        worker = harness.worker(with_graph=True)
        await worker.drain()
        await worker.drain()
        first = await harness.mention_count()

        await harness.upload(b"Team Osprey carries the Dovetail rotation.")
        await worker.drain()
        await worker.drain()
        return first, await harness.mention_count(), len(harness.calls)

    first, second, calls = _run(scenario)

    assert first > 0
    # The second version's mentions are added beside the first's, each naming
    # the version it was read from.
    assert second > first
    assert calls >= 2


def test_an_extraction_request_left_by_a_graph_now_switched_off_is_acked() -> None:
    """It will not become runnable by waiting, so leaving it queued would make
    a worker rediscover it forever."""

    async def scenario(harness: _Harness) -> Any:
        await harness.upload(b"Team Marlin carries the Cinder rotation.")
        await harness.worker(with_graph=True).drain()
        assert await harness.queued_kinds() == ("graph_extraction_requested",)

        result = await harness.worker(with_graph=False).drain()
        return result, await harness.queued_kinds()

    result, queued = _run(scenario)

    assert result.skipped == 1
    assert queued == ()
