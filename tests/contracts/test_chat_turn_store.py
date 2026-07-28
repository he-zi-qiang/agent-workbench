"""Contract shared by the memory and PostgreSQL chat-turn fact stores."""

from __future__ import annotations

import asyncio

import pytest
from harness import StoreHarness
from pydantic import ValidationError

from agent_workbench.domain.context import Citation
from agent_workbench.domain.errors import ErrorInfo, NotFoundError
from agent_workbench.domain.messages import assistant_message, user_message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.conversation_store import (
    ChatTurnBusyError,
    ChatTurnClaim,
    ChatTurnConflictError,
    ChatTurnResult,
    ChatTurnStore,
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
) -> ChatTurnClaim:
    return await store.claim_turn(
        session_id=SESSION,
        tenant_id=TENANT,
        principal_id=OWNER,
        idempotency_key=key,
        request_hash=request_hash,
        run_id=run_id,
        user_message=user_message(text),
    )


def _completed(*, answer: str = "grounded answer") -> ChatTurnResult:
    return ChatTurnResult(
        outcome=AgentOutcome(
            agent_run_id=RUN,
            status="completed",
            stop_reason="completed",
            output_text=answer,
        ),
        answer=answer,
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
    assert claim.turn.user_message_id.startswith("msg_")
    assert history_before == ["previous question", "previous answer"]
    assert history_after == [
        "previous question",
        "previous answer",
        "current question",
    ]


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
    assert first.assistant_message_id is None
    assert second == first
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
    assert first.assistant_message_id is not None
    assert second == first
    assert history == ["current question", result.answer]


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
    assert first.assistant_message_id is None
    assert first.failure_outcome == outcome
    assert second == first
    assert history == ["current question"]


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
        )

    with pytest.raises(NotFoundError, match="conversation session not found"):
        chat_turn_conversations.run(scenario)
