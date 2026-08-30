"""The final authorization check, expressed as an event publication boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import (
    InMemoryChatReleaseCoordinator,
    InMemoryConversationStore,
    InMemoryEventLog,
)
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.application.answer_release import AnswerReleaseSink
from agent_workbench.application.chat import (
    ChatExecutionError,
    ChatRequest,
    ChatService,
)
from agent_workbench.application.chat_execution import FixedTwoStepExecution
from agent_workbench.application.retrieval import AuthorizedContext
from agent_workbench.domain.context import (
    Citation,
    ContextChunk,
    ContextPacket,
)
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.events import (
    AnswerCommitted,
    ModelCompleted,
    ModelDelta,
    ModelThinkingDelta,
)
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import RunBudget
from agent_workbench.domain.schema import BOUNDED_TEXT_LIMIT
from agent_workbench.ports.conversation_store import (
    ChatTurnConflictError,
    ChatTurnResult,
    StoredChatTurn,
)
from agent_workbench.ports.event_log import EventScope
from agent_workbench.runtime import ClaudeLikeAgentRuntime, ToolGateway

SECRET = "the answer must not be public before its evidence is checked"
SAFE_REFUSAL = "The answer was withheld."
SCOPE = EventScope(stream_id="ses_1", run_id="run_1")


def _sink(log: InMemoryEventLog) -> ScopedEventSink:
    return ScopedEventSink(log=log, scope=SCOPE)


def test_pre_commit_model_events_are_redacted_and_a_refusal_is_safe() -> None:
    async def scenario() -> tuple[Any, Any, tuple[Any, ...]]:
        log = InMemoryEventLog()
        release = AnswerReleaseSink(_sink(log))
        delta = await release.emit(ModelDelta(model_call_id="mc_1", text=SECRET))
        completed = await release.emit(
            ModelCompleted(
                model_call_id="mc_1",
                finish_reason="stop",
                text=SECRET,
            )
        )
        await release.withhold(text=SAFE_REFUSAL)
        return delta, completed, await log.read(SCOPE.stream_id)

    delta, completed, replayed = asyncio.run(scenario())
    serialized = "\n".join(event.model_dump_json() for event in replayed)

    assert delta.payload.text == ""
    assert completed.payload.text == ""
    assert SECRET not in serialized
    assert [event.event_type for event in replayed] == [
        "ModelCompleted",
        "AnswerWithheld",
    ]
    assert SAFE_REFUSAL in serialized


def test_a_checked_answer_is_published_once_after_model_completion() -> None:
    citation = Citation(
        chunk_id="chk_1",
        document_id="doc_1",
        document_version="ver_1",
    )

    async def scenario() -> tuple[Any, ...]:
        log = InMemoryEventLog()
        release = AnswerReleaseSink(_sink(log))
        await release.emit(
            ModelCompleted(
                model_call_id="mc_1",
                finish_reason="stop",
                text=SECRET,
            )
        )
        await release.commit(text=SECRET, citations=(citation,))
        return await log.read(SCOPE.stream_id)

    replayed = asyncio.run(scenario())

    assert [event.event_type for event in replayed] == [
        "ModelCompleted",
        "AnswerCommitted",
    ]
    assert replayed[0].payload.text == ""
    assert replayed[1].payload.text == SECRET
    assert replayed[1].payload.citations == (citation,)


def test_an_answer_release_sink_cannot_publish_both_outcomes() -> None:
    async def scenario() -> None:
        release = AnswerReleaseSink(_sink(InMemoryEventLog()))
        await release.withhold(text=SAFE_REFUSAL)
        await release.commit(text=SECRET, citations=())

    with pytest.raises(RuntimeError, match="only once"):
        asyncio.run(scenario())


def test_the_generic_emit_method_cannot_bypass_the_release_gate() -> None:
    async def scenario() -> None:
        release = AnswerReleaseSink(_sink(InMemoryEventLog()))
        await release.emit(AnswerCommitted(text=SECRET))

    with pytest.raises(RuntimeError, match=r"must pass through"):
        asyncio.run(scenario())


class _EmptyRetrieval:
    confirmed = False
    retrieve_calls = 0

    async def retrieve(self, request: Any) -> AuthorizedContext:
        self.retrieve_calls += 1
        return AuthorizedContext(packet=ContextPacket(), authorized_revisions=())

    async def confirm_unchanged(self, context: Any, **kwargs: Any) -> None:
        self.confirmed = True

    async def revisions_unchanged(
        self,
        revisions: Any,
        **kwargs: Any,
    ) -> bool:
        self.confirmed = True
        return True


class _ChangingRetrieval(_EmptyRetrieval):
    async def confirm_unchanged(self, context: Any, **kwargs: Any) -> None:
        from agent_workbench.application.retrieval import SourcesChangedError

        raise SourcesChangedError("changed at the release barrier")

    async def revisions_unchanged(
        self,
        revisions: Any,
        **kwargs: Any,
    ) -> bool:
        return False


def _chat(
    retrieval: Any,
    conversations: InMemoryConversationStore,
    turns: list[ScriptedTurn],
    *,
    releaser: Any | None = None,
) -> ChatService:
    registry = StaticToolRegistry([])
    return ChatService(
        execution=FixedTwoStepExecution(
            retrieval=retrieval,
            executor=ClaudeLikeAgentRuntime(
                model=FakeModel(turns),
                gateway=ToolGateway(
                    registry=registry,
                    policy=EnvelopePolicyEngine(registry=registry),
                ),
                policy_identity="test-policy",
            ),
            budget=RunBudget(max_steps=1, max_tool_calls=1),
        ),
        conversations=conversations,
        releaser=(
            releaser
            if releaser is not None
            else InMemoryChatReleaseCoordinator(
                conversations=conversations,
                revisions=retrieval,
            )
        ),
        request_timeout_seconds=30,
        orphan_grace_seconds=5,
    )


async def _conversations() -> InMemoryConversationStore:
    conversations = InMemoryConversationStore()
    await conversations.create_session(
        session_id="ses_1",
        tenant_id="tenant_a",
        owner_id="user_1",
    )
    return conversations


def _request() -> ChatRequest:
    return ChatRequest(
        session_id="ses_1",
        question="private question",
        principal=PrincipalContext(
            principal_id="user_1",
            tenant_id="tenant_a",
        ),
        knowledge_base_id="kb_main",
        idempotency_key="request-1",
        run_id="run_1",
        stream_id="ses_1",
    )


def test_chat_publishes_a_success_only_after_the_release_check() -> None:
    async def scenario() -> tuple[list[str], tuple[Any, ...], bool, str]:
        conversations = await _conversations()
        retrieval = _EmptyRetrieval()
        service = _chat(retrieval, conversations, [ScriptedTurn(text=SECRET)])
        log = InMemoryEventLog()

        turn = await service.ask(_request(), _sink(log))

        history = await service.history(
            session_id="ses_1",
            tenant_id="tenant_a",
            principal_id="user_1",
        )
        return (
            [message.message.role for message in history],
            await log.read(SCOPE.stream_id),
            retrieval.confirmed,
            turn.outcome.agent_run_id,
        )

    roles, events, confirmed, run_id = asyncio.run(scenario())
    serialized_before_commit = "\n".join(
        event.model_dump_json() for event in events[:-1]
    )

    assert confirmed is True
    assert run_id == "run_1"
    assert {event.run_id for event in events} == {"run_1"}
    assert roles == ["user", "assistant"]
    assert events[-1].event_type == "AnswerCommitted"
    assert SECRET not in serialized_before_commit
    assert events[-1].payload.text == SECRET
    assert [event.event_type for event in events].index("RunCompleted") < len(
        events
    ) - 1


def test_a_long_answer_is_published_whole_rather_than_cut_to_a_preview() -> None:
    """The asker gets the answer the provider wrote (ADR-035 §3.2).

    This boundary is where the answer becomes visible, and the turn record
    requires it to equal what the run produced -- so a ceiling here is a
    ceiling on the answer itself, not on how much of it is displayed.
    """

    long_answer = "Paragraph about hybrid retrieval. " * 400

    async def scenario() -> tuple[tuple[Any, ...], str]:
        conversations = await _conversations()
        service = _chat(
            _EmptyRetrieval(), conversations, [ScriptedTurn(text=long_answer)]
        )
        log = InMemoryEventLog()
        turn = await service.ask(_request(), _sink(log))
        return await log.read(SCOPE.stream_id), turn.answer

    events, answer = asyncio.run(scenario())

    assert len(long_answer) > BOUNDED_TEXT_LIMIT
    assert events[-1].event_type == "AnswerCommitted"
    assert events[-1].payload.text == long_answer
    assert answer == long_answer


def test_a_completed_request_retry_returns_the_original_turn_without_rerunning() -> (
    None
):
    async def scenario() -> tuple[tuple[bool, int, int, int], int]:
        conversations = await _conversations()
        retrieval = _EmptyRetrieval()
        service = _chat(retrieval, conversations, [ScriptedTurn(text=SECRET)])
        log = InMemoryEventLog()

        first = await service.ask(_request(), _sink(log))
        repeated = await service.ask(_request(), _sink(log))
        history = await service.history(
            session_id="ses_1",
            tenant_id="tenant_a",
            principal_id="user_1",
        )
        model = service.execution.executor._model
        assert isinstance(model, FakeModel)
        events = await log.read(SCOPE.stream_id)
        return (
            repeated == first,
            retrieval.retrieve_calls,
            len(model.requests),
            sum(event.event_type == "AnswerCommitted" for event in events),
        ), len(history)

    counters, history_length = asyncio.run(scenario())

    assert counters == (True, 1, 1, 1)
    assert history_length == 2


def test_reusing_a_request_key_for_different_content_fails_before_retrieval() -> None:
    async def scenario() -> int:
        conversations = await _conversations()
        retrieval = _EmptyRetrieval()
        service = _chat(retrieval, conversations, [ScriptedTurn(text=SECRET)])

        await service.ask(_request(), _sink(InMemoryEventLog()))
        with pytest.raises(ChatTurnConflictError, match="idempotency conflict"):
            await service.ask(
                replace(_request(), question="different question"),
                _sink(InMemoryEventLog()),
            )
        return retrieval.retrieve_calls

    assert asyncio.run(scenario()) == 1


def test_chat_publishes_only_a_safe_refusal_when_sources_change() -> None:
    async def scenario() -> tuple[list[str], tuple[Any, ...], str]:
        conversations = await _conversations()
        service = _chat(
            _ChangingRetrieval(),
            conversations,
            [ScriptedTurn(text=SECRET)],
        )
        log = InMemoryEventLog()

        turn = await service.ask(_request(), _sink(log))
        assert turn.withheld is True
        history = await service.history(
            session_id="ses_1",
            tenant_id="tenant_a",
            principal_id="user_1",
        )
        return (
            [message.message.role for message in history],
            await log.read(SCOPE.stream_id),
            turn.outcome.output_text,
        )

    roles, events, retained_output = asyncio.run(scenario())
    serialized = "\n".join(event.model_dump_json() for event in events)

    assert roles == ["user", "assistant"]
    assert SECRET not in serialized
    assert retained_output == ""
    assert events[-1].event_type == "AnswerWithheld"
    assert SAFE_REFUSAL not in serialized
    assert "no longer able to read" in serialized


class _FailFirstReleaseTransitionStore(InMemoryConversationStore):
    """Inject the crash window after event publication, before DB visibility."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_release = True

    async def mark_released(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        withheld_result: ChatTurnResult | None = None,
    ) -> StoredChatTurn:
        if self.fail_next_release:
            self.fail_next_release = False
            raise RuntimeError("injected failure after answer publication")
        return await super().mark_released(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            turn_id=turn_id,
            withheld_result=withheld_result,
        )


