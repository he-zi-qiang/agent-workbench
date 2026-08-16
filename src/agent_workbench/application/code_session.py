"""A coding session: one conversation, one workspace, no coordination plane.

Code shares an identity with Chat -- a tenant, a principal, an ordered history,
the same two tables -- and shares no lifecycle with it at all. Chat publishes an
answer through a turn ledger: claim, lease, release_pending, an assistant
message that an ``AnswerCommitted`` authorised. Code writes no row in that
ledger. Its product is the files in a workspace and a report about them, and
neither of those is an answer that has to pass a fence.

What follows from that is the whole design, and every part of it is a cost
taken deliberately rather than a feature deferred:

* **One turn per session, held in this process.** A set of session ids, not a
  database row. A durable active-turn slot would need a writer that can release
  it after a crash, and that writer is the lease and the reaper -- exactly the
  machinery being declined. So the slot dies with the process, which is correct,
  because so does the turn.

* **A turn is not recoverable.** No lease to expire, no ``release_pending`` to
  finish, nothing half-written to reclaim. If this process dies mid-turn the
  turn is gone and the workspace stands at its last successful write. The user
  says the sentence again. Recorded as ``docs/known-gaps.md`` F-01.

* **The workspace pointer moves per write, not at the end.** A cancelled turn
  keeps the files it had finished, because those files are what the user was
  building. ``application/session_workspace.py`` argues that at length.

* **No answer may be published.** The sink is a ``ProcessOnlySink``, and it is
  named in the signature rather than in a comment so that handing this service
  an ordinary sink does not type-check.

The concurrency bound and the per-session slot are both fail-fast rather than
queues. A caller that waits for a slot is a caller holding a connection open to
find out it could have been told immediately, and a queue in front of a
minutes-long turn is a queue nobody can reason about the length of.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent_workbench.application.answer_release import ProcessOnlySink
from agent_workbench.application.code_approvals import ApprovalScope
from agent_workbench.application.code_prompt import CODER_SYSTEM_PROMPT
from agent_workbench.application.session_titles import title_from_instruction
from agent_workbench.application.session_workspace import SessionWorkspace
from agent_workbench.application.workspace import (
    Workspace,
    WorkspaceListing,
    WorkspaceSession,
)
from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.identifiers import Identifier, new_id
from agent_workbench.domain.messages import Message, assistant_message, user_message
from agent_workbench.domain.policies import AuthorizationEnvelope, PrincipalContext
from agent_workbench.domain.runs import (
    AgentOutcome,
    AgentRunRequest,
    RunBudget,
    TraceContext,
)
from agent_workbench.domain.tools import ToolName
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.conversation_store import (
    ConversationSession,
    ConversationStore,
)

#: What a coding session is allowed to reach. Read tools plus the two writes,
#: and nothing external: this list is also the reason no approval is currently
#: requested, since the envelope below requires one only for external and
#: destructive risks. The gate is wired regardless, so that granting such a
#: tool is a change to this tuple and not to the machinery underneath it.
CODE_TOOLS: tuple[ToolName, ...] = (
    "workspace_edit",
    "workspace_grep",
    "workspace_list",
    "workspace_read",
    "workspace_write",
)


class CodeTurnBusyError(RuntimeError):
    """This session is already running a turn."""


class CodeCapacityError(RuntimeError):
    """This process is already running as many turns as it admits."""


def new_code_session_id() -> str:
    return new_id("ses")


@dataclass(frozen=True, slots=True)
class CodeRequest:
    """One thing a user asked a coding session to do."""

    session_id: Identifier
    instruction: str
    principal: PrincipalContext
    run_id: Identifier


@dataclass(frozen=True, slots=True)
class CodeTurn:
    """What one turn produced, as the caller needs it."""

    run_id: Identifier
    report: str
    #: Where the working set stands now. ``None`` means nothing was ever
    #: written -- including by earlier turns, so it is not "this turn wrote
    #: nothing".
    workspace_version: Identifier | None
    outcome: AgentOutcome


@dataclass(slots=True)
class CodeSessionService:
    """Opens coding sessions and runs one turn of one at a time."""

    conversations: ConversationStore
    artifacts: ArtifactStore
    #: A runtime per turn, not one per process, and the reason is the approval
    #: gate: a held call is answered by a request naming a session, so the gate
    #: has to be bound to one -- and the gateway that holds the gate is built
    #: with it. Everything else about the runtime is identical between turns;
    #: what a turn costs to build is five schema validations.
    executor_for: Callable[[ApprovalScope], AgentExecutor]
    scope: WorkspaceScope
    #: No default. A turn's ceiling is a deployment decision, and a silent one
    #: is how a runaway loop becomes somebody's bill.
    budget: RunBudget
    turn_timeout_seconds: int
    max_concurrent_turns: int
    clock: Callable[[], datetime]
    tool_names: tuple[ToolName, ...] = CODE_TOOLS
    _running: set[str] = field(default_factory=set[str], init=False)
    _turns: int = field(default=0, init=False)

    async def open(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        title: str | None = None,
    ) -> Identifier:
        session_id = new_code_session_id()
        await self.conversations.create_session(
            session_id=session_id,
            tenant_id=tenant_id,
            owner_id=principal_id,
            title=title,
            mode="code",
        )
        return session_id

    async def sessions(
        self, *, tenant_id: str, principal_id: str, limit: int = 50
    ) -> tuple[ConversationSession, ...]:
        """This principal's coding sessions, most recently spoken in first.

        Server-side rather than in the browser, which is where this list used
        to live. A list kept only in `localStorage` answers "what did I do on
        this machine, in this browser, since I last cleared it" -- and the
        sessions it forgets are still there, still owned, and no longer
        reachable, because a session id is the only way in.
        """

        return await self.conversations.list_sessions(
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
            limit=limit,
        )

    async def rename(
        self, *, session_id: str, tenant_id: str, principal_id: str, title: str
    ) -> ConversationSession:
        """Replace the name a session was given by its first instruction."""

        return await self.conversations.rename_session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            title=title,
            mode="code",
        )

    async def delete(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> None:
        """Remove one coding session, its transcript and its event stream.

        ``mode="code"`` is fixed here for the reason every other method on this
        service fixes it: a caller that could hand this one a chat session id
        would be deleting a conversation this service never ran.

        The workspace artifacts stay, unreachable rather than removed (ADR-056
        §5) -- the same thing that already happens to a workspace version each
        time a write supersedes it.
        """

        await self.conversations.delete_session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )

    async def history(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> tuple[Message, ...]:
        """This principal's own coding conversation, oldest first.

        The mode is fixed here for the same reason ``ChatService.history``
        fixes its own: this service is the Code one, and a caller able to ask
        it for a chat session's history would be reading a conversation whose
        turns it never ran.
        """

        stored = await self.conversations.history(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        return tuple(record.message for record in stored)

    async def workspace(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> tuple[WorkspaceListing, ...]:
        """What this session's working set currently holds.

        The whole product of a coding session is these files, and until this
        existed the only way to see one was to ask the agent to read it back --
        which spends a turn and a model call to answer a question the store
        already knows.

        Reachable by name only. The version is read from the session row, never
        taken from the caller: a principal who could name a version could name
        one belonging to another of their own sessions, and the artifact store
        scopes reads to a tenant and a principal and nothing narrower. That is
        an architecture test, not a habit -- see
        ``tests/architecture/test_a_workspace_version_is_never_asked_for.py``.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        return await Workspace(
            artifacts=self.artifacts,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ).list(session.workspace_version)

    async def open_workspace_file(
        self, *, session_id: str, tenant_id: str, principal_id: str, name: str
    ) -> tuple[ArtifactRef, AsyncIterator[bytes]]:
        """One of this session's files: what it is, and a stream of its bytes.

        Both together, from one store, on purpose. The caller needs the
        reference for its headers and the stream for its body, and the first
        shape this had returned only the reference -- leaving the route to
        stream from `dependencies.artifacts`, a *second* handle to what must be
        the same store. Nothing guaranteed it was: the API's test harness
        builds exactly that world by accident, which is evidence enough that
        production could. Two handles means headers describing a file from one
        store and bytes arriving from another.

        The artifact id never leaves this process. It is what the listing
        deliberately withholds; a client holding one could address a version
        this session has already moved past.

        `iter_chunks` is called here rather than awaited later because it is
        deliberately not `async def` (see the `ArtifactStore` port): its
        authorization runs at call time, so a refusal happens while the caller
        can still choose a status code.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        entry = await Workspace(
            artifacts=self.artifacts,
            tenant_id=tenant_id,
            principal_id=principal_id,
        ).locate(session.workspace_version, name)
        chunks = self.artifacts.iter_chunks(
            tenant_id=tenant_id,
            artifact_id=entry.artifact_id,
            principal_id=principal_id,
        )
        return entry, chunks

    async def ask(
        self,
        request: CodeRequest,
        sink: ProcessOnlySink,
        cancellation: CancellationToken,
    ) -> CodeTurn:
        """Run one turn: append what was asked, work, append the report."""

        # Admission first, and both checks are fail-fast with no await between
        # test and claim -- which is what makes them atomic here.
        if self._turns >= self.max_concurrent_turns:
            raise CodeCapacityError(
                f"this process runs at most {self.max_concurrent_turns} "
                "coding turns at once"
            )
        if request.session_id in self._running:
            raise CodeTurnBusyError("this session is already running a turn")
        self._turns += 1
        self._running.add(request.session_id)
        try:
            return await self._run(request, sink, cancellation)
        finally:
            self._running.discard(request.session_id)
            self._turns -= 1

    async def _run(
        self,
        request: CodeRequest,
        sink: ProcessOnlySink,
        cancellation: CancellationToken,
    ) -> CodeTurn:
        principal = request.principal
        # Authorization and the workspace pointer arrive together: a caller who
        # may not address this session must not learn where its files are.
        session = await self.conversations.session(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            mode="code",
        )

        # Read without repeating the mode, deliberately. The call above is this
        # turn's one authorization, and a second gate behind it would be a gate
        # nothing can test: whichever fires first hides the other, and the
        # covered one is then whichever happens to be written first.
        stored = await self.conversations.history(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
        )
        history = tuple(record.message for record in stored)
        asked = user_message(request.instruction)
        # Appended before the run, not after it. What the user said is a fact
        # the moment they said it, and a turn that fails must not lose the
        # sentence that caused it.
        await self.conversations.append(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            messages=(asked,),
        )

        # Named here rather than at creation, because a coding session is opened
        # before any instruction exists -- the console's "new session" button
        # has nothing to name it with. A client could rename it after the first
        # turn returned, but a closed tab or a dropped connection would lose
        # exactly the session that then cannot be found again, which is the
        # failure this exists to remove.
        #
        # The read above only decides whether to bother; the store's
        # `WHERE title IS NULL` is the arbiter, so a retry writes nothing and a
        # name somebody typed is never overwritten.
        if session.title is None:
            derived = title_from_instruction(request.instruction)
            if derived is not None:
                await self.conversations.set_title_if_unset(
                    session_id=request.session_id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    title=derived,
                    mode="code",
                )

        workspace = WorkspaceSession(
            workspace=SessionWorkspace(
                workspace=Workspace(
                    artifacts=self.artifacts,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                ),
                conversations=self.conversations,
                session_id=request.session_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
            ),
            version=session.workspace_version,
        )

        # One workspace for the whole turn, entered around the run rather than
        # around each tool: the tools find it through a ContextVar, and a scope
        # entered per call would hand each of them a version the last one had
        # already moved past.
        executor = self.executor_for(
            ApprovalScope(
                tenant_id=principal.tenant_id,
                session_id=request.session_id,
                principal_id=principal.principal_id,
            )
        )
        with self.scope.using(workspace):
            outcome = await executor.run(
                self._request_for(request, history=history, asked=asked),
                sink,
                cancellation,
            )

        if outcome.output_text:
            # Only when there is one. A failed or cancelled run produces no
            # report, and an empty assistant message would read as one.
            await self.conversations.append(
                session_id=request.session_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                messages=(assistant_message(text=outcome.output_text),),
            )

        return CodeTurn(
            run_id=request.run_id,
            report=outcome.output_text,
            # Read from the object the tools actually advanced, not from the
            # row: the pointer was written through per write, so this is the
            # same value and one fewer round trip.
            workspace_version=workspace.version,
            outcome=outcome,
        )

    def _request_for(
        self,
        request: CodeRequest,
        *,
        history: tuple[Message, ...],
        asked: Message,
    ) -> AgentRunRequest:
        return AgentRunRequest(
            trace=TraceContext(agent_run_id=request.run_id),
            run_kind="code",
            # One stream per session, so a subscriber follows the conversation
            # rather than one turn of it.
            stream_id=request.session_id,
            principal=request.principal,
            envelope=AuthorizationEnvelope(
                allowed_tools=self.tool_names,
                max_tool_risk="write",
                # Approval is armed for the risks this envelope does not
                # currently grant. That is not a contradiction: the gate is
                # wired, so granting an external tool later is a change to
                # `tool_names` and the ceiling above, not to the machinery.
                approval_required_risks=("external", "destructive"),
            ),
            budget=self.budget.model_copy(
                update={
                    "deadline": self.clock()
                    + timedelta(seconds=self.turn_timeout_seconds)
                }
            ),
            system_prompt=CODER_SYSTEM_PROMPT,
            messages=(*history, asked),
            # Both, and they are not the same thing: the envelope says what
            # policy would permit, `tool_names` is what the model is offered.
            tool_names=self.tool_names,
        )

    async def drain_cleanup(self, *, timeout_seconds: float) -> None:
        """Wait for turns in flight, so a deploy does not cut one in half.

        Nothing is cancelled here and nothing is recovered afterwards -- there
        is no half-finished state to reclaim, so the only thing worth doing is
        giving a turn that is nearly done the chance to finish. A turn that
        needs longer than the grace period is cut off, and its workspace stands
        at its last successful write. ``docs/known-gaps.md`` F-02 records the
        arithmetic.
        """

        deadline = self.clock() + timedelta(seconds=timeout_seconds)
        while self._turns and self.clock() < deadline:
            await asyncio.sleep(0.05)


__all__ = [
    "CODE_TOOLS",
    "CodeCapacityError",
    "CodeRequest",
    "CodeSessionService",
    "CodeTurn",
    "CodeTurnBusyError",
    "new_code_session_id",
]
