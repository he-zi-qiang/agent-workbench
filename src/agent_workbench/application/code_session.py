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
from agent_workbench.application.code_prompt import (
    CODER_SYSTEM_PROMPT,
    CODER_SYSTEM_PROMPT_WITH_SANDBOX,
    CODER_SYSTEM_PROMPT_WITH_SANDBOX_UNGATED,
)
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
from agent_workbench.domain.sandbox import SANDBOX_RUN_TOOL
from agent_workbench.domain.tools import ToolName
from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import CancellationToken
from agent_workbench.ports.conversation_store import (
    ConversationSession,
    ConversationStore,
)

#: What a coding session is allowed to reach with the sandbox off: read tools
#: plus the two writes, and nothing external. A deployment that leaves
#: `code.sandbox_enabled` false gets exactly this, and therefore never reaches
#: the approval gate -- the envelope requires one only for external and
#: destructive risks.
CODE_TOOLS: tuple[ToolName, ...] = (
    "workspace_edit",
    "workspace_grep",
    "workspace_list",
    "workspace_read",
    "workspace_write",
)

#: The same list plus the one external tool a coding session may be granted
#: (ADR-057). Spelled out as its own tuple rather than assembled at the call
#: site, so "what may a coding session reach" has two answers to read rather
#: than one answer and an append.
#:
#: `sandbox_run` is `external` risk, which is what makes every call stop at the
#: approval gate. That is the intended cost of granting it, not a side effect:
#: running code on somebody's machine is the kind of thing worth being asked
#: about, and it is why the gate was armed before anything could trigger it.
CODE_TOOLS_WITH_SANDBOX: tuple[ToolName, ...] = (
    *CODE_TOOLS,
    SANDBOX_RUN_TOOL,
)


class CodeTurnBusyError(RuntimeError):
    """This session is already running a turn."""


class CodeCapacityError(RuntimeError):
    """This process is already running as many turns as it admits."""


class CodeRunUnavailableError(RuntimeError):
    """Somebody asked to run a file and this deployment cannot run code.

    Not a 404 and not a 403: the file is there, the caller may see it, and
    nothing about the request is wrong -- ``code.sandbox_enabled`` is off, or
    the sandbox this process was told to use did not answer at boot. A 503 with
    that sentence in it is the only answer that names the fix.
    """


class CodeRunRefusedError(RuntimeError):
    """The sandbox or the working set refused one run that was asked for.

    Distinct from a script that ran and failed, which is not an error here at
    all: that is an exit code and a traceback, and it is the thing the reader
    clicked to see. This is the container that could not start, the result that
    did not parse, the output that would not fit -- states nothing about the
    request can fix, carrying the refusing side's own words.
    """


