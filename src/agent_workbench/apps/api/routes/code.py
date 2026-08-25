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
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_workbench.adapters.tools.sandbox import (
    MAX_INPUT_BYTES,
    MAX_INPUT_NAMES,
    SandboxRefusedError,
)
from agent_workbench.application.answer_release import ProcessOnlySink
from agent_workbench.application.code_approvals import (
    ApprovalScope,
    CodeApprovalRegistry,
)
from agent_workbench.application.code_session import (
    CodeMode,
    CodeRequest,
    CodeRunNotPermittedError,
    CodeRunRefusedError,
    CodeRunUnavailableError,
    CodeSessionService,
)
from agent_workbench.application.workspace import (
    WorkspaceEntryNotFoundError,
    WorkspaceListing,
)
from agent_workbench.apps.api.disconnects import watched
from agent_workbench.apps.api.downloads import content_disposition
from agent_workbench.apps.api.sse import resume_from, stream_events
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.errors import NotFoundError, ToolInputInvalidError
from agent_workbench.domain.events import ApprovalDecision
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.sandbox import SANDBOX_RUN_SCOPE
from agent_workbench.ports.cancellation import (
    CancellationSource,
    NullCancellationToken,
)

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
    #: Which project this session was filed into, or none (ADR-071). A coding
    #: session *is* a `conversation_sessions` row -- `CodeSession.open` writes
    #: it with mode="code" -- so the column has been there since the projects
    #: migration. Only this view was not reporting it, which left the interface
    #: unable to show the membership it was already allowed to set.
    project_id: Identifier | None = None


class SessionListResponse(BaseModel):
    sessions: tuple[SessionView, ...]


class RenameSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=INSTRUCTION_MAX_LENGTH)
    #: ``"plan"`` narrows this turn to the tools that only read (ADR-0079).
    #: Defaulted rather than required, so every client written before plan mode
    #: existed keeps meaning what it meant. A named value rather than a
    #: `plan: bool`, because `plan=false` reads as an absence in a request body
    #: while `"act"` reads as a choice somebody made.
    mode: CodeMode = "act"


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
    #: The arguments as far as they fit, marked where they were cut.
    #:
    #: The field that makes the card answerable. `argument_digest` stays
    #: because it is what a standing rule is keyed by and what `ToolProposed`
    #: published, but nobody can consent to a hash -- and once a Code session
    #: can run a command on the machine, the arguments are no longer a detail
    #: of the effect, they *are* it.
    approval_preview: str
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
                project_id=session.project_id,
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
        project_id=session.project_id,
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
                # Frozen for the turn here, in the same statement that builds
                # the request the envelope is signed from. A mode that could
                # change mid-turn would change what a running model is holding
                # after its envelope had been signed with the other list.
                mode=body.mode,
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


@router.put("/sessions/{session_id}/workspace/{name}")
async def put_workspace_file(
    session_id: str, name: str, request: Request
) -> WorkspaceResponse:
    """Put a file a person supplied into this session's working set.

    A raw-body PUT, matching `PUT /v1/uploads/{id}/content` rather than the
    JSON routes around it. That is this repository's line between the control
    plane and the data plane (`app.document_upload_transport`), and an
    attachment is data: a multipart form here would put document bytes through
    the JSON body limit and give the control plane a second content type to
    know about.

    The media type comes from the request's own `content-type`, defaulting to
    `application/octet-stream` -- the same thing the browser sends for a file
    it cannot classify, and an honest answer rather than a guess from the
    extension.

    Answers with the whole listing rather than the one entry, because the
    caller's next question is always "what is in there now" and a write has
    just changed it.
    """

    principal = dependencies_of(request).principals.resolve(request)
    body = await request.body()
    files = await _code(request).put_workspace_file(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        name=name,
        content=body,
        # Stripped and lower-cased, not only split. `MediaType` is
        # `^[a-z]+/...` (`domain/artifacts.py`), so a client sending the header
        # in the case RFC 9110 explicitly allows -- `Content-Type: TEXT/PLAIN`,
        # or anything with a space before the parameter -- failed
        # `ArtifactRef`'s validation, and `ValidationError` is not in
        # `main.py`'s status table: the upload answered 500. Both other places
        # in this repository that compare a media type already normalise
        # (`routes/code.py`'s run gate, `adapters/tools/workspace.py`); this was
        # the one that did not.
        media_type=(request.headers.get("content-type") or "application/octet-stream")
        .split(";")[0]
        .strip()
        .lower(),
    )
    return WorkspaceResponse(
        files=tuple(
            WorkspaceEntryView(
                name=entry.name,
                size_bytes=entry.size_bytes,
                media_type=entry.media_type,
            )
            for entry in files
        )
    )


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
            # Same as the artifact download: the manifest's media type is the
            # answer, and sniffing would let a browser promote the label.
            "x-content-type-options": "nosniff",
        },
    )


