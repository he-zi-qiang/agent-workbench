"""One chat turn: claim it, produce an answer, re-check the evidence, release.

What produces the answer is no longer this module's business. The deployment
picks its RAG shape, while each request explicitly chooses Direct or RAG, and
both decisions live behind ``TurnExecution`` in ``chat_execution``. The rest of
a turn is not worth writing twice: claiming, recovery and release remain one
lifecycle whichever execution answered.

Everything below the seam is the same either way, and most of it is failure
handling: an idempotent claim, a lease, a request deadline, a best-effort close
after a disconnect, and the release fence.

Two things happen around the model call, and both are authorization. The
context is built from candidates already checked against PostgreSQL, and the
answer is withheld until they are checked again -- a grant can be withdrawn
while the model is still writing. An answer built on a document the asker can
no longer read must not be delivered just because it was authorized when the
retrieval ran.

Nothing in a retrieved passage is an instruction. The context is quoted into
the prompt as evidence, and the system prompt says so; a document that contains
"ignore your instructions" is a document that says that, not a document that
does it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from contextlib import suppress
from dataclasses import dataclass, field

from agent_workbench.application.answer_release import AnswerReleaseSink
from agent_workbench.application.chat_execution import (
    SYSTEM_PROMPT,
    ChatRequest,
    TurnExecution,
)
from agent_workbench.domain.context import Citation
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.messages import Message, user_message
from agent_workbench.domain.runs import (
    AgentOutcome,
)
from agent_workbench.ports.cancellation import CancellationToken, NullCancellationToken
from agent_workbench.ports.chat_release import ChatReleaseCoordinator
from agent_workbench.ports.conversation_store import (
    AuthorizedRevision,
    ChatTurnBusyError,
    ChatTurnConflictError,
    ChatTurnLeaseExpiredError,
    ChatTurnResult,
    ChatTurnStore,
    ConversationSession,
    StoredChatTurn,
    StoredMessage,
)
from agent_workbench.ports.event_log import EventSink

logger = logging.getLogger(__name__)

REFUSAL = (
    "That answer was built from a document you are no longer able to read, so "
    "it has been withheld. Ask again to search what is currently available."
)


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """What one question produced."""

    turn_id: str
    answer: str
    citations: tuple[Citation, ...]
    outcome: AgentOutcome
    withheld: bool = False
    #: Whether this answer rested on retrieved evidence (ADR-018).
    #:
    #: Travels to the client because the routed shape can produce either kind
    #: within one conversation, so a reader cannot infer it from the session.
    #: Nor from the citation list: an answer that retrieved and cited nothing
    #: is a different claim from one that never retrieved, and only the first
    #: went through the release fence.
    grounded: bool = True


class ChatExecutionError(RuntimeError):
    """The model-tool run ended without a publishable answer."""

    def __init__(self, outcome: AgentOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            "the chat run did not complete"
            if outcome.status == "failed"
            else "the chat run was cancelled"
        )


@dataclass(frozen=True, slots=True)
class ChatService:
    """Claim a turn, have it answered, re-check the evidence, release it."""

    # Which execution answers. It may be one concrete shape or the per-request
    # selector; the lifecycle deliberately does not branch on that distinction.
    execution: TurnExecution
    conversations: ChatTurnStore
    releaser: ChatReleaseCoordinator
    request_timeout_seconds: float
    orphan_grace_seconds: float
    _cleanup_tasks: set[asyncio.Task[StoredChatTurn | None]] = field(
        default_factory=lambda: set[asyncio.Task[StoredChatTurn | None]](),
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.request_timeout_seconds <= 0:
            raise ValueError("chat request timeout must be positive")
        if self.orphan_grace_seconds <= 0:
            raise ValueError("chat orphan grace must be positive")

    async def ask(
        self,
        request: ChatRequest,
        sink: EventSink,
        cancellation: CancellationToken | None = None,
    ) -> ChatTurn:
        """Claim, execute, authorize and release one idempotent turn."""

        if request.stream_id not in {None, request.session_id}:
            raise ValueError("a Chat event stream must be its conversation session")

        turn: StoredChatTurn | None = None
        try:
            async with asyncio.timeout(self.request_timeout_seconds):
                claim = await self.conversations.claim_turn(
                    session_id=request.session_id,
                    tenant_id=request.tenant_id,
                    principal_id=request.principal_id,
                    idempotency_key=request.idempotency_key,
                    request_hash=_request_hash(request),
                    run_id=request.run_id,
                    user_message=user_message(request.question),
                    lease_seconds=max(
                        1,
                        math.ceil(
                            self.request_timeout_seconds + self.orphan_grace_seconds
                        ),
                    ),
                )
                turn = claim.turn
                if turn.run_id != request.run_id:
                    raise ChatTurnConflictError(
                        "the idempotent chat turn belongs to a different run id"
                    )

                if not claim.newly_claimed:
                    if turn.status in {"committed", "withheld"}:
                        return _public_turn(turn)
                    if turn.status in {"failed", "cancelled"}:
                        if (
                            turn.failure_outcome is None
                        ):  # pragma: no cover - model invariant
                            raise ChatTurnConflictError(
                                "terminal chat turn has no outcome"
                            )
                        raise ChatExecutionError(turn.failure_outcome)
                    if turn.status == "running":
                        raise ChatTurnBusyError(
                            "the idempotent chat turn is still running"
                        )

                if not claim.newly_claimed:
                    released = await self._release(turn, request, sink)
                    return _public_turn(released)
                return await self._execute_new_turn(
                    request,
                    sink,
                    cancellation=(
                        cancellation
                        if cancellation is not None
                        else NullCancellationToken()
                    ),
                    turn=turn,
                    history=tuple(record.message for record in claim.history_before),
                )
        except TimeoutError:
            outcome = _deadline_outcome(request.run_id)
            if turn is not None:
                try:
                    settled = await self._finish_running_best_effort(
                        request=request,
                        turn=turn,
                        outcome=outcome,
                    )
                except ChatTurnLeaseExpiredError as exc:
                    settled = None
                    outcome = exc.outcome
                if settled is not None:
                    if settled.status in {"committed", "withheld"}:
                        return _public_turn(settled)
                    if (
                        settled.status in {"failed", "cancelled"}
                        and settled.failure_outcome is not None
                    ):
                        outcome = settled.failure_outcome
            raise ChatExecutionError(outcome) from None
        except asyncio.CancelledError:
            if turn is not None:
                # Cancellation remains cancellation to the disconnected
                # caller; the coordinator will publish an elapsed lease.
                with suppress(ChatTurnLeaseExpiredError):
                    await self._finish_running_best_effort(
                        request=request,
                        turn=turn,
                        outcome=_cancelled_outcome(request.run_id),
                    )
            raise
        except ChatTurnLeaseExpiredError as exc:
            # Expiry has one durable writer: ChatExpirationCoordinator commits
            # both the failed Turn and ChatTurnExpired. Request cleanup must
            # not create a naked stale_execution row before that transaction.
            raise ChatExecutionError(exc.outcome) from None
        except Exception as exc:
            outcome = (
                exc.outcome
                if isinstance(exc, ChatExecutionError)
                else _exception_outcome(request.run_id, exc)
            )
            if turn is not None:
                try:
                    settled = await self._finish_running_best_effort(
                        request=request,
                        turn=turn,
                        outcome=outcome,
                    )
                except ChatTurnLeaseExpiredError as expired:
                    raise ChatExecutionError(expired.outcome) from None
                if settled is not None:
                    if settled.status in {"committed", "withheld"}:
                        return _public_turn(settled)
                    if (
                        settled.status in {"failed", "cancelled"}
                        and settled.failure_outcome is not None
                    ):
                        raise ChatExecutionError(settled.failure_outcome) from None
            raise

    async def _execute_new_turn(
        self,
        request: ChatRequest,
        sink: EventSink,
        *,
        cancellation: CancellationToken,
        turn: StoredChatTurn,
        history: tuple[Message, ...],
    ) -> ChatTurn:
        # Asked before the run, because the wrapper it configures is what the
        # run writes through. Asked of the execution rather than decided here:
        # whether a turn can end in `AnswerWithheld` is a property of the shape
        # that produces it, and this service would be guessing.
        live_text = self.execution.live_text_policy(request)
        produced = await self.execution.produce(
            request,
            history=history,
            # Wrapped here rather than by each shape: withholding an answer is
            # the turn's business, and a shape that forgot the wrapper would
            # publish one before the fence ran.
            sink=AnswerReleaseSink(sink, live_text=live_text),
            cancellation=cancellation,
        )
        outcome = produced.outcome

        if live_text == "provisional" and produced.authorized_revisions:
            # The shape said its answer rests on nothing that can be revoked,
            # and then returned revisions the fence would have to re-check.
            # Text has already been streamed under the first claim, so there is
            # no safe way to publish under the second: fail the turn.
            #
            # Unreachable through the shapes in this module -- the only
            # provisional one hardcodes an empty tuple on both of its returns
            # -- and kept anyway, because what it guards is a *future* shape
            # declaring itself provisional by mistake. Stated plainly rather
            # than dressed up as coverage: the test that exercises it supplies
            # a stub execution, and no production path reaches it.
            await self.conversations.finish_failed(
                session_id=request.session_id,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                turn_id=turn.turn_id,
                outcome=outcome,
            )
            raise RuntimeError(
                "a shape that streams provisional text returned authorized "
                "revisions, which the release fence would have to re-check"
            )

        if outcome.status != "completed":
            await self.conversations.finish_failed(
                session_id=request.session_id,
                tenant_id=request.tenant_id,
                principal_id=request.principal_id,
                turn_id=turn.turn_id,
                outcome=outcome,
            )
            raise ChatExecutionError(outcome)

        answer = outcome.output_text or ""
        result = ChatTurnResult(
            outcome=outcome,
            answer=answer,
            authorized_revisions=tuple(
                AuthorizedRevision(
                    document_id=document_id,
                    source_revision=source_revision,
                )
                for document_id, source_revision in produced.authorized_revisions
            ),
            citations=produced.citations,
            # Carried, never inferred. The release coordinator picks its
            # terminal event from this, and reconstructing it downstream from
            # "were there citations" would merge a retrieval turn that cited
            # nothing with a turn that never retrieved.
            grounded=produced.grounded,
        )

        prepared = await self.conversations.prepare_release(
            session_id=request.session_id,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            turn_id=turn.turn_id,
            result=result,
        )
        released = await self._release(prepared, request, sink)
        return _public_turn(released)

    async def _finish_running_best_effort(
        self,
        *,
        request: ChatRequest,
        turn: StoredChatTurn,
        outcome: AgentOutcome,
    ) -> StoredChatTurn | None:
        """Close an unfinished execution without hiding the original failure."""

        async def finish() -> StoredChatTurn | None:
            try:
                return await self.conversations.finish_running_if_current(
                    session_id=request.session_id,
                    tenant_id=request.tenant_id,
                    principal_id=request.principal_id,
                    turn_id=turn.turn_id,
                    outcome=outcome,
                )
            except ChatTurnLeaseExpiredError:
                # The caller must return this stable expiry outcome instead of
                # the earlier timeout/provider exception. The row remains
                # untouched for the atomic coordinator.
                raise
            except Exception:
                # The fixed lease is the final recovery path if the request's
                # best-effort cleanup cannot reach PostgreSQL.
                logger.exception(
                    "failed to close interrupted chat turn %s",
                    turn.turn_id,
                )
                return None

        try:
            # A second ASGI cancellation must not kill the cleanup or replace
            # the original failure being propagated to the caller.
            cleanup = asyncio.create_task(
                finish(),
                name=f"chat-turn-cleanup-{turn.turn_id}",
            )
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            logger.warning(
                "chat turn cleanup continues after repeated cancellation: %s",
                turn.turn_id,
            )
            return None

    async def drain_cleanup(self, *, timeout_seconds: float) -> None:
        """Drain detached cleanup work before its database engine is closed."""

        if timeout_seconds <= 0:
            raise ValueError("cleanup shutdown timeout must be positive")
        pending = tuple(self._cleanup_tasks)
        if not pending:
            return
        _, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

    async def history(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> tuple[StoredMessage, ...]:
        """This principal's own chat conversation, oldest first.

        The mode is fixed here rather than taken as an argument: this service
        is the Chat one, and a caller able to ask it for a code session's
        history would be reading a conversation that this service's own turn
        lifecycle never produced.
        """

        stored = await self.conversations.history(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="chat",
        )
        # The whole `StoredMessage`, not the `Message` inside it. What the turn
        # spent hangs off the outer record; unwrapping one layer here dropped
        # it, and the caller's reason for asking is that number.
        return stored

    async def sessions(
        self, *, tenant_id: str, principal_id: str, limit: int = 50
    ) -> tuple[ConversationSession, ...]:
        """This principal's chat sessions, most recently active first.

        The store holds Chat and Code in one table, so the mode is fixed here
        rather than accepted from the route. A caller asking the Chat service
        for a list can never widen that list into another product's sessions.
        """

        return await self.conversations.list_sessions(
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="chat",
            limit=limit,
        )

    async def session(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> ConversationSession:
        """One owner-visible Chat session, including older deep links.

        The recent-session list is deliberately bounded. Resolving a selected
        session through the same three-axis store gate keeps an older valid
        deep link usable without widening the sidebar query or trusting the
        identifier as authorization.
        """

        return await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="chat",
        )

    async def rename(
        self, *, session_id: str, tenant_id: str, principal_id: str, title: str
    ) -> ConversationSession:
        """Replace the user-facing name of this principal's chat session."""

        return await self.conversations.rename_session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            title=title,
            mode="chat",
        )

    async def delete(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> None:
        """Remove one chat conversation, its turns and its event stream.

        ``mode="chat"`` is fixed for the same reason ``history`` fixes it: a
        caller able to hand this one a code session id would be deleting a
        conversation this service never ran.
        """

        await self.conversations.delete_session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="chat",
        )

    async def _release(
        self,
        turn: StoredChatTurn,
        request: ChatRequest,
        sink: EventSink,
    ) -> StoredChatTurn:
        """Cross the final authorization fence and expose one durable result.

        PostgreSQL locks every cited source and commits the answer event,
        assistant message and Turn transition in the same transaction. The
        stable event key remains a defence against duplicate callers; no
        answer-bearing state becomes externally visible before that unit of
        work commits.
        """

        return await self.releaser.release(
            turn=turn,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            stream_id=request.session_id,
            run_id=request.run_id,
            refusal_text=REFUSAL,
            sink=sink,
        )


