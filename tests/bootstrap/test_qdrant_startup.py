"""Qdrant startup checks use the read alias without mutating remote routing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from qdrant_client import models

from agent_workbench.bootstrap.projections import EmbeddingConfig, QdrantConfig
from agent_workbench.bootstrap.qdrant_startup import (
    QdrantStartupError,
    verify_qdrant_startup,
)
from agent_workbench.ports.vector_index import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME

EMBEDDING = EmbeddingConfig(
    model_id="test-embedder",
    revision="test-revision",
    vector_size=4,
    batch_size=8,
    device="cpu",
)


def _collections() -> dict[str, object]:
    return {}


def _aliases() -> dict[str, str]:
    return {}


def _payload_indexes() -> list[tuple[str, str]]:
    return []


def _config(*, bootstrap: bool) -> QdrantConfig:
    return QdrantConfig(
        url="http://qdrant:6333",
        read_alias="knowledge_active",
        write_collection="knowledge_write_v1",
        api_key=None,
        request_timeout_seconds=10,
        distance="cosine",
        allow_local_bootstrap=bootstrap,
    )


def _collection(size: int = 4, *, sparse: bool = True) -> object:
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=size, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors=(
                    {SPARSE_VECTOR_NAME: models.SparseVectorParams()} if sparse else {}
                ),
            )
        )
    )


@dataclass
class _Client:
    collections: dict[str, object] = field(default_factory=_collections)
    aliases: dict[str, str] = field(default_factory=_aliases)
    payload_indexes: list[tuple[str, str]] = field(default_factory=_payload_indexes)
    alias_updates: int = 0

    async def collection_exists(self, name: str) -> bool:
        return name in self.collections

    async def create_collection(
        self,
        *,
        collection_name: str,
        vectors_config: object,
        sparse_vectors_config: object,
    ) -> None:
        self.collections[collection_name] = SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=vectors_config,
                    sparse_vectors=sparse_vectors_config,
                )
            )
        )

    async def create_payload_index(
        self, *, collection_name: str, field_name: str, field_schema: object
    ) -> None:
        del field_schema
        self.payload_indexes.append((collection_name, field_name))

    async def get_collection(self, name: str) -> object:
        if name not in self.collections:
            raise RuntimeError("collection not found")
        return self.collections[name]

    async def get_aliases(self) -> object:
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias, collection_name=collection)
                for alias, collection in self.aliases.items()
            ]
        )

    async def update_collection_aliases(self, operations: object) -> None:
        self.alias_updates += 1
        for operation in cast(list[Any], operations):
            created = getattr(operation, "create_alias", None)
            if created is not None:
                self.aliases[created.alias_name] = created.collection_name


def test_empty_local_profile_creates_the_write_collection_and_missing_alias() -> None:
    async def scenario() -> _Client:
        client = _Client()
        await verify_qdrant_startup(
            client, qdrant=_config(bootstrap=True), embedding=EMBEDDING
        )
        return client

    client = asyncio.run(scenario())

    assert client.aliases == {"knowledge_active": "knowledge_write_v1"}
    assert set(client.payload_indexes) == {
        ("knowledge_write_v1", "tenant_id"),
        ("knowledge_write_v1", "knowledge_base_id"),
        ("knowledge_write_v1", "authorized_principals"),
    }


@pytest.mark.parametrize(
    "collection",
    (_collection(size=7), _collection(sparse=False)),
)
def test_schema_mismatch_is_rejected_before_serving(collection: object) -> None:
    async def scenario() -> None:
        client = _Client(
            collections={"knowledge_write_v1": collection},
            aliases={"knowledge_active": "knowledge_write_v1"},
        )
        await verify_qdrant_startup(
            client, qdrant=_config(bootstrap=False), embedding=EMBEDDING
        )

    with pytest.raises(QdrantStartupError, match=r"(dense vector size|sparse vector)"):
        asyncio.run(scenario())


def test_remote_style_scope_rejects_a_missing_alias_without_creating_one() -> None:
    async def scenario() -> _Client:
        client = _Client(collections={"knowledge_write_v1": _collection()})
        await verify_qdrant_startup(
            client, qdrant=_config(bootstrap=False), embedding=EMBEDDING
        )
        return client

    with pytest.raises(QdrantStartupError, match=r"read alias.*missing"):
        asyncio.run(scenario())


def test_active_read_alias_may_target_a_different_valid_generation() -> None:
    async def scenario() -> _Client:
        client = _Client(
            collections={
                "knowledge_write_v1": _collection(),
                "knowledge_active_v0": _collection(),
            },
            aliases={"knowledge_active": "knowledge_active_v0"},
        )
        await verify_qdrant_startup(
            client, qdrant=_config(bootstrap=True), embedding=EMBEDDING
        )
        return client

    client = asyncio.run(scenario())

    assert client.aliases["knowledge_active"] == "knowledge_active_v0"
    assert client.alias_updates == 0


def test_lifespan_disposes_dependencies_when_qdrant_startup_fails() -> None:
    """A failed async check must not leave its client/engine process-owned."""

    class _Uploads:
        def is_data_plane_path(self, path: str) -> bool:
            del path
            return False

    class _Dependencies:
        max_control_request_body_bytes = 1024
        uploads = _Uploads()
        serves_chat = False
        serves_search = False
        chat_reaper = None
        chat_pending_recovery = None

        def __init__(self) -> None:
            self.disposed = False

        async def startup(self) -> None:
            raise QdrantStartupError("schema mismatch")

        async def dispose(self) -> None:
            self.disposed = True

    async def scenario() -> bool:
        from agent_workbench.apps.api.dependencies import ApiDependencies
        from agent_workbench.apps.api.main import create_app
        from agent_workbench.apps.api.middleware import ControlPlaneLimit

        dependencies = _Dependencies()
        wrapped = create_app(cast(ApiDependencies, dependencies))
        assert isinstance(wrapped, ControlPlaneLimit)
        app = cast(Any, wrapped)._app
        with pytest.raises(QdrantStartupError, match="schema mismatch"):
            async with app.router.lifespan_context(app):
                raise AssertionError("startup should have failed")
        return dependencies.disposed

    assert asyncio.run(scenario()) is True
