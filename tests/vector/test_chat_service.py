"""One chat turn, end to end, against real Qdrant and real PostgreSQL.

The turn is fixed two-step: retrieve once, answer from what came back. What is
worth testing is not that a scripted model produces a scripted answer -- it is
the two authorization checks around it, and what the session ends up holding
when the second one fails.
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
from agent_workbench.adapters.memory import InMemoryEventLog
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.persistence import (
    PostgresConversationStore,
    PostgresDocumentStore,
    create_query_engine,
)
from agent_workbench.adapters.policy.envelope import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chat import REFUSAL, ChatRequest, ChatService
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionRequest, IngestionService
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import RunBudget, TokenUsage
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway

DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
QDRANT_URL_ENV_VAR = "AGENT_WORKBENCH_TEST_QDRANT_URL"

TENANT = "tenant_a"
KB = "kb_main"
OWNER = "user_owner"
READER = "user_reader"
SIZE = 8
ANSWER = "The acquisition closes on the fourteenth."

SECRET = (
    "The acquisition closes on the fourteenth. Dense retrieval finds this "
    "passage by meaning, and fusion happens once inside Qdrant."
)

TABLES = (
    "artifacts, upload_intents, document_acl, document_versions, documents, "
    "outbox_events, messages, conversation_sessions"
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
    def __init__(
        self,
        index: QdrantVectorIndex,
        documents: PostgresDocumentStore,
        conversations: PostgresConversationStore,
        engine: Any,
    ) -> None:
        self.index = index
        self.documents = documents
        self.conversations = conversations
        self.engine = engine
        self.embedder = DeterministicEmbedder(dimension=SIZE)
        self.log = InMemoryEventLog()
        self.session_id = f"ses_{uuid.uuid4().hex}"
        self.ingestion = IngestionService(
            parser=TextDocumentParser(),
            chunker=Chunker(
                size_tokens=8, overlap_tokens=2, counter=ApproximateTokenCounter()
            ),
            embedder=self.embedder,
            index=index,
        )

    def chat(self, *, answer: str = ANSWER) -> ChatService:
        registry = StaticToolRegistry([])
        return ChatService(
            retrieval=RetrievalService(
                embedder=self.embedder, index=self.index, documents=self.documents
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
                    registry=registry, policy=EnvelopePolicyEngine(registry=registry)
                ),
                policy_identity="test-policy",
            ),
            conversations=self.conversations,
            budget=RunBudget(max_steps=1, max_tool_calls=1),
        )

    def sink(self) -> ScopedEventSink:
        return ScopedEventSink(
            log=self.log,
            scope=EventScope(
                stream_id=f"str_{uuid.uuid4().hex}",
                run_id=f"run_{uuid.uuid4().hex}",
            ),
        )

    async def publish(self, *, granted: tuple[str, ...] = ()) -> None:
        body = SECRET.encode()
        digest = hashlib.sha256(body).hexdigest()
        upload = await self.documents.create_upload(
            upload_id=f"upl_{uuid.uuid4().hex}",
            tenant_id=TENANT,
            owner_id=OWNER,
            declared_size_bytes=len(body),
            declared_sha256=digest,
            media_type="text/plain",
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
            granted_principals=granted,
        )
        await self.ingestion.ingest(
            IngestionRequest(
                tenant_id=TENANT,
                knowledge_base_id=KB,
                document_id="doc_1",
                document_version=version.version_id,
                owner_id=OWNER,
                authorized_principals=(OWNER, *granted),
                source_revision=version.source_revision,
                media_type="text/plain",
                content=body,
            )
        )

    async def open_session(self, owner: str) -> None:
        await self.conversations.create_session(
            session_id=self.session_id, tenant_id=TENANT, owner_id=owner
        )

    async def revoke(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM document_acl WHERE document_id = 'doc_1'")
            )
            await connection.execute(
                text(
                    "UPDATE documents SET source_revision = source_revision + 1 "
                    "WHERE document_id = 'doc_1'"
                )
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
            harness = _Harness(
                index,
                PostgresDocumentStore(engine),
                PostgresConversationStore(engine),
                engine,
            )
            return await scenario(harness)
        finally:
            try:
                await client.delete_collection(collection)
            finally:
                await client.close()
                await engine.dispose()

    return asyncio.run(execute())


def _ask(harness: _Harness, principal: str) -> ChatRequest:
    return ChatRequest(
        session_id=harness.session_id,
        question="when does the acquisition close",
        principal=PrincipalContext(principal_id=principal, tenant_id=TENANT),
        knowledge_base_id=KB,
    )


# --- the ordinary turn -------------------------------------------------------


def test_a_turn_answers_with_citations() -> None:
    async def scenario(harness: _Harness) -> tuple[str, int, bool]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        turn = await harness.chat().ask(_ask(harness, READER), harness.sink())
        return turn.answer, len(turn.citations), turn.withheld

    answer, citations, withheld = _run(scenario)

    assert answer == ANSWER
    assert citations > 0
    assert withheld is False


def test_the_evidence_reaches_the_model_labelled_by_chunk_id() -> None:
    """A citation is checkable only against what the model was actually shown."""

    async def scenario(harness: _Harness) -> tuple[str, tuple[str, ...]]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        service = harness.chat()
        turn = await service.ask(_ask(harness, READER), harness.sink())
        model = service.executor._model
        assert isinstance(model, FakeModel)
        prompt = model.requests[0].messages[0].content[0].text
        return prompt, tuple(c.chunk_id for c in turn.citations)

    prompt, chunk_ids = _run(scenario)

    assert "fourteenth" in prompt
    for chunk_id in chunk_ids:
        assert chunk_id in prompt


def test_the_question_and_the_answer_are_both_persisted() -> None:
    async def scenario(harness: _Harness) -> list[str]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        service = harness.chat()
        await service.ask(_ask(harness, READER), harness.sink())
        history = await service.history(
            session_id=harness.session_id, tenant_id=TENANT, principal_id=READER
        )
        return [message.role for message in history]

    assert _run(scenario) == ["user", "assistant"]


def test_a_question_with_no_evidence_still_runs() -> None:
    """An empty context is a legitimate turn; the model is told to say so."""

    async def scenario(harness: _Harness) -> tuple[int, bool]:
        await harness.publish(granted=())
        await harness.open_session(READER)
        turn = await harness.chat().ask(_ask(harness, READER), harness.sink())
        return len(turn.citations), turn.withheld

    assert _run(scenario) == (0, False)


# --- withheld ----------------------------------------------------------------


def test_an_answer_is_withheld_when_a_source_is_revoked_mid_turn() -> None:
    """The model has already written it. It still does not get delivered."""

    async def scenario(harness: _Harness) -> tuple[str, bool, int]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        service = _RevokingChat(harness)
        turn = await service.ask(_ask(harness, READER), harness.sink())
        return turn.answer, turn.withheld, len(turn.citations)

    answer, withheld, citations = _run(scenario)

    assert withheld is True
    assert answer == REFUSAL
    assert citations == 0


def test_the_withheld_answer_does_not_enter_the_history() -> None:
    """Storing it would leave the text where the next turn reads it back."""

    async def scenario(harness: _Harness) -> str:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        service = _RevokingChat(harness)
        await service.ask(_ask(harness, READER), harness.sink())
        history = await service.history(
            session_id=harness.session_id, tenant_id=TENANT, principal_id=READER
        )
        return repr([m.model_dump() for m in history])

    dumped = _run(scenario)

    assert "fourteenth" not in dumped
    assert REFUSAL in dumped


# --- the session belongs to somebody -----------------------------------------


def test_a_neighbour_cannot_ask_into_someone_elses_session() -> None:
    """The session id is not the credential."""

    async def scenario(harness: _Harness) -> None:
        await harness.publish(granted=(READER, "user_neighbour"))
        await harness.open_session(READER)
        await harness.chat().ask(_ask(harness, "user_neighbour"), harness.sink())

    with pytest.raises(NotFoundError):
        _run(scenario)


class _RevokingChat(ChatService):
    """A chat service that revokes the grant while the model is answering.

    Subclassed rather than hooked into production code: the window being
    defended is between the run and the second check, and overriding the run is
    the smallest way to stand inside it.
    """

    def __init__(self, harness: _Harness) -> None:
        base = harness.chat()
        super().__init__(
            retrieval=base.retrieval,
            executor=_RevokingExecutor(base.executor, harness),
            conversations=base.conversations,
            budget=base.budget,
        )


class _RevokingExecutor:
    def __init__(self, inner: Any, harness: _Harness) -> None:
        self._inner = inner
        self._harness = harness

    async def run(self, request: Any, sink: Any, cancellation: Any) -> Any:
        outcome = await self._inner.run(request, sink, cancellation)
        await self._harness.revoke()
        return outcome
