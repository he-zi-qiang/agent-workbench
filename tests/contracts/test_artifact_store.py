"""Contract for the artifact store, including cross-tenant indistinguishability.

Every test runs against both the in-memory store and the local filesystem one.
The filesystem is where object keys stop being an abstraction, so it is where
"the caller never supplies a location" has to hold literally.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
from harness import StoreHarness

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.ports.artifact_store import ArtifactStore

CONTENT = b"Qdrant owns hybrid fusion.\n"
TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_1"
NEIGHBOUR = "user_2"


async def _stored(store: ArtifactStore) -> ArtifactRef:
    return await store.put(
        tenant_id=TENANT,
        owner_id=OWNER,
        kind="tool_result",
        media_type="text/plain",
        content=CONTENT,
        filename="passage.txt",
    )


def test_stored_bytes_come_back_unchanged(artifacts: StoreHarness) -> None:
    async def scenario(store: ArtifactStore) -> bytes:
        ref = await _stored(store)
        return await store.get(
            tenant_id=TENANT, artifact_id=ref.artifact_id, principal_id=OWNER
        )

    assert artifacts.run(scenario) == CONTENT


def test_the_reference_records_size_and_digest(artifacts: StoreHarness) -> None:
    async def scenario(store: ArtifactStore) -> ArtifactRef:
        return await _stored(store)

    ref = artifacts.run(scenario)

    assert ref.size_bytes == len(CONTENT)
    assert ref.sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert ref.tenant_id == TENANT


def test_the_store_generates_the_object_identity(artifacts: StoreHarness) -> None:
    """A caller supplies a tenant and bytes, never a location."""

    async def scenario(store: ArtifactStore) -> tuple[str, str]:
        first = await _stored(store)
        second = await _stored(store)
        return first.artifact_id, second.artifact_id

    first_id, second_id = artifacts.run(scenario)

    assert first_id != second_id
    assert "/" not in first_id


def test_empty_content_is_storable(artifacts: StoreHarness) -> None:
    """A zero-byte result is a result, not a missing one."""

    async def scenario(store: ArtifactStore) -> tuple[int, bytes]:
        ref = await store.put(
            tenant_id=TENANT,
            owner_id=OWNER,
            kind="tool_result",
            media_type="text/plain",
            content=b"",
        )
        return ref.size_bytes, await store.get(
            tenant_id=TENANT,
            artifact_id=ref.artifact_id,
            principal_id=OWNER,
        )

    assert artifacts.run(scenario) == (0, b"")


def test_another_tenant_cannot_read_the_object(artifacts: StoreHarness) -> None:
    async def scenario(store: ArtifactStore) -> None:
        ref = await _stored(store)
        await store.get(
            tenant_id=OTHER_TENANT, artifact_id=ref.artifact_id, principal_id=OWNER
        )

    with pytest.raises(NotFoundError):
        artifacts.run(scenario)


def test_a_wrong_tenant_and_a_missing_id_fail_identically(
    artifacts: StoreHarness,
) -> None:
    """Any difference between the two would confirm the object exists."""

    async def scenario(store: ArtifactStore) -> tuple[str, str]:
        ref = await _stored(store)
        wrong_tenant = ""
        unknown = ""
        try:
            await store.head(
                tenant_id=OTHER_TENANT, artifact_id=ref.artifact_id, principal_id=OWNER
            )
        except NotFoundError as exc:
            wrong_tenant = str(exc)
        try:
            await store.head(
                tenant_id=OTHER_TENANT, artifact_id="art_missing", principal_id=OWNER
            )
        except NotFoundError as exc:
            unknown = str(exc)
        return wrong_tenant, unknown

    wrong_tenant, unknown = artifacts.run(scenario)

    assert wrong_tenant == unknown
    assert wrong_tenant != ""


def test_head_describes_without_transferring(artifacts: StoreHarness) -> None:
    async def scenario(store: ArtifactStore) -> ArtifactRef:
        ref = await _stored(store)
        return await store.head(
            tenant_id=TENANT, artifact_id=ref.artifact_id, principal_id=OWNER
        )

    described = artifacts.run(scenario)

    assert described.filename == "passage.txt"
    assert described.kind == "tool_result"
    assert described.media_type == "text/plain"


def test_a_neighbour_in_the_same_tenant_cannot_read_it(
    artifacts: StoreHarness,
) -> None:
    """P1-2. An artifact id is not a secret, so it cannot be the credential.

    Ids appear in tool results, event payloads and URLs. A store that answered
    any id belonging to the right tenant would make every one of those places
    a capability that was never granted.
    """

    async def scenario(store: ArtifactStore) -> None:
        ref = await _stored(store)
        await store.get(
            tenant_id=TENANT, artifact_id=ref.artifact_id, principal_id=NEIGHBOUR
        )

    with pytest.raises(NotFoundError):
        artifacts.run(scenario)


def test_a_neighbour_cannot_describe_it_either(artifacts: StoreHarness) -> None:
    """Size, digest and filename are as much the owner's as the bytes."""

    async def scenario(store: ArtifactStore) -> None:
        ref = await _stored(store)
        await store.head(
            tenant_id=TENANT, artifact_id=ref.artifact_id, principal_id=NEIGHBOUR
        )

    with pytest.raises(NotFoundError):
        artifacts.run(scenario)


