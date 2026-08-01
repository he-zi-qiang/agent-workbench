"""One chat turn, end to end, against real Qdrant and real PostgreSQL.

The turn is fixed two-step: retrieve once, answer from what came back. What is
worth testing is not that a scripted model produces a scripted answer -- it is
the authorization check before retrieval and the revision-locked publication
fence after generation, plus what the session holds when that fence refuses.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast

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
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.adapters.vector import QdrantVectorIndex
from agent_workbench.application.chat import REFUSAL, ChatRequest, ChatService
from agent_workbench.application.chat_execution import FixedTwoStepExecution
from agent_workbench.application.chunking import Chunker
from agent_workbench.application.ingestion import IngestionRequest, IngestionService
from agent_workbench.application.retrieval import RetrievalService
from agent_workbench.apps.api.routes.events import _frame
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import RunBudget, TokenUsage
from agent_workbench.ports.chat_release import ChatReleaseCoordinator
from agent_workbench.ports.conversation_store import StoredChatTurn
from agent_workbench.ports.event_log import EventScope, EventSink
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
        self.log = PostgresEventLog(engine)
        self.session_id = f"ses_{uuid.uuid4().hex}"
        self.ingestion = IngestionService(
            parser=TextDocumentParser(),
            chunker=Chunker(
                size_tokens=8, overlap_tokens=2, counter=ApproximateTokenCounter()
            ),
            embedder=self.embedder,
            index=index,
        )

    def chat(
        self,
        *,
        answer: str = ANSWER,
        releaser: ChatReleaseCoordinator | None = None,
    ) -> ChatService:
        registry = StaticToolRegistry([])
        return ChatService(
            execution=FixedTwoStepExecution(
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
                        registry=registry,
                        policy=EnvelopePolicyEngine(registry=registry),
                    ),
                    policy_identity="test-policy",
                ),
                budget=RunBudget(max_steps=1, max_tool_calls=1),
            ),
            conversations=self.conversations,
            releaser=(
                PostgresChatReleaseCoordinator(self.engine)
                if releaser is None
                else releaser
            ),
            request_timeout_seconds=30,
            orphan_grace_seconds=5,
        )

    def sink(self, request: ChatRequest) -> ScopedEventSink:
        return ScopedEventSink(
            log=self.log,
            scope=EventScope(
                stream_id=self.session_id,
                run_id=request.run_id,
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

    async def revoke(
        self,
        *,
        attempting: asyncio.Event | None = None,
        acquired: asyncio.Event | None = None,
        backend_pid: asyncio.Future[int] | None = None,
    ) -> None:
        async with self.engine.begin() as connection:
            pid = (
                await connection.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            if backend_pid is not None and not backend_pid.done():
                backend_pid.set_result(pid)
            if attempting is not None:
                attempting.set()
            await connection.execute(
                text(
                    "SELECT document_id FROM documents "
                    "WHERE document_id = 'doc_1' FOR UPDATE"
                )
            )
            if acquired is not None:
                acquired.set()
            await connection.execute(
                text("DELETE FROM document_acl WHERE document_id = 'doc_1'")
            )
            await connection.execute(
                text(
                    "UPDATE documents SET source_revision = source_revision + 1 "
                    "WHERE document_id = 'doc_1'"
                )
            )

    async def wait_until_blocked(self, backend_pid: int) -> None:
        """Wait until PostgreSQL reports the concurrent writer's lock wait."""

        for _ in range(200):
            async with self.engine.connect() as connection:
                blockers = (
                    await connection.execute(
                        text("SELECT pg_blocking_pids(:pid)"),
                        {"pid": backend_pid},
                    )
                ).scalar_one()
            if blockers:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the concurrent ACL revoke never blocked on the fence")


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


def _ask(harness: _Harness, principal: str, *, key: str | None = None) -> ChatRequest:
    return ChatRequest(
        session_id=harness.session_id,
        question="when does the acquisition close",
        principal=PrincipalContext(principal_id=principal, tenant_id=TENANT),
        knowledge_base_id=KB,
        idempotency_key=f"request-{principal}" if key is None else f"request-{key}",
    )


