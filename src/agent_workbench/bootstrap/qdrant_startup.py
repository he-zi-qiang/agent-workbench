"""Fail-closed Qdrant collection and read-alias startup validation.

The write collection is an ingestion target.  Chat always queries the read
alias so a validated alias switch routes only new requests. Startup is the
only place where a local development/test process may create that collection
and bind a missing alias; remote deployments must already have both in place.
"""

from __future__ import annotations

from typing import Any, cast

from qdrant_client import models

from agent_workbench.adapters.vector.qdrant import FILTER_KEYS
from agent_workbench.bootstrap.projections import EmbeddingConfig, QdrantConfig
from agent_workbench.ports.vector_index import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME


class QdrantStartupError(RuntimeError):
    """The live index cannot safely serve the configured retrieval contract."""


async def verify_qdrant_startup(
    client: Any,
    *,
    qdrant: QdrantConfig,
    embedding: EmbeddingConfig,
) -> None:
    """Validate the write schema and bind the read alias before serving.

    ``allow_local_bootstrap`` is deliberately an explicit deployment setting.
    It is never inferred from a missing collection, and cannot be enabled in a
    remote scope by settings validation.  That keeps an operator typo from
    creating a fresh empty index in production.
    """

    exists = await client.collection_exists(qdrant.write_collection)
    if not exists:
        if not qdrant.allow_local_bootstrap:
            raise QdrantStartupError(
                "configured Qdrant write collection is missing; creation is disabled"
            )
        await _create_or_verify_write_collection(
            client, qdrant=qdrant, embedding=embedding
        )
    await _verify_collection_schema(
        client,
        collection=qdrant.write_collection,
        qdrant=qdrant,
        embedding=embedding,
    )
    await _verify_or_bind_read_alias(client, qdrant=qdrant, embedding=embedding)


async def _create_or_verify_write_collection(
    client: Any,
    *,
    qdrant: QdrantConfig,
    embedding: EmbeddingConfig,
) -> None:
    """Create a local collection once, accepting only the normal startup race."""

    try:
        await client.create_collection(
            collection_name=qdrant.write_collection,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=embedding.vector_size,
                    distance=_distance(qdrant.distance),
                )
            },
            sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams()},
        )
    except Exception as error:
        if not await client.collection_exists(qdrant.write_collection):
            raise QdrantStartupError(
                "could not create the configured Qdrant write collection"
            ) from error
    for key in FILTER_KEYS:
        await client.create_payload_index(
            collection_name=qdrant.write_collection,
            field_name=key,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


async def _verify_collection_schema(
    client: Any,
    *,
    collection: str,
    qdrant: QdrantConfig,
    embedding: EmbeddingConfig,
) -> None:
    """Reject a collection that cannot safely accept/query our vectors."""

    try:
        response = await client.get_collection(collection)
    except Exception as error:
        raise QdrantStartupError(
            f"configured Qdrant collection {collection!r} is unavailable"
        ) from error
    try:
        params: Any = response.config.params
        vectors: object = params.vectors
        dense = (
            cast(dict[str, models.VectorParams], vectors).get(DENSE_VECTOR_NAME)
            if isinstance(vectors, dict)
            else None
        )
        sparse: object = params.sparse_vectors
    except (AttributeError, KeyError, TypeError) as error:
        raise QdrantStartupError(
            f"configured Qdrant collection {collection!r} has an incompatible schema"
        ) from error

    if dense is None or dense.size != embedding.vector_size:
        found = None if dense is None else dense.size
        raise QdrantStartupError(
            f"configured Qdrant collection {collection!r} has dense vector size "
            f"{found!r}, expected {embedding.vector_size}"
        )
    if dense.distance != _distance(qdrant.distance):
        raise QdrantStartupError(
            f"configured Qdrant collection {collection!r} has a different "
            "dense distance"
        )
    if not isinstance(sparse, dict) or SPARSE_VECTOR_NAME not in sparse:
        raise QdrantStartupError(
            f"configured Qdrant collection {collection!r} has no required sparse vector"
        )


async def _verify_or_bind_read_alias(
    client: Any,
    *,
    qdrant: QdrantConfig,
    embedding: EmbeddingConfig,
) -> None:
    aliases: Any = await client.get_aliases()
    try:
        alias_entries = cast(list[models.AliasDescription], aliases.aliases)
        current = next(
            (
                alias.collection_name
                for alias in alias_entries
                if alias.alias_name == qdrant.read_alias
            ),
            None,
        )
    except AttributeError as error:
        raise QdrantStartupError(
            "Qdrant returned an invalid aliases response"
        ) from error

    if current is None:
        if not qdrant.allow_local_bootstrap:
            raise QdrantStartupError(
                f"configured Qdrant read alias {qdrant.read_alias!r} is missing"
            )
        await client.update_collection_aliases(
            [
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(
                        collection_name=qdrant.write_collection,
                        alias_name=qdrant.read_alias,
                    )
                )
            ]
        )
        return

    # A different target is the ordinary blue/green state. It is never
    # "repaired" back to the writer; the reader only needs that target to
    # satisfy the active embedding/schema contract.
    await _verify_collection_schema(
        client,
        collection=current,
        qdrant=qdrant,
        embedding=embedding,
    )


def _distance(value: str) -> models.Distance:
    if value == "cosine":
        return models.Distance.COSINE
    raise QdrantStartupError(f"unsupported configured Qdrant distance {value!r}")


__all__ = ["QdrantStartupError", "verify_qdrant_startup"]