def _public_turn(turn: StoredChatTurn) -> ChatTurn:
    """Convert a released durable fact to the public application result."""

    if turn.status not in {"committed", "withheld"} or turn.result is None:
        raise ChatTurnConflictError("chat turn is not released")
    return ChatTurn(
        turn_id=turn.turn_id,
        grounded=turn.result.grounded,
        answer=turn.result.answer,
        citations=turn.result.citations,
        outcome=turn.result.outcome,
        withheld=turn.result.withheld,
    )


def _request_hash(request: ChatRequest) -> str:
    """Hash only the semantic request fields covered by the idempotency key."""

    canonical = json.dumps(
        {
            "answer_mode": request.answer_mode,
            "knowledge_base_id": request.knowledge_base_id,
            "question": request.question,
            "top_k": request.top_k,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _exception_outcome(run_id: str, exc: Exception) -> AgentOutcome:
    """Close a claimed turn after an adapter exception without leaking detail."""

    return AgentOutcome(
        agent_run_id=run_id,
        status="failed",
        stop_reason="error",
        error=ErrorInfo.from_exception(exc),
    )


def _deadline_outcome(run_id: str) -> AgentOutcome:
    return AgentOutcome(
        agent_run_id=run_id,
        status="failed",
        stop_reason="deadline",
        error=ErrorInfo(
            code="budget_exceeded",
            message="chat request deadline expired",
            retryable=False,
        ),
    )


def _cancelled_outcome(run_id: str) -> AgentOutcome:
    return AgentOutcome(
        agent_run_id=run_id,
        status="cancelled",
        stop_reason="cancelled",
    )


__all__ = [
    "REFUSAL",
    "SYSTEM_PROMPT",
    "ChatExecutionError",
    "ChatRequest",
    "ChatService",
    "ChatTurn",
]