def test_retry_heals_a_crash_after_answer_publication_without_duplication() -> None:
    async def scenario() -> tuple[int, int, list[str], str]:
        conversations = _FailFirstReleaseTransitionStore()
        await conversations.create_session(
            session_id="ses_1",
            tenant_id="tenant_a",
            owner_id="user_1",
        )
        service = _chat(
            _EmptyRetrieval(),
            conversations,
            [ScriptedTurn(text=SECRET)],
        )
        log = InMemoryEventLog()

        with pytest.raises(RuntimeError, match="injected failure"):
            await service.ask(_request(), _sink(log))
        history_before_retry = await service.history(
            session_id="ses_1",
            tenant_id="tenant_a",
            principal_id="user_1",
        )

        recovered = await service.ask(_request(), _sink(log))
        history = await service.history(
            session_id="ses_1",
            tenant_id="tenant_a",
            principal_id="user_1",
        )
        events = await log.read(SCOPE.stream_id)
        return (
            len(history_before_retry),
            sum(event.event_type == "AnswerCommitted" for event in events),
            [message.message.role for message in history],
            recovered.answer,
        )

    hidden_count, committed_events, roles, answer = asyncio.run(scenario())

    assert hidden_count == 1
    assert committed_events == 1
    assert roles == ["user", "assistant"]
    assert answer == SECRET


