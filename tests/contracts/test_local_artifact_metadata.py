"""What the filesystem store does with metadata it cannot vouch for.

The shared contract covers behaviour every backend must reproduce. This covers
the sidecar itself, which only this store has: what it writes, and what it does
when it reads back something it did not write.

Fail closed is the whole rule here. Metadata that cannot say who may read an
object is not permission to let anyone read it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.artifacts.local import METADATA_FORMAT
from agent_workbench.domain.errors import NotFoundError

CONTENT = b"Fusion happens once per query.\n"
TENANT = "tenant_a"
OWNER = "user_1"


def _store(root: Path) -> LocalArtifactStore:
    return LocalArtifactStore(root)


def _stored(store: LocalArtifactStore) -> str:
    async def run() -> str:
        reference = await store.put(
            tenant_id=TENANT,
            owner_id=OWNER,
            kind="tool_result",
            media_type="text/plain",
            content=CONTENT,
        )
        return reference.artifact_id

    return asyncio.run(run())


def _sidecar(root: Path, artifact_id: str) -> Path:
    return root / TENANT / f"{artifact_id}.json"


def _head(store: LocalArtifactStore, artifact_id: str, principal_id: str) -> Any:
    async def run() -> Any:
        return await store.head(
            tenant_id=TENANT, artifact_id=artifact_id, principal_id=principal_id
        )

    return asyncio.run(run())


def test_the_sidecar_records_the_owner_beside_the_reference(tmp_path: Path) -> None:
    """Beside it, not inside it: an ArtifactRef travels and stays a pointer."""

    store = _store(tmp_path)
    artifact_id = _stored(store)

    envelope = json.loads(_sidecar(tmp_path, artifact_id).read_text(encoding="utf-8"))

    assert envelope["owner_id"] == OWNER
    assert envelope["format"] == METADATA_FORMAT
    assert "owner_id" not in envelope["reference"]


def test_metadata_written_before_ownership_existed_is_unreadable(
    tmp_path: Path,
) -> None:
    """A bare serialized reference names nobody, so it authorizes nobody."""

    store = _store(tmp_path)
    artifact_id = _stored(store)
    reference = _head(store, artifact_id, OWNER)
    _sidecar(tmp_path, artifact_id).write_text(
        reference.model_dump_json(), encoding="utf-8"
    )

    with pytest.raises(NotFoundError):
        _head(store, artifact_id, OWNER)


def test_an_envelope_from_a_later_format_is_unreadable(tmp_path: Path) -> None:
    """Even one naming the right owner.

    This is what the format marker is for, and the reason it is asserted
    separately: the previous test would pass without it, because a bare
    reference happens to carry no owner either. A future format that moved or
    renamed the owner would not.
    """

    store = _store(tmp_path)
    artifact_id = _stored(store)
    sidecar = _sidecar(tmp_path, artifact_id)
    envelope = json.loads(sidecar.read_text(encoding="utf-8"))
    envelope["format"] = METADATA_FORMAT + 1
    sidecar.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(NotFoundError):
        _head(store, artifact_id, OWNER)


def test_a_sidecar_that_is_not_an_object_is_unreadable(tmp_path: Path) -> None:
    """Valid JSON is not the same as metadata."""

    store = _store(tmp_path)
    artifact_id = _stored(store)
    _sidecar(tmp_path, artifact_id).write_text('["not", "an", "envelope"]', "utf-8")

    with pytest.raises(NotFoundError):
        _head(store, artifact_id, OWNER)


def test_an_intact_sidecar_is_still_readable(tmp_path: Path) -> None:
    """The control: these refusals are about damaged metadata, not about reads."""

    store = _store(tmp_path)
    artifact_id = _stored(store)

    assert _head(store, artifact_id, OWNER).size_bytes == len(CONTENT)
