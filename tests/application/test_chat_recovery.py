"""Fixed-deadline recovery for synchronous Chat executions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import (
    InMemoryChatReleaseCoordinator,
    InMemoryConversationStore,
    InMemoryEventLog,
)
from agent_workbench.application.chat import (
    REFUSAL,
    ChatExecutionError,
    ChatRequest,
    ChatService,
)
from agent_workbench.application.chat_recovery import (
    ChatPendingReleaseRecovery,
    ChatTurnReaper,
)
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.runs import AgentOutcome, RunBudget
from agent_workbench.ports.conversation_store import ChatTurnResult
from agent_workbench.ports.event_log import EventScope

TENANT = "tenant_a"
PRINCIPAL = "user_a"
SESSION = "ses_1"


class _BlockingRetrieval:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def retrieve(self, request: Any) -> Any:
        del request
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def revisions_unchanged(self, revisions: Any, **kwargs: Any) -> bool:
        del revisions, kwargs
        return True


class _NeverExecutor:
    async def run(self, request: Any, emit: Any, cancellation: Any) -> Any:
        del request, emit, cancellation
        raise AssertionError("retrieval never completed")


async def _service(
    *,
    timeout: float,
) -> tuple[ChatService, InMemoryConversationStore, _BlockingRetrieval]:
    conversations = InMemoryConversationStore()
    await conversations.create_session(
        session_id=SESSION,
        tenant_id=TENANT,
        owner_id=PRINCIPAL,
    )
    retrieval = _BlockingRetrieval()
    return (
        ChatService(
            retrieval=retrieval,  # pyright: ignore[reportArgumentType]
            executor=_NeverExecutor(),
            conversations=conversations,
            releaser=InMemoryChatReleaseCoordinator(
                conversations=conversations,
                revisions=retrieval,
            ),
            budget=RunBudget(max_steps=1, max_tool_calls=1),
            request_timeout_seconds=timeout,
            orphan_grace_seconds=5,
        ),
        conversations,
        retrieval,
    )


def _request(*, key: str = "request-1", run_id: str = "run_1") -> ChatRequest:
    return ChatRequest(
        session_id=SESSION,
        question="what happened",
        principal=PrincipalContext(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
        ),
        knowledge_base_id="kb_main",
        idempotency_key=key,
        run_id=run_id,
    )


def _sink(run_id: str = "run_1") -> ScopedEventSink:
    return ScopedEventSink(
        log=InMemoryEventLog(),
        scope=EventScope(stream_id=SESSION, run_id=run_id),
    )


def test_external_task_cancellation_terminalizes_only_the_running_turn() -> None:
    async def scenario() -> tuple[str, bool, int]:
        service, conversations, retrieval = await _service(timeout=30)
        request = _request()
        task = asyncio.create_task(service.ask(request, _sink()))
        await retrieval.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(ChatExecutionError) as repeated:
            await service.ask(request, _sink())
        next_claim = await conversations.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            idempotency_key="request-2",
            request_hash="b" * 64,
            run_id="run_2",
            user_message=user_message("try a new turn"),
            lease_seconds=30,
        )
        return (
            repeated.value.outcome.status,
            next_claim.newly_claimed,
            retrieval.calls,
        )

    status, next_claimed, retrieval_calls = asyncio.run(scenario())

    assert status == "cancelled"
    assert next_claimed is True
    assert retrieval_calls == 1


def test_request_timeout_is_a_stable_deadline_failure_not_a_busy_turn() -> None:
    async def scenario() -> tuple[str, str, bool, int]:
        service, _, retrieval = await _service(timeout=0.01)
        request = _request()
        with pytest.raises(ChatExecutionError) as first:
            await service.ask(request, _sink())
        with pytest.raises(ChatExecutionError) as repeated:
            await service.ask(request, _sink())
        assert first.value.outcome.error is not None
        return (
            first.value.outcome.stop_reason,
            repeated.value.outcome.agent_run_id,
            first.value.outcome.error.retryable,
            retrieval.calls,
        )

    stop_reason, repeated_run, retryable, retrieval_calls = asyncio.run(scenario())

    assert stop_reason == "deadline"
    assert repeated_run == "run_1"
    assert retryable is False
    assert retrieval_calls == 1


def test_reaper_terminalizes_an_orphan_without_replaying_it() -> None:
    async def scenario() -> tuple[str, str, bool]:
        current = [datetime(2026, 7, 28, tzinfo=UTC)]
        conversations = InMemoryConversationStore(clock=lambda: current[0])
        await conversations.create_session(
            session_id=SESSION,
            tenant_id=TENANT,
            owner_id=PRINCIPAL,
        )
        orphan = await conversations.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            idempotency_key="orphan",
            request_hash="a" * 64,
            run_id="run_orphan",
            user_message=user_message("unanswered"),
            lease_seconds=5,
        )
        current[0] += timedelta(seconds=6)
        expired = await ChatTurnReaper(
            conversations=conversations,
            poll_seconds=1,
            batch_size=10,
        ).run_once()
        same_key = await conversations.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            idempotency_key="orphan",
            request_hash="a" * 64,
            run_id="run_orphan",
            user_message=user_message("unanswered"),
            lease_seconds=5,
        )
        next_key = await conversations.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            idempotency_key="next",
            request_hash="b" * 64,
            run_id="run_next",
            user_message=user_message("new work"),
            lease_seconds=5,
        )
        assert orphan.turn.lease_until is not None
        return expired[0].status, same_key.turn.status, next_key.newly_claimed

    expired_status, repeated_status, next_claimed = asyncio.run(scenario())

    assert expired_status == "failed"
    assert repeated_status == "failed"
    assert next_claimed is True


def test_a_model_result_arriving_after_expiry_cannot_be_prepared() -> None:
    async def scenario() -> tuple[str, int]:
        current = [datetime(2026, 7, 28, tzinfo=UTC)]
        conversations = InMemoryConversationStore(clock=lambda: current[0])
        await conversations.create_session(
            session_id=SESSION,
            tenant_id=TENANT,
            owner_id=PRINCIPAL,
        )
        claim = await conversations.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            idempotency_key="late",
            request_hash="c" * 64,
            run_id="run_late",
            user_message=user_message("slow question"),
            lease_seconds=5,
        )
        current[0] += timedelta(seconds=6)
        prepared = await conversations.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            turn_id=claim.turn.turn_id,
            result=ChatTurnResult(
                outcome=AgentOutcome(
                    agent_run_id="run_late",
                    status="completed",
                    stop_reason="completed",
                    output_text="late secret",
                ),
                answer="late secret",
                authorized_revisions=(),
            ),
        )
        history = await conversations.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
        )
        return prepared.status, len(history)

    status, history_size = asyncio.run(scenario())

    assert status == "failed"
    assert history_size == 1


def test_release_pending_is_recovered_without_the_original_client_retry() -> None:
    """The crash window after prepare must not hold the session forever."""

    async def scenario() -> tuple[str, list[str], bool]:
        conversations = InMemoryConversationStore()
        await conversations.create_session(
            session_id=SESSION,
            tenant_id=TENANT,
            owner_id=PRINCIPAL,
        )
        claim = await conversations.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            idempotency_key="prepared",
            request_hash="d" * 64,
            run_id="run_prepared",
            user_message=user_message("prepared question"),
            lease_seconds=30,
        )
        await conversations.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            turn_id=claim.turn.turn_id,
            result=ChatTurnResult(
                outcome=AgentOutcome(
                    agent_run_id="run_prepared",
                    status="completed",
                    stop_reason="completed",
                    output_text="prepared answer",
                ),
                answer="prepared answer",
                authorized_revisions=(),
            ),
        )
        log = InMemoryEventLog()
        recovery = ChatPendingReleaseRecovery(
            conversations=conversations,
            releaser=InMemoryChatReleaseCoordinator(
                conversations=conversations,
                revisions=_BlockingRetrieval(),
            ),
            sink_for=lambda stream_id, run_id: ScopedEventSink(
                log=log,
                scope=EventScope(stream_id=stream_id, run_id=run_id),
            ),
            refusal_text=REFUSAL,
            poll_seconds=1,
            batch_size=10,
        )

        recovered = await recovery.run_once()
        repeated = await recovery.run_once()
        next_turn = await conversations.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            idempotency_key="next",
            request_hash="e" * 64,
            run_id="run_next",
            user_message=user_message("next question"),
            lease_seconds=30,
        )
        events = await log.read(SESSION)
        assert repeated == ()
        return (
            recovered[0].status,
            [event.event_type for event in events],
            next_turn.newly_claimed,
        )

    status, event_types, next_claimed = asyncio.run(scenario())

    assert status == "committed"
    assert event_types == ["AnswerCommitted"]
    assert next_claimed is True


def test_one_pending_release_failure_does_not_abort_the_rest_of_the_batch() -> None:
    async def scenario() -> tuple[int, int, str, int]:
        conversations = InMemoryConversationStore()
        prepared = []
        for suffix in ("a", "b"):
            session_id = f"ses_{suffix}"
            run_id = f"run_{suffix}"
            await conversations.create_session(
                session_id=session_id,
                tenant_id=TENANT,
                owner_id=PRINCIPAL,
            )
            claim = await conversations.claim_turn(
                session_id=session_id,
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                idempotency_key=f"request-{suffix}",
                request_hash=suffix * 64,
                run_id=run_id,
                user_message=user_message(f"question {suffix}"),
                lease_seconds=30,
            )
            prepared.append(
                await conversations.prepare_release(
                    session_id=session_id,
                    tenant_id=TENANT,
                    principal_id=PRINCIPAL,
                    turn_id=claim.turn.turn_id,
                    result=ChatTurnResult(
                        outcome=AgentOutcome(
                            agent_run_id=run_id,
                            status="completed",
                            stop_reason="completed",
                            output_text=f"answer {suffix}",
                        ),
                        answer=f"answer {suffix}",
                        authorized_revisions=(),
                    ),
                )
            )

        delegate = InMemoryChatReleaseCoordinator(
            conversations=conversations,
            revisions=_BlockingRetrieval(),
        )
        failed_turn_id = min(turn.turn_id for turn in prepared)

        class _FailOneRelease:
            async def release(self, **kwargs: Any) -> Any:
                if kwargs["turn"].turn_id == failed_turn_id:
                    raise RuntimeError("deterministic release failure")
                return await delegate.release(**kwargs)

        log = InMemoryEventLog()
        recovery = ChatPendingReleaseRecovery(
            conversations=conversations,
            releaser=_FailOneRelease(),  # pyright: ignore[reportArgumentType]
            sink_for=lambda stream_id, run_id: ScopedEventSink(
                log=log,
                scope=EventScope(stream_id=stream_id, run_id=run_id),
            ),
            refusal_text=REFUSAL,
            poll_seconds=1,
            batch_size=10,
        )
        recovered = await recovery.run_once()
        remaining = await conversations.list_release_pending(limit=10)
        events = []
        for turn in prepared:
            events.extend(await log.read(turn.session_id))
        return (
            len(recovered),
            len(remaining),
            remaining[0].turn.turn_id,
            len(events),
        )

    recovered_count, remaining_count, remaining_turn_id, event_count = asyncio.run(
        scenario()
    )

    assert recovered_count == 1
    assert remaining_count == 1
    assert remaining_turn_id.startswith("turn_")
    assert event_count == 1
