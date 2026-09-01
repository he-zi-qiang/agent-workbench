"""Asking a question, and getting an answer that names its sources.

Registered only when the process has an embedder. A route that exists and
cannot answer is worse than one that is absent: the absence is a 404 a client
detects once, while the alternative is a 500 per request and a support thread
about why the assistant is broken.

Sessions are opened explicitly rather than implied by a first question. A
question that silently created one would make "which conversation is this"
depend on ordering, and a client retrying a timed-out request would find itself
in a second conversation holding half its history.

Every authorization here is somebody else's already: the session belongs to its
owner in the conversation store, and the evidence is checked against PostgreSQL
inside the chat service, before the model sees it and again before the answer
is delivered. This layer resolves who is asking and passes it down.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agent_workbench.application.chat import (
    ChatRequest,
    ChatService,
)
from agent_workbench.application.citation_source import (
    CitationSourceUnavailableError,
)
from agent_workbench.apps.api.disconnects import watched
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.context import Citation
from agent_workbench.domain.identifiers import Identifier, new_session_id
from agent_workbench.domain.runs import BudgetUsage
from agent_workbench.ports.cancellation import CancellationSource

CHAT_PREFIX = "/v1/chat"

QUESTION_MAX_LENGTH = 4096
TITLE_MAX_LENGTH = 200


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)


class CreateSessionResponse(BaseModel):
    session_id: Identifier


class SessionView(BaseModel):
    """One chat session, projected for its owner's recent-session list."""

    session_id: Identifier
    title: str | None
    last_activity_at: AwareDatetime | None
    # Which project this session was opened for, or none (ADR-071). Reported so
    # an interface can show the membership it is about to change; it is not an
    # authorization fact, and None is the normal state.
    project_id: Identifier | None = None


class SessionListResponse(BaseModel):
    sessions: tuple[SessionView, ...]


class RenameSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("title must contain a visible character")
        return title


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=QUESTION_MAX_LENGTH)
    knowledge_base_id: Identifier | None = None
    #: Explicit on new clients. The ``before`` validator preserves old clients:
    #: they selected the RAG path by sending a knowledge base, while omitting
    #: one now means the Direct path the old contract could not express.
    answer_mode: Literal["direct", "rag"] = "rag"
    top_k: int = Field(default=8, ge=1, le=50)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_answer_mode(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        raw = cast(dict[str, Any], value)
        if "answer_mode" in raw:
            return raw
        normalized = dict(raw)
        normalized["answer_mode"] = (
            "rag" if normalized.get("knowledge_base_id") is not None else "direct"
        )
        return normalized

    @model_validator(mode="after")
    def validate_answer_scope(self) -> AskRequest:
        if self.answer_mode == "direct" and self.knowledge_base_id is not None:
            raise ValueError("direct chat must not name a knowledge base")
        if self.answer_mode == "rag" and self.knowledge_base_id is None:
            raise ValueError("rag chat requires a knowledge base")
        return self


def _ensure_answer_mode_available(
    shape: str, answer_mode: Literal["direct", "rag"]
) -> None:
    """Reject a requested capability this deployment did not assemble."""

    if shape == "ungrounded" and answer_mode == "rag":
        raise HTTPException(
            status_code=422,
            detail="this deployment supports direct chat only",
        )


class AskResponse(BaseModel):
    """One answer, its sources, and whether it was withheld.

    ``withheld`` is reported rather than turned into a 403. The question was
    allowed; what changed is that a source stopped being readable while the
    model was writing. A client that can tell the difference can offer to ask
    again, instead of showing an access error for something that was permitted.
    """

    answer: str
    citations: tuple[Citation, ...]
    withheld: bool
    #: False when this answer was produced without retrieved evidence. The
    #: routed shape can return either within one conversation, so a client
    #: cannot infer it from the session, and an empty citation list does not
    #: imply it -- a grounded answer may simply have cited nothing.
    grounded: bool
    run_id: Identifier
    turn_id: Identifier


class TurnUsageView(BaseModel):
    """这一轮烧了多少。缺席表示这里问不出答案，不表示花了零。"""

    input_tokens: int = 0
    output_tokens: int = 0
    #: `input_tokens` 的子集，不是它之外的另一笔。
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micro_usd: int = 0


class MessageView(BaseModel):
    role: str
    text: str
    #: 只有助手消息、且那一轮已经落定时才有。用户那一条永远没有——一句提问没有
    #: 自己的花销，给它一个零会让每一轮在屏幕上多出一行说谎的脚注。
    usage: TurnUsageView | None = None


class HistoryResponse(BaseModel):
    messages: tuple[MessageView, ...]


router = APIRouter(prefix=CHAT_PREFIX, tags=["chat"])


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest, request: Request
) -> CreateSessionResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    session_id = new_session_id()
    await _chat(request).conversations.create_session(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        owner_id=principal.principal_id,
        title=body.title,
    )
    return CreateSessionResponse(session_id=session_id)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SessionListResponse:
    """This principal's recent chat sessions, most recently active first.

    The result is bounded rather than paged, matching Code's recent-session
    contract. Tenant, principal and mode are all applied in the store query;
    none is accepted from the request body or query string.
    """

    principal = dependencies_of(request).principals.resolve(request)
    sessions = await _chat(request).sessions(
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


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> SessionView:
    """Resolve one owner-visible Chat session outside the recent-list window."""

    principal = dependencies_of(request).principals.resolve(request)
    session = await _chat(request).session(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    return SessionView(
        session_id=session.session_id,
        title=session.title,
        last_activity_at=session.last_activity_at,
        project_id=session.project_id,
    )


@router.patch("/sessions/{session_id}")
async def rename_session(
    session_id: str, body: RenameSessionRequest, request: Request
) -> SessionView:
    """Replace the name of one chat session owned by the caller."""

    principal = dependencies_of(request).principals.resolve(request)
    session = await _chat(request).rename(
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
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    run_id = _stable_run_id(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
    )

    # The shape this process *serves*, not the one it configured. A deployment
    # whose embedding runtime is missing serves Direct only, whatever the file
    # says, and reading the configured value here would accept a grounded turn
    # that the selector underneath can only fail.
    _ensure_answer_mode_available(
        dependencies.effective_retrieval_shape, body.answer_mode
    )

    cancellation = CancellationSource()
    chat_task = asyncio.create_task(
        _chat(request).ask(
            ChatRequest(
                session_id=session_id,
                question=body.question,
                principal=principal,
                knowledge_base_id=body.knowledge_base_id,
                idempotency_key=idempotency_key,
                answer_mode=body.answer_mode,
                top_k=body.top_k,
                run_id=run_id,
                stream_id=session_id,
            ),
            # One stream per session, one run per turn: a subscriber follows
            # the conversation and resumes where it left off.
            dependencies.sink_for(stream_id=session_id, run_id=run_id),
            cancellation,
        ),
        name=f"chat-request-{run_id}",
    )
    async with watched(
        request,
        cancellation,
        target=chat_task,
        poll_seconds=dependencies.config.chat_recovery.disconnect_poll_seconds,
        name=f"chat-disconnect-{run_id}",
    ):
        turn = await chat_task
    return AskResponse(
        answer=turn.answer,
        citations=turn.citations,
        withheld=turn.withheld,
        grounded=turn.grounded,
        run_id=turn.outcome.agent_run_id,
        turn_id=turn.turn_id,
    )


@router.get("/sessions/{session_id}/messages")
async def history(session_id: str, request: Request) -> HistoryResponse:
    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    messages = await _chat(request).history(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    return HistoryResponse(
        messages=tuple(
            MessageView(
                role=record.message.role,
                text="".join(
                    block.text
                    for block in record.message.content
                    if block.kind == "text"
                ),
                usage=_usage_view(record.usage),
            )
            for record in messages
        )
    )


class CitedPassageView(BaseModel):
    """The stored text behind one citation, and where it sits.

    Deliberately not the whole document, and deliberately not a range the caller
    picks: what this serves is exactly the chunk the answer named. A route that
    took an offset or a length would be a document reader wearing a citation's
    clothes, and the argument that lets this exist -- the asker is reading back
    the evidence for an answer they already have -- would not cover it.
    """

    chunk_id: Identifier
    document_id: Identifier
    document_version: Identifier
    text: str
    ordinal: int
    #: Absent for every format without pages. Never defaulted to 1.
    page: int | None


@router.get("/sessions/{session_id}/turns/{turn_id}/citations/{chunk_id}")
async def cited_passage(
    session_id: str, turn_id: str, chunk_id: str, request: Request
) -> CitedPassageView:
    """The passage behind one citation of one answer.

    **Reading a citation is a new read, never a replay of a stored conclusion.**
    The turn's result records which chunk was cited; it is not evidence that the
    asker may still read it. So this re-decides authorization from scratch --
    PostgreSQL says whether the document is readable now, and the index read is
    narrowed on tenant, knowledge base and principal exactly as a search is --
    and a citation from yesterday can correctly answer 404 today. That is the
    behaviour to keep: the alternative is a permanent read channel minted by
    every answer, outliving the grant that justified it.

    Mounted under the turn rather than as ``GET /v1/chunks/{chunk_id}``, and not
    for tidiness. A bare chunk id cannot be turned into the
    ``knowledge_base_id`` that ``VectorIndexPort.fetch`` requires: it lives in
    the ask request, not on ``ConversationSession`` and not in ``chat_turns``,
    and PostgreSQL holds no chunks table to look one up in. The turn is what
    supplies the ``document_id``, and the document is what supplies the
    knowledge base. The shape follows the data.

    Two refusals, both 404 and both with their own sentence. "No such citation"
    covers a chunk this turn never named, a turn in another session, and a
    session belonging to somebody else -- one answer, so that none of them
    confirms the others exist. "Readable no longer" covers a revoked grant, a
    revision the index has not caught up to, and a point that has left the
    index. Distinguishing the second group is not a leak: the caller is holding
    the citation already, in a turn they own, so the sentence tells them nothing
    the answer did not.
    """

    dependencies = dependencies_of(request)
    principal = dependencies.principals.resolve(request)
    reader = dependencies.citation_source
    if reader is None:
        # A deployment with no vector index answers honestly rather than 404:
        # the citation may be perfectly real, and "not found" would send the
        # reader looking for a mistake in their own data.
        raise CitationSourceUnavailableError(
            "this deployment has no vector index to read cited passages from"
        )
    passage = await reader.passage(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        turn_id=turn_id,
        chunk_id=chunk_id,
    )
    return CitedPassageView(
        chunk_id=passage.chunk_id,
        document_id=passage.document_id,
        document_version=passage.document_version,
        text=passage.text,
        ordinal=passage.ordinal,
        page=passage.page,
    )


class DeletedView(BaseModel):
    """What a delete answers with: the id that is now gone."""

    session_id: Identifier


@router.delete("/sessions/{session_id}", status_code=200)
async def delete_session(session_id: str, request: Request) -> DeletedView:
    """Remove one chat conversation and everything that was only its.

    200 with the id rather than 204, matching the code router: the console's
    `apiRequest` parses every successful body as JSON, so an empty one throws
    where `response.ok` guarantees nothing will catch it.

    A client may still keep local presentation metadata for a session, and is
    responsible for dropping that derived row after this durable delete.
    """

    principal = dependencies_of(request).principals.resolve(request)
    await _chat(request).delete(
        session_id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
    )
    return DeletedView(session_id=session_id)


def _chat(request: Request) -> ChatService:
    """The chat service, which exists because this router was registered.

    Registration is conditional on it, so reaching here without one would mean
    the application was assembled differently from how it was described.
    """

    chat = dependencies_of(request).chat
    if chat is None:  # pragma: no cover - the router is not mounted without one
        raise RuntimeError("the chat router was registered without a chat service")
    return chat


def _stable_run_id(
    *,
    tenant_id: str,
    principal_id: str,
    session_id: str,
    idempotency_key: str,
) -> str:
    """Derive the retry-stable run id without exposing the client key."""

    material = "\x1f".join(
        (tenant_id, principal_id, session_id, idempotency_key)
    ).encode()
    return f"run_{hashlib.sha256(material).hexdigest()}"


__all__ = [
    "CHAT_PREFIX",
    "AskRequest",
    "AskResponse",
    "CreateSessionRequest",
    "CreateSessionResponse",
    "HistoryResponse",
    "RenameSessionRequest",
    "SessionListResponse",
    "SessionView",
    "router",
]


def _usage_view(usage: BudgetUsage | None) -> TurnUsageView | None:
    if usage is None:
        return None
    return TurnUsageView(
        input_tokens=usage.tokens.input_tokens,
        output_tokens=usage.tokens.output_tokens,
        cache_read_tokens=usage.tokens.cache_read_tokens,
        cache_write_tokens=usage.tokens.cache_write_tokens,
        cost_micro_usd=usage.cost_micro_usd,
    )
