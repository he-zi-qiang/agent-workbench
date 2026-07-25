"""Contract for the artifact store, including cross-tenant indistinguishability."""

from __future__ import annotations

import asyncio
import hashlib

import pytest

from agent_workbench.adapters.memory import InMemoryArtifactStore
from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import NotFoundError

CONTENT = b"Qdrant owns hybrid fusion.\n"
TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"


async def _stored(store: InMemoryArtifactStore) -> ArtifactRef:
    return await store.put(
        tenant_id=TENANT,
        kind="tool_result",
        media_type="text/plain",
        content=CONTENT,
        filename="passage.txt",
    )


def test_stored_bytes_come_back_unchanged() -> None:
    async def scenario() -> bytes:
        store = InMemoryArtifactStore()
        ref = await _stored(store)
        return await store.get(tenant_id=TENANT, artifact_id=ref.artifact_id)

    assert asyncio.run(scenario()) == CONTENT


def test_the_reference_records_size_and_digest() -> None:
    async def scenario() -> ArtifactRef:
        return await _stored(InMemoryArtifactStore())

    ref = asyncio.run(scenario())

    assert ref.size_bytes == len(CONTENT)
    assert ref.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert ref.tenant_id == TENANT


def test_the_store_generates_the_object_identity() -> None:
    """A caller supplies a tenant and bytes, never a location."""

    async def scenario() -> tuple[str, str]:
        store = InMemoryArtifactStore()
        first = await _stored(store)
        second = await _stored(store)
        return first.artifact_id, second.artifact_id

    first_id, second_id = asyncio.run(scenario())

    assert first_id != second_id
    assert "/" not in first_id


def test_another_tenant_cannot_read_the_object() -> None:
    async def scenario() -> None:
        store = InMemoryArtifactStore()
        ref = await _stored(store)
        await store.get(tenant_id=OTHER_TENANT, artifact_id=ref.artifact_id)

    with pytest.raises(NotFoundError):
        asyncio.run(scenario())


def test_a_wrong_tenant_and_a_missing_id_fail_identically() -> None:
    """Any difference between the two would confirm the object exists."""

    async def wrong_tenant() -> str:
        store = InMemoryArtifactStore()
        ref = await _stored(store)
        try:
            await store.head(tenant_id=OTHER_TENANT, artifact_id=ref.artifact_id)
        except NotFoundError as exc:
            return str(exc)
        raise AssertionError("expected NotFoundError")

    async def unknown_id() -> str:
        store = InMemoryArtifactStore()
        try:
            await store.head(tenant_id=OTHER_TENANT, artifact_id="art_missing")
        except NotFoundError as exc:
            return str(exc)
        raise AssertionError("expected NotFoundError")

    assert asyncio.run(wrong_tenant()) == asyncio.run(unknown_id())


def test_head_describes_without_transferring() -> None:
    async def scenario() -> ArtifactRef:
        store = InMemoryArtifactStore()
        ref = await _stored(store)
        return await store.head(tenant_id=TENANT, artifact_id=ref.artifact_id)

    described = asyncio.run(scenario())

    assert described.filename == "passage.txt"
    assert described.kind == "tool_result"