class _MutableRevisionRetrieval(_EmptyRetrieval):
    def __init__(self) -> None:
        self.allowed = True
        self.confirmed = False
        self.retrieve_calls = 0

    async def retrieve(self, request: Any) -> AuthorizedContext:
        self.retrieve_calls += 1
        citation = Citation(
            chunk_id="chunk_1",
            document_id="doc_1",
            document_version="version_1",
        )
        return AuthorizedContext(
            packet=ContextPacket(
                chunks=(
                    ContextChunk(
                        chunk_id="chunk_1",
                        document_id="doc_1",
                        document_version="version_1",
                        tenant_id="tenant_a",
                        text="authorized evidence",
                    ),
                ),
                citations=(citation,),
            ),
            authorized_revisions=(("doc_1", 1),),
        )

    async def revisions_unchanged(
        self,
        revisions: Any,
        **kwargs: Any,
    ) -> bool:
        self.confirmed = True
        return self.allowed


class _FailBeforePublicationCoordinator:
    def __init__(self, inner: InMemoryChatReleaseCoordinator) -> None:
        self.inner = inner
        self.fail_next_release = True

    async def release(self, **kwargs: Any) -> StoredChatTurn:
        if self.fail_next_release:
            self.fail_next_release = False
            raise RuntimeError("injected failure before answer publication")
        return await self.inner.release(**kwargs)