class RunFileResponse(BaseModel):
    """What one click on 运行 produced.

    Shaped like the thing that happened rather than like a rendering of it.
    ``stdout`` and ``stderr`` stay separate because a script that printed its
    answer and also warned about something is two facts, and interleaving them
    -- which no server can do faithfully after the fact, the two streams having
    been written independently -- would present a guess as a transcript.

    ``written`` names the files that reached the working set, and
    ``workspace_version`` is where the set now stands, so a caller can refresh
    a listing without asking a second question. ``omitted_inputs`` is the
    honest half: a workspace larger than one call may carry does not silently
    lose files, it says which ones did not go in.
    """

    exit_code: int
    stdout: str
    stderr: str
    written: tuple[str, ...]
    workspace_version: Identifier | None
    omitted_inputs: tuple[str, ...]
    #: The working set as it stands after this run, the same shape
    #: ``GET /workspace`` answers with.
    #:
    #: Added because ``written`` alone is a list of names, and a console that
    #: wants to *show* what a run produced needs the type and the size of each
    #: one -- which it could only get by re-reading the listing, on a request
    #: that races the response it is reacting to. For one render after every
    #: run the names existed and the entries did not, so the produced files
    #: degraded to a line of text (known-gaps F-15).
    #:
    #: The whole listing rather than entries for ``written``, and the reason is
    #: the one ``PUT /workspace/{name}`` already gives one screen up: the
    #: caller's next question is always "what is in there now", a run has just
    #: changed it, and a second round trip to ask is a round trip this route
    #: already has the answer to. It also covers the case a per-file list
    #: cannot -- a script that *deleted* nothing but rewrote a file it did not
    #: report, which shows up as a size change here and nowhere else.
    #:
    #: Additive: a client that ignores it behaves exactly as before.
    files: tuple[WorkspaceEntryView, ...] = ()


#: Run a file the way a person means it: `python thing.py`, in a directory that
#: holds the rest of the working set.
#:
#: `runpy` rather than pasting the file's own source in as the script, and the
#: difference is the traceback. The sandbox writes whatever it is given to
#: `/sandbox/script.py`, so a pasted body raises from a filename the reader has
#: never seen; `run_path` keeps the name they clicked, at the line they can
#: count to. `sys.argv` is set for the same reason -- a script that reads
#: `argv[0]` should see itself.
#:
#: The two lines before `run_path` are both there because a real container said
#: so, and both are about the same ordinary script: one that imports a module
#: sitting beside it in the working set.
#:
#: `sys.path.insert(0, "")` is the working directory. Without it, a `sq.py`
#: opening with `import helper` raised `ModuleNotFoundError` with `helper.py`
#: right there. Two things conspire: `runpy.run_path` does not touch `sys.path`
#: for a plain file, and the sandbox runs `python -I`, whose isolation implies
#: `-P` -- so no directory is prepended at all. `python sq.py` prepends the
#: script's own directory, which here *is* the working directory, so this
#: restores what the button claims to offer and nothing more.
#:
#: `dont_write_bytecode` is the consequence of having fixed that. The import
#: then succeeded and the *run* was refused anyway, by the output collector:
#: `output_unsupported: '__pycache__' is a directory; the working directory is
#: flat`. That refusal is right and is not being worked around -- ADR-029 keeps
#: the transfer flat, and refusing a directory rather than skipping it is what
#: stops a script's results from vanishing silently. What is wrong is producing
#: the directory at all: nothing in a throwaway container is ever going to read
#: that cache back.
_ENTRY_SCRIPT = (
    "import runpy, sys\n"
    'sys.path.insert(0, "")\n'
    "sys.dont_write_bytecode = True\n"
    "sys.argv = [{name}]\n"
    "runpy.run_path({name}, run_name='__main__')\n"
)

#: What a runnable file is called. Deliberately a suffix and not the media type
#: alone: a `.py` uploaded by a person arrives as whatever their browser
#: guessed, and `text/x-python` is only what *this* project's own writer labels
#: one with.
#:
#: The parameter is stripped before comparing even though the PUT route already
#: strips one off an upload's `content-type`. Two places deciding the same thing
#: is worth one `split` here: the console's own `isRunnablePython` strips, and a
#: server that did not would refuse a file its client had just offered a 运行
#: button for -- which reads as the button being broken.
_PYTHON_SUFFIX = ".py"
_PYTHON_MEDIA_TYPES = frozenset({"text/x-python", "text/x-python-script"})


