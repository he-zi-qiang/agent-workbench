"""Driving a coding session, and answering the questions one stops on.

Registered only when this process runs coding turns itself. A route that
existed while the service did not would be a 500 per request; a route that
existed while the *turn* ran somewhere else would be worse, because the
approval endpoint would accept decisions that could never reach the coroutine
waiting for them.

Every authorization here belongs to somebody else already. The session belongs
to its owner in the conversation store, which refuses another tenant, another
principal and a chat session identically. A held call belongs to its session in
the approval registry, which refuses on the same three axes. This layer
resolves who is asking and passes it down.

The turn is synchronous, like a chat turn: the request stays open while the
agent works, and the caller watches the steps on the event stream if it wants
to. That is what makes a disconnect meaningful -- it is the signal that nobody
is waiting for this any more -- and why this shares Chat's disconnect watcher
rather than carrying a copy of it.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_workbench.application.answer_release import ProcessOnlySink
from agent_workbench.application.code_approvals import (
    ApprovalScope,
    CodeApprovalRegistry,
)
from agent_workbench.application.code_session import CodeRequest, CodeSessionService
from agent_workbench.application.workspace import WorkspaceEntryNotFoundError
from agent_workbench.apps.api.disconnects import watched
from agent_workbench.apps.api.downloads import content_disposition
from agent_workbench.apps.api.sse import resume_from, stream_events
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.events import ApprovalDecision
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.cancellation import CancellationSource

CODE_PREFIX = "/v1/code"

INSTRUCTION_MAX_LENGTH = 8192
TITLE_MAX_LENGTH = 200


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)


class CreateSessionResponse(BaseModel):
    session_id: Identifier
    #: Whatever the caller named it, echoed back. Usually null: a coding
    #: session is opened before there is an instruction to name it after, and
    #: the name arrives with the first turn.
    title: str | None = None


class SessionView(BaseModel):
    """One coding session, as a list row.

    No message count and no workspace size. Both would mean a query per row on
    a list rendered before anybody has chosen a session, to decorate a link
    that opening answers exactly.
    """

    session_id: Identifier
    title: str | None
    last_activity_at: AwareDatetime | None


class SessionListResponse(BaseModel):
    sessions: tuple[SessionView, ...]


class RenameSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=INSTRUCTION_MAX_LENGTH)


class AskResponse(BaseModel):
    """What one turn produced.

    ``report`` is the agent's own account of the turn, not an answer: nothing
    here crossed a publication fence, because there was no fence to cross. The
    files are the product, and ``workspace_version`` names the set of them this
    turn left behind.
    """

    report: str
    workspace_version: Identifier | None
    run_id: Identifier
    status: str
    stop_reason: str


class MessageView(BaseModel):
    role: str
    text: str


class HistoryResponse(BaseModel):
    messages: tuple[MessageView, ...]


class PendingApprovalView(BaseModel):
    approval_id: Identifier
    tool_name: str
    argument_digest: str
    risk: str | None


class PendingApprovalsResponse(BaseModel):
    approvals: tuple[PendingApprovalView, ...]


class DeletedView(BaseModel):
    """What a delete answers with: the id that is now gone."""

    session_id: Identifier


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision


router = APIRouter(prefix=CODE_PREFIX, tags=["code"])


def _code(request: Request) -> CodeSessionService:
    code = dependencies_of(request).code
    if code is None:  # pragma: no cover - the router is not mounted without it
        raise NotFoundError("this process does not run coding sessions")
    return code


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest, request: Request
) -> CreateSessionResponse:
    principal = dependencies_of(request).principals.resolve(request)
    session_id = await _code(request).open(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        title=body.title,
    )
    return CreateSessionResponse(session_id=session_id, title=body.title)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SessionListResponse:
    """This principal's coding sessions, most recently spoken in first.

    A limit and no cursor, deliberately. `/v1/tasks` pages because a tenant
    accumulates tasks without bound; this is one person's recent coding
    sessions, and a keyset cursor over a list bounded by human memory is
    machinery with no reader.

    The title is a truncated copy of what its owner typed, so this list carries
    the conversation's confidentiality: it is scoped by tenant, by principal
    and by mode, exactly as the history is.
    """

    principal = dependencies_of(request).principals.resolve(request)
    sessions = await _code(request).sessions(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        limit=limit,
    )
    return SessionListResponse(
        sessions=tuple(
            SessionView(
                session_id=session.session_id,
                title=session.title,
                last_activity_at=session.last_activity_at,
            )
            for session in sessions
        )
    )


@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: str, body: RenameSessionRequest, request: Request
) -> SessionView:
    """Replace the name the first instruction gave this session.

    PATCH rather than PUT: a session is not being replaced, and a PUT would
    invite a body that carried the whole row -- including the workspace version,
    which nothing outside this process may name.
    """

    principal = dependencies_of(request).principals.resolve(request)
    session = await _code(request).rename(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        title=body.title,
    )
    return SessionView(
        session_id=session.session_id,
        title=session.title,
        last_activity_at=session.last_activity_at,
    )


@router.delete("/sessions/{session_id}", status_code=200)
async def delete_session(session_id: str, request: Request) -> DeletedView:
    """Remove one coding session and everything that was only its.

    Answers 200 with the id rather than 204. A 204 would be the more habitual
    REST answer, and it is the one that costs a client a special case: the
    console's `apiRequest` reads every successful response as JSON, so an empty
    body throws a `SyntaxError` in the one place `response.ok` guarantees there
    is nothing to catch it. Saying what was deleted is both cheaper and more
    useful than a status code that says only "something was".

    404 when it is not this principal's session, 409 when a turn is still
    running -- both from the store, both already in the error table.
    """

    principal = dependencies_of(request).principals.resolve(request)
    await _code(request).delete(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    return DeletedView(session_id=session_id)


@router.post("/sessions/{session_id}/messages")
async def ask(
    session_id: str,
    body: AskRequest,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:+=@/-]{0,127}$",
        ),
    ],
) -> AskResponse:
    """Run one turn, and hold the request open while it runs.

    The key derives a stable run id, the same way Chat's does, so a retry that
    reaches a session already working is refused as busy rather than becoming a
    second turn under a second id. It buys no durable idempotency: Code keeps
    no turn ledger, so a retry after this process died starts a new turn. That
    is the documented cost of having no coordination plane, not an oversight.
    """

    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    run_id = _stable_run_id(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
    )

    cancellation = CancellationSource()
    turn_task = asyncio.create_task(
        _code(request).ask(
            CodeRequest(
                session_id=session_id,
                instruction=body.instruction,
                principal=principal,
                run_id=run_id,
            ),
            # One stream per session, one run per turn, and a fence that has no
            # publish methods at all: this run produces files and a report, and
            # an answer event on its stream would name a boundary it never had.
            ProcessOnlySink(dependencies.sink_for(stream_id=session_id, run_id=run_id)),
            cancellation,
        ),
        name=f"code-turn-{run_id}",
    )
    async with watched(
        request,
        cancellation,
        target=turn_task,
        poll_seconds=dependencies.config.chat_recovery.disconnect_poll_seconds,
        name=f"code-disconnect-{run_id}",
    ):
        turn = await turn_task
    return AskResponse(
        report=turn.report,
        workspace_version=turn.workspace_version,
        run_id=turn.run_id,
        status=turn.outcome.status,
        stop_reason=turn.outcome.stop_reason,
    )


@router.get("/sessions/{session_id}/messages")
async def history(session_id: str, request: Request) -> HistoryResponse:
    principal = dependencies_of(request).principals.resolve(request)
    messages = await _code(request).history(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    return HistoryResponse(
        messages=tuple(
            MessageView(role=message.role, text=message.text()) for message in messages
        )
    )


class WorkspaceEntryView(BaseModel):
    name: str
    size_bytes: int
    media_type: str


class WorkspaceResponse(BaseModel):
    """What the session's working set holds, by name.

    No version field, and that is deliberate rather than an omission: a version
    is meaningful only to a caller that could then ask for it, and nothing here
    accepts one. What a reader needs is the files.
    """

    files: tuple[WorkspaceEntryView, ...]


@router.get("/sessions/{session_id}/workspace")
async def workspace(session_id: str, request: Request) -> WorkspaceResponse:
    """The files this session has produced.

    Its own endpoint rather than something folded into the history, because it
    answers a different question at a different rate: the transcript grows once
    per turn, and the working set changes with every write inside one.
    """

    principal = dependencies_of(request).principals.resolve(request)
    entries = await _code(request).workspace(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    return WorkspaceResponse(
        files=tuple(
            WorkspaceEntryView(
                name=entry.name,
                size_bytes=entry.size_bytes,
                media_type=entry.media_type,
            )
            for entry in entries
        )
    )


@router.get("/sessions/{session_id}/workspace/{name}")
async def workspace_file(
    session_id: str, name: str, request: Request
) -> StreamingResponse:
    """One file out of the working set, by the name the session knows it under.

    The bytes, unconditionally, with no ``?preview=`` or ``?disposition=``
    beside them. A mode flag would be a second behaviour to authorize on a path
    whose whole safety argument is that there is one; the console previews text
    by fetching this and reading the body, which is what it already does for an
    artifact. ``routes/artifacts.py``'s preview endpoint makes the same
    argument from the other side -- it exists only because .docx cannot be read
    by fetching it.

    The name is a path segment rather than a query parameter because
    ``WorkspaceName`` forbids separators: a name containing one matches no
    route, which is the same 404 as a name the manifest does not bind.

    Every store call happens before the response object exists. ``iter_chunks``
    is deliberately not ``async def`` (see the ``ArtifactStore`` port) so that
    its authorization runs here, while the status code can still change -- a
    refusal discovered mid-body is a 200 that stops, which a client cannot tell
    from a dropped connection.
    """

    principal = dependencies_of(request).principals.resolve(request)
    try:
        entry, chunks = await _code(request).open_workspace_file(
            session_id=session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            name=name,
        )
    except WorkspaceEntryNotFoundError as missing:
        # Translated rather than left to escape. It is a `KeyError`, so an
        # unhandled one is a 500 -- and a 500 would distinguish "no such file
        # in a session you own" from "no such session", which is exactly the
        # distinction every other refusal on this router refuses to draw.
        raise NotFoundError("workspace entry not found") from missing

    # Straight off the manifest entry, with no `head()` first: the entry already
    # carries what the headers need, and the entry is what the name resolved to.
    # Describing the response from anything else would let the headers and the
    # body disagree about which version this name was read at.
    return StreamingResponse(
        chunks,
        media_type=entry.media_type,
        headers={
            "content-disposition": content_disposition(entry.filename or name),
            "content-length": str(entry.size_bytes),
            "x-artifact-sha256": entry.sha256,
        },
    )


@router.get("/sessions/{session_id}/events")
async def subscribe(session_id: str, request: Request) -> StreamingResponse:
    """Stream this session's durable events, resuming from ``Last-Event-ID``.

    The transport is the one the chat subscriber uses; what differs is the one
    thing that depends on what the stream belongs to. The session is authorized
    through the service, which pins the mode, so a chat session's id here is
    answered exactly as an id that does not exist -- and the refusal happens
    before a ``StreamingResponse`` is built, because a subscription refused
    once streaming had begun is indistinguishable to a client from a stream
    that carried nothing.
    """

    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    await _code(request).history(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )

    after = resume_from(request, session_id)
    live = dependencies.live_events.subscribe(session_id)
    stream = dependencies.config.event_stream
    return StreamingResponse(
        stream_events(
            dependencies.events,
            stream_id=session_id,
            after_sequence=after,
            poll_seconds=stream.catchup_poll_seconds,
            page_size=stream.replay_page_size,
            heartbeat_seconds=dependencies.config.sse_heartbeat_seconds,
            disconnected=request.is_disconnected,
            live=live,
            coalesce_seconds=stream.live_delta_coalesce_ms / 1000,
        ),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-store",
            "x-accel-buffering": "no",
        },
    )


@router.get("/sessions/{session_id}/approvals")
async def pending_approvals(
    session_id: str, request: Request
) -> PendingApprovalsResponse:
    """What this session is currently stopped on, if anything.

    Readable rather than push-only because a client that reconnects mid-turn
    has missed the event that announced the question, and a held call with no
    visible question is a session that looks hung.
    """

    registry, scope = _approvals(request, session_id)
    return PendingApprovalsResponse(
        approvals=tuple(
            PendingApprovalView(
                approval_id=held.approval_id,
                tool_name=held.tool_name,
                argument_digest=held.argument_digest,
                risk=held.risk,
            )
            for held in registry.pending(scope)
        )
    )


@router.post("/sessions/{session_id}/approvals/{approval_id}")
async def decide(
    session_id: str,
    approval_id: str,
    body: DecideRequest,
    request: Request,
) -> None:
    """Answer one held call.

    Refusals are the interesting part. An id that is not this caller's answers
    exactly like an id that does not exist. A second decision for the same id
    finds nothing pending, because resolving one removes it in the same breath.
    And ``approve_for_session`` is refused for an external or destructive tool:
    a blanket yes to an irreversible effect is the thing that must be asked
    every time.
    """

    registry, scope = _approvals(request, session_id)
    registry.decide(approval_id=approval_id, scope=scope, decision=body.decision)


def _approvals(
    request: Request, session_id: str
) -> tuple[CodeApprovalRegistry, ApprovalScope]:
    dependencies = dependencies_of(request)
    registry = dependencies.code_approvals
    if registry is None:  # pragma: no cover - the router is not mounted without it
        raise NotFoundError("this process does not run coding sessions")
    principal = dependencies.principals.resolve(request)
    return registry, ApprovalScope(
        tenant_id=principal.tenant_id,
        session_id=session_id,
        principal_id=principal.principal_id,
    )


def _stable_run_id(
    *,
    tenant_id: str,
    principal_id: str,
    session_id: str,
    idempotency_key: str,
) -> str:
    material = "\x1f".join(
        (tenant_id, principal_id, session_id, idempotency_key)
    ).encode()
    return f"run_{hashlib.sha256(material).hexdigest()}"


__all__ = [
    "CODE_PREFIX",
    "AskRequest",
    "AskResponse",
    "CreateSessionRequest",
    "CreateSessionResponse",
    "DecideRequest",
    "HistoryResponse",
    "PendingApprovalsResponse",
    "WorkspaceResponse",
    "router",
]