def test_retry_reauthorizes_a_pending_candidate_before_publication() -> None:
    async def scenario() -> tuple[str, str, list[str], list[str]]:
        conversations = await _conversations()
        retrieval = _MutableRevisionRetrieval()
        releaser = _FailBeforePublicationCoordinator(
            InMemoryChatReleaseCoordinator(
                conversations=conversations,
                revisions=retrieval,
            )
        )
        service = _chat(
            retrieval,
            conversations,
            [ScriptedTurn(text=SECRET)],
            releaser=releaser,
        )
        log = InMemoryEventLog()

        with pytest.raises(RuntimeError, match="before answer publication"):
            await service.ask(_request(), _sink(log))
        retrieval.allowed = False

        recovered = await service.ask(_request(), _sink(log))
        history = await service.history(
            session_id="ses_1",
            tenant_id="tenant_a",
            principal_id="user_1",
        )
        events = await log.read(SCOPE.stream_id)
        return (
            recovered.answer,
            recovered.outcome.output_text,
            [message.message.text() for message in history],
            [event.event_type for event in events],
        )

    answer, retained_output, history, event_types = asyncio.run(scenario())

    assert "no longer able to read" in answer
    assert retained_output == ""
    assert SECRET not in "\n".join(history)
    assert event_types.count("AnswerWithheld") == 1
    assert "AnswerCommitted" not in event_types


def test_a_failed_model_run_is_not_saved_or_published_as_an_answer() -> None:
    async def scenario() -> tuple[list[str], str]:
        conversations = await _conversations()
        service = _chat(
            _EmptyRetrieval(),
            conversations,
            [
                ScriptedTurn(
                    text=SECRET,
                    error=ErrorInfo(
                        code="provider_error",
                        message="scripted provider failure",
                    ),
                ),
            ],
        )
        log = InMemoryEventLog()

        with pytest.raises(ChatExecutionError):
            await service.ask(
                _request(),
                _sink(log),
            )

        history = await service.history(
            session_id="ses_1",
            tenant_id="tenant_a",
            principal_id="user_1",
        )
        replayed = await log.read(SCOPE.stream_id)
        return [message.message.role for message in history], "\n".join(
            event.model_dump_json() for event in replayed
        )

    roles, serialized = asyncio.run(scenario())

    assert roles == ["user"]
    assert SECRET not in serialized
    assert "AnswerCommitted" not in serialized
    assert "AnswerWithheld" not in serialized
    assert "RunFailed" in serialized


