"""In-memory conversation store.

Sequences are assigned per session and never reused, so a later PostgreSQL
implementation can keep the same ordering guarantee with a unique constraint
instead of a lock. Tenant scoping is enforced on every operation, including the
append path: a session that cannot be read must not be writable either.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_id, new_message_id
from agent_workbench.domain.messages import Message, assistant_message
from agent_workbench.domain.runs import AgentOutcome, stale_execution_outcome
from agent_workbench.ports.conversation_store import (
    ChatTurnBusyError,
    ChatTurnClaim,
    ChatTurnConflictError,
    ChatTurnLeaseExpiredError,
    ChatTurnResult,
    ConversationSession,
    PendingChatRelease,
    SessionMode,
    StoredChatTurn,
    StoredMessage,
)


class InMemoryConversationStore:
    """Chat sessions and their messages, held in process memory."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions: dict[str, ConversationSession] = {}
        self._messages: dict[str, list[StoredMessage]] = {}
        self._turns: dict[str, StoredChatTurn] = {}
        self._turn_ids_by_key: dict[tuple[str, str], str] = {}
        self._turn_ids_by_run_id: dict[str, str] = {}
        self._active_turn_ids: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    async def create_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        owner_id: str,
        title: str | None = None,
        mode: SessionMode = "chat",
    ) -> ConversationSession:
        session = ConversationSession(
            session_id=session_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            title=title,
            mode=mode,
        )
        async with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"session already exists: {session_id}")
            self._sessions[session_id] = session
            self._messages[session_id] = []
        return session

    async def append(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        async with self._lock:
            self._require_session(
                session_id=session_id, tenant_id=tenant_id, principal_id=principal_id
            )
            return self._append_messages(session_id=session_id, messages=messages)

    async def history(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        limit: int | None = None,
        mode: SessionMode | None = None,
    ) -> tuple[StoredMessage, ...]:
        async with self._lock:
            self._require_session(
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
                mode=mode,
            )
            stored = tuple(self._messages[session_id])
        return stored if limit is None else stored[:limit]

    async def claim_turn(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        run_id: str,
        user_message: Message,
        lease_seconds: int,
    ) -> ChatTurnClaim:
        """Atomically reserve one turn, its history, and its user message."""

        if lease_seconds < 1:
            raise ValueError("chat turn lease_seconds must be positive")
        async with self._lock:
            self._require_session(
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            key = (session_id, idempotency_key)
            existing_turn_id = self._turn_ids_by_key.get(key)
            if existing_turn_id is not None:
                existing = self._turns[existing_turn_id]
                if existing.request_hash != request_hash:
                    raise ChatTurnConflictError("chat turn idempotency conflict")
                return ChatTurnClaim(
                    turn=existing,
                    history_before=self._history_before(existing),
                    newly_claimed=False,
                )

            if session_id in self._active_turn_ids:
                raise ChatTurnBusyError(
                    "conversation already has an unfinished chat turn"
                )
            if run_id in self._turn_ids_by_run_id:
                raise ChatTurnConflictError("chat turn run id conflict")
            if user_message.role != "user":
                raise ValueError("claim_turn requires a user message")

            history_before = tuple(self._messages[session_id])
            stored_user = StoredMessage(
                message_id=new_message_id(),
                session_id=session_id,
                sequence=len(history_before) + 1,
                message=user_message,
            )
            turn = StoredChatTurn(
                turn_id=new_id("turn"),
                session_id=session_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                run_id=run_id,
                status="running",
                lease_until=self._clock() + timedelta(seconds=lease_seconds),
                user_message_id=stored_user.message_id,
            )

            self._messages[session_id].append(stored_user)
            self._turns[turn.turn_id] = turn
            self._turn_ids_by_key[key] = turn.turn_id
            self._turn_ids_by_run_id[run_id] = turn.turn_id
            self._active_turn_ids[session_id] = turn.turn_id
            return ChatTurnClaim(
                turn=turn,
                history_before=history_before,
                newly_claimed=True,
            )

    async def prepare_release(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        result: ChatTurnResult,
    ) -> StoredChatTurn:
        """Save a completed answer without exposing it to history yet."""

        async with self._lock:
            self._require_session(
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            turn = self._require_turn(turn_id=turn_id, session_id=session_id)
            if _is_expired_terminal(turn):
                assert turn.failure_outcome is not None
                raise ChatTurnLeaseExpiredError(turn.failure_outcome)
            if turn.status in {"release_pending", "committed", "withheld"}:
                if turn.result == result:
                    return turn
                raise ChatTurnConflictError("chat turn release result conflict")
            if turn.status != "running":
                raise ChatTurnConflictError(
                    "chat turn cannot prepare release from its current state"
                )
            if turn.lease_until is None:  # pragma: no cover - model invariant
                raise ChatTurnConflictError("running chat turn has no execution lease")
            if self._clock() >= turn.lease_until:
                raise ChatTurnLeaseExpiredError(stale_execution_outcome(turn.run_id))

            prepared = self._updated_turn(
                turn,
                status="release_pending",
                lease_until=None,
                result=result,
            )
            self._turns[turn_id] = prepared
            return prepared

    async def mark_released(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        withheld_result: ChatTurnResult | None = None,
    ) -> StoredChatTurn:
        """Publish the prepared fact as committed or withheld exactly once."""

        async with self._lock:
            self._require_session(
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            turn = self._require_turn(turn_id=turn_id, session_id=session_id)
            if turn.status in {"committed", "withheld"}:
                if withheld_result is not None and turn.result != withheld_result:
                    raise ChatTurnConflictError(
                        "chat turn withheld result conflicts with its terminal fact"
                    )
                return turn
            if turn.status != "release_pending" or turn.result is None:
                raise ChatTurnConflictError(
                    "chat turn cannot be released from its current state"
                )
            result = (
                self._validated_withheld_result(turn, withheld_result)
                if withheld_result is not None
                else turn.result
            )

            stored_assistant = StoredMessage(
                message_id=new_message_id(),
                session_id=session_id,
                sequence=len(self._messages[session_id]) + 1,
                message=assistant_message(text=result.answer),
            )
            released = self._updated_turn(
                turn,
                status="withheld" if result.withheld else "committed",
                assistant_message_id=stored_assistant.message_id,
                result=result,
            )
            self._messages[session_id].append(stored_assistant)
            self._turns[turn_id] = released
            self._active_turn_ids.pop(session_id, None)
            return released

    @staticmethod
    def _validated_withheld_result(
        turn: StoredChatTurn,
        result: ChatTurnResult,
    ) -> ChatTurnResult:
        if not result.withheld:
            raise ChatTurnConflictError(
                "a release result override must be a safe withheld result"
            )
        if result.outcome.agent_run_id != turn.run_id:
            raise ChatTurnConflictError(
                "a withheld result must belong to the chat turn's run"
            )
        return result

    async def finish_failed(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        outcome: AgentOutcome,
    ) -> StoredChatTurn:
        """Finish a run failure without ever creating an assistant message."""

        async with self._lock:
            self._require_session(
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            turn = self._require_turn(turn_id=turn_id, session_id=session_id)
            if outcome.status not in {"failed", "cancelled"}:
                raise ValueError("finish_failed requires a failed or cancelled outcome")
            if turn.status in {"failed", "cancelled"}:
                if turn.failure_outcome == outcome:
                    return turn
                raise ChatTurnConflictError("chat turn failure outcome conflict")
            if turn.status != "running":
                raise ChatTurnConflictError(
                    "chat turn cannot fail from its current state"
                )
            if turn.lease_until is None:  # pragma: no cover - model invariant
                raise ChatTurnConflictError("running chat turn has no execution lease")
            if self._clock() >= turn.lease_until:
                raise ChatTurnLeaseExpiredError(stale_execution_outcome(turn.run_id))
            _reject_expiration_outcome(outcome)

            failed = self._updated_turn(
                turn,
                status=outcome.status,
                lease_until=None,
                failure_outcome=outcome,
            )
            self._turns[turn_id] = failed
            self._active_turn_ids.pop(session_id, None)
            return failed

    async def finish_running_if_current(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        turn_id: str,
        outcome: AgentOutcome,
    ) -> StoredChatTurn:
        """Close only the still-running Turn; never overwrite later facts."""

        if outcome.status not in {"failed", "cancelled"}:
            raise ValueError(
                "finish_running_if_current requires a failed or cancelled outcome"
            )
        async with self._lock:
            self._require_session(
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            turn = self._require_turn(turn_id=turn_id, session_id=session_id)
            if outcome.agent_run_id != turn.run_id:
                raise ChatTurnConflictError(
                    "chat turn cleanup outcome belongs to another run"
                )
            if turn.status != "running":
                return turn
            if turn.lease_until is None:  # pragma: no cover - model invariant
                raise ChatTurnConflictError("running chat turn has no execution lease")
            if self._clock() >= turn.lease_until:
                raise ChatTurnLeaseExpiredError(stale_execution_outcome(turn.run_id))
            _reject_expiration_outcome(outcome)
            finished = self._updated_turn(
                turn,
                status=outcome.status,
                lease_until=None,
                failure_outcome=outcome,
            )
            self._turns[turn_id] = finished
            self._active_turn_ids.pop(session_id, None)
            return finished

    async def expire_due_for_coordinator(
        self,
        *,
        limit: int,
        publish: Callable[[StoredChatTurn], Awaitable[bool]],
        excluding: frozenset[str] = frozenset(),
    ) -> tuple[StoredChatTurn, ...]:
        """Publish then close due Turns while claims remain serialized.

        ``publish`` returns false after an isolated event failure. In that
        case the candidate stays running, so the in-memory double never
        creates the naked terminal row that production forbids.
        """

        if limit < 1:
            raise ValueError("reaper limit must be positive")
        async with self._lock:
            now = self._clock()
            expired = sorted(
                (
                    turn
                    for turn in self._turns.values()
                    if turn.status == "running"
                    and turn.lease_until is not None
                    and turn.lease_until <= now
                    and turn.turn_id not in excluding
                ),
                key=lambda turn: (turn.lease_until, turn.turn_id),
            )[:limit]
            reaped: list[StoredChatTurn] = []
            for turn in expired:
                terminal = self._updated_turn(
                    turn,
                    status="failed",
                    lease_until=None,
                    failure_outcome=stale_execution_outcome(turn.run_id),
                )
                if not await publish(terminal):
                    continue
                self._turns[terminal.turn_id] = terminal
                self._active_turn_ids.pop(terminal.session_id, None)
                reaped.append(terminal)
            return tuple(reaped)

    async def list_release_pending(
        self,
        *,
        limit: int,
    ) -> tuple[PendingChatRelease, ...]:
        """Snapshot prepared Turns in a deterministic recovery order."""

        if limit < 1:
            raise ValueError("pending release limit must be positive")
        async with self._lock:
            turns = sorted(
                (
                    turn
                    for turn in self._turns.values()
                    if turn.status == "release_pending"
                ),
                key=lambda turn: turn.turn_id,
            )[:limit]
            return tuple(
                PendingChatRelease(
                    turn=turn,
                    tenant_id=self._sessions[turn.session_id].tenant_id,
                    principal_id=self._sessions[turn.session_id].owner_id,
                )
                for turn in turns
            )

    def _append_messages(
        self,
        *,
        session_id: str,
        messages: tuple[Message, ...],
    ) -> tuple[StoredMessage, ...]:
        stored = self._messages[session_id]
        appended = tuple(
            StoredMessage(
                message_id=new_message_id(),
                session_id=session_id,
                sequence=len(stored) + offset + 1,
                message=message,
            )
            for offset, message in enumerate(messages)
        )
        stored.extend(appended)
        return appended

    def _history_before(self, turn: StoredChatTurn) -> tuple[StoredMessage, ...]:
        stored = self._messages[turn.session_id]
        for index, message in enumerate(stored):
            if message.message_id == turn.user_message_id:
                return tuple(stored[:index])
        raise ChatTurnConflictError("chat turn user message is missing")

    def _require_turn(self, *, turn_id: str, session_id: str) -> StoredChatTurn:
        turn = self._turns.get(turn_id)
        if turn is None or turn.session_id != session_id:
            raise NotFoundError("chat turn not found")
        return turn

    @staticmethod
    def _updated_turn(
        turn: StoredChatTurn,
        **updates: Any,
    ) -> StoredChatTurn:
        payload = turn.model_dump(mode="python")
        payload.update(updates)
        return StoredChatTurn.model_validate(payload)

    def _require_session(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        mode: SessionMode | None = None,
    ) -> ConversationSession:
        session = self._sessions.get(session_id)
        # A wrong tenant, a wrong principal, a wrong mode and a missing session
        # answer identically: any difference would confirm somebody else's
        # exists. The mode joins that list rather than raising its own error
        # because "this id is a code session" is exactly the kind of detail a
        # caller probing the Chat API with guessed ids would want.
        if (
            session is None
            or session.tenant_id != tenant_id
            or session.owner_id != principal_id
            or (mode is not None and session.mode != mode)
        ):
            raise NotFoundError("conversation session not found")
        return session


__all__ = ["InMemoryConversationStore"]


def _reject_expiration_outcome(outcome: AgentOutcome) -> None:
    if outcome.error is not None and outcome.error.code == "stale_execution":
        raise ValueError("stale_execution is reserved for ChatExpirationCoordinator")


def _is_expired_terminal(turn: StoredChatTurn) -> bool:
    return (
        turn.status == "failed"
        and turn.failure_outcome is not None
        and turn.failure_outcome.error is not None
        and turn.failure_outcome.error.code == "stale_execution"
    )
