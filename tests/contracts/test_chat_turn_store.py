"""Contract shared by the memory and PostgreSQL chat-turn fact stores."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import pytest
from harness import StoreHarness
from pydantic import ValidationError

from agent_workbench.domain.context import Citation
from agent_workbench.domain.errors import ErrorInfo, NotFoundError
from agent_workbench.domain.messages import assistant_message, user_message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.conversation_store import (
    AuthorizedRevision,
    ChatTurnBusyError,
    ChatTurnClaim,
    ChatTurnConflictError,
    ChatTurnResult,
    ChatTurnStore,
    PendingChatRelease,
    StoredChatTurn,
)

SESSION = "session_1"
TENANT = "tenant_a"
OWNER = "user_1"
NEIGHBOUR = "user_2"
RUN = "run_0000000000000000000000000000001"
KEY = "request-1"
REQUEST_HASH = "a" * 64
OTHER_HASH = "b" * 64
LEASE_SECONDS = 300


@runtime_checkable
class _ExpiryControl(Protocol):
    async def expire_turn_for_test(self, turn_id: str) -> None: ...


@runtime_checkable
class _RowLockControl(Protocol):
    async def hold_turn_lock_for_test(
        self,
        turn_id: str,
        *,
        locked: asyncio.Event,
        release: asyncio.Event,
    ) -> None: ...


async def _with_session(store: ChatTurnStore) -> ChatTurnStore:
    await store.create_session(session_id=SESSION, tenant_id=TENANT, owner_id=OWNER)
    return store


async def _claim(
    store: ChatTurnStore,
    *,
    key: str = KEY,
    request_hash: str = REQUEST_HASH,
    run_id: str = RUN,
    text: str = "current question",
    lease_seconds: int = LEASE_SECONDS,
) -> ChatTurnClaim:
    return await store.claim_turn(
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
        idempotency_key=key,
        request_hash=request_hash,
        run_id=run_id,
        user_message=user_message(text),
        lease_seconds=lease_seconds,
    )


async def _claim_in_session(
    store: ChatTurnStore,
    *,
    session_id: str,
    key: str,
    request_hash: str,
    run_id: str,
    text: str,
) -> ChatTurnClaim:
    return await store.claim_turn(
        session_id=session_id,
        tenant_id=TENANT,
        principal_id=OWNER,
        idempotency_key=key,
        request_hash=request_hash,
        run_id=run_id,
        user_message=user_message(text),
        lease_seconds=LEASE_SECONDS,
    )


def _completed(
    *,
    answer: str = "grounded answer",
    run_id: str = RUN,
) -> ChatTurnResult:
    return ChatTurnResult(
        outcome=AgentOutcome(
            agent_run_id=run_id,
            status="completed",
            stop_reason="completed",
            output_text=answer,
        ),
        answer=answer,
        authorized_revisions=(
            AuthorizedRevision(document_id="document_1", source_revision=1),
        ),
        citations=(
            Citation(
                chunk_id="chunk_1",
                document_id="document_1",
                document_version="version_1",
            ),
        ),
    )


def _withheld() -> ChatTurnResult:
    return ChatTurnResult(
        outcome=AgentOutcome(
            agent_run_id=RUN,
            status="completed",
            stop_reason="completed",
            output_text="",
        ),
        answer="The source is no longer available.",
        authorized_revisions=(),
        withheld=True,
    )


def _failed(*, cancelled: bool = False) -> AgentOutcome:
    if cancelled:
        return AgentOutcome(
            agent_run_id=RUN,
            status="cancelled",
            stop_reason="cancelled",
        )
    return AgentOutcome(
        agent_run_id=RUN,
        status="failed",
        stop_reason="error",
        error=ErrorInfo(code="provider_error", message="provider unavailable"),
    )


def test_a_withheld_turn_cannot_retain_the_denied_model_output() -> None:
    with pytest.raises(ValidationError, match="must not retain model output"):
        ChatTurnResult(
            outcome=AgentOutcome(
                agent_run_id=RUN,
                status="completed",
                stop_reason="completed",
                output_text="denied candidate",
            ),
            answer="The source is no longer available.",
            authorized_revisions=(),
            withheld=True,
        )


def test_a_committed_answer_matches_the_outcome_that_produced_it() -> None:
    with pytest.raises(ValidationError, match="must match"):
        ChatTurnResult(
            outcome=AgentOutcome(
                agent_run_id=RUN,
                status="completed",
                stop_reason="completed",
                output_text="model result",
            ),
            answer="different answer",
            authorized_revisions=(),
        )


def test_claim_snapshots_history_and_appends_the_user_once(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[ChatTurnClaim, list[str], list[str]]:
        await _with_session(store)
        await store.append(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            messages=(
                user_message("previous question"),
                assistant_message(text="previous answer"),
            ),
        )

        claim = await _claim(store)
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return (
            claim,
            [stored.message.text() for stored in claim.history_before],
            [stored.message.text() for stored in history],
        )

    claim, history_before, history_after = chat_turn_conversations.run(scenario)

    assert claim.newly_claimed is True
    assert claim.turn.status == "running"
    assert claim.turn.lease_until is not None
    assert claim.turn.user_message_id.startswith("msg_")
    assert history_before == ["previous question", "previous answer"]
    assert history_after == [
        "previous question",
        "previous answer",
        "current question",
    ]


def test_claim_requires_a_positive_fixed_execution_lease(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await _with_session(store)
        await _claim(store, lease_seconds=0)

    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        chat_turn_conversations.run(scenario)


def test_same_key_and_hash_return_the_original_turn(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[ChatTurnClaim, ChatTurnClaim, list[str]]:
        await _with_session(store)
        first = await _claim(store)
        second = await _claim(
            store,
            run_id="run_retry_000000000000000000000000001",
        )
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return first, second, [stored.message.text() for stored in history]

    first, second, history = chat_turn_conversations.run(scenario)

    assert first.newly_claimed is True
    assert second.newly_claimed is False
    assert second.turn == first.turn
    assert history == ["current question"]


def test_same_key_after_expiry_returns_the_failed_fact_without_reexecution(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[ChatTurnClaim, ChatTurnClaim, list[str]]:
        await _with_session(store)
        first = await _claim(store)
        assert isinstance(store, _ExpiryControl)
        await store.expire_turn_for_test(first.turn.turn_id)
        retried = await _claim(store)
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return first, retried, [message.message.text() for message in history]

    first, retried, history = chat_turn_conversations.run(scenario)

    assert first.turn.status == "running"
    assert retried.newly_claimed is False
    assert retried.turn.status == "failed"
    assert retried.turn.lease_until is None
    assert retried.turn.failure_outcome is not None
    assert retried.turn.failure_outcome.stop_reason == "deadline"
    assert retried.turn.failure_outcome.error is not None
    assert retried.turn.failure_outcome.error.code == "stale_execution"
    assert retried.turn.failure_outcome.error.retryable is False
    assert history == ["current question"]


def test_an_expired_running_turn_is_reaped_before_claiming_the_next_key(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[ChatTurnClaim, ChatTurnClaim, ChatTurnClaim]:
        await _with_session(store)
        first = await _claim(store)
        assert isinstance(store, _ExpiryControl)
        await store.expire_turn_for_test(first.turn.turn_id)
        second = await _claim(
            store,
            key="request-2",
            request_hash=OTHER_HASH,
            run_id="run_0000000000000000000000000000002",
            text="next question",
        )
        expired = await _claim(store)
        return first, second, expired

    first, second, expired = chat_turn_conversations.run(scenario)

    assert first.turn.status == "running"
    assert expired.turn.status == "failed"
    assert second.newly_claimed is True
    assert second.turn.status == "running"
    assert second.turn.lease_until is not None


def test_same_key_with_a_different_hash_fails_closed(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> list[str]:
        await _with_session(store)
        await _claim(store)
        with pytest.raises(
            ChatTurnConflictError,
            match="idempotency conflict",
        ):
            await _claim(store, request_hash=OTHER_HASH)
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return [stored.message.text() for stored in history]

    assert chat_turn_conversations.run(scenario) == ["current question"]


def test_an_idempotency_key_is_scoped_to_its_session(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> tuple[str, str]:
        await _with_session(store)
        first = await _claim(store)
        await store.create_session(
            session_id="session_2",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        second = await store.claim_turn(
            session_id="session_2",
            tenant_id=TENANT,
            principal_id=OWNER,
            idempotency_key=KEY,
            request_hash=OTHER_HASH,
            run_id="run_0000000000000000000000000000002",
            user_message=user_message("an unrelated question"),
            lease_seconds=LEASE_SECONDS,
        )
        return first.turn.turn_id, second.turn.turn_id

    first_turn_id, second_turn_id = chat_turn_conversations.run(scenario)

    assert first_turn_id != second_turn_id


def test_a_run_id_can_back_only_one_turn(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await _with_session(store)
        await _claim(store)
        await store.create_session(
            session_id="session_2",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        await store.claim_turn(
            session_id="session_2",
            tenant_id=TENANT,
            principal_id=OWNER,
            idempotency_key="request-2",
            request_hash=OTHER_HASH,
            run_id=RUN,
            user_message=user_message("an unrelated question"),
            lease_seconds=LEASE_SECONDS,
        )

    with pytest.raises(ChatTurnConflictError, match="run id conflict"):
        chat_turn_conversations.run(scenario)


@pytest.mark.parametrize("release_pending", [False, True])
def test_another_active_key_is_busy(
    chat_turn_conversations: StoreHarness,
    release_pending: bool,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await _with_session(store)
        claim = await _claim(store)
        if release_pending:
            await store.prepare_release(
                session_id=SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_id=claim.turn.turn_id,
                result=_completed(),
            )
        await _claim(
            store,
            key="request-2",
            request_hash=OTHER_HASH,
            run_id="run_0000000000000000000000000000002",
        )

    with pytest.raises(ChatTurnBusyError, match="unfinished chat turn"):
        chat_turn_conversations.run(scenario)


@pytest.mark.parametrize("failed", [False, True])
def test_a_terminal_turn_releases_the_session_for_the_next_key(
    chat_turn_conversations: StoreHarness,
    failed: bool,
) -> None:
    async def scenario(store: ChatTurnStore) -> ChatTurnClaim:
        await _with_session(store)
        claim = await _claim(store)
        if failed:
            await store.finish_failed(
                session_id=SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_id=claim.turn.turn_id,
                outcome=_failed(),
            )
        else:
            await store.prepare_release(
                session_id=SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_id=claim.turn.turn_id,
                result=_completed(),
            )
            await store.mark_released(
                session_id=SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_id=claim.turn.turn_id,
            )
        return await _claim(
            store,
            key="request-2",
            request_hash=OTHER_HASH,
            run_id="run_0000000000000000000000000000002",
            text="next question",
        )

    next_claim = chat_turn_conversations.run(scenario)

    assert next_claim.newly_claimed is True
    assert next_claim.turn.status == "running"
    assert [message.message.text() for message in next_claim.history_before] == (
        ["current question"] if failed else ["current question", "grounded answer"]
    )


def test_concurrent_retries_create_one_turn_and_one_user_message(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> tuple[int, set[str], int]:
        await _with_session(store)
        claims = await asyncio.gather(*(_claim(store) for _ in range(8)))
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return (
            sum(claim.newly_claimed for claim in claims),
            {claim.turn.turn_id for claim in claims},
            len(history),
        )

    newly_claimed, turn_ids, history_length = chat_turn_conversations.run(scenario)

    assert newly_claimed == 1
    assert len(turn_ids) == 1
    assert history_length == 1


def test_idempotency_keys_are_scoped_to_one_conversation(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> tuple[str, str]:
        await _with_session(store)
        await store.create_session(
            session_id="session_2",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        first = await _claim(store)
        second = await store.claim_turn(
            session_id="session_2",
            tenant_id=TENANT,
            principal_id=OWNER,
            idempotency_key=KEY,
            request_hash=OTHER_HASH,
            run_id="run_0000000000000000000000000000002",
            user_message=user_message("another conversation"),
            lease_seconds=LEASE_SECONDS,
        )
        return first.turn.turn_id, second.turn.turn_id

    first_turn, second_turn = chat_turn_conversations.run(scenario)

    assert first_turn != second_turn


def test_one_run_id_cannot_back_turns_in_two_conversations(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await _with_session(store)
        await store.create_session(
            session_id="session_2",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        await _claim(store)
        await store.claim_turn(
            session_id="session_2",
            tenant_id=TENANT,
            principal_id=OWNER,
            idempotency_key="request-2",
            request_hash=OTHER_HASH,
            run_id=RUN,
            user_message=user_message("another conversation"),
            lease_seconds=LEASE_SECONDS,
        )

    with pytest.raises(ChatTurnConflictError, match="run id"):
        chat_turn_conversations.run(scenario)


def test_prepare_release_hides_the_assistant_and_is_idempotent(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[StoredChatTurn, StoredChatTurn, list[str]]:
        await _with_session(store)
        claim = await _claim(store)
        result = _completed()
        first = await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=result,
        )
        second = await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=result,
        )
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return first, second, [stored.message.text() for stored in history]

    first, second, history = chat_turn_conversations.run(scenario)

    assert first.status == "release_pending"
    assert first.lease_until is None
    assert first.assistant_message_id is None
    assert second == first
    assert history == ["current question"]


def test_late_prepare_terminalizes_an_expired_execution_without_the_candidate(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[StoredChatTurn, list[str]]:
        await _with_session(store)
        claim = await _claim(store)
        assert isinstance(store, _ExpiryControl)
        await store.expire_turn_for_test(claim.turn.turn_id)
        expired = await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=_completed(answer="late candidate"),
        )
        with pytest.raises(ChatTurnConflictError, match="cannot prepare release"):
            await store.prepare_release(
                session_id=SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_id=claim.turn.turn_id,
                result=_completed(answer="late candidate"),
            )
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return expired, [message.message.text() for message in history]

    expired, history = chat_turn_conversations.run(scenario)

    assert expired.status == "failed"
    assert expired.lease_until is None
    assert expired.result is None
    assert expired.assistant_message_id is None
    assert expired.failure_outcome is not None
    assert expired.failure_outcome.stop_reason == "deadline"
    assert expired.failure_outcome.error is not None
    assert expired.failure_outcome.error.code == "stale_execution"
    assert "late candidate" not in expired.model_dump_json()
    assert history == ["current question"]


def test_prepare_release_rejects_a_different_result(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> list[str]:
        await _with_session(store)
        claim = await _claim(store)
        await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=_completed(),
        )
        with pytest.raises(ChatTurnConflictError, match="release result conflict"):
            await store.prepare_release(
                session_id=SESSION,
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_id=claim.turn.turn_id,
                result=_completed(answer="different answer"),
            )
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return [stored.message.text() for stored in history]

    assert chat_turn_conversations.run(scenario) == [
        "current question",
    ]


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        pytest.param(_completed(), "committed", id="committed"),
        pytest.param(_withheld(), "withheld", id="withheld"),
    ],
)
def test_mark_released_uses_the_prepared_result_and_is_idempotent(
    chat_turn_conversations: StoreHarness,
    result: ChatTurnResult,
    expected_status: str,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[StoredChatTurn, StoredChatTurn, list[str]]:
        await _with_session(store)
        claim = await _claim(store)
        await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=result,
        )
        first = await store.mark_released(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
        )
        second = await store.mark_released(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
        )
        prepared_again = await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=result,
        )
        assert prepared_again == first
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return first, second, [stored.message.text() for stored in history]

    first, second, history = chat_turn_conversations.run(scenario)

    assert first.status == expected_status
    assert first.lease_until is None
    assert first.assistant_message_id is not None
    assert second == first
    assert history == ["current question", result.answer]


def test_final_authorization_can_replace_a_pending_candidate_with_a_safe_refusal(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> tuple[StoredChatTurn, list[str]]:
        await _with_session(store)
        claim = await _claim(store)
        await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=_completed(answer="candidate that was later revoked"),
        )
        released = await store.mark_released(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            withheld_result=_withheld(),
        )
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return released, [message.message.text() for message in history]

    released, history = chat_turn_conversations.run(scenario)

    assert released.status == "withheld"
    assert released.result == _withheld()
    assert "candidate that was later revoked" not in released.model_dump_json()
    assert history == ["current question", _withheld().answer]


def test_release_override_cannot_replace_one_publishable_answer_with_another(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await _with_session(store)
        claim = await _claim(store)
        await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=_completed(),
        )
        await store.mark_released(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            withheld_result=_completed(answer="different publishable answer"),
        )

    with pytest.raises(ChatTurnConflictError, match="safe withheld"):
        chat_turn_conversations.run(scenario)


def test_release_pending_turns_are_listed_in_stable_order_with_owner_scope(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def prepare(
        store: ChatTurnStore,
        *,
        session_id: str,
        tenant_id: str,
        owner_id: str,
        suffix: str,
    ) -> StoredChatTurn:
        await store.create_session(
            session_id=session_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        run_id = f"run_pending_{suffix}"
        claim = await store.claim_turn(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=owner_id,
            idempotency_key=f"request-{suffix}",
            request_hash=suffix * 64,
            run_id=run_id,
            user_message=user_message(f"question {suffix}"),
            lease_seconds=LEASE_SECONDS,
        )
        return await store.prepare_release(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=owner_id,
            turn_id=claim.turn.turn_id,
            result=_completed(answer=f"answer {suffix}", run_id=run_id),
        )

    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[
        tuple[PendingChatRelease, ...],
        tuple[PendingChatRelease, ...],
        dict[str, tuple[str, str]],
    ]:
        first = await prepare(
            store,
            session_id=SESSION,
            tenant_id=TENANT,
            owner_id=OWNER,
            suffix="a",
        )
        second = await prepare(
            store,
            session_id="session_2",
            tenant_id="tenant_b",
            owner_id="user_2",
            suffix="b",
        )
        third = await prepare(
            store,
            session_id="session_3",
            tenant_id=TENANT,
            owner_id="user_3",
            suffix="c",
        )
        await store.create_session(
            session_id="session_running",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        await _claim_in_session(
            store,
            session_id="session_running",
            key="request-running",
            request_hash="d" * 64,
            run_id="run_running",
            text="still running",
        )
        all_pending = await store.list_release_pending(limit=10)
        limited = await store.list_release_pending(limit=2)
        expected_scope = {
            first.turn_id: (TENANT, OWNER),
            second.turn_id: ("tenant_b", "user_2"),
            third.turn_id: (TENANT, "user_3"),
        }
        return all_pending, limited, expected_scope

    all_pending, limited, expected_scope = chat_turn_conversations.run(scenario)

    ids = tuple(item.turn.turn_id for item in all_pending)
    assert ids == tuple(sorted(expected_scope))
    assert tuple(item.turn.turn_id for item in limited) == ids[:2]
    assert all(item.turn.status == "release_pending" for item in all_pending)
    assert {
        item.turn.turn_id: (item.tenant_id, item.principal_id) for item in all_pending
    } == expected_scope


@pytest.mark.parametrize("limit", [0, -1])
def test_pending_release_listing_requires_a_positive_limit(
    chat_turn_conversations: StoreHarness,
    limit: int,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await store.list_release_pending(limit=limit)

    with pytest.raises(ValueError, match="pending release limit must be positive"):
        chat_turn_conversations.run(scenario)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(_failed(), id="failed"),
        pytest.param(_failed(cancelled=True), id="cancelled"),
    ],
)
def test_finish_failed_never_appends_an_assistant_and_is_idempotent(
    chat_turn_conversations: StoreHarness,
    outcome: AgentOutcome,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[StoredChatTurn, StoredChatTurn, list[str]]:
        await _with_session(store)
        claim = await _claim(store)
        first = await store.finish_failed(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            outcome=outcome,
        )
        second = await store.finish_failed(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            outcome=outcome,
        )
        history = await store.history(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return first, second, [stored.message.text() for stored in history]

    first, second, history = chat_turn_conversations.run(scenario)

    assert first.status == outcome.status
    assert first.lease_until is None
    assert first.assistant_message_id is None
    assert first.failure_outcome == outcome
    assert second == first
    assert history == ["current question"]


def test_best_effort_cleanup_changes_only_the_still_running_turn(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[StoredChatTurn, StoredChatTurn, ChatTurnClaim]:
        await _with_session(store)
        claim = await _claim(store)
        first = await store.finish_running_if_current(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            outcome=_failed(),
        )
        second = await store.finish_running_if_current(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            outcome=_failed(),
        )
        stored = await _claim(store)
        return first, second, stored

    first, second, stored = chat_turn_conversations.run(scenario)

    assert first.status == "failed"
    assert second == first
    assert stored.turn.status == "failed"
    assert stored.turn.lease_until is None


def test_best_effort_cleanup_never_overwrites_a_prepared_result(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[StoredChatTurn, ChatTurnClaim]:
        await _with_session(store)
        claim = await _claim(store)
        prepared = await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=_completed(),
        )
        changed = await store.finish_running_if_current(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            outcome=_failed(),
        )
        stored = await _claim(store)
        assert stored.turn == prepared
        return changed, stored

    changed, stored = chat_turn_conversations.run(scenario)

    assert changed.status == "release_pending"
    assert changed == stored.turn
    assert stored.turn.status == "release_pending"
    assert stored.turn.lease_until is None
    assert stored.turn.result == _completed()


def test_reaper_fails_expired_turns_in_bounded_batches(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[tuple[StoredChatTurn, ...], tuple[StoredChatTurn, ...]]:
        await _with_session(store)
        await store.create_session(
            session_id="session_2",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        first = await _claim(store)
        second = await _claim_in_session(
            store,
            session_id="session_2",
            key="request-2",
            request_hash=OTHER_HASH,
            run_id="run_0000000000000000000000000000002",
            text="another question",
        )
        assert isinstance(store, _ExpiryControl)
        await store.expire_turn_for_test(first.turn.turn_id)
        await store.expire_turn_for_test(second.turn.turn_id)
        first_batch = await store.reap_expired_running(limit=1)
        second_batch = await store.reap_expired_running(limit=10)
        return first_batch, second_batch

    first_batch, second_batch = chat_turn_conversations.run(scenario)

    assert len(first_batch) == 1
    assert len(second_batch) == 1
    assert first_batch[0].turn_id != second_batch[0].turn_id
    for expired in (*first_batch, *second_batch):
        assert expired.status == "failed"
        assert expired.lease_until is None
        assert expired.failure_outcome is not None
        assert expired.failure_outcome.stop_reason == "deadline"
        assert expired.failure_outcome.error is not None
        assert expired.failure_outcome.error.code == "stale_execution"
        assert expired.failure_outcome.error.retryable is False


def test_reaper_requires_a_positive_batch_limit(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await store.reap_expired_running(limit=0)

    with pytest.raises(ValueError, match="reaper limit must be positive"):
        chat_turn_conversations.run(scenario)


def test_postgres_pending_listing_does_not_claim_or_wait_on_rows(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> tuple[PendingChatRelease, ...]:
        if not isinstance(store, _RowLockControl):
            pytest.skip("non-locking recovery reads are a PostgreSQL contract")
        await _with_session(store)
        claim = await _claim(store)
        await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=_completed(),
        )
        locked = asyncio.Event()
        release = asyncio.Event()
        holder = asyncio.create_task(
            store.hold_turn_lock_for_test(
                claim.turn.turn_id,
                locked=locked,
                release=release,
            )
        )
        try:
            await asyncio.wait_for(locked.wait(), timeout=10)
            return await asyncio.wait_for(
                store.list_release_pending(limit=10),
                timeout=2,
            )
        finally:
            release.set()
            await asyncio.gather(holder, return_exceptions=True)

    pending = chat_turn_conversations.run(scenario)

    assert len(pending) == 1
    assert pending[0].turn.status == "release_pending"
    assert pending[0].tenant_id == TENANT
    assert pending[0].principal_id == OWNER


def test_postgres_reaper_skips_a_turn_locked_by_another_worker(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(
        store: ChatTurnStore,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not isinstance(store, _RowLockControl):
            pytest.skip("SKIP LOCKED is a PostgreSQL concurrency contract")
        await _with_session(store)
        await store.create_session(
            session_id="session_2",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        first = await _claim(store)
        second = await _claim_in_session(
            store,
            session_id="session_2",
            key="request-2",
            request_hash=OTHER_HASH,
            run_id="run_0000000000000000000000000000002",
            text="another question",
        )
        assert isinstance(store, _ExpiryControl)
        await store.expire_turn_for_test(first.turn.turn_id)
        await store.expire_turn_for_test(second.turn.turn_id)

        locked = asyncio.Event()
        release = asyncio.Event()
        holder = asyncio.create_task(
            store.hold_turn_lock_for_test(
                first.turn.turn_id,
                locked=locked,
                release=release,
            )
        )
        try:
            await asyncio.wait_for(locked.wait(), timeout=10)
            available = await asyncio.wait_for(
                store.reap_expired_running(limit=10),
                timeout=2,
            )
        finally:
            release.set()
            await asyncio.gather(holder, return_exceptions=True)
        formerly_locked = await store.reap_expired_running(limit=10)
        return (
            tuple(turn.turn_id for turn in available),
            tuple(turn.turn_id for turn in formerly_locked),
        )

    available, formerly_locked = chat_turn_conversations.run(scenario)

    assert len(available) == 1
    assert len(formerly_locked) == 1
    assert available != formerly_locked


def test_terminal_transitions_fail_closed(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await _with_session(store)
        claim = await _claim(store)
        await store.prepare_release(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            result=_completed(),
        )
        await store.finish_failed(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=OWNER,
            turn_id=claim.turn.turn_id,
            outcome=_failed(),
        )

    with pytest.raises(ChatTurnConflictError, match="cannot fail"):
        chat_turn_conversations.run(scenario)


def test_claim_checks_session_ownership_before_idempotency(
    chat_turn_conversations: StoreHarness,
) -> None:
    async def scenario(store: ChatTurnStore) -> None:
        await _with_session(store)
        await _claim(store)
        await store.claim_turn(
            session_id=SESSION,
            tenant_id=TENANT,
            principal_id=NEIGHBOUR,
            idempotency_key=KEY,
            request_hash=REQUEST_HASH,
            run_id=RUN,
            user_message=user_message("current question"),
            lease_seconds=LEASE_SECONDS,
        )

    with pytest.raises(NotFoundError, match="conversation session not found"):
        chat_turn_conversations.run(scenario)