def test_an_unauthorized_session_is_rejected_before_retrieval() -> None:
    from agent_workbench.domain.errors import NotFoundError

    async def scenario() -> int:
        conversations = await _conversations()
        retrieval = _EmptyRetrieval()
        service = _chat(retrieval, conversations, [ScriptedTurn(text=SECRET)])

        with pytest.raises(NotFoundError):
            await service.ask(
                ChatRequest(
                    session_id="ses_1",
                    question="make the owner pay for retrieval",
                    principal=PrincipalContext(
                        principal_id="user_neighbour",
                        tenant_id="tenant_a",
                    ),
                    knowledge_base_id="kb_main",
                    idempotency_key="unauthorized-request",
                ),
                _sink(InMemoryEventLog()),
            )
        return retrieval.retrieve_calls

    assert asyncio.run(scenario()) == 0


def test_a_later_turn_replays_only_committed_conversation_history() -> None:
    """Multi-turn context must not persist or replay an earlier RAG packet."""

    async def scenario() -> tuple[list[str], list[str]]:
        conversations = await _conversations()
        retrieval = _EmptyRetrieval()
        service = _chat(
            retrieval,
            conversations,
            [
                ScriptedTurn(text="first committed answer"),
                ScriptedTurn(text="second committed answer"),
            ],
        )

        await service.ask(_request(), _sink(InMemoryEventLog()))
        await service.ask(
            ChatRequest(
                session_id="ses_1",
                question="follow-up question",
                principal=PrincipalContext(
                    principal_id="user_1",
                    tenant_id="tenant_a",
                ),
                knowledge_base_id="kb_main",
                idempotency_key="request-2",
                run_id="run_2",
                stream_id="ses_1",
            ),
            ScopedEventSink(
                log=InMemoryEventLog(),
                scope=EventScope(stream_id="ses_1", run_id="run_2"),
            ),
        )

        model = service.execution.executor._model
        assert isinstance(model, FakeModel)
        second = model.requests[1]
        return (
            [message.role for message in second.messages],
            [message.text() for message in second.messages],
        )

    roles, texts = asyncio.run(scenario())

    assert roles == ["user", "assistant", "user"]
    assert texts[0] == "private question"
    assert texts[1] == "first committed answer"
    assert texts[2].startswith("follow-up question")
    assert texts[2].count("follow-up question") == 1


def test_a_withheld_candidate_is_never_replayed_into_the_next_turn() -> None:
    """Only the safe refusal crosses from a revoked turn into later context."""

    async def scenario() -> str:
        conversations = await _conversations()
        service = _chat(
            _ChangingRetrieval(),
            conversations,
            [
                ScriptedTurn(text=SECRET),
                ScriptedTurn(text="safe answer after retry"),
            ],
        )

        await service.ask(_request(), _sink(InMemoryEventLog()))
        service = _chat(
            _EmptyRetrieval(),
            conversations,
            [ScriptedTurn(text="safe answer after retry")],
        )
        await service.ask(
            ChatRequest(
                session_id="ses_1",
                question="try again",
                principal=PrincipalContext(
                    principal_id="user_1",
                    tenant_id="tenant_a",
                ),
                knowledge_base_id="kb_main",
                idempotency_key="request-2",
                run_id="run_2",
                stream_id="ses_1",
            ),
            ScopedEventSink(
                log=InMemoryEventLog(),
                scope=EventScope(stream_id="ses_1", run_id="run_2"),
            ),
        )

        model = service.execution.executor._model
        assert isinstance(model, FakeModel)
        return "\n".join(
            message.model_dump_json() for message in model.requests[0].messages
        )

    replayed = asyncio.run(scenario())

    assert SECRET not in replayed
    assert "no longer able to read" in replayed


def test_an_ungrounded_answer_is_not_an_answer_with_no_citations() -> None:
    """The two are different facts, and the log has to keep them apart.

    ``commit(citations=())`` says a retrieval turn cited nothing it was shown.
    ``commit_ungrounded()`` says nothing was retrieved at all. Collapsing them
    would leave an auditor unable to tell a verified answer from an unverified
    one, which is the whole reason ADR-018 gave this path its own event.
    """

    async def scenario() -> tuple[str, str]:
        grounded = AnswerReleaseSink(_sink(InMemoryEventLog()))
        ungrounded = AnswerReleaseSink(_sink(InMemoryEventLog()))
        a = await grounded.commit(text=SECRET, citations=())
        b = await ungrounded.commit_ungrounded(text=SECRET)
        return a.payload.kind, b.payload.kind

    grounded_kind, ungrounded_kind = asyncio.run(scenario())

    assert grounded_kind == "AnswerCommitted"
    assert ungrounded_kind == "UngroundedAnswerCommitted"


