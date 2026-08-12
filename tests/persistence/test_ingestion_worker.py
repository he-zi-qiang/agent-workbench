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
from dataclasses import replace
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
    UnsupportedMediaTypeError,
)
from agent_workbench.adapters.persistence import (
    PostgresDocumentStore,
    PostgresExecutionGuardFactory,
    PostgresOutbox,
    create_query_engine,
)
from agent_workbench.adapters.persistence.knowledge_bases import (
    PostgresKnowledgeBaseStore,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionService
from agent_workbench.ports.knowledge_bases import KnowledgeDocument
from agent_workbench.workers.ingestion import IngestionWorker

DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
SIZE = 8
FIRST = b"Dense retrieval finds passages by meaning.\n"
SECOND = b"Sparse retrieval finds them by term overlap instead.\n"
# Declared text/plain by the upload path and not decodable as UTF-8, so the
# parser refuses it -- the ordinary shape of a document this build cannot read,
# rather than a fault injected into the worker.
UNREADABLE = b"\xff\xfe\x00 not UTF-8 at all\n"

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

    async def projected(self) -> KnowledgeDocument:
        """What a reader of this knowledge base is told about the document.

        Read through the real projection rather than the row, because the row
        is not what anybody sees: the whole point of recording a refusal is the
        status the interface derives from it.
        """

        documents = await PostgresKnowledgeBaseStore(
            self.engine
        ).list_readable_documents(
            tenant_id=TENANT, principal_id=OWNER, knowledge_base_id=KB
        )
        return documents[0]

    async def recorded_refusal(self) -> tuple[int | None, str | None]:
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT failed_revision, failure_code FROM documents")
                )
            ).one()
        return row.failed_revision, row.failure_code


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


def test_a_document_the_parser_refuses_is_reported_as_failed(tmp_path: Path) -> None:
    """The wait has to end somewhere, and for this document it never does.

    Nothing about the retry changes: the event stays unacknowledged and comes
    back after its lease, and it will be refused again for the same reason.
    What changes is that the refusal is now visible while that happens --
    before, the only two states were "indexed" and "not indexed yet", and a
    file no parser here can read sat in the second one for ever.
    """

    async def scenario(harness: _Harness) -> tuple[str, str, Any, Any]:
        await harness.upload(UNREADABLE)
        before = await harness.projected()
        with pytest.raises(UnsupportedMediaTypeError):
            await harness.worker.drain()
        after = await harness.projected()
        return (
            before.status,
            after.status,
            after.failure_code,
            await harness.recorded_refusal(),
        )

    before, after, code, recorded = _run(scenario, tmp_path)

    # The control: the same document, one drain earlier, is genuinely still
    # waiting. Without this the assertion below could pass on a projection that
    # called everything failed.
    assert before == "processing"
    assert after == "failed"
    # The code, not the parser's message -- that message quotes the bytes it
    # refused, and this field is readable by everyone the base is shared with.
    assert code == "invalid_tool_input"
    assert recorded == (1, "invalid_tool_input")


def test_a_later_revision_that_indexes_leaves_no_refusal_behind(
    tmp_path: Path,
) -> None:
    """A refusal describes one revision, and must not outlive it.

    Re-uploading readable bytes is the ordinary answer to a rejected file. If
    the marker survived, the document would be searchable and still labelled
    failed, which is the original complaint with the sign flipped.
    """

    async def scenario(harness: _Harness) -> tuple[str, Any, Any]:
        await harness.upload(UNREADABLE)
        with pytest.raises(UnsupportedMediaTypeError):
            await harness.worker.drain()
        await harness.upload(FIRST)
        await harness.worker.drain()
        projected = await harness.projected()
        return (
            projected.status,
            projected.failure_code,
            await harness.recorded_refusal(),
        )

    status, code, recorded = _run(scenario, tmp_path)

    assert status == "ready"
    assert code is None
    assert recorded == (None, None)


def test_a_held_document_guard_defers_without_acknowledging_the_event(
    tmp_path: Path,
) -> None:
    """Two live ingestion processes never write one document concurrently."""

    async def scenario(harness: _Harness) -> tuple[int, int, int]:
        await harness.upload(FIRST)
        guards = PostgresExecutionGuardFactory(_dsn(), healthcheck_seconds=0.05)
        held = await guards.acquire(
            task_id="document:doc_1",
            worker_id="worker_other",
            epoch=1,
        )
        guarded_worker = replace(harness.worker, guards=guards)
        try:
            deferred = await guarded_worker.drain()
            pending_while_held = await PostgresOutbox(harness.engine).pending_count()
        finally:
            await held.release()

        applied = await guarded_worker.drain()
        await guards.dispose()
        return deferred.deferred, pending_while_held, applied.indexed

    assert _run(scenario, tmp_path) == (1, 1, 1)
