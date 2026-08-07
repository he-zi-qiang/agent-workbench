"""LlamaIndex's vector-store contract, answered by ``VectorIndexPort``.

This is the extension point that lets LlamaIndex own retrieval without owning
the collection. LlamaIndex builds the query -- embedding, top_k, mode, filters
-- and this store executes it through the port every other part of the system
already writes and reads through. The alternative, ``llama-index-vector-stores-qdrant``
over the same collection, would bring a second opinion about how points are
laid out and where fusion happens; ADR-017 gives both of those to this project
and to Qdrant respectively, so the integration is written at the layer above.

**Fusion happens once, inside Qdrant.** A hybrid query becomes exactly one
``search_hybrid`` call, whose two prefetches and single RRF are performed by the
database. This adapter never holds two ranked lists, which is the only way to
be certain it never combines them a second time. It also does not reorder what
comes back: the order the index produced is the order the nodes are returned
in, and a contract test pins that, because re-sorting is what a second fusion
would look like from the outside.

**An unrecognised filter is a refusal.** Tenant, knowledge base and principal
reach this adapter as LlamaIndex metadata filters, and they are the difference
between a query scoped to one customer and a query over everything. Dropping a
filter nobody understood would still return results -- more of them -- so the
translation refuses anything it cannot map exactly. Every rejection below is a
narrowing that would otherwise have gone missing quietly.

Note what these filters are *not*. They narrow; they do not authorize. What a
principal may actually read is decided against PostgreSQL by ``RetrievalService``
after this store returns, and again before an answer is released.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from llama_index.core.schema import BaseNode
from llama_index.core.vector_stores.types import (
    BasePydanticVectorStore,
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
    VectorStoreQuery,
    VectorStoreQueryMode,
    VectorStoreQueryResult,
)
from pydantic import PrivateAttr

from agent_workbench.adapters.llama_index.nodes import to_node
from agent_workbench.ports.sparse import SparseEncoderPort
from agent_workbench.ports.vector_index import VectorIndexPort

#: The metadata keys this store knows how to narrow by. They are the port's
#: own three arguments under LlamaIndex's names; there is intentionally no
#: passthrough for anything else, because a filter this adapter cannot express
#: is a narrowing the index would not perform.
TENANT_FILTER_KEY = "tenant_id"
KNOWLEDGE_BASE_FILTER_KEY = "knowledge_base_id"
PRINCIPAL_FILTER_KEY = "authorized_principal"

REQUIRED_FILTER_KEYS = (
    TENANT_FILTER_KEY,
    KNOWLEDGE_BASE_FILTER_KEY,
    PRINCIPAL_FILTER_KEY,
)


class UnsupportedFilterError(ValueError):
    """A query asked for narrowing this store cannot express, so it ran none."""


def build_filters(
    *, tenant_id: str, knowledge_base_id: str, principal_id: str
) -> MetadataFilters:
    """The only filter shape this store accepts, built where it is understood.

    Exported so the retriever does not hand-assemble a structure the store will
    then re-validate: one producer, one consumer, and a translation that can be
    tested against inputs the producer would never make.
    """

    return MetadataFilters(
        filters=[
            MetadataFilter(
                key=TENANT_FILTER_KEY, value=tenant_id, operator=FilterOperator.EQ
            ),
            MetadataFilter(
                key=KNOWLEDGE_BASE_FILTER_KEY,
                value=knowledge_base_id,
                operator=FilterOperator.EQ,
            ),
            MetadataFilter(
                key=PRINCIPAL_FILTER_KEY,
                value=principal_id,
                operator=FilterOperator.EQ,
            ),
        ],
        condition=FilterCondition.AND,
    )


def _narrowing(filters: MetadataFilters | None) -> dict[str, str]:
    """Translate LlamaIndex's filters into the port's arguments, or refuse."""

    if filters is None:
        raise UnsupportedFilterError(
            "a query with no filters would search every tenant in the collection"
        )
    if filters.condition != FilterCondition.AND:
        # OR over a tenant filter is a query that matches other tenants' points
        # by construction; NOT is worse. Only conjunction narrows.
        raise UnsupportedFilterError(
            f"filters must be combined with AND, not {filters.condition}"
        )

    narrowing: dict[str, str] = {}
    for entry in filters.filters:
        if not isinstance(entry, MetadataFilter):
            # A nested MetadataFilters can express a disjunction one level down,
            # which would re-admit exactly what the condition check above
            # rejects.
            raise UnsupportedFilterError("nested filter groups are not supported")
        if entry.key not in REQUIRED_FILTER_KEYS:
            raise UnsupportedFilterError(
                f"this store cannot narrow by {entry.key!r}; it would have been ignored"
            )
        if entry.operator != FilterOperator.EQ:
            raise UnsupportedFilterError(
                f"{entry.key!r} may only be matched with ==, not {entry.operator}"
            )
        if entry.key in narrowing:
            # Two values for one key under AND is either unsatisfiable or, if
            # this adapter picked one, a narrowing the caller did not ask for.
            raise UnsupportedFilterError(f"{entry.key!r} was filtered twice")
        if not isinstance(entry.value, str):
            raise UnsupportedFilterError(
                f"{entry.key!r} must be matched against a string"
            )
        narrowing[entry.key] = entry.value

    absent = [key for key in REQUIRED_FILTER_KEYS if key not in narrowing]
    if absent:
        raise UnsupportedFilterError(
            f"a query without {', '.join(absent)} would read beyond its scope"
        )
    return narrowing


class PortBackedVectorStore(BasePydanticVectorStore):
    """Reads through ``VectorIndexPort``; refuses to write through it."""

    stores_text: bool = True

    _index: VectorIndexPort = PrivateAttr()
    _sparse_encoder: SparseEncoderPort | None = PrivateAttr(default=None)

    def __init__(
        self,
        index: VectorIndexPort,
        *,
        sparse_encoder: SparseEncoderPort | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._index = index
        self._sparse_encoder = sparse_encoder

    @property
    def client(self) -> Any:
        """No native handle is exposed.

        LlamaIndex offers this so callers can reach past the abstraction. The
        thing behind this one is a port, and handing out the Qdrant client
        underneath it would let a caller run a query that skips both the
        narrowing above and the authorization after it.
        """

        return None

    def add(self, nodes: Sequence[BaseNode], **kwargs: Any) -> list[str]:
        raise NotImplementedError(
            "ingestion does not write through this adapter; a second write path "
            "into the same collection is what ADR-017's migration rules forbid"
        )

    def delete(self, ref_doc_id: str, **kwargs: Any) -> None:
        raise NotImplementedError(
            "deletion belongs to ingestion, which does not use this adapter"
        )

    def query(self, query: VectorStoreQuery, **kwargs: Any) -> VectorStoreQueryResult:
        raise NotImplementedError(
            "this store is async-only; the retriever uses the async path"
        )

    async def aquery(
        self, query: VectorStoreQuery, **kwargs: Any
    ) -> VectorStoreQueryResult:
        """One index call, whose result order is passed through untouched."""

        if query.query_embedding is None:
            raise UnsupportedFilterError("a query reached the store without a vector")

        narrowing = _narrowing(query.filters)
        vector = tuple(query.query_embedding)
        limit = query.similarity_top_k
        tenant_id = narrowing[TENANT_FILTER_KEY]
        knowledge_base_id = narrowing[KNOWLEDGE_BASE_FILTER_KEY]
        principals = (narrowing[PRINCIPAL_FILTER_KEY],)

        hybrid = query.mode == VectorStoreQueryMode.HYBRID
        if hybrid and self._sparse_encoder is None:
            # Silently answering a hybrid query with the dense arm would put a
            # dense measurement under a hybrid label, which is the one failure
            # an ablation cannot detect from its own output.
            raise UnsupportedFilterError(
                "a hybrid query reached a store with no sparse encoder"
            )

        if not hybrid or self._sparse_encoder is None:
            chunks = await self._index.search(
                vector=vector,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                authorized_principals=principals,
                limit=limit,
            )
        else:
            if query.query_str is None:
                raise UnsupportedFilterError(
                    "a hybrid query arrived without the text to encode lexically"
                )
            weights = await self._sparse_encoder.encode_query(query.query_str)
            chunks = await self._index.search_hybrid(
                vector=vector,
                sparse_indices=weights.indices,
                sparse_values=weights.values,
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                authorized_principals=principals,
                limit=limit,
                # Each arm proposes a full candidate set; the single RRF inside
                # Qdrant is what narrows them. Passing the retriever's
                # sparse_top_k/hybrid_top_k here instead would let LlamaIndex
                # shorten one list before fusion, which changes which retriever
                # is being measured.
                dense_limit=limit,
                sparse_limit=limit,
            )

        return VectorStoreQueryResult(
            nodes=[to_node(chunk) for chunk in chunks],
            similarities=[chunk.score for chunk in chunks],
            ids=[chunk.chunk_id for chunk in chunks],
        )


__all__ = [
    "KNOWLEDGE_BASE_FILTER_KEY",
    "PRINCIPAL_FILTER_KEY",
    "REQUIRED_FILTER_KEYS",
    "TENANT_FILTER_KEY",
    "PortBackedVectorStore",
    "UnsupportedFilterError",
    "build_filters",
]