# --- the ordinary turn -------------------------------------------------------


def test_a_turn_offers_only_the_citations_its_answer_actually_named() -> None:
    """Three answers over the same evidence, and three different source lists.

    The first cites a chunk it was shown and gets it back. The second cites
    nothing and gets nothing -- which is the behaviour change: citations used to
    be whatever retrieval found, so an answer that ignored every passage still
    shipped with a full set of sources beneath it. The third names an id that
    was never retrieved, and it is dropped rather than echoed: a guessed
    identifier returned as a source would carry this system's authority for a
    passage nobody has.

    All three are fenced identically -- the release check covers what the model
    was *shown*, not what it chose to mention.
    """

    async def scenario(harness: _Harness) -> tuple[Any, ...]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)

        # Learn a real chunk id the way the model does: from its own prompt.
        probe = harness.chat()
        first = _ask(harness, READER)
        await probe.ask(first, harness.sink(first))
        model = probe.execution.executor._model
        assert isinstance(model, FakeModel)
        prompt = model.requests[0].messages[0].content[0].text
        chunk_id = prompt.split("[", 1)[1].split("]", 1)[0]

        async def answered(text: str, key: str) -> Any:
            request = _ask(harness, READER, key=key)
            return await harness.chat(answer=text).ask(request, harness.sink(request))

        cited = await answered(f"{ANSWER} [{chunk_id}]", "cited")
        silent = await answered(ANSWER, "silent")
        invented = await answered(f"{ANSWER} [chunk_nowhere]", "invented")
        return (
            tuple(c.chunk_id for c in cited.citations),
            chunk_id,
            tuple(c.chunk_id for c in silent.citations),
            tuple(c.chunk_id for c in invented.citations),
            (cited.withheld, silent.withheld, invented.withheld),
        )

    cited, chunk_id, silent, invented, withheld = _run(scenario)

    assert cited == (chunk_id,)
    assert silent == ()
    assert invented == ()
    # None of them is withheld: an unnamed or invented citation is a claim about
    # sources, not a permission failure.
    assert withheld == (False, False, False)


def test_the_evidence_reaches_the_model_labelled_by_chunk_id() -> None:
    """A citation is checkable only against what the model was actually shown."""

    async def scenario(harness: _Harness) -> tuple[str, tuple[str, ...]]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        service = harness.chat()
        request = _ask(harness, READER)
        turn = await service.ask(request, harness.sink(request))
        model = service.execution.executor._model
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
        request = _ask(harness, READER)
        await service.ask(request, harness.sink(request))
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
        request = _ask(harness, READER)
        turn = await harness.chat().ask(request, harness.sink(request))
        return len(turn.citations), turn.withheld

    assert _run(scenario) == (0, False)


# --- withheld ----------------------------------------------------------------


def test_an_answer_is_withheld_when_a_source_is_revoked_mid_turn() -> None:
    """The model has already written it. It still does not get delivered."""

    async def scenario(harness: _Harness) -> tuple[str, bool, int]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        service = _RevokingChat(harness)
        request = _ask(harness, READER)
        turn = await service.ask(request, harness.sink(request))
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
        request = _ask(harness, READER)
        await service.ask(request, harness.sink(request))
        history = await service.history(
            session_id=harness.session_id, tenant_id=TENANT, principal_id=READER
        )
        return repr([m.model_dump() for m in history])

    dumped = _run(scenario)

    assert "fourteenth" not in dumped
    assert REFUSAL in dumped