def test_an_ungrounded_answer_still_releases_only_once() -> None:
    """The single-release rule is the turn's, not any one shape's."""

    async def scenario() -> None:
        release = AnswerReleaseSink(_sink(InMemoryEventLog()))
        await release.commit_ungrounded(text=SECRET)
        await release.commit(text=SECRET, citations=())

    with pytest.raises(RuntimeError, match="only once"):
        asyncio.run(scenario())


# --- which shapes may show their text while it is being written ---------------


def test_a_provisional_sink_passes_deltas_through_and_still_redacts_the_answer() -> (
    None
):
    """Two different things, and only one of them is loosened.

    A delta is what is being written; ``ModelCompleted.text`` is the finished
    candidate. Publishing the candidate is what the commit methods are for, so
    it stays redacted under either policy -- otherwise a provisional shape
    would put an answer into the durable log that nothing decided to publish.
    """

    async def scenario() -> tuple[Any, Any]:
        log = InMemoryEventLog()
        release = AnswerReleaseSink(_sink(log), live_text="provisional")
        delta = await release.emit(ModelDelta(model_call_id="mc_1", text=SECRET))
        completed = await release.emit(
            ModelCompleted(
                model_call_id="mc_1",
                finish_reason="stop",
                text=SECRET,
            )
        )
        return delta.payload, completed.payload

    delta, completed = asyncio.run(scenario())

    assert delta.text == SECRET
    assert completed.text == ""
    assert completed.output_ref is None


def test_reasoning_is_fenced_exactly_like_the_answer_it_precedes() -> None:
    """Thinking is not a third channel out (ADR-061).

    The model reasons *about* the evidence it was shown, so a redacted shape
    -- one that may still end in ``AnswerWithheld`` -- must not stream the
    reasoning either: it can quote the very passages the withheld answer was
    refused for. The durable excerpt on ``ModelCompleted`` is blanked with the
    candidate text for the same reason.
    """

    async def scenario() -> tuple[Any, Any, str]:
        log = InMemoryEventLog()
        release = AnswerReleaseSink(_sink(log))
        thinking = await release.emit(
            ModelThinkingDelta(model_call_id="mc_1", text=SECRET)
        )
        completed = await release.emit(
            ModelCompleted(
                model_call_id="mc_1",
                finish_reason="stop",
                text="",
                thinking_preview=SECRET,
            )
        )
        replayed = await log.read(SCOPE.stream_id)
        return (
            thinking.payload,
            completed.payload,
            "\n".join(event.model_dump_json() for event in replayed),
        )

    thinking, completed, serialized = asyncio.run(scenario())

    assert thinking.text == ""
    assert completed.thinking_preview == ""
    assert SECRET not in serialized


def test_a_provisional_shape_may_show_its_reasoning() -> None:
    # The control for the test above, and the same loosening a delta gets:
    # nothing was retrieved, so no grant can be withdrawn between the model
    # finishing and the answer shipping.
    async def scenario() -> tuple[Any, Any]:
        log = InMemoryEventLog()
        release = AnswerReleaseSink(_sink(log), live_text="provisional")
        thinking = await release.emit(
            ModelThinkingDelta(model_call_id="mc_1", text=SECRET)
        )
        completed = await release.emit(
            ModelCompleted(
                model_call_id="mc_1",
                finish_reason="stop",
                thinking_preview=SECRET,
            )
        )
        return thinking.payload, completed.payload

    thinking, completed = asyncio.run(scenario())

    assert thinking.text == SECRET
    # Still blanked: the durable record of a candidate is what the commit
    # methods release, and that rule does not bend for the process text.
    assert completed.thinking_preview == ""


def test_the_default_policy_is_the_one_every_caller_had_before() -> None:
    """A caller that does not think about it must get the strict reading."""

    async def scenario() -> Any:
        log = InMemoryEventLog()
        release = AnswerReleaseSink(_sink(log))
        envelope = await release.emit(ModelDelta(model_call_id="mc_1", text=SECRET))
        return envelope.payload

    assert asyncio.run(scenario()).text == ""


