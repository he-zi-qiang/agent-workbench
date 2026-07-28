"""Observable atomicity of the deterministic Chat-expiration double."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agent_workbench.adapters.memory import (
    InMemoryChatExpirationCoordinator,
    InMemoryConversationStore,
    InMemoryEventLog,
)
from agent_workbench.domain.events import (
    ChatTurnExpired,
    EventEnvelope,
    EventPayload,
)
from agent_workbench.domain.messages import user_message
from agent_workbench.ports.conversation_store import ChatTurnBusyError
from agent_workbench.ports.event_log import EventKey, EventScope

TENANT = "tenant_a"
OWNER = "user_1"


class _ControlledEventLog:
    def __init__(
        self,
        *,
        fail_turn_ids: frozenset[str] = frozenset(),
        block_turn_id: str | None = None,
    ) -> None:
        self.inner = InMemoryEventLog()
        self.fail_turn_ids = fail_turn_ids
        self.block_turn_id = block_turn_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def append(
        self,
        scope: EventScope,
        payload: EventPayload,
        *,
        parent_event_id: str | None = None,
        event_key: EventKey | None = None,
    ) -> EventEnvelope:
        if isinstance(payload, ChatTurnExpired):
            if payload.turn_id == self.block_turn_id:
                self.entered.set()
                await self.release.wait()
            if payload.turn_id in self.fail_turn_ids:
                raise RuntimeError("injected event append failure")
        return await self.inner.append(
            scope,
            payload,
            parent_event_id=parent_event_id,
            event_key=event_key,
        )

    async def read(
        self,
        stream_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 500,
    ) -> tuple[EventEnvelope, ...]:
        return await self.inner.read(
            stream_id,
            after_sequence=after_sequence,
            limit=limit,
        )


def test_event_failure_leaves_the_turn_running_for_a_later_retry() -> None:
    async def scenario() -> None:
        now = [datetime(2026, 7, 28, tzinfo=UTC)]
        conversations = InMemoryConversationStore(clock=lambda: now[0])
        await conversations.create_session(
            session_id="session_failure",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        turn = (
            await conversations.claim_turn(
                session_id="session_failure",
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key="request-1",
                request_hash="a" * 64,
                run_id="run_1",
                user_message=user_message("question"),
                lease_seconds=5,
            )
        ).turn
        now[0] += timedelta(seconds=6)
        events = _ControlledEventLog(fail_turn_ids=frozenset({turn.turn_id}))
        coordinator = InMemoryChatExpirationCoordinator(
            conversations=conversations,
            events=events,
        )

        assert await coordinator.expire_due(limit=1) == ()
        same_key = await conversations.claim_turn(
            session_id=turn.session_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            idempotency_key=turn.idempotency_key,
            request_hash=turn.request_hash,
            run_id=turn.run_id,
            user_message=user_message("ignored"),
            lease_seconds=5,
        )
        assert same_key.turn.status == "running"
        assert same_key.turn.failure_outcome is None
        assert await events.read(turn.session_id) == ()
        with pytest.raises(ChatTurnBusyError):
            await conversations.claim_turn(
                session_id=turn.session_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key="request-2",
                request_hash="b" * 64,
                run_id="run_2",
                user_message=user_message("next"),
                lease_seconds=5,
            )

    asyncio.run(scenario())


def test_claim_waits_until_the_expiry_event_and_turn_are_both_visible() -> None:
    async def scenario() -> None:
        now = [datetime(2026, 7, 28, tzinfo=UTC)]
        conversations = InMemoryConversationStore(clock=lambda: now[0])
        await conversations.create_session(
            session_id="session_barrier",
            tenant_id=TENANT,
            owner_id=OWNER,
        )
        turn = (
            await conversations.claim_turn(
                session_id="session_barrier",
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key="request-1",
                request_hash="a" * 64,
                run_id="run_1",
                user_message=user_message("question"),
                lease_seconds=5,
            )
        ).turn
        now[0] += timedelta(seconds=6)
        events = _ControlledEventLog(block_turn_id=turn.turn_id)
        coordinator = InMemoryChatExpirationCoordinator(
            conversations=conversations,
            events=events,
        )
        expiry = asyncio.create_task(coordinator.expire_due(limit=1))
        await events.entered.wait()
        next_claim = asyncio.create_task(
            conversations.claim_turn(
                session_id=turn.session_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key="request-2",
                request_hash="b" * 64,
                run_id="run_2",
                user_message=user_message("next"),
                lease_seconds=5,
            )
        )
        await asyncio.sleep(0)
        assert not next_claim.done()

        events.release.set()
        expired = await expiry
        claimed = await next_claim

        assert [item.turn_id for item in expired] == [turn.turn_id]
        assert claimed.newly_claimed is True
        replayed = await events.read(turn.session_id)
        assert [event.event_type for event in replayed] == ["ChatTurnExpired"]

    asyncio.run(scenario())


def test_later_event_failure_does_not_leave_a_naked_terminal_turn() -> None:
    async def scenario() -> None:
        now = [datetime(2026, 7, 28, tzinfo=UTC)]
        conversations = InMemoryConversationStore(clock=lambda: now[0])
        for session_id in ("session_first", "session_second"):
            await conversations.create_session(
                session_id=session_id,
                tenant_id=TENANT,
                owner_id=OWNER,
            )
        first = (
            await conversations.claim_turn(
                session_id="session_first",
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key="request-first",
                request_hash="a" * 64,
                run_id="run_first",
                user_message=user_message("first"),
                lease_seconds=5,
            )
        ).turn
        now[0] += timedelta(seconds=1)
        second = (
            await conversations.claim_turn(
                session_id="session_second",
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key="request-second",
                request_hash="b" * 64,
                run_id="run_second",
                user_message=user_message("second"),
                lease_seconds=5,
            )
        ).turn
        now[0] += timedelta(seconds=6)
        events = _ControlledEventLog(fail_turn_ids=frozenset({second.turn_id}))
        coordinator = InMemoryChatExpirationCoordinator(
            conversations=conversations,
            events=events,
        )

        expired = await coordinator.expire_due(limit=2)
        first_stored = (
            await conversations.claim_turn(
                session_id=first.session_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key=first.idempotency_key,
                request_hash=first.request_hash,
                run_id=first.run_id,
                user_message=user_message("ignored"),
                lease_seconds=5,
            )
        ).turn
        second_stored = (
            await conversations.claim_turn(
                session_id=second.session_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key=second.idempotency_key,
                request_hash=second.request_hash,
                run_id=second.run_id,
                user_message=user_message("ignored"),
                lease_seconds=5,
            )
        ).turn

        assert [turn.turn_id for turn in expired] == [first.turn_id]
        assert first_stored.status == "failed"
        assert second_stored.status == "running"
        assert second_stored.failure_outcome is None
        assert [event.event_type for event in await events.read(first.session_id)] == [
            "ChatTurnExpired"
        ]
        assert await events.read(second.session_id) == ()

    asyncio.run(scenario())


def test_memory_poison_cannot_starve_a_newer_turn_at_batch_size_one() -> None:
    async def scenario() -> None:
        now = [datetime(2026, 7, 28, tzinfo=UTC)]
        conversations = InMemoryConversationStore(clock=lambda: now[0])

        async def claim(suffix: str, request_hash: str) -> str:
            session_id = f"session_{suffix}"
            await conversations.create_session(
                session_id=session_id,
                tenant_id=TENANT,
                owner_id=OWNER,
            )
            return (
                await conversations.claim_turn(
                    session_id=session_id,
                    tenant_id=TENANT,
                    principal_id=OWNER,
                    idempotency_key=f"request-{suffix}",
                    request_hash=request_hash,
                    run_id=f"run_{suffix}",
                    user_message=user_message(suffix),
                    lease_seconds=5,
                )
            ).turn.turn_id

        poison_id = await claim("poison", "a" * 64)
        now[0] += timedelta(seconds=1)
        healthy_id = await claim("healthy", "b" * 64)
        now[0] += timedelta(seconds=6)
        coordinator = InMemoryChatExpirationCoordinator(
            conversations=conversations,
            events=_ControlledEventLog(fail_turn_ids=frozenset({poison_id})),
        )

        first = await coordinator.expire_due(limit=1)
        second = await coordinator.expire_due(limit=1)

        assert first == ()
        assert [turn.turn_id for turn in second] == [healthy_id]

    asyncio.run(scenario())