def test_the_withheld_answer_does_not_enter_the_event_log_or_sse() -> None:
    """Every public exit is downstream of the same answer release gate."""

    async def scenario(harness: _Harness) -> tuple[str, list[str]]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        service = _RevokingChat(harness)
        request = _ask(harness, READER)
        sink = harness.sink(request)
        await service.ask(request, sink)
        events = await harness.log.read(sink.scope.stream_id)
        frames = "".join(
            _frame(event, sink.scope.stream_id, event.sequence)
            for event in events
            if event.sequence is not None
        )
        return frames, [event.event_type for event in events]

    frames, event_types = _run(scenario)

    assert ANSWER not in frames
    assert SECRET not in frames
    assert "AnswerCommitted" not in event_types
    assert event_types[-1] == "AnswerWithheld"
    assert REFUSAL in frames


def test_release_pending_retry_rechecks_revisions_and_scrubs_the_candidate() -> None:
    """A crash after prepare cannot turn a later revoke into a stale release."""

    async def scenario(harness: _Harness) -> tuple[str, bool, str, list[str]]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        request = _ask(harness, READER)
        interrupted = _FailBeforeAtomicRelease()

        with pytest.raises(_ReleaseInterrupted):
            await harness.chat(releaser=interrupted).ask(
                request,
                harness.sink(request),
            )
        assert interrupted.called is True
        async with harness.engine.connect() as connection:
            prepared_status = (
                await connection.execute(
                    text(
                        "SELECT status FROM chat_turns WHERE session_id = :session_id"
                    ),
                    {"session_id": harness.session_id},
                )
            ).scalar_one()
        assert prepared_status == "release_pending"

        # The Turn is now release_pending. Move the evidence and ACL before
        # retrying the same idempotency key; the retry must authorize the
        # stored revisions again, not trust the old prepared candidate.
        await harness.revoke()
        service = harness.chat()
        turn = await service.ask(request, harness.sink(request))
        history = await service.history(
            session_id=harness.session_id,
            tenant_id=TENANT,
            principal_id=READER,
        )
        events = await harness.log.read(harness.session_id, limit=1000)
        async with harness.engine.connect() as connection:
            persisted_result = (
                await connection.execute(
                    text(
                        "SELECT result FROM chat_turns WHERE session_id = :session_id"
                    ),
                    {"session_id": harness.session_id},
                )
            ).scalar_one()

        public_and_persisted = "\n".join(
            (
                repr(
                    {
                        "answer": turn.answer,
                        "citations": turn.citations,
                        "outcome": turn.outcome,
                        "withheld": turn.withheld,
                    }
                ),
                repr([message.model_dump() for message in history]),
                repr([event.model_dump() for event in events]),
                repr(cast(object, persisted_result)),
            )
        )
        return (
            turn.answer,
            turn.withheld,
            public_and_persisted,
            [event.event_type for event in events],
        )

    answer, withheld, dumped, event_types = _run(scenario)

    assert answer == REFUSAL
    assert withheld is True
    assert ANSWER not in dumped
    assert SECRET not in dumped
    assert "AnswerCommitted" not in event_types
    assert event_types[-1] == "AnswerWithheld"