class CodeRunNotPermittedError(RuntimeError):
    """The caller does not hold the scope that running code needs.

    The same ``sandbox:run`` the tool declares, checked here because this path
    has no Policy Gateway in front of it -- there is no envelope, no step and
    no tool call, so the one gate the agent's route through this capability
    passes is a gate this route has to be. A console that asks for the scope
    and a deployment that grants it are two decisions, and this is where they
    have to meet.
    """


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
    #: Whether each ``sandbox_run`` call stops for a human (ADR-058). Defaults
    #: to the settings default rather than contradicting it; the assembly in
    #: `apps/api/dependencies.py` always passes the configured value.
    sandbox_requires_approval: bool = False
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

    async def put_workspace_file(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        name: str,
        content: bytes,
        media_type: str,
    ) -> tuple[WorkspaceListing, ...]:
        """Put a file a *person* supplied into this session's working set.

        The counterpart to `open_workspace_file`, and the half a coding session
        was missing: an agent could produce files and read them back, and there
        was no way to hand it one. Until this existed, giving a session a log to
        look at meant pasting it into the instruction -- which spends context on
        content the workspace is built to hold, and truncates anything large.

        Binary types are allowed here, and deliberately so. `WorkspaceWriteTool`
        refuses docx, xlsx, pptx and pdf, and that refusal is about what the
        *model* may synthesise: a model emitting what it claims are docx bytes
        is producing something no reader can trust. A person attaching a PDF is
        the opposite situation -- the bytes are the thing they have, and the
        session's job is to look at them.

        Reuses `SessionWorkspace`, so the version pointer advances with the same
        compare-and-set every tool write uses. An upload racing a running turn
        loses that comparison and is refused, rather than leaving the session
        pointing at a manifest that names only the uploaded file.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        workspace = SessionWorkspace(
            workspace=Workspace(
                artifacts=self.artifacts,
                tenant_id=tenant_id,
                principal_id=principal_id,
            ),
            conversations=self.conversations,
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        version = await workspace.write(
            session.workspace_version, name, content, media_type=media_type
        )
        return await workspace.list(version)

    async def workspace_session(
        self, *, session_id: str, tenant_id: str, principal_id: str
    ) -> WorkspaceSession:
        """This session's working set, opened for writing, for one caller.

        The same object a turn runs inside, handed out so that something other
        than a turn can advance the working set -- today that is the console's
        运行 button (ADR-065), which runs one file the reader is looking at.

        Authorization is the first call and there is no second one: the store
        refuses another tenant, another principal and a chat session id
        identically, and where the files are is only learned after it has not
        refused. What comes back is a *writer* -- ``SessionWorkspace`` records
        each version on the session row as it commits -- so a caller that runs
        code with it leaves the same trail a tool call would, and a caller that
        crashes half way leaves the files it had finished. That is the same
        per-write pointer every other Code write moves; see
        ``application/session_workspace.py`` for why it is not deferred to the
        end.
        """

        session = await self.conversations.session(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            mode="code",
        )
        return self._workspace_at(
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            version=session.workspace_version,
        )

    def _workspace_at(
        self,
        *,
        session_id: str,
        tenant_id: str,
        principal_id: str,
        version: Identifier | None,
    ) -> WorkspaceSession:
        """The writer, from a version somebody has already been authorized for.

        Deliberately not `async` and deliberately taking the version rather
        than reading it: the read is the authorization, and a constructor that
        performed its own would be a second gate behind the first -- whichever
        fires first hides the other, and the covered one is then whichever
        happens to be written first (the same argument ``_run`` makes about the
        history read it does not repeat the mode on).
        """

        return WorkspaceSession(
            workspace=SessionWorkspace(
                workspace=Workspace(
                    artifacts=self.artifacts,
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                ),
                conversations=self.conversations,
                session_id=session_id,
                tenant_id=tenant_id,
                principal_id=principal_id,
            ),
            version=version,
        )

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

        workspace = self._workspace_at(
            session_id=request.session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
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
                # Derived from the tool list rather than configured beside it.
                # The ceiling exists to admit exactly one tool -- `sandbox_run`
                # is the only `external` thing a coding session may be given --
                # so two separate switches would be two ways to describe one
                # decision, and the interesting bug is the pair disagreeing: a
                # deployment that granted the tool and left the ceiling at
                # `write` would offer the model a tool its own envelope denies,
                # which costs a wasted turn ending in
                # `outside_submitted_envelope`.
                max_tool_risk=(
                    "external" if SANDBOX_RUN_TOOL in self.tool_names else "write"
                ),
                # `destructive` is armed unconditionally -- nothing grants such
                # a tool today, and the day something does, the gate must
                # already be there. Whether `external` joins it is ADR-058's
                # question: the gate F-05 armed early turned out to buy latency
                # rather than consent (the card shows a digest, ADR-054), and
                # the Task path has always run the same `sandbox_run` ungated,
                # so the deployment now says which arrangement it wants.
                approval_required_risks=(
                    ("external", "destructive")
                    if self.sandbox_requires_approval
                    else ("destructive",)
                ),
            ),
            budget=self.budget.model_copy(
                update={
                    "deadline": self.clock()
                    + timedelta(seconds=self.turn_timeout_seconds)
                }
            ),
            # Selected from the same facts the envelope reads, so the model is
            # never told it cannot do something it has been granted -- nor that
            # it will wait for a human who is not going to be asked (ADR-058:
            # the gated text says "expect to wait, do not spend one", which is
            # an instruction to avoid the tool this deployment just freed).
            system_prompt=(
                (
                    CODER_SYSTEM_PROMPT_WITH_SANDBOX
                    if self.sandbox_requires_approval
                    else CODER_SYSTEM_PROMPT_WITH_SANDBOX_UNGATED
                )
                if SANDBOX_RUN_TOOL in self.tool_names
                else CODER_SYSTEM_PROMPT
            ),
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
    "CODE_TOOLS_WITH_SANDBOX",
    "CodeCapacityError",
    "CodeRequest",
    "CodeRunNotPermittedError",
    "CodeRunRefusedError",
    "CodeRunUnavailableError",
    "CodeSessionService",
    "CodeTurn",
    "CodeTurnBusyError",
    "new_code_session_id",
]
