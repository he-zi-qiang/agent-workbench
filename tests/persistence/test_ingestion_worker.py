"""Draining the outbox: what gets indexed, and what is recognised as stale.

The gap this closes is that nothing consumed the outbox at all -- an upload
wrote a document, a version and an event, and no process ever turned that into
a searchable chunk. Everything that appeared to work called IngestionService
directly.

What is worth testing is not "an event indexes a document". It is that an event
is a wake-up rather than a payload: the worker indexes what PostgreSQL says now,
so a delayed event cannot overwrite a newer version, and a replay after a crash
converges instead of undoing.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.embedding import DeterministicEmbedder
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.persistence import (
    PostgresDocumentStore,
    PostgresOutbox,
    create_query_engine,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionService
from agent_workbench.workers.ingestion import IngestionWorker

DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
SIZE = 8
FIRST = b"Dense retrieval finds passages by meaning.\n"
SECOND = b"Sparse retrieval finds them by term overlap instead.\n"

TABLES = (
    "artifacts, upload_intents, document_acl, document_versions, documents, "
    "outbox_events"
)


def _dsn() -> str:
    dsn = os.environ.get(DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{DSN_ENV_VAR} is not set")
    return dsn


def _url() -> str:
    url = os.environ.get(QDRANT_URL_ENV_VAR)
    if not url:
        pytest.skip(f"{QDRANT_URL_ENV_VAR} is not set")
    return url


class _Harness:
    def __init__(self, engine: Any, index: QdrantVectorIndex, root: Path) -> None:
        self.engine = engine
        self.index = index
        self.store = PostgresDocumentStore(engine)
        self.artifacts = LocalArtifactStore(root)
        self.embedder = DeterministicEmbedder(dimension=SIZE)
        self.worker = IngestionWorker(
            engine=engine,
            outbox=PostgresOutbox(engine),
            ingestion=IngestionService(
                parser=TextDocumentParser(),
                chunker=Chunker(
                    size_tokens=8, overlap_tokens=2, counter=ApproximateTokenCounter()
                ),
                embedder=self.embedder,
                index=index,
            ),
            artifacts=self.artifacts,
            worker_id="worker_1",
        )

    async def upload(self, body: bytes) -> None:
        """One completed upload: document, version and outbox event."""

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

    async def indexed_texts(self) -> tuple[str, ...]:
        hits = await self.index.search(
            vector=await self.embedder.embed_query("retrieval"),
            tenant_id=TENANT,
            knowledge_base_id=KB,
            authorized_principals=(OWNER,),
            limit=50,
        )
        return tuple(hit.text for hit in hits)

    async def applied_revision(self) -> int:
        async with self.engine.connect() as connection:
            return int(
                (
                    await connection.execute(
                        text("SELECT last_applied_revision FROM documents")
                    )
                ).scalar_one()
            )


def _run(scenario: Callable[[_Harness], Awaitable[Any]], root: Path) -> Any:
    dsn, url = _dsn(), _url()
    collection = f"test_{uuid.uuid4().hex}"

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        client = AsyncQdrantClient(url=url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            index = QdrantVectorIndex(client, collection=collection)
            await index.ensure_collection(vector_size=SIZE)
            return await scenario(_Harness(engine, index, root))
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()
                await engine.dispose()

    return asyncio.run(execute())


def test_an_uploaded_document_becomes_searchable(tmp_path: Path) -> None:
    """The gap this closes: before, nothing turned an upload into a chunk."""

    async def scenario(harness: _Harness) -> tuple[int, bool]:
        await harness.upload(FIRST)
        result = await harness.worker.drain()
        texts = await harness.indexed_texts()
        return result.indexed, any("meaning" in text for text in texts)

    assert _run(scenario, tmp_path) == (1, True)


def test_nothing_is_indexed_before_the_worker_runs(tmp_path: Path) -> None:
    """The control. Without it the test above could pass on an eager write."""

    async def scenario(harness: _Harness) -> int:
        await harness.upload(FIRST)
        return len(await harness.indexed_texts())

    assert _run(scenario, tmp_path) == 0


def test_a_replayed_event_converges_rather_than_undoing(tmp_path: Path) -> None:
    """A crash between indexing and acking replays the same event."""

    async def scenario(harness: _Harness) -> tuple[int, int, int, int]:
        await harness.upload(FIRST)
        first = await harness.worker.drain()
        after_first = len(await harness.indexed_texts())
        # A crash between indexing and acknowledging, simulated the way it
        # actually happens: the acknowledgement never lands and the lease
        # expires, so the event becomes claimable again. Clearing claim_token
        # while leaving the lease in place would leave it held by a worker that
        # no longer exists, which is a different failure and not this one.
        async with harness.engine.begin() as connection:
            await connection.execute(
                text("UPDATE documents SET last_applied_revision = 0")
            )
            await connection.execute(
                text(
                    "UPDATE outbox_events SET acked_at = NULL, "
                    "lease_until = now() - interval '1 hour'"
                )
            )
        second = await harness.worker.drain()
        return (
            first.indexed,
            second.indexed,
            after_first,
            len(await harness.indexed_texts()),
        )

    first_indexed, second_indexed, before, after = _run(scenario, tmp_path)

    assert (first_indexed, second_indexed) == (1, 1)
    # Converged, not duplicated: stable ids mean the replay overwrote the same
    # points rather than writing a second copy beside them.
    assert before > 0
    assert after == before


def test_an_event_older_than_the_index_is_superseded(tmp_path: Path) -> None:
    """Delayed delivery must not overwrite a newer version with an older one."""

    async def scenario(harness: _Harness) -> tuple[int, int, tuple[str, ...]]:
        await harness.upload(FIRST)
        await harness.upload(SECOND)
        # Both events are queued; the worker indexes the current snapshot once
        # and recognises the second event as describing a past state.
        result = await harness.worker.drain()
        return result.indexed, result.superseded, await harness.indexed_texts()

    indexed, superseded, texts = _run(scenario, tmp_path)

    assert indexed == 1
    assert superseded == 1
    assert any("term overlap" in text for text in texts)


def test_the_applied_revision_records_what_the_index_has(tmp_path: Path) -> None:
    async def scenario(harness: _Harness) -> tuple[int, int]:
        await harness.upload(FIRST)
        before = await harness.applied_revision()
        await harness.worker.drain()
        return before, await harness.applied_revision()

    assert _run(scenario, tmp_path) == (0, 1)


def test_a_drained_queue_is_empty(tmp_path: Path) -> None:
    """A superseded event is acknowledged, or the worker rediscovers it forever."""

    async def scenario(harness: _Harness) -> int:
        await harness.upload(FIRST)
        await harness.upload(SECOND)
        await harness.worker.drain()
        return await PostgresOutbox(harness.engine).pending_count()

    assert _run(scenario, tmp_path) == 0