def test_document_lock_linearizes_release_before_a_concurrent_revoke() -> None:
    """A revoke waits while the fenced answer transaction holds its source."""

    async def scenario(
        harness: _Harness,
    ) -> tuple[str, bool, bool, int, int, list[str]]:
        await harness.publish(granted=(READER,))
        await harness.open_session(READER)
        request = _ask(harness, READER)
        barrier = _BarrierChatRelease(harness.engine)
        chat_task = asyncio.create_task(
            harness.chat(releaser=barrier).ask(
                request,
                harness.sink(request),
            )
        )
        revoke_task: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(barrier.authorization_locked.wait(), timeout=10)

            attempting = asyncio.Event()
            acquired = asyncio.Event()
            backend_pid = asyncio.get_running_loop().create_future()
            revoke_task = asyncio.create_task(
                harness.revoke(
                    attempting=attempting,
                    acquired=acquired,
                    backend_pid=backend_pid,
                )
            )
            pid = await asyncio.wait_for(backend_pid, timeout=10)
            await asyncio.wait_for(attempting.wait(), timeout=10)
            await harness.wait_until_blocked(pid)
            blocked_before_release = not acquired.is_set()

            barrier.continue_release.set()
            turn = await asyncio.wait_for(chat_task, timeout=10)
            await asyncio.wait_for(revoke_task, timeout=10)

            async with harness.engine.connect() as connection:
                revision = (
                    await connection.execute(
                        text(
                            "SELECT source_revision FROM documents "
                            "WHERE document_id = 'doc_1'"
                        )
                    )
                ).scalar_one()
                grant_count = (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM document_acl "
                            "WHERE document_id = 'doc_1' "
                            "AND principal_id = :principal_id"
                        ),
                        {"principal_id": READER},
                    )
                ).scalar_one()
            events = await harness.log.read(harness.session_id, limit=1000)
            return (
                turn.answer,
                turn.withheld,
                blocked_before_release,
                revision,
                grant_count,
                [event.event_type for event in events],
            )
        finally:
            barrier.continue_release.set()
            if not chat_task.done():
                chat_task.cancel()
            if revoke_task is not None and not revoke_task.done():
                revoke_task.cancel()
            await asyncio.gather(
                chat_task,
                *(() if revoke_task is None else (revoke_task,)),
                return_exceptions=True,
            )

    answer, withheld, blocked, revision, grants, event_types = _run(scenario)

    assert blocked is True
    assert answer == ANSWER
    assert withheld is False
    assert revision == 2
    assert grants == 0
    assert "AnswerCommitted" in event_types
    assert "AnswerWithheld" not in event_types


# --- the session belongs to somebody -----------------------------------------


def test_a_neighbour_cannot_ask_into_someone_elses_session() -> None:
    """The session id is not the credential."""

    async def scenario(harness: _Harness) -> None:
        await harness.publish(granted=(READER, "user_neighbour"))
        await harness.open_session(READER)
        request = _ask(harness, "user_neighbour")
        await harness.chat().ask(request, harness.sink(request))

    with pytest.raises(NotFoundError):
        _run(scenario)


class _RevokingChat(ChatService):
    """A chat service that revokes the grant while the model is answering.

    Subclassed rather than hooked into production code: the revoke lands after
    generation and before the atomic release fence, and overriding the run is
    the smallest way to stand inside that window.
    """

    def __init__(self, harness: _Harness) -> None:
        base = harness.chat()
        super().__init__(
            execution=FixedTwoStepExecution(
                retrieval=base.execution.retrieval,
                executor=_RevokingExecutor(base.execution.executor, harness),
                budget=base.execution.budget,
            ),
            conversations=base.conversations,
            releaser=base.releaser,
            request_timeout_seconds=base.request_timeout_seconds,
            orphan_grace_seconds=base.orphan_grace_seconds,
        )


class _RevokingExecutor:
    def __init__(self, inner: Any, harness: _Harness) -> None:
        self._inner = inner
        self._harness = harness

    async def run(self, request: Any, sink: Any, cancellation: Any) -> Any:
        outcome = await self._inner.run(request, sink, cancellation)
        await self._harness.revoke()
        return outcome


class _ReleaseInterrupted(RuntimeError):
    """The process stopped after prepare and before entering publication."""


class _FailBeforeAtomicRelease:
    def __init__(self) -> None:
        self.called = False

    async def release(
        self,
        *,
        turn: StoredChatTurn,
        tenant_id: str,
        principal_id: str,
        stream_id: str,
        run_id: str,
        refusal_text: str,
        sink: EventSink,
    ) -> StoredChatTurn:
        del turn, tenant_id, principal_id, stream_id, run_id, refusal_text, sink
        self.called = True
        raise _ReleaseInterrupted


class _BarrierChatRelease(PostgresChatReleaseCoordinator):
    """Hold the source locks so the test can observe PostgreSQL's wait graph."""

    def __init__(self, engine: Any) -> None:
        super().__init__(engine)
        self.authorization_locked = asyncio.Event()
        self.continue_release = asyncio.Event()

    async def _after_authorization_locked(self) -> None:
        self.authorization_locked.set()
        await self.continue_release.wait()