def test_every_retrieval_shape_keeps_its_text_redacted() -> None:
    """Each of the three can end in ``AnswerWithheld``, so none may stream.

    Asserted per shape rather than "the ones that retrieve", because that
    phrase is the thing a future shape gets wrong.
    """

    from agent_workbench.application.chat_execution import (
        AgenticExecution,
        FixedTwoStepExecution,
        RoutedExecution,
    )

    shapes = (FixedTwoStepExecution, AgenticExecution, RoutedExecution)
    request = object()

    for shape in shapes:
        policy = shape.live_text_policy(None, request)  # pyright: ignore[reportArgumentType]
        assert policy == "redacted", shape.__name__


def test_the_ungrounded_shape_may_show_its_text_as_it_writes_it() -> None:
    """The control for the assertion above: not every shape is redacted.

    Without it, a bug that returned "redacted" everywhere would look like a
    passing fence rather than a feature that never ships.
    """

    from agent_workbench.application.chat_execution import UngroundedExecution

    assert UngroundedExecution.live_text_policy(None, object()) == "provisional"  # pyright: ignore[reportArgumentType]


def test_a_transient_event_type_nobody_decided_about_stops_the_process() -> None:
    """The fence is a whitelist, so a new transient type cannot slip past it.

    Simulated by asking the module's own rule about a widened set, rather than
    by mutating ``EVENT_DURABILITY`` -- the check runs at import, and an import
    that has already happened cannot be re-run inside a test.
    """

    from agent_workbench.application import answer_release

    widened = frozenset(answer_release.TRANSIENT_EVENT_TYPES) | {"ModelThought"}

    assert widened - answer_release._TRANSIENT_HANDLED == {"ModelThought"}
    # The control: everything that exists today *is* decided, which is what
    # makes the assertion above about the new type rather than about the rule
    # being vacuous.
    assert not answer_release.TRANSIENT_EVENT_TYPES - answer_release._TRANSIENT_HANDLED


def test_a_shape_that_claims_provisional_and_returns_revisions_fails_the_turn() -> None:
    """Defensive, and unreachable through the shapes in this repository.

    ``UngroundedExecution`` is the only provisional one and it hardcodes an
    empty revision tuple on both of its returns, so nothing in production can
    put the service in this state. What the backstop guards is a *future* shape
    that declares itself provisional by mistake: by the time its answer comes
    back, text has already been streamed on the strength of that claim, and
    there is no honest way to publish under the opposite one.

    Exercised with a stub execution rather than a real shape, which is exactly
    why this test proves the guard and not the production path. Said out loud
    because the alternative -- a test that looked like a regression test for
    something that can happen -- would misdescribe what is covered here.
    """

    class _ProvisionalLiar:
        async def produce(self, request: ChatRequest, **_: Any) -> Any:
            from agent_workbench.application.chat_execution import ProducedAnswer
            from agent_workbench.domain.runs import AgentOutcome

            return ProducedAnswer(
                outcome=AgentOutcome(
                    agent_run_id=request.run_id,
                    status="completed",
                    stop_reason="completed",
                    output_text="an answer built on something revocable",
                ),
                grounded=True,
                authorized_revisions=(("doc_1", 1),),
                citations=(),
            )

        def live_text_policy(self, _request: ChatRequest) -> str:
            return "provisional"

    async def scenario() -> tuple[bool, tuple[str, ...]]:
        conversations = await _conversations()
        retrieval = _EmptyRetrieval()
        service = _chat(retrieval, conversations, [ScriptedTurn(text=SECRET)])
        service = replace(service, execution=_ProvisionalLiar())  # pyright: ignore[reportArgumentType]
        log = InMemoryEventLog()
        raised = False
        try:
            await service.ask(_request(), sink=_sink(log))
        except RuntimeError:
            raised = True
        events = tuple(e.event_type for e in await log.read(SCOPE.stream_id))
        return raised, events

    raised, events = asyncio.run(scenario())

    assert raised
    # No answer of any kind was published: the turn failed rather than
    # choosing between the two contradictory claims.
    assert not {
        "AnswerCommitted",
        "UngroundedAnswerCommitted",
        "AnswerWithheld",
    } & set(events)
