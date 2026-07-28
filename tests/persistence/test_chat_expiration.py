"""PostgreSQL contracts for atomic Chat execution expiry.

These guarantees are transaction and lock guarantees.  A memory double cannot
prove that ``SKIP LOCKED`` makes progress or that an event-log write rolls back
with a Turn update, so this suite deliberately runs only against a real test
database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence import (
    PostgresChatExpirationCoordinator,
    PostgresConversationStore,
    PostgresEventLog,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import chat_turns, events
from agent_workbench.domain.events import ChatTurnExpired, RunCompleted
from agent_workbench.domain.messages import user_message
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.ports.chat_expiration import ChatExpirationCoordinator
from agent_workbench.ports.conversation_store import (
    ChatTurnBusyError,
    ChatTurnLeaseExpiredError,
    ChatTurnResult,
    StoredChatTurn,
    chat_turn_terminal_event_key,
)
from agent_workbench.ports.event_log import EventScope

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"
REQUIRED_TEST_DATABASE_SUFFIX = "_test"

TENANT = "tenant_a"
OWNER = "user_1"
REQUEST_HASH = "a" * 64


class InjectedFailure(RuntimeError):
    """A deterministic crash between two writes in the expiry transaction."""


class _FailAfterUpdate(PostgresChatExpirationCoordinator):
    async def _after_update(
        self,
        connection: AsyncConnection,
        turn: StoredChatTurn,
    ) -> None:
        del connection, turn
        raise InjectedFailure("after update")


class _FailAfterEvent(PostgresChatExpirationCoordinator):
    async def _after_event(
        self,
        connection: AsyncConnection,
        turn: StoredChatTurn,
    ) -> None:
        del connection, turn
        raise InjectedFailure("after event")


class _BarrierAfterUpdate(PostgresChatExpirationCoordinator):
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        locked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(engine)
        self._locked = locked
        self._release = release

    async def _after_update(
        self,
        connection: AsyncConnection,
        turn: StoredChatTurn,
    ) -> None:
        del connection, turn
        self._locked.set()
        await self._release.wait()


class _BarrierBeforeTurnLockStore(PostgresConversationStore):
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(engine)
        self._entered = entered
        self._release = release

    async def _locked_turn(
        self,
        connection: AsyncConnection,
        *,
        session_id: str,
        turn_id: str,
    ) -> StoredChatTurn:
        self._entered.set()
        await self._release.wait()
        return await super()._locked_turn(
            connection,
            session_id=session_id,
            turn_id=turn_id,
        )


@dataclass(frozen=True, slots=True)
class Harness:
    engine: AsyncEngine
    conversations: PostgresConversationStore
    expiration: PostgresChatExpirationCoordinator
    events: PostgresEventLog


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    database = dsn.rsplit("/", maxsplit=1)[-1].split("?", maxsplit=1)[0]
    if not database.endswith(REQUIRED_TEST_DATABASE_SUFFIX):
        raise AssertionError(
            f"{TEST_DSN_ENV_VAR} must name a database ending in "
            f"{REQUIRED_TEST_DATABASE_SUFFIX!r}; this suite truncates it"
        )
    return dsn


def _run(scenario: Callable[[Harness], Awaitable[Any]]) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "TRUNCATE events, event_streams, chat_turns, messages, "
                        "conversation_sessions CASCADE"
                    )
                )
            harness = Harness(
                engine=engine,
                conversations=PostgresConversationStore(engine),
                expiration=PostgresChatExpirationCoordinator(engine),
                events=PostgresEventLog(engine),
            )
            return await scenario(harness)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


async def _claim(
    harness: Harness,
    *,
    suffix: str,
) -> StoredChatTurn:
    session_id = f"session_{suffix}"
    await harness.conversations.create_session(
        session_id=session_id,
        tenant_id=TENANT,
        owner_id=OWNER,
    )
    claimed = await harness.conversations.claim_turn(
        session_id=session_id,
        tenant_id=TENANT,
        principal_id=OWNER,
        idempotency_key=f"request-{suffix}",
        request_hash=REQUEST_HASH,
        run_id=f"run_{suffix}",
        user_message=user_message(f"question {suffix}"),
        lease_seconds=300,
    )
    return claimed.turn


async def _make_due(
    harness: Harness,
    turn: StoredChatTurn,
    *,
    age_seconds: int = 1,
) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(
            chat_turns.update()
            .where(chat_turns.c.turn_id == turn.turn_id)
            .values(
                lease_until=(
                    func.statement_timestamp()
                    - func.make_interval(0, 0, 0, 0, 0, 0, age_seconds)
                )
            )
        )


async def _stored_row(harness: Harness, turn_id: str) -> Any:
    async with harness.engine.connect() as connection:
        return (
            (
                await connection.execute(
                    select(chat_turns).where(chat_turns.c.turn_id == turn_id)
                )
            )
            .mappings()
            .one()
        )


def test_postgres_coordinator_satisfies_the_expiration_port() -> None:
    async def scenario(harness: Harness) -> bool:
        return isinstance(harness.expiration, ChatExpirationCoordinator)

    assert _run(scenario) is True


def test_due_turn_and_terminal_event_commit_together_in_stable_batches() -> None:
    async def scenario(harness: Harness) -> None:
        younger = await _claim(harness, suffix="younger")
        older = await _claim(harness, suffix="older")
        current = await _claim(harness, suffix="current")
        await _make_due(harness, younger, age_seconds=10)
        await _make_due(harness, older, age_seconds=20)

        first = await harness.expiration.expire_due(limit=1)
        second = await harness.expiration.expire_due(limit=10)
        empty = await harness.expiration.expire_due(limit=10)

        assert [turn.turn_id for turn in first] == [older.turn_id]
        assert [turn.turn_id for turn in second] == [younger.turn_id]
        assert empty == ()
        for terminal in (*first, *second):
            assert terminal.status == "failed"
            assert terminal.lease_until is None
            assert terminal.result is None
            assert terminal.assistant_message_id is None
            assert terminal.failure_outcome is not None
            assert terminal.failure_outcome.agent_run_id == terminal.run_id
            assert terminal.failure_outcome.stop_reason == "deadline"
            assert terminal.failure_outcome.error is not None
            assert terminal.failure_outcome.error.code == "stale_execution"
            assert terminal.failure_outcome.error.retryable is False

            replayed = await harness.events.read(terminal.session_id)
            assert len(replayed) == 1
            assert replayed[0].run_id == terminal.run_id
            assert replayed[0].event_type == "ChatTurnExpired"
            assert replayed[0].payload == ChatTurnExpired(turn_id=terminal.turn_id)

        current_row = await _stored_row(harness, current.turn_id)
        assert current_row["status"] == "running"
        assert current_row["lease_until"] is not None

        async with harness.engine.connect() as connection:
            event_keys = (
                await connection.execute(
                    select(events.c.event_key).order_by(events.c.event_key)
                )
            ).scalars()
            assert list(event_keys) == sorted(
                [
                    chat_turn_terminal_event_key(older.turn_id),
                    chat_turn_terminal_event_key(younger.turn_id),
                ]
            )

    _run(scenario)


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_expiration_limit_is_rejected(limit: int) -> None:
    async def scenario(harness: Harness) -> None:
        with pytest.raises(ValueError, match="limit"):
            await harness.expiration.expire_due(limit=limit)

    _run(scenario)


@pytest.mark.parametrize(
    "coordinator_type",
    [_FailAfterUpdate, _FailAfterEvent],
    ids=["after-update", "after-event"],
)
def test_fault_seams_roll_back_both_the_turn_and_event(
    coordinator_type: type[PostgresChatExpirationCoordinator],
) -> None:
    async def scenario(harness: Harness) -> None:
        turn = await _claim(harness, suffix="rollback")
        await _make_due(harness, turn)

        coordinator = coordinator_type(harness.engine)
        assert await coordinator.expire_due(limit=1) == ()

        rolled_back = await _stored_row(harness, turn.turn_id)
        assert rolled_back["status"] == "running"
        assert rolled_back["lease_until"] is not None
        assert rolled_back["failure_outcome"] is None
        assert await harness.events.read(turn.session_id) == ()

        recovered = await harness.expiration.expire_due(limit=1)
        assert [item.turn_id for item in recovered] == [turn.turn_id]
        replayed = await harness.events.read(turn.session_id)
        assert len(replayed) == 1
        assert replayed[0].event_type == "ChatTurnExpired"

    _run(scenario)


def test_poison_candidate_cannot_starve_a_later_turn_when_batch_size_is_one() -> None:
    async def scenario(harness: Harness) -> None:
        poison = await _claim(harness, suffix="poison")
        healthy = await _claim(harness, suffix="healthy")
        await _make_due(harness, poison, age_seconds=20)
        await _make_due(harness, healthy, age_seconds=10)
        await harness.events.append(
            EventScope(stream_id=poison.session_id, run_id=poison.run_id),
            RunCompleted(stop_reason="completed"),
            event_key=chat_turn_terminal_event_key(poison.turn_id),
        )

        first = await harness.expiration.expire_due(limit=1)
        second = await harness.expiration.expire_due(limit=1)

        assert first == ()
        assert [turn.turn_id for turn in second] == [healthy.turn_id]
        poison_row = await _stored_row(harness, poison.turn_id)
        assert poison_row["status"] == "running"
        assert poison_row["failure_outcome"] is None
        healthy_events = await harness.events.read(healthy.session_id)
        assert [event.event_type for event in healthy_events] == ["ChatTurnExpired"]

    _run(scenario)


def test_preexisting_identical_terminal_event_converges_the_turn() -> None:
    async def scenario(harness: Harness) -> None:
        turn = await _claim(harness, suffix="identical-event")
        await _make_due(harness, turn)
        await harness.events.append(
            EventScope(stream_id=turn.session_id, run_id=turn.run_id),
            ChatTurnExpired(turn_id=turn.turn_id),
            event_key=chat_turn_terminal_event_key(turn.turn_id),
        )

        expired = await harness.expiration.expire_due(limit=1)

        assert [item.turn_id for item in expired] == [turn.turn_id]
        replayed = await harness.events.read(turn.session_id)
        assert [event.event_type for event in replayed] == ["ChatTurnExpired"]

    _run(scenario)


def test_skip_locked_does_not_wait_for_another_expiry_worker() -> None:
    async def scenario(harness: Harness) -> None:
        locked_turn = await _claim(harness, suffix="locked")
        available_turn = await _claim(harness, suffix="available")
        await _make_due(harness, locked_turn, age_seconds=20)
        await _make_due(harness, available_turn, age_seconds=10)

        locked = asyncio.Event()
        release = asyncio.Event()

        async def hold_lock() -> None:
            async with harness.engine.begin() as connection:
                await connection.execute(
                    select(chat_turns.c.turn_id)
                    .where(chat_turns.c.turn_id == locked_turn.turn_id)
                    .with_for_update()
                )
                locked.set()
                await release.wait()

        holder = asyncio.create_task(hold_lock())
        await asyncio.wait_for(locked.wait(), timeout=2)
        try:
            expired = await asyncio.wait_for(
                harness.expiration.expire_due(limit=10),
                timeout=2,
            )
            assert [turn.turn_id for turn in expired] == [available_turn.turn_id]
        finally:
            release.set()
            await holder

        formerly_locked = await harness.expiration.expire_due(limit=10)
        assert [turn.turn_id for turn in formerly_locked] == [locked_turn.turn_id]

    _run(scenario)


def test_two_reapers_converge_one_due_turn_to_one_terminal_event() -> None:
    async def scenario(harness: Harness) -> None:
        turn = await _claim(harness, suffix="race")
        await _make_due(harness, turn)
        locked = asyncio.Event()
        release = asyncio.Event()
        first_coordinator = _BarrierAfterUpdate(
            harness.engine,
            locked=locked,
            release=release,
        )
        first_task = asyncio.create_task(first_coordinator.expire_due(limit=10))
        try:
            await asyncio.wait_for(locked.wait(), timeout=2)
            second = await asyncio.wait_for(
                harness.expiration.expire_due(limit=10),
                timeout=2,
            )
            assert second == ()
        finally:
            release.set()
        first = await first_task

        assert [item.turn_id for item in first] == [turn.turn_id]
        replayed = await harness.events.read(turn.session_id)
        assert [event.event_type for event in replayed] == ["ChatTurnExpired"]

    _run(scenario)


def test_prepare_started_before_reaper_observes_the_durable_expiry_fact() -> None:
    async def scenario(harness: Harness) -> None:
        turn = await _claim(harness, suffix="prepare-race")
        await _make_due(harness, turn)
        entered = asyncio.Event()
        release = asyncio.Event()
        blocked_store = _BarrierBeforeTurnLockStore(
            harness.engine,
            entered=entered,
            release=release,
        )
        result = ChatTurnResult(
            outcome=AgentOutcome(
                agent_run_id=turn.run_id,
                status="completed",
                stop_reason="completed",
                output_text="late answer",
            ),
            answer="late answer",
            authorized_revisions=(),
        )
        prepare = asyncio.create_task(
            blocked_store.prepare_release(
                session_id=turn.session_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_id=turn.turn_id,
                result=result,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        expired = await harness.expiration.expire_due(limit=1)
        release.set()

        with pytest.raises(ChatTurnLeaseExpiredError) as late:
            await prepare

        assert [item.turn_id for item in expired] == [turn.turn_id]
        assert late.value.outcome == expired[0].failure_outcome
        same_key = await harness.conversations.claim_turn(
            session_id=turn.session_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            idempotency_key=turn.idempotency_key,
            request_hash=turn.request_hash,
            run_id=turn.run_id,
            user_message=user_message("ignored retry body"),
            lease_seconds=300,
        )
        assert same_key.turn.failure_outcome == late.value.outcome

    _run(scenario)


def test_runtime_completion_and_chat_expiry_are_distinct_terminal_facts() -> None:
    async def scenario(harness: Harness) -> None:
        turn = await _claim(harness, suffix="runtime-completed")
        await harness.events.append(
            EventScope(stream_id=turn.session_id, run_id=turn.run_id),
            RunCompleted(stop_reason="completed"),
        )
        await _make_due(harness, turn)

        await harness.expiration.expire_due(limit=10)
        replayed = await harness.events.read(turn.session_id)

        assert [event.event_type for event in replayed] == [
            "RunCompleted",
            "ChatTurnExpired",
        ]
        assert "RunFailed" not in {event.event_type for event in replayed}

    _run(scenario)


def test_new_claim_waits_for_atomic_expiry_then_session_becomes_available() -> None:
    async def scenario(harness: Harness) -> None:
        turn = await _claim(harness, suffix="claim")
        await _make_due(harness, turn)

        with pytest.raises(ChatTurnBusyError, match="unfinished"):
            await harness.conversations.claim_turn(
                session_id=turn.session_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                idempotency_key="request-next",
                request_hash="b" * 64,
                run_id="run_next",
                user_message=user_message("next question"),
                lease_seconds=300,
            )

        await harness.expiration.expire_due(limit=10)
        next_claim = await harness.conversations.claim_turn(
            session_id=turn.session_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            idempotency_key="request-next",
            request_hash="b" * 64,
            run_id="run_next",
            user_message=user_message("next question"),
            lease_seconds=300,
        )
        assert next_claim.newly_claimed is True
        assert next_claim.turn.status == "running"

    _run(scenario)
