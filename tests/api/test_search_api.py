"""The retrieval endpoint, and the two things about it that are new.

Whether a passage may be read is ``RetrievalService``'s question and is tested
against real PostgreSQL in ``tests/vector/test_authorized_retrieval``. What is
new here is the surface: that the principal comes from the request rather than
from the body, that the endpoint exists exactly when this process can retrieve,
and that a missing model provider no longer takes retrieval down with it.

That last one is the whole reason the endpoint exists. Retrieval used to be
reachable only inside a chat turn, so a deployment with no provider could index
documents and never look at them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_workbench.application.retrieval import AuthorizedContext, RetrievalRequest
from agent_workbench.apps.api.routes import search
from agent_workbench.apps.api.routes.search import SearchRequest
from agent_workbench.apps.api.state import STATE_ATTRIBUTE
from agent_workbench.domain.context import Citation, ContextChunk, ContextPacket
from agent_workbench.domain.policies import PrincipalContext

OWNER = {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"}
NEIGHBOUR = {"x-tenant-id": "tenant_a", "x-principal-id": "user_2"}
STRANGER = {"x-tenant-id": "tenant_b", "x-principal-id": "user_1"}

CHUNK = "chk_" + "a" * 32


class _Retrieval:
    """Records who it was asked to search as, and answers for user_1 only."""

    mode = "hybrid"

    def __init__(self) -> None:
        self.asked: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> AuthorizedContext:
        self.asked.append(request)
        readable = request.tenant_id == "tenant_a" and request.principal_id == "user_1"
        if not readable:
            return AuthorizedContext(packet=ContextPacket(), authorized_revisions=())
        return AuthorizedContext(
            packet=ContextPacket(
                chunks=(
                    ContextChunk(
                        chunk_id=CHUNK,
                        document_id="doc_1",
                        document_version="v1",
                        tenant_id="tenant_a",
                        text="Fusion runs inside the database.",
                    ),
                ),
                citations=(
                    Citation(
                        document_id="doc_1", chunk_id=CHUNK, document_version="v1"
                    ),
                ),
            ),
            authorized_revisions=(("doc_1", 1),),
        )


class _Principals:
    def resolve(self, request: Any) -> PrincipalContext:
        return PrincipalContext(
            tenant_id=request.headers["x-tenant-id"],
            principal_id=request.headers["x-principal-id"],
        )


class _Dependencies:
    def __init__(self, retrieval: object | None) -> None:
        self.retrieval = retrieval
        self.principals = _Principals()

    @property
    def serves_search(self) -> bool:
        return self.retrieval is not None


def _app(retrieval: object | None) -> FastAPI:
    app = FastAPI()
    setattr(app.state, STATE_ATTRIBUTE, _Dependencies(retrieval))
    app.include_router(search.router)
    return app


def _post(app: FastAPI, headers: dict[str, str], **body: Any) -> Any:
    async def call() -> Any:
        transport = ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        async with AsyncClient(transport=transport, base_url="http://api.test") as c:
            return await c.post(
                "/v1/search",
                headers=headers,
                json={
                    "query": "where does fusion run",
                    "knowledge_base_id": "kb",
                    **body,
                },
            )

    return asyncio.run(call())


# --------------------------------------------------------------------------
# Who the search runs as
# --------------------------------------------------------------------------


def test_the_search_runs_as_the_resolved_principal() -> None:
    retrieval = _Retrieval()

    response = _post(_app(retrieval), OWNER)

    assert response.status_code == 200
    asked = retrieval.asked[0]
    assert (asked.tenant_id, asked.principal_id) == ("tenant_a", "user_1")
    assert response.json()["hits"][0]["chunk_id"] == CHUNK
    assert response.json()["retriever"] == "hybrid"


@pytest.mark.parametrize("headers", [NEIGHBOUR, STRANGER])
def test_someone_else_searches_as_themselves_and_finds_nothing(
    headers: dict[str, str],
) -> None:
    """The control group is the test above: same query, different caller.

    Empty rather than forbidden. "Nothing there" and "nothing there for you"
    have to be the same answer, or the endpoint becomes a way to ask whether a
    document exists.
    """

    retrieval = _Retrieval()

    response = _post(_app(retrieval), headers)

    assert response.status_code == 200
    assert response.json()["hits"] == []
    assert response.json()["citations"] == []
    asked = retrieval.asked[0]
    assert (asked.tenant_id, asked.principal_id) == (
        headers["x-tenant-id"],
        headers["x-principal-id"],
    )


def test_the_body_cannot_name_whose_documents_to_search() -> None:
    """A caller that could would be choosing its own permissions.

    ``extra="forbid"`` makes it a 422 rather than a field quietly ignored --
    the same rule the knowledge_search tool follows, for the same reason.
    """

    for smuggled in ("principal_id", "tenant_id", "owner_id"):
        response = _post(_app(_Retrieval()), OWNER, **{smuggled: "user_admin"})
        assert response.status_code == 422, smuggled

    assert set(SearchRequest.model_fields) == {"query", "knowledge_base_id", "top_k"}


# --------------------------------------------------------------------------
# When the endpoint exists
# --------------------------------------------------------------------------


def test_a_process_that_cannot_retrieve_refuses_rather_than_answers() -> None:
    """Unreachable through a real app -- the router is not mounted without
    retrieval, which `test_chat_assembly` pins -- so this asserts the guard
    itself rather than a status code no deployment can produce."""

    from fastapi import Request

    from agent_workbench.apps.api.routes.search import SearchUnavailableError, search

    scope = {
        "type": "http",
        "headers": [(k.encode(), v.encode()) for k, v in OWNER.items()],
        "app": _app(None),
    }
    with pytest.raises(SearchUnavailableError):
        asyncio.run(
            search(
                SearchRequest(query="q", knowledge_base_id="kb"),
                Request(scope),  # type: ignore[arg-type]
            )
        )


def test_the_response_carries_no_tenant() -> None:
    """The caller's own tenant is the only one that can appear, so echoing it
    back adds nothing and makes every response a place identity could leak
    from."""

    body = _post(_app(_Retrieval()), OWNER).json()

    assert "tenant_id" not in body["hits"][0]
