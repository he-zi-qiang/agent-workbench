"""The knowledge-base API is a readable product projection, not an id textbox."""

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

from agent_workbench.adapters.persistence import create_query_engine
from agent_workbench.apps.api.main import build_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_owner"
NEIGHBOUR = "user_neighbour"
STRANGER = "user_stranger"

OWNER_HEADERS = {"x-tenant-id": TENANT, "x-principal-id": OWNER}
NEIGHBOUR_HEADERS = {"x-tenant-id": TENANT, "x-principal-id": NEIGHBOUR}
STRANGER_HEADERS = {"x-tenant-id": TENANT, "x-principal-id": STRANGER}
OTHER_TENANT_HEADERS = {
    "x-tenant-id": OTHER_TENANT,
    "x-principal-id": OWNER,
}

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


def _run(scenario: Callable[[httpx.AsyncClient], Awaitable[Any]], root: Path) -> Any:
    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
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


async def _create_base(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] = OWNER_HEADERS,
    name: str = "Employee handbook",
) -> dict[str, Any]:
    response = await client.post(
        "/v1/knowledge-bases",
        headers=headers,
        json={"name": name, "description": "People policies"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload(
    client: httpx.AsyncClient,
    *,
    knowledge_base_id: str,
    document_id: str,
    headers: dict[str, str] = OWNER_HEADERS,
    filename: str = "handbook.md",
    content: bytes = b"Vacation policy.\n",
    granted_principals: tuple[str, ...] = (),
) -> httpx.Response:
    digest = hashlib.sha256(content).hexdigest()
    intent = await client.post(
        "/v1/uploads",
        headers=headers,
        json={
            "declared_size_bytes": len(content),
            "declared_sha256": digest,
            "media_type": "text/markdown",
            "filename": filename,
        },
    )
    assert intent.status_code == 201, intent.text
    transferred = await client.put(
        intent.json()["content_path"], headers=headers, content=content
    )
    assert transferred.status_code == 200, transferred.text
    return await client.post(
        f"/v1/uploads/{intent.json()['upload_id']}/complete",
        headers=headers,
        json={
            "artifact_id": transferred.json()["artifact_id"],
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "granted_principals": list(granted_principals),
        },
    )


def test_create_list_and_get_use_the_same_human_facing_contract(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[dict[str, Any], Any, Any]:
        created = await _create_base(client)
        listed = await client.get("/v1/knowledge-bases", headers=OWNER_HEADERS)
        fetched = await client.get(
            f"/v1/knowledge-bases/{created['knowledge_base_id']}",
            headers=OWNER_HEADERS,
        )
        return created, listed.json(), fetched.json()

    created, listed, fetched = _run(scenario, tmp_path)

    assert created["knowledge_base_id"].startswith("kb_")
    assert set(created) == {
        "knowledge_base_id",
        "name",
        "description",
        "document_count",
        "ready_document_count",
        "processing_document_count",
        "created_at",
        "updated_at",
    }
    assert created["document_count"] == 0
    assert listed == {"knowledge_bases": [created]}
    assert fetched == created


def test_unknown_cross_owner_and_cross_tenant_reads_are_identical_404s(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient) -> list[tuple[int, str]]:
        created = await _create_base(client)
        base_id = created["knowledge_base_id"]
        responses = [
            await client.get(
                f"/v1/knowledge-bases/{base_id}", headers=NEIGHBOUR_HEADERS
            ),
            await client.get(
                f"/v1/knowledge-bases/{base_id}", headers=OTHER_TENANT_HEADERS
            ),
            await client.get("/v1/knowledge-bases/kb_missing", headers=OWNER_HEADERS),
        ]
        return [(response.status_code, response.text) for response in responses]

    responses = _run(scenario, tmp_path)

    assert {status for status, _ in responses} == {404}
    assert len({body for _, body in responses}) == 1
    assert "knowledge base not found" in responses[0][1]


def test_document_acl_reveals_only_the_base_and_documents_it_grants(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[Any, Any, Any]:
        created = await _create_base(client)
        base_id = created["knowledge_base_id"]
        shared = await _upload(
            client,
            knowledge_base_id=base_id,
            document_id="doc_shared",
            granted_principals=(NEIGHBOUR,),
        )
        private = await _upload(
            client,
            knowledge_base_id=base_id,
            document_id="doc_private",
            filename="private.md",
        )
        assert shared.status_code == private.status_code == 201
        owner_list = await client.get("/v1/knowledge-bases", headers=OWNER_HEADERS)
        neighbour_list = await client.get(
            "/v1/knowledge-bases", headers=NEIGHBOUR_HEADERS
        )
        neighbour_docs = await client.get(
            f"/v1/knowledge-bases/{base_id}/documents",
            headers=NEIGHBOUR_HEADERS,
        )
        return owner_list.json(), neighbour_list.json(), neighbour_docs.json()

    owner, neighbour, documents = _run(scenario, tmp_path)

    assert owner["knowledge_bases"][0]["document_count"] == 2
    assert neighbour["knowledge_bases"][0]["document_count"] == 1
    assert [document["document_id"] for document in documents["documents"]] == [
        "doc_shared"
    ]


def test_document_projection_reports_verified_metadata_and_real_index_state(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[Any, Any]:
        created = await _create_base(client)
        base_id = created["knowledge_base_id"]
        content = b"A real uploaded document.\n"
        completed = await _upload(
            client,
            knowledge_base_id=base_id,
            document_id="doc_1",
            filename="facts.md",
            content=content,
        )
        assert completed.status_code == 201, completed.text
        processing = await client.get(
            f"/v1/knowledge-bases/{base_id}/documents", headers=OWNER_HEADERS
        )
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE documents SET last_applied_revision = source_revision "
                        "WHERE document_id = 'doc_1'"
                    )
                )
        finally:
            await engine.dispose()
        ready = await client.get(
            f"/v1/knowledge-bases/{base_id}/documents", headers=OWNER_HEADERS
        )
        return processing.json(), ready.json()

    processing, ready = _run(scenario, tmp_path)

    first = processing["documents"][0]
    assert first["filename"] == "facts.md"
    assert first["media_type"] == "text/markdown"
    assert first["size_bytes"] == len(b"A real uploaded document.\n")
    assert (first["source_revision"], first["last_applied_revision"]) == (1, 0)
    assert first["status"] == "processing"
    assert ready["documents"][0]["status"] == "ready"
    assert ready["documents"][0]["last_applied_revision"] == 1


def test_upload_completion_requires_owner_write_access_to_the_target_base(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, int, int]:
        created = await _create_base(client)
        refused = await _upload(
            client,
            knowledge_base_id=created["knowledge_base_id"],
            document_id="doc_refused",
            headers=NEIGHBOUR_HEADERS,
        )
        missing = await _upload(
            client,
            knowledge_base_id="kb_missing",
            document_id="doc_missing",
            headers=OWNER_HEADERS,
        )
        documents = await client.get(
            f"/v1/knowledge-bases/{created['knowledge_base_id']}/documents",
            headers=OWNER_HEADERS,
        )
        return (
            refused.status_code,
            missing.status_code,
            len(documents.json()["documents"]),
        )

    assert _run(scenario, tmp_path) == (404, 404, 0)
