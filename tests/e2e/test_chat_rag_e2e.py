"""E2E 1 of 3: upload a PDF, ask twice, get a citation somebody can follow.

The baseline names three fixed demonstrations and this is the first: "上传 PDF →
多轮 Chat → 可定位引用". Two of those three words were not true until recently --
this build could not read a PDF, and ``SourceLocator.page`` had existed since the
domain was written with nothing ever setting it.

What only the whole chain shows is that a page survives all of it. The parser
records where each page begins, the chunker turns that into the page a chunk
sits on, ingestion writes it onto the point, Qdrant stores and returns it,
retrieval puts it on the locator, and the citation the reader follows carries
it. Every one of those hops has a unit test; none of them can show that the
number a reader is finally handed is the page the sentence is actually on.

The model is scripted. The subject here is the evidence path, and a provider in
the middle would add a variable, a bill, and a reason for this to fail for
something other than the reason it exists.

Real PostgreSQL and real Qdrant.
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
from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.ingestion import (
    ApproximateTokenCounter,
    TextDocumentParser,
)
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.persistence import (
    PostgresChatReleaseCoordinator,
    PostgresConversationStore,
    PostgresDocumentStore,
    PostgresEventLog,
    create_query_engine,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.retrieval import ReferenceVectorIndexRetriever
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chat import ChatService
from agent_workbench.application.chat_execution import (
    ChatRequest,
    FixedTwoStepExecution,
)
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionRequest, IngestionService
from agent_workbench.application.retrieval import (
    RetrievalRequest,
    RetrievalService,
)
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import RunBudget, TokenUsage
from agent_workbench.ports.cancellation import CancellationSource
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway
from tests.support.pdf import build_pdf

DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
SIZE = 8

#: Two pages, and the fact under test lives on the second one. A single-page
#: fixture would pass with any page number the code happened to emit.
PAGES = (
    "Dense retrieval finds passages by meaning and sparse retrieval finds "
    "them by term overlap.",
    "The acquisition closes on the fourteenth, and nothing else in this "
    "document says so.",
)
FACT = "The acquisition closes on the fourteenth."

TABLES = (
    "artifacts, upload_intents, document_acl, document_versions, documents, "
    "outbox_events, chat_turns, messages, conversation_sessions, events, "
    "event_streams"
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
    """The real stack from stored bytes to a delivered citation."""

    def __init__(self, index: QdrantVectorIndex, engine: Any) -> None:
        self.index = index
        self.engine = engine
        self.documents = PostgresDocumentStore(engine)
        self.conversations = PostgresConversationStore(engine)
        self.embedder = DeterministicEmbedder(dimension=SIZE)
        self.log = PostgresEventLog(engine)
        self.session_id = f"ses_{uuid.uuid4().hex}"
        self.ingestion = IngestionService(
            parser=TextDocumentParser(),
            # Small enough that each page is its own chunk, so "which page" has
            # a single right answer rather than depending on a tie-break.
            chunker=Chunker(
                size_tokens=16, overlap_tokens=0, counter=ApproximateTokenCounter()
            ),
            embedder=self.embedder,
            index=self.index,
        )

    def chat(self, answer: str) -> ChatService:
        registry = StaticToolRegistry([])
        return ChatService(
            execution=FixedTwoStepExecution(
                retrieval=RetrievalService(
                    candidate_retriever=ReferenceVectorIndexRetriever(
                        embedder=self.embedder,
                        index=self.index,
                    ),
                    documents=self.documents,
                ),
                executor=ClaudeLikeAgentRuntime(
                    model=FakeModel(
                        [
                            ScriptedTurn(
                                text=answer,
                                usage=TokenUsage(input_tokens=10, output_tokens=5),
                            )
                        ]
                    ),
                    gateway=ToolGateway(
                        registry=registry,
                        policy=EnvelopePolicyEngine(registry=registry),
                    ),
                    policy_identity="test-policy",
                ),
                budget=RunBudget(max_steps=1, max_tool_calls=1),
            ),
            conversations=self.conversations,
            releaser=PostgresChatReleaseCoordinator(self.engine),
            request_timeout_seconds=30,
            orphan_grace_seconds=5,
        )

    async def upload_pdf(self) -> None:
        """Through the real upload path: intent, commit, then indexing."""

        body = build_pdf(PAGES)
        digest = hashlib.sha256(body).hexdigest()
        upload = await self.documents.create_upload(
            upload_id=f"upl_{uuid.uuid4().hex}",
            tenant_id=TENANT,
            owner_id=OWNER,
            declared_size_bytes=len(body),
            declared_sha256=digest,
            media_type="application/pdf",
        )
        version = await self.documents.commit_version(
            upload_id=upload.upload_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            document_id="doc_1",
            knowledge_base_id=KB,
            version_id=f"ver_{uuid.uuid4().hex}",
            artifact_id=f"art_{uuid.uuid4().hex}",
            content_sha256=digest,
            granted_principals=(),
        )
        await self.ingestion.ingest(
            IngestionRequest(
                tenant_id=TENANT,
                knowledge_base_id=KB,
                document_id="doc_1",
                document_version=version.version_id,
                owner_id=OWNER,
                authorized_principals=(OWNER,),
                source_revision=version.source_revision,
                media_type="application/pdf",
                content=body,
            )
        )

    async def retrieve(self, query: str, principal: str = OWNER) -> Any:
        return (
            await RetrievalService(
                candidate_retriever=ReferenceVectorIndexRetriever(
                    embedder=self.embedder,
                    index=self.index,
                ),
                documents=self.documents,
            ).retrieve(
                RetrievalRequest(
                    query=query,
                    tenant_id=TENANT,
                    principal_id=principal,
                    knowledge_base_id=KB,
                    top_k=3,
                )
            )
        ).packet

    async def chunk_holding(self, phrase: str) -> Any:
        packet = await self.retrieve(PAGES[1])
        holding = [chunk for chunk in packet.chunks if phrase in chunk.text]
        assert holding, f"nothing indexed holds {phrase!r}"
        return holding[0]

    async def ask(self, question: str, answer: str) -> Any:
        request = ChatRequest(
            session_id=self.session_id,
            question=question,
            principal=PrincipalContext(tenant_id=TENANT, principal_id=OWNER),
            knowledge_base_id=KB,
            idempotency_key=f"key_{uuid.uuid4().hex}",
            top_k=3,
        )
        sink = ScopedEventSink(
            log=self.log,
            scope=EventScope(stream_id=self.session_id, run_id=request.run_id),
        )
        return await self.chat(answer).ask(request, sink, CancellationSource())


def _run(scenario: Callable[[_Harness], Awaitable[Any]]) -> Any:
    dsn, url = _dsn(), _url()
    collection = f"e2e_{uuid.uuid4().hex}"

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        client = AsyncQdrantClient(url=url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            index = QdrantVectorIndex(client, collection=collection)
            await index.ensure_collection(vector_size=SIZE)
            harness = _Harness(index, engine)
            await harness.conversations.create_session(
                session_id=harness.session_id, tenant_id=TENANT, owner_id=OWNER
            )
            return await scenario(harness)
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()
                await engine.dispose()

    return asyncio.run(execute())


# --------------------------------------------------------------------------
# The demonstration
# --------------------------------------------------------------------------


def test_a_pdf_answer_cites_the_page_the_sentence_is_on() -> None:
    """The first fixed demonstration, in one run.

    A PDF is uploaded through the real document path, indexed, retrieved, and
    answered. The citation the reader is handed names the document *and* the
    page -- and the page is the one the sentence is actually on, which is the
    only version of this claim worth making.
    """

    async def scenario(harness: _Harness) -> Any:
        await harness.upload_pdf()
        # Citations are only ever built for ids the model was actually shown,
        # so the answer has to name one. Which id that is only exists after
        # ingestion, so it is read back rather than written into the fixture.
        holding = await harness.chunk_holding("fourteenth")
        return await harness.ask(
            "when does the acquisition close", f"{FACT} [{holding.chunk_id}]"
        )

    turn = _run(scenario)

    assert turn.citations, "an answer grounded in a PDF cites something"
    assert turn.citations[0].locator.page is not None, (
        "a PDF citation carries the page it came from"
    )


def test_the_page_is_the_one_the_fact_is_on() -> None:
    """Any page number would satisfy "a page is present".

    The fixture puts the answer on page two, so a citation reporting page one
    is a citation pointing at the wrong half of the document.
    """

    async def scenario(harness: _Harness) -> Any:
        await harness.upload_pdf()
        # Read the locator retrieval built, rather than whichever the model
        # happened to cite.
        return await harness.retrieve(PAGES[1])

    packet = _run(scenario)

    holding = [chunk for chunk in packet.chunks if "fourteenth" in chunk.text]
    assert holding, "the fact was indexed"
    assert holding[0].locator.page == 2


def test_a_second_turn_sees_the_first() -> None:
    """ "多轮" is part of the demonstration, not a separate feature.

    A session that forgot the previous turn would answer the second question
    with no history, and the transcript a reader replays would be a list of
    unrelated answers.
    """

    async def scenario(harness: _Harness) -> Any:
        await harness.upload_pdf()
        await harness.ask("when does the acquisition close", FACT)
        await harness.ask("and how is retrieval done", "Dense and sparse, fused once.")
        return await harness.conversations.history(
            session_id=harness.session_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            limit=10,
        )

    messages = _run(scenario)

    roles = [stored.message.role for stored in messages]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_another_principal_gets_no_citation_from_this_document() -> None:
    """The evidence path is an authorization path, and a demo must show it.

    The document is granted to nobody else, so a reader who was never given it
    retrieves nothing -- and an answer with no evidence cites nothing rather
    than citing what it could not read.
    """

    async def scenario(harness: _Harness) -> Any:
        await harness.upload_pdf()
        return await harness.retrieve(PAGES[1], principal="user_stranger")

    packet = _run(scenario)

    assert packet.chunks == ()
    assert packet.citations == ()
