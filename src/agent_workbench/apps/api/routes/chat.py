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

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_workbench.application.chat import (
    ChatRequest,
    ChatService,
    new_session_id,
)
from agent_workbench.apps.api.disconnects import watched
from agent_workbench.apps.api.state import dependencies_of
from agent_workbench.domain.context import Citation
from agent_workbench.domain.identifiers import Identifier
from agent_workbench.ports.cancellation import CancellationSource

CHAT_PREFIX = "/v1/chat"

QUESTION_MAX_LENGTH = 4096
TITLE_MAX_LENGTH = 200


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=TITLE_MAX_LENGTH)


class CreateSessionResponse(BaseModel):
    session_id: Identifier


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


class MessageView(BaseModel):
    role: str
    text: str


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
                role=message.role,
                text="".join(
                    block.text for block in message.content if block.kind == "text"
                ),
            )
            for message in messages
        )
    )


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
    "router",
]
