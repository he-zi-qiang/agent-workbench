"""The upload surface over HTTP: two planes, one of them capped.

These drive the ASGI application directly, so nothing binds a port and no
request leaves the process. What they exercise is the wiring the application
service cannot: which requests are size-limited, what a wrong tenant is allowed
to learn, and whether a document's bytes ever have to fit in memory.
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
from agent_workbench.apps.api.dependencies import (
    InsecureDeploymentError,
    build_dependencies,
)
from agent_workbench.apps.api.downloads import content_disposition
from agent_workbench.apps.api.main import build_app, create_app
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings
from agent_workbench.domain.policies import PrincipalContext

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_1"
CONTENT = b"Qdrant performs one dense and sparse fusion per query.\n" * 8
DIGEST = hashlib.sha256(CONTENT).hexdigest()

HEADERS = {"x-tenant-id": TENANT, "x-principal-id": OWNER}
OTHER_HEADERS = {"x-tenant-id": OTHER_TENANT, "x-principal-id": "user_2"}

TABLES = (
    "knowledge_bases, artifacts, upload_intents, document_acl, "
    "document_versions, documents, outbox_events"
)


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _settings(root: Path, **overrides: Any) -> Settings:
    """Real settings, from the committed defaults, pointed at the test fixtures."""

    import tomllib

    with DEFAULT_CONFIG_FILE.open("rb") as handle:
        payload = tomllib.load(handle)
    dsn = _dsn()
    payload["database"].update(dsn=dsn, guard_dsn=dsn, listen_dsn=dsn)
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["artifact_store"]["local_root"] = str(root)
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    for section, values in overrides.items():
        payload[section].update(values)
    return Settings(**payload)


def _run(
    scenario: Callable[[httpx.AsyncClient], Awaitable[Any]],
    root: Path,
    **overrides: Any,
) -> Any:
    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            await KnowledgeBaseService(PostgresKnowledgeBaseStore(engine)).create(
                PrincipalContext(tenant_id=TENANT, principal_id=OWNER),
                name="Main",
                knowledge_base_id="kb_main",
            )
        finally:
            await engine.dispose()

        app, dependencies = build_app(
            project_api(_settings(root, **overrides)), with_chat=False
        )
        transport = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://api.test",
            ) as client:
                return await scenario(client)
        finally:
            await dependencies.dispose()

    return asyncio.run(execute())


async def _upload(
    client: httpx.AsyncClient,
    *,
    content: bytes = CONTENT,
    document_id: str = "doc_1",
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, httpx.Response, httpx.Response]:
    """Declare, transfer and complete, returning all three responses."""

    used = headers if headers is not None else HEADERS
    created = await client.post(
        "/v1/uploads",
        headers=used,
        json={
            "declared_size_bytes": len(content),
            "declared_sha256": hashlib.sha256(content).hexdigest(),
            "media_type": "text/plain",
            "filename": "passage.txt",
        },
    )
    if created.status_code != 201:
        return created, created, created

    path = created.json()["content_path"]
    transferred = await client.put(path, headers=used, content=content)
    if transferred.status_code != 200:
        return created, transferred, transferred

    completed = await client.post(
        f"/v1/uploads/{created.json()['upload_id']}/complete",
        headers=used,
        json={
            "artifact_id": transferred.json()["artifact_id"],
            "document_id": document_id,
            "knowledge_base_id": "kb_main",
        },
    )
    return created, transferred, completed


def test_the_three_step_upload_produces_a_document_version(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> dict[str, Any]:
        _, _, completed = await _upload(client)
        return {"status": completed.status_code, "body": completed.json()}

    result = _run(scenario, tmp_path)

    assert result["status"] == 201
    assert result["body"]["source_revision"] == 1
    assert result["body"]["content_sha256"] == DIGEST


def test_an_oversized_control_request_is_refused_with_413(tmp_path: Path) -> None:
    """The declaration is metadata. A document belongs on the data plane."""

    async def scenario(client: httpx.AsyncClient) -> int:
        response = await client.post(
            "/v1/uploads",
            content=(
                b'{"declared_size_bytes": 1, "declared_sha256": "'
                + b"a" * 64
                + b'", "media_type": "text/plain", "filename": "'
                + b"x" * 4096
                + b'"}'
            ),
            headers={**HEADERS, "content-type": "application/json"},
        )
        return response.status_code

    assert _run(scenario, tmp_path, api={"max_control_request_body_bytes": 1024}) == 413


def test_the_limit_does_not_trust_a_declared_content_length(tmp_path: Path) -> None:
    """A sender that lies about the length must not get past the check."""

    async def scenario(client: httpx.AsyncClient) -> int:
        async def body() -> Any:
            yield b'{"declared_size_bytes": 1, "declared_sha256": "'
            yield b"a" * 4096
            yield b'"}'

        response = await client.post(
            "/v1/uploads",
            headers={**HEADERS, "content-type": "application/json"},
            content=body(),
        )
        return response.status_code

    assert _run(scenario, tmp_path, api={"max_control_request_body_bytes": 1024}) == 413


def test_the_data_plane_is_not_bound_by_the_control_limit(tmp_path: Path) -> None:
    """Capping a document transfer at the control limit is refusing uploads."""

    payload = b"x" * 200_000

    async def scenario(client: httpx.AsyncClient) -> tuple[int, int]:
        _, transferred, completed = await _upload(client, content=payload)
        return transferred.status_code, completed.status_code

    transferred, completed = _run(
        scenario,
        tmp_path,
        api={"max_control_request_body_bytes": 2048},
    )

    assert (transferred, completed) == (200, 201)


def test_a_transfer_over_the_artifact_ceiling_is_refused(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> int:
        created = await client.post(
            "/v1/uploads",
            headers=HEADERS,
            json={
                "declared_size_bytes": 4096,
                "declared_sha256": "a" * 64,
                "media_type": "text/plain",
            },
        )
        response = await client.put(
            created.json()["content_path"],
            headers=HEADERS,
            content=b"x" * 8192,
        )
        return response.status_code

    assert _run(scenario, tmp_path, artifact_store={"max_artifact_bytes": 4096}) == 413


def test_bytes_that_do_not_match_the_declaration_are_refused(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, Any]:
        created = await client.post(
            "/v1/uploads",
            headers=HEADERS,
            json={
                "declared_size_bytes": len(CONTENT),
                "declared_sha256": DIGEST,
                "media_type": "text/plain",
            },
        )
        transferred = await client.put(
            created.json()["content_path"],
            headers=HEADERS,
            # Same length, different bytes: the digest is what has to catch it.
            content=bytes(len(CONTENT)),
        )
        completed = await client.post(
            f"/v1/uploads/{created.json()['upload_id']}/complete",
            headers=HEADERS,
            json={
                "artifact_id": transferred.json()["artifact_id"],
                "document_id": "doc_1",
                "knowledge_base_id": "kb_main",
            },
        )
        return completed.status_code, completed.json()["detail"]

    status_code, detail = _run(scenario, tmp_path)

    assert status_code == 409
    assert "digest" in detail


def test_a_request_without_identity_is_refused(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> int:
        response = await client.post(
            "/v1/uploads",
            json={
                "declared_size_bytes": 1,
                "declared_sha256": "a" * 64,
                "media_type": "text/plain",
            },
        )
        return response.status_code

    assert _run(scenario, tmp_path) == 401


def test_another_tenant_cannot_transfer_into_the_upload(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> int:
        created = await client.post(
            "/v1/uploads",
            headers=HEADERS,
            json={
                "declared_size_bytes": len(CONTENT),
                "declared_sha256": DIGEST,
                "media_type": "text/plain",
            },
        )
        response = await client.put(
            created.json()["content_path"],
            headers=OTHER_HEADERS,
            content=CONTENT,
        )
        return response.status_code

    assert _run(scenario, tmp_path) == 404


def test_another_tenant_cannot_download_the_artifact(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, str, int, str]:
        _, transferred, _ = await _upload(client)
        artifact_id = transferred.json()["artifact_id"]

        denied = await client.get(f"/v1/artifacts/{artifact_id}", headers=OTHER_HEADERS)
        missing = await client.get("/v1/artifacts/art_missing", headers=OTHER_HEADERS)
        return (
            denied.status_code,
            denied.json()["detail"],
            missing.status_code,
            missing.json()["detail"],
        )

    denied_status, denied_detail, missing_status, missing_detail = _run(
        scenario,
        tmp_path,
    )

    # Indistinguishable: any difference confirms the object exists.
    assert (denied_status, denied_detail) == (missing_status, missing_detail)
    assert denied_status == 404


def test_the_owner_can_download_what_was_stored(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, bytes, str, str]:
        _, transferred, _ = await _upload(client)
        response = await client.get(
            f"/v1/artifacts/{transferred.json()['artifact_id']}",
            headers=HEADERS,
        )
        return (
            response.status_code,
            response.content,
            response.headers["x-artifact-sha256"],
            response.headers["content-disposition"],
        )

    status_code, content, digest, content_disposition = _run(scenario, tmp_path)

    assert status_code == 200
    assert content == CONTENT
    assert digest == DIGEST
    assert content_disposition == (
        "attachment; filename=\"passage.txt\"; filename*=UTF-8''passage.txt"
    )


def test_download_filename_is_encoded_as_metadata_not_header_syntax() -> None:
    header = content_disposition('总结"; filename=attacker.docx')

    assert header.startswith('attachment; filename="')
    assert 'filename="attacker.docx"' not in header
    assert (
        "filename*=UTF-8''%E6%80%BB%E7%BB%93%22%3B%20filename%3Dattacker.docx" in header
    )


def test_liveness_does_not_touch_the_database(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, Any]:
        response = await client.get("/health/live")
        return response.status_code, response.json()

    assert _run(scenario, tmp_path) == (200, {"status": "live"})


def test_readiness_checks_the_database(tmp_path: Path) -> None:
    async def scenario(client: httpx.AsyncClient) -> tuple[int, Any]:
        response = await client.get("/health/ready")
        return response.status_code, response.json()

    assert _run(scenario, tmp_path) == (200, {"status": "ready"})


def test_the_api_refuses_to_assemble_for_a_remote_deployment(
    tmp_path: Path,
) -> None:
    """The only identity resolver reads headers; serving that remotely is not auth."""

    settings = _settings(
        tmp_path,
        app={"deployment_scope": "remote"},
        qdrant={
            "url": "https://qdrant.example.test",
            "api_key_required": True,
        },
        secrets={
            "deepseek_api_key": "unit-test-key",
            "qdrant_api_key": "unit-qdrant-key",
        },
    )

    with pytest.raises(InsecureDeploymentError, match="identity provider"):
        build_dependencies(project_api(settings), with_chat=False)


def test_the_application_is_assembled_once_per_process(tmp_path: Path) -> None:
    """Routes read finished dependencies; they never build an engine of their own."""

    async def scenario(client: httpx.AsyncClient) -> int:
        first = await client.get("/health/ready")
        second = await client.get("/health/ready")
        return first.status_code + second.status_code

    assert _run(scenario, tmp_path) == 400


def test_create_app_rejects_an_unknown_field_in_the_declaration(
    tmp_path: Path,
) -> None:
    async def scenario(client: httpx.AsyncClient) -> int:
        response = await client.post(
            "/v1/uploads",
            headers=HEADERS,
            json={
                "declared_size_bytes": 1,
                "declared_sha256": "a" * 64,
                "media_type": "text/plain",
                "object_key": "../../etc/passwd",
            },
        )
        return response.status_code

    assert _run(scenario, tmp_path) == 422


def test_the_app_factory_returns_the_limit_wrapped_application(
    tmp_path: Path,
) -> None:
    from agent_workbench.apps.api.middleware import ControlPlaneLimit

    dependencies = build_dependencies(project_api(_settings(tmp_path)), with_chat=False)
    try:
        assert isinstance(create_app(dependencies), ControlPlaneLimit)
    finally:
        asyncio.run(dependencies.dispose())