def test_the_refusal_matches_a_missing_id_exactly(artifacts: StoreHarness) -> None:
    """Three refusals, one message: not yours, not your tenant's, not there."""

    async def scenario(store: ArtifactStore) -> list[str]:
        ref = await _stored(store)
        outcomes: list[str] = []
        for tenant, principal, artifact_id in (
            (TENANT, NEIGHBOUR, ref.artifact_id),
            (OTHER_TENANT, OWNER, ref.artifact_id),
            (TENANT, OWNER, "art_00000000000000000000000000000"),
        ):
            try:
                await store.head(
                    tenant_id=tenant,
                    artifact_id=artifact_id,
                    principal_id=principal,
                )
            except NotFoundError as refusal:
                outcomes.append(str(refusal))
            else:
                # Recorded rather than swallowed: collecting only the refusals
                # would let a case that was allowed through leave the set
                # looking uniform.
                outcomes.append("allowed")
        return outcomes

    assert artifacts.run(scenario) == ["artifact not found"] * 3


def test_the_owner_still_reads_their_own(artifacts: StoreHarness) -> None:
    """The control: the refusal is about who is asking, not about reads."""

    async def scenario(store: ArtifactStore) -> bytes:
        ref = await _stored(store)
        return await store.get(
            tenant_id=TENANT, artifact_id=ref.artifact_id, principal_id=OWNER
        )

    assert artifacts.run(scenario) == CONTENT


def test_two_principals_get_separate_artifacts(artifacts: StoreHarness) -> None:
    """Identical bytes from two people are two objects, each one theirs."""

    async def scenario(store: ArtifactStore) -> tuple[bool, bytes, bytes]:
        mine = await store.put(
            tenant_id=TENANT,
            owner_id=OWNER,
            kind="tool_result",
            media_type="text/plain",
            content=CONTENT,
        )
        theirs = await store.put(
            tenant_id=TENANT,
            owner_id=NEIGHBOUR,
            kind="tool_result",
            media_type="text/plain",
            content=CONTENT,
        )
        return (
            mine.artifact_id != theirs.artifact_id,
            await store.get(
                tenant_id=TENANT, artifact_id=mine.artifact_id, principal_id=OWNER
            ),
            await store.get(
                tenant_id=TENANT,
                artifact_id=theirs.artifact_id,
                principal_id=NEIGHBOUR,
            ),
        )

    assert artifacts.run(scenario) == (True, CONTENT, CONTENT)


def test_a_streamed_artifact_is_owned_too(artifacts: StoreHarness) -> None:
    """put_stream is the upload path, so it is the one that matters most."""

    async def scenario(store: ArtifactStore) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            yield CONTENT

        ref = await store.put_stream(
            tenant_id=TENANT,
            owner_id=OWNER,
            kind="source_document",
            media_type="text/plain",
            chunks=chunks(),
            max_bytes=1024,
        )
        await store.get(
            tenant_id=TENANT, artifact_id=ref.artifact_id, principal_id=NEIGHBOUR
        )

    with pytest.raises(NotFoundError):
        artifacts.run(scenario)
