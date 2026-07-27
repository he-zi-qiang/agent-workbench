"""Retrieval against real Qdrant and real PostgreSQL, with a revoke in the middle.

The barrier test is the reason this file exists. WP04's exit condition asks for
a revoke committed *after* Qdrant has answered and *before* the context is
built, and for the revoked chunk to reach neither the context, the answer, nor
a citation. Everything else here is the ordinary case that gives that test
something to contrast with.

The interleaving is produced without a sleep and without a hook in production
code: a wrapper around the index performs the revoke inside ``search``, after
the real query returns and before the service sees the result. That is exactly
the window being defended, and it is deterministic -- the revoke has certainly
committed by the time authorization runs, every run.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

from agent_workbench.adapters.embedding import DeterministicEmbedder
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.persistence import (
    PostgresDocumentStore,
    create_query_engine,
)
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionRequest, IngestionService
from agent_workbench.application.retrieval import (
    AuthorizedContext,
    RetrievalRequest,
    RetrievalService,
    SourcesChangedError,
)
from agent_workbench.ports.vector_index import ScoredChunk

DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
READER = "user_reader"
STRANGER = "user_stranger"
SIZE = 8

SECRET = (
    "The acquisition closes on the fourteenth. Dense retrieval finds this "
    "passage by meaning, and fusion happens once inside Qdrant."
)

TABLES = (
    "artifacts, upload_intents, document_acl, "
    "document_versions, documents, outbox_events"
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
    """Real Qdrant, real PostgreSQL, one collection per test."""

    def __init__(self, index: QdrantVectorIndex, store: PostgresDocumentStore) -> None:
        self.index = index
        self.store = store
        self.embedder = DeterministicEmbedder(dimension=SIZE)
        self.ingestion = IngestionService(
            parser=TextDocumentParser(),
            chunker=Chunker(
                size_tokens=8, overlap_tokens=2, counter=ApproximateTokenCounter()
            ),
            embedder=self.embedder,
            index=index,
        )

    def retrieval(
        self, index: Any = None, *, sparse_encoder: Any = None
    ) -> RetrievalService:
        return RetrievalService(
            embedder=self.embedder,
            index=index if index is not None else self.index,
            documents=self.store,
            sparse_encoder=sparse_encoder,
        )

    async def publish(
        self,
        *,
        document_id: str = "doc_1",
        granted: tuple[str, ...] = (),
        content: str = SECRET,
    ) -> None:
        """Upload a document through the real store, then index it."""

        body = content.encode()
        digest = hashlib.sha256(body).hexdigest()
        upload = await self.store.create_upload(
            upload_id=f"upl_{uuid.uuid4().hex}",
            tenant_id=TENANT,
            owner_id=OWNER,
            declared_size_bytes=len(body),
            declared_sha256=digest,
            media_type="text/plain",
        )
        version = await self.store.commit_version(
            upload_id=upload.upload_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            document_id=document_id,
            knowledge_base_id=KB,
            version_id=f"ver_{uuid.uuid4().hex}",
            artifact_id=f"art_{uuid.uuid4().hex}",
            content_sha256=digest,
            granted_principals=granted,
        )
        await self.ingestion.ingest(
            IngestionRequest(
                tenant_id=TENANT,
                knowledge_base_id=KB,
                document_id=document_id,
                document_version=version.version_id,
                owner_id=OWNER,
                authorized_principals=(OWNER, *granted),
                source_revision=version.source_revision,
                media_type="text/plain",
                content=body,
            )
        )

    async def revoke(self, *, document_id: str = "doc_1") -> None:
        """Take the grant away in PostgreSQL, leaving the index untouched.

        Deliberately not re-indexing: the index keeps the stale ACL, which is
        the situation the second check exists for.
        """

        async with self.store._engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM document_acl WHERE document_id = :d"),
                {"d": document_id},
            )
            await connection.execute(
                text(
                    "UPDATE documents SET source_revision = source_revision + 1 "
                    "WHERE document_id = :d"
                ),
                {"d": document_id},
            )


def _run(scenario: Callable[[_Harness], Awaitable[Any]]) -> Any:
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
            return await scenario(_Harness(index, PostgresDocumentStore(engine)))
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()
                await engine.dispose()

    return asyncio.run(execute())


def _ask(principal: str) -> RetrievalRequest:
    return RetrievalRequest(
        query="when does the acquisition close",
        tenant_id=TENANT,
        principal_id=principal,
        knowledge_base_id=KB,
        top_k=8,
    )


# --- the ordinary case -------------------------------------------------------


def test_a_granted_reader_gets_context_and_citations() -> None:
    async def scenario(harness: _Harness) -> tuple[int, int, bool]:
        await harness.publish(granted=(READER,))
        context = await harness.retrieval().retrieve(_ask(READER))
        packet = context.packet
        return (
            len(packet.chunks),
            len(packet.citations),
            any("fourteenth" in chunk.text for chunk in packet.chunks),
        )

    chunks, citations, found = _run(scenario)

    assert chunks > 0
    assert citations == chunks
    assert found


def test_every_chunk_has_a_citation_that_names_it() -> None:
    """A citation without its chunk references something nobody read."""

    async def scenario(harness: _Harness) -> tuple[list[str], list[str]]:
        await harness.publish(granted=(READER,))
        packet = (await harness.retrieval().retrieve(_ask(READER))).packet
        return (
            sorted(chunk.chunk_id for chunk in packet.chunks),
            sorted(citation.chunk_id for citation in packet.citations),
        )

    chunk_ids, citation_ids = _run(scenario)

    assert chunk_ids == citation_ids


def test_a_stranger_gets_nothing(harness_unused: None = None) -> None:
    """The index would have narrowed this away too; PostgreSQL is why it holds."""

    async def scenario(harness: _Harness) -> int:
        await harness.publish(granted=(READER,))
        context = await harness.retrieval().retrieve(_ask(STRANGER))
        return len(context.packet.chunks)

    assert _run(scenario) == 0


# --- the barrier -------------------------------------------------------------


class _RevokingIndex:
    """Commits a revoke after Qdrant answers, before the service sees it.

    This is the window WP04's exit condition names. Wrapping the port rather
    than adding a hook to the service keeps the seam entirely in the test: the
    production path has no idea it is being interleaved with.
    """

    def __init__(self, inner: QdrantVectorIndex, harness: _Harness) -> None:
        self._inner = inner
        self._harness = harness
        self.returned: tuple[ScoredChunk, ...] = ()

    async def search(self, **kwargs: Any) -> tuple[ScoredChunk, ...]:
        found = await self._inner.search(**kwargs)
        self.returned = found
        await self._harness.revoke()
        return found

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_a_revoke_after_the_query_keeps_the_chunk_out_of_the_context() -> None:
    """WP04's exit condition, stated as a test.

    Qdrant answers with the chunk -- its stale payload still lists the reader.
    The revoke commits. Authorization then runs against PostgreSQL, and the
    chunk must not survive it.
    """

    async def scenario(harness: _Harness) -> tuple[int, int, int]:
        await harness.publish(granted=(READER,))
        racing = _RevokingIndex(harness.index, harness)
        context = await harness.retrieval(index=racing).retrieve(_ask(READER))
        return (
            len(racing.returned),
            len(context.packet.chunks),
            len(context.packet.citations),
        )

    from_index, in_context, cited = _run(scenario)

    # The index really did return it -- otherwise this test proves nothing.
    assert from_index > 0
    assert in_context == 0
    assert cited == 0


def test_the_revoked_text_does_not_appear_anywhere_in_the_packet() -> None:
    """Asserted on the text, not the count: leaking it once is leaking it."""

    async def scenario(harness: _Harness) -> tuple[int, str]:
        await harness.publish(granted=(READER,))
        racing = _RevokingIndex(harness.index, harness)
        context = await harness.retrieval(index=racing).retrieve(_ask(READER))
        return len(racing.returned), repr(context.packet.model_dump())

    from_index, dumped = _run(scenario)

    assert from_index > 0
    assert "fourteenth" not in dumped


def test_the_owner_is_unaffected_by_a_revoked_grant() -> None:
    """The control: the revoke removed a grant, not the document."""

    async def scenario(harness: _Harness) -> tuple[int, int]:
        await harness.publish(granted=(READER,))
        racing = _RevokingIndex(harness.index, harness)
        context = await harness.retrieval(index=racing).retrieve(_ask(OWNER))
        return len(racing.returned), len(context.packet.chunks)

    from_index, in_context = _run(scenario)

    assert from_index > 0
    assert in_context > 0


# --- the second check --------------------------------------------------------


def test_an_answer_is_refused_when_a_source_was_revoked_meanwhile() -> None:
    """Between building a context and committing an answer there is a model call."""

    async def scenario(harness: _Harness) -> AuthorizedContext:
        await harness.publish(granted=(READER,))
        service = harness.retrieval()
        context = await service.retrieve(_ask(READER))
        assert context.packet.chunks
        await harness.revoke()
        await service.confirm_unchanged(context, tenant_id=TENANT, principal_id=READER)
        return context

    with pytest.raises(SourcesChangedError):
        _run(scenario)


def test_an_unchanged_source_confirms(  # the control for the test above
) -> None:
    """Without it, a confirm_unchanged that always raised would look correct."""

    async def scenario(harness: _Harness) -> str:
        await harness.publish(granted=(READER,))
        service = harness.retrieval()
        context = await service.retrieve(_ask(READER))
        await service.confirm_unchanged(context, tenant_id=TENANT, principal_id=READER)
        return "confirmed"

    assert _run(scenario) == "confirmed"


def test_a_regranted_document_still_fails_the_second_check() -> None:
    """Revoked and re-granted is not the same as never touched.

    Re-asking "may I read it" would say yes. The revision says the ACL was
    replaced, which is what the answer was built against.
    """

    async def scenario(harness: _Harness) -> None:
        await harness.publish(granted=(READER,))
        service = harness.retrieval()
        context = await service.retrieve(_ask(READER))
        await harness.revoke()
        async with harness.store._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO document_acl (document_id, principal_id) "
                    "VALUES ('doc_1', :p)"
                ),
                {"p": READER},
            )
        await service.confirm_unchanged(context, tenant_id=TENANT, principal_id=READER)

    with pytest.raises(SourcesChangedError):
        _run(scenario)


# --- which retriever is this ------------------------------------------------


class _OneTermSparse:
    """A sparse encoder whose single term matches what ingestion wrote."""

    @property
    def vocabulary_size(self) -> int:
        return 250002

    @property
    def identity(self) -> str:
        return "sparse-v1"

    async def encode_documents(self, texts: tuple[str, ...]) -> tuple[Any, ...]:
        from agent_workbench.ports.sparse import SparseVector

        return tuple(SparseVector(indices=(99,), values=(1.0,)) for _ in texts)

    async def encode_query(self, text: str) -> Any:
        from agent_workbench.ports.sparse import SparseVector

        return SparseVector(indices=(99,), values=(1.0,))


def test_the_mode_says_dense_without_a_sparse_encoder() -> None:
    """An evaluation report must not be able to label a dense run as hybrid."""

    async def scenario(harness: _Harness) -> str:
        return harness.retrieval().mode

    assert _run(scenario) == "dense"


def test_the_mode_says_hybrid_with_one() -> None:
    """The control: an ablation comparing the two must not compare one to itself."""

    async def scenario(harness: _Harness) -> str:
        return harness.retrieval(sparse_encoder=_OneTermSparse()).mode

    assert _run(scenario) == "hybrid"


def test_hybrid_retrieval_still_authorizes_against_postgresql() -> None:
    """Adding an arm must not add a way past the check that follows it."""

    async def scenario(harness: _Harness) -> int:
        await harness.publish(granted=())
        service = harness.retrieval(sparse_encoder=_OneTermSparse())
        context = await service.retrieve(
            RetrievalRequest(
                query="fusion",
                tenant_id=TENANT,
                principal_id=STRANGER,
                knowledge_base_id=KB,
                top_k=5,
            )
        )
        return len(context.packet.chunks)

    assert _run(scenario) == 0
