"""P1-1. Two people in one tenant are still two people.

The existing IDOR tests all swap the tenant, and every one of them passed while
a colleague could take over your upload, overwrite your document and replace
its ACL. A tenant says whose database this is; it does not say who is asking.

So everything here holds the tenant fixed and changes only the principal. The
ids are handed over deliberately -- these tests grant the attacker knowledge of
the upload id and the document id, because that is the situation being defended
against. An id that travels through a log line, a URL or a support ticket is
not a secret, and "hard to guess" is not an authorization rule.

Refusals are 404 rather than 403: learning that upload ``upl_x`` exists but is
not yours is itself an answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import (
    PostgresKnowledgeBaseStore,
    create_query_engine,
)
from agent_workbench.application.knowledge_bases import KnowledgeBaseService
from agent_workbench.apps.api.main import build_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.artifact_store import DEFAULT_CHUNK_BYTES

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TENANT = "tenant_a"
OWNER = "user_owner"
NEIGHBOUR = "user_neighbour"

# One tenant, two principals. This is the whole point of the file.
OWNER_HEADERS = {"x-tenant-id": TENANT, "x-principal-id": OWNER}
NEIGHBOUR_HEADERS = {"x-tenant-id": TENANT, "x-principal-id": NEIGHBOUR}

CONTENT = b"Dense and sparse retrieval are fused once per query.\n" * 4
HOSTILE = b"Replaced by somebody who was merely in the same tenant.\n" * 4

TABLES = (
    "knowledge_bases, artifacts, upload_intents, document_acl, "
    "document_versions, documents, outbox_events"
)


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _settings(root: Path) -> Settings:
    import tomllib

    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload: dict[str, Any] = tomllib.load(handle)
    dsn = _dsn()
    payload["database"].update(dsn=dsn, guard_dsn=dsn, listen_dsn=dsn)
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["artifact_store"]["local_root"] = str(root)
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    return Settings(**payload)


async def _seed_knowledge_bases(engine: Any) -> None:
    service = KnowledgeBaseService(PostgresKnowledgeBaseStore(engine))
    for knowledge_base_id, owner_id in (
        ("kb_main", OWNER),
        ("kb_other", OWNER),
        ("kb_neighbour", NEIGHBOUR),
    ):
        await service.create(
            PrincipalContext(tenant_id=TENANT, principal_id=owner_id),
            name=knowledge_base_id,
            knowledge_base_id=knowledge_base_id,
        )


def _run(scenario: Callable[[httpx.AsyncClient], Awaitable[Any]], root: Path) -> Any:
    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            await _seed_knowledge_bases(engine)
        finally:
            await engine.dispose()

        app, dependencies = build_app(project_api(_settings(root)), with_chat=False)
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test"
            ) as client:
                return await scenario(client)
        finally:
            await dependencies.dispose()

    return asyncio.run(execute())


async def _declare(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    content: bytes = CONTENT,
) -> tuple[str, str]:
    """Declare an upload; return its id and content path."""

    response = await client.post(
        "/v1/uploads",
        headers=headers,
        json={
            "declared_size_bytes": len(content),
            "declared_sha256": hashlib.sha256(content).hexdigest(),
            "media_type": "text/plain",
            "filename": "private-notes.txt",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["upload_id"], body["content_path"]


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    content: bytes = CONTENT,
    document_id: str = "doc_1",
    knowledge_base_id: str = "kb_main",
    granted: tuple[str, ...] = (),
) -> httpx.Response:
    """A whole upload by one principal, returning the completion response."""

    upload_id, path = await _declare(client, headers, content)
    transferred = await client.put(path, headers=headers, content=content)
    assert transferred.status_code == 200, transferred.text
    return await client.post(
        f"/v1/uploads/{upload_id}/complete",
        headers=headers,
        json={
            "artifact_id": transferred.json()["artifact_id"],
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "granted_principals": list(granted),
        },
    )


# --- taking over somebody else's upload ---------------------------------------


def test_a_neighbour_cannot_transfer_into_the_owners_upload(tmp_path: Path) -> None:
    """The transfer route reads the intent first, and must not find this one."""

    async def scenario(client: httpx.AsyncClient) -> int:
        _, path = await _declare(client, OWNER_HEADERS)
        response = await client.put(path, headers=NEIGHBOUR_HEADERS, content=CONTENT)
        return response.status_code

    assert _run(scenario, tmp_path) == 404


def test_the_refusal_does_not_leak_the_declared_filename(tmp_path: Path) -> None:
    """A 404 that quoted the intent would answer the question it refused."""

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str]:
        _, path = await _declare(client, OWNER_HEADERS)
        response = await client.put(path, headers=NEIGHBOUR_HEADERS, content=CONTENT)
        return response.status_code, response.text

    status, body = _run(scenario, tmp_path)

    # Asserted together: a body with no filename in it proves nothing if the
    # request was allowed through in the first place.
    assert status == 404
    assert "private-notes.txt" not in body


def test_a_neighbour_cannot_complete_the_owners_upload(tmp_path: Path) -> None:
    """Even holding bytes that satisfy the declaration exactly."""

    async def scenario(client: httpx.AsyncClient) -> int:
        upload_id, path = await _declare(client, OWNER_HEADERS)
        transferred = await client.put(path, headers=OWNER_HEADERS, content=CONTENT)
        response = await client.post(
            f"/v1/uploads/{upload_id}/complete",
            headers=NEIGHBOUR_HEADERS,
            json={
                "artifact_id": transferred.json()["artifact_id"],
                "document_id": "doc_1",
                "knowledge_base_id": "kb_main",
            },
        )
        return response.status_code

    assert _run(scenario, tmp_path) == 404


# --- overwriting somebody else's document -------------------------------------


def test_a_neighbour_cannot_commit_a_version_to_the_owners_document(
    tmp_path: Path,
) -> None:
    """The attack the tenant boundary never stopped: their upload, your document."""

    async def scenario(client: httpx.AsyncClient) -> int:
        assert (await _upload(client, OWNER_HEADERS)).status_code == 201
        hostile = await _upload(
            client,
            NEIGHBOUR_HEADERS,
            content=HOSTILE,
            knowledge_base_id="kb_neighbour",
        )
        return hostile.status_code

    assert _run(scenario, tmp_path) == 404


def test_the_refused_overwrite_leaves_the_document_untouched(tmp_path: Path) -> None:
    """A refusal that still wrote would be worse than no refusal at all.

    Read from the database rather than by downloading the owner's artifact:
    that artifact survives either way, because a takeover writes a *new* one
    and moves the document to it. What has to be unchanged is which version
    the document points at.
    """

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int, str]:
        first = await _upload(client, OWNER_HEADERS)
        await _upload(
            client,
            NEIGHBOUR_HEADERS,
            content=HOSTILE,
            knowledge_base_id="kb_neighbour",
        )
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                versions = (
                    await connection.execute(
                        text("SELECT count(*) FROM document_versions")
                    )
                ).scalar_one()
                revision = (
                    await connection.execute(
                        text("SELECT source_revision FROM documents")
                    )
                ).scalar_one()
                digest = (
                    await connection.execute(
                        text("SELECT content_sha256 FROM document_versions")
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        assert first.status_code == 201
        return int(versions), int(revision), str(digest)

    assert _run(scenario, tmp_path) == (
        1,
        1,
        hashlib.sha256(CONTENT).hexdigest(),
    )


def test_a_read_grant_does_not_confer_the_right_to_overwrite(tmp_path: Path) -> None:
    """Being on the ACL means the document is visible, not writable."""

    async def scenario(client: httpx.AsyncClient) -> int:
        granted = await _upload(client, OWNER_HEADERS, granted=(NEIGHBOUR,))
        assert granted.status_code == 201, granted.text
        hostile = await _upload(
            client,
            NEIGHBOUR_HEADERS,
            content=HOSTILE,
            knowledge_base_id="kb_neighbour",
        )
        return hostile.status_code

    assert _run(scenario, tmp_path) == 404


def test_the_owner_can_still_commit_a_second_version(tmp_path: Path) -> None:
    """The control: the refusal is about who is asking, not about the path."""

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        first = await _upload(client, OWNER_HEADERS)
        second = await _upload(client, OWNER_HEADERS, content=HOSTILE)
        return first.json()["source_revision"], second.json()["source_revision"]

    assert _run(scenario, tmp_path) == (1, 2)


def test_a_neighbour_can_own_their_own_document(tmp_path: Path) -> None:
    """The second control: same tenant, different document, no refusal."""

    async def scenario(client: httpx.AsyncClient) -> int:
        await _upload(client, OWNER_HEADERS)
        theirs = await _upload(
            client,
            NEIGHBOUR_HEADERS,
            content=HOSTILE,
            document_id="doc_2",
            knowledge_base_id="kb_neighbour",
        )
        return theirs.status_code

    assert _run(scenario, tmp_path) == 201


# --- the knowledge base a document is actually in -----------------------------


def test_a_version_cannot_move_the_document_to_another_knowledge_base(
    tmp_path: Path,
) -> None:
    """The row would keep KB-A while the outbox told the index KB-B."""

    async def scenario(client: httpx.AsyncClient) -> int:
        await _upload(client, OWNER_HEADERS, knowledge_base_id="kb_main")
        moved = await _upload(
            client,
            OWNER_HEADERS,
            content=HOSTILE,
            knowledge_base_id="kb_other",
        )
        return moved.status_code

    assert _run(scenario, tmp_path) == 409


def test_the_rejected_move_leaves_no_outbox_event(tmp_path: Path) -> None:
    """A refused commit must not tell the index anything at all."""

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        await _upload(client, OWNER_HEADERS, knowledge_base_id="kb_main")
        await _upload(
            client, OWNER_HEADERS, content=HOSTILE, knowledge_base_id="kb_other"
        )
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.connect() as connection:
                events = (
                    await connection.execute(text("SELECT count(*) FROM outbox_events"))
                ).scalar_one()
                revision = (
                    await connection.execute(
                        text("SELECT source_revision FROM documents")
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        return int(events), int(revision)

    assert _run(scenario, tmp_path) == (1, 1)


# --- downloading somebody else's bytes ----------------------------------------


def test_a_neighbour_cannot_download_the_uploaded_bytes(tmp_path: Path) -> None:
    """P1-2. The artifact id comes back in the transfer response and travels."""

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str]:
        completed = await _upload(client, OWNER_HEADERS)
        artifact = completed.json()["artifact_id"]
        response = await client.get(
            f"/v1/artifacts/{artifact}", headers=NEIGHBOUR_HEADERS
        )
        return response.status_code, response.text

    status, body = _run(scenario, tmp_path)

    assert status == 404
    assert "private-notes.txt" not in body


def test_a_read_grant_does_not_yet_reach_the_bytes(tmp_path: Path) -> None:
    """A known limitation, pinned so it cannot change without somebody noticing.

    Artifacts are owned by the principal that stored them. The document ACL
    does not reach them, because nothing maps an artifact back to the document
    version that references it. A granted principal can therefore see that the
    document exists and not download it.

    Fail-closed and incomplete, in that order. When the reverse lookup lands,
    this test is the one that has to change, deliberately.
    """

    async def scenario(client: httpx.AsyncClient) -> int:
        completed = await _upload(client, OWNER_HEADERS, granted=(NEIGHBOUR,))
        artifact = completed.json()["artifact_id"]
        response = await client.get(
            f"/v1/artifacts/{artifact}", headers=NEIGHBOUR_HEADERS
        )
        return response.status_code

    assert _run(scenario, tmp_path) == 404


def test_the_uploader_can_still_download_what_they_stored(tmp_path: Path) -> None:
    """The control."""

    async def scenario(client: httpx.AsyncClient) -> tuple[int, str]:
        completed = await _upload(client, OWNER_HEADERS)
        artifact = completed.json()["artifact_id"]
        response = await client.get(f"/v1/artifacts/{artifact}", headers=OWNER_HEADERS)
        return response.status_code, response.text

    assert _run(scenario, tmp_path) == (200, CONTENT.decode())


def test_the_download_leaves_the_app_in_more_than_one_piece(tmp_path: Path) -> None:
    """P1-4. "StreamingResponse" over one whole ``bytes`` streams nothing.

    Counted off the ASGI wire rather than through the test client, which
    buffers a response into a single piece and would report success either
    way. What is asserted here is the number of ``http.response.body``
    messages the application actually emits.

    The object is deliberately larger than one chunk; below that size a single
    piece is the correct answer and would prove nothing.
    """

    big = b"x" * (DEFAULT_CHUNK_BYTES * 2 + 17)

    async def execute() -> tuple[int, bytes]:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            await _seed_knowledge_bases(engine)
        finally:
            await engine.dispose()

        app, dependencies = build_app(project_api(_settings(tmp_path)), with_chat=False)
        try:
            transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
            async with httpx.AsyncClient(
                transport=transport, base_url="http://api.test"
            ) as client:
                completed = await _upload(client, OWNER_HEADERS, content=big)
            artifact = completed.json()["artifact_id"]

            sent: list[dict[str, Any]] = []

            # One request message, then disconnect. A receive() that kept
            # answering "http.request" spins StreamingResponse forever: it
            # polls for the disconnect that ends the response.
            pending = [
                {"type": "http.request", "body": b"", "more_body": False},
                {"type": "http.disconnect"},
            ]

            async def receive() -> dict[str, Any]:
                return pending.pop(0) if pending else {"type": "http.disconnect"}

            async def send(message: dict[str, Any]) -> None:
                sent.append(message)

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "path": f"/v1/artifacts/{artifact}",
                    "raw_path": f"/v1/artifacts/{artifact}".encode(),
                    "query_string": b"",
                    "root_path": "",
                    "scheme": "http",
                    "headers": [
                        (key.encode(), value.encode())
                        for key, value in OWNER_HEADERS.items()
                    ],
                    "client": ("127.0.0.1", 51234),
                    "server": ("api.test", 80),
                },
                receive,
                send,
            )
            # Non-empty only. Starlette always appends a final empty body
            # message to close the response, so counting every one of them
            # would report two pieces for a single whole-object yield -- which
            # is exactly the shape this test exists to reject.
            bodies = [
                message["body"]
                for message in sent
                if message["type"] == "http.response.body" and message.get("body")
            ]
            return len(bodies), b"".join(bodies)
        finally:
            await dependencies.dispose()

    count, body = asyncio.run(execute())

    assert body == big
    assert count > 1


def test_a_refused_download_never_starts_a_body(tmp_path: Path) -> None:
    """The status has to be decided before the first byte leaves."""

    async def scenario(client: httpx.AsyncClient) -> tuple[int, bytes]:
        completed = await _upload(client, OWNER_HEADERS)
        artifact = completed.json()["artifact_id"]
        async with client.stream(
            "GET", f"/v1/artifacts/{artifact}", headers=NEIGHBOUR_HEADERS
        ) as response:
            return response.status_code, await response.aread()

    status, body = _run(scenario, tmp_path)

    assert status == 404
    assert CONTENT not in body