@router.post("/sessions/{session_id}/workspace/{name}/run")
async def run_workspace_file(
    session_id: str, name: str, request: Request
) -> RunFileResponse:
    """Run one Python file out of the working set, and keep what it wrote.

    The console can already *run* an HTML page -- it goes into an opaque-origin
    frame and paints (ADR-062) -- and could only ever *read* a `.py`. On a
    coding console that asymmetry is backwards: the produced file most likely
    to be a program is the one kind whose behaviour could not be seen without
    spending a whole model turn asking the agent to run it and paste the
    output. ADR-065 is that argument at length.

    What this is not: a shell, a REPL, and an interpreter session. It is one
    call of the same pure function ``sandbox_run`` already is (ADR-029) -- a
    throwaway container, ``--network=none``, the working set in, files and two
    streams out, nothing kept between calls. The reader supplies no script;
    the only thing they choose is which of their own files runs.

    Three refusals, each for a different reason and each answered differently:
    a deployment that did not enable the sandbox cannot do this at all (503), a
    principal without ``sandbox:run`` may not (403), and a name that is not a
    Python file has nothing to run (422). The last is checked here rather than
    left to the container, where it would surface as a ``SyntaxError`` in
    somebody's Markdown.
    """

    dependencies = dependencies_of(request)
    sandbox = dependencies.code_sandbox
    runner = None if sandbox is None else sandbox.runner
    if runner is None:
        raise CodeRunUnavailableError(
            "this deployment's coding sessions cannot run code; set "
            "code.sandbox_enabled and start the sandbox server"
        )
    principal = dependencies.principals.resolve(request)
    if SANDBOX_RUN_SCOPE not in principal.scopes:
        raise CodeRunNotPermittedError(
            f"running a file needs the {SANDBOX_RUN_SCOPE} scope"
        )

    code = _code(request)
    # Authorization, and the working set to write into, in one call. Everything
    # below is inside a session this principal owns or has already been refused.
    session = await code.workspace_session(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    entries = await session.workspace.list(session.version)
    target = next((entry for entry in entries if entry.name == name), None)
    if target is None:
        raise NotFoundError("workspace entry not found")
    declared = target.media_type.split(";")[0].strip().lower()
    if not (name.endswith(_PYTHON_SUFFIX) or declared in _PYTHON_MEDIA_TYPES):
        raise ToolInputInvalidError(f"{name} is not a Python file")

    inputs, omitted = _inputs_for(name, entries)
    try:
        outcome = await runner.run(
            session,
            script=_ENTRY_SCRIPT.format(name=repr(name)),
            inputs=inputs,
            # Not `watched`, unlike a turn, and not a source this route could
            # cancel. A turn is minutes of model calls and a disconnect is the
            # signal that nobody is waiting; this is one container start bounded
            # by the sandbox's own wall clock, and the only cancellable stretch
            # left is the loop that saves what the script produced -- which a
            # closed tab is not a reason to abandon half way.
            cancellation=NullCancellationToken(),
        )
    except SandboxRefusedError as refusal:
        # Everything this can be is a refusal by the sandbox or by the
        # workspace, never a bug in this process -- so it answers as a 409
        # carrying the sandbox's own words, rather than as a 500 carrying none.
        # A script that *ran* and failed is not here: that is an exit code and
        # a traceback on stderr, which is a 200 and the thing the reader came
        # to see.
        raise CodeRunRefusedError(str(refusal)) from refusal

    return RunFileResponse(
        exit_code=outcome.exit_code,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        written=outcome.written,
        # Read off the session the run advanced, not re-fetched: the pointer
        # moved per write, so this is the same value and one fewer round trip.
        workspace_version=session.version,
        omitted_inputs=omitted,
        # Re-listed, unlike the version above, because the *contents* did
        # change: this is the point of the field. One `list` against the
        # version this run advanced to, which is the same call the listing
        # route makes and is what makes the produced files drawable on the
        # first render rather than the second.
        files=tuple(
            WorkspaceEntryView(
                name=entry.name,
                size_bytes=entry.size_bytes,
                media_type=entry.media_type,
            )
            for entry in await session.workspace.list(session.version)
        ),
    )


def _inputs_for(
    name: str, entries: Sequence[WorkspaceListing]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Which files go into the container, and which did not fit.

    The whole working set, because a script that reads ``data.csv`` beside
    itself is the ordinary case and a reader clicking 运行 has not been asked
    to declare its inputs. The file itself goes first, so a workspace at the
    ceiling still runs.

    What does not fit is *named*, never quietly dropped. A run missing an input
    fails somewhere inside the script, with a traceback that says nothing about
    the real cause -- the same argument the tool's own ``not_found`` branch
    makes -- and a console that could not say which file was left out would be
    reporting that failure as the script's.
    """

    taken: list[str] = [name]
    omitted: list[str] = []
    budget = MAX_INPUT_BYTES
    for entry in entries:
        if entry.name == name:
            budget -= entry.size_bytes
            continue
        if len(taken) >= MAX_INPUT_NAMES or entry.size_bytes > budget:
            omitted.append(entry.name)
            continue
        taken.append(entry.name)
        budget -= entry.size_bytes
    return tuple(taken), tuple(omitted)


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
                approval_preview=held.approval_preview,
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
