"""``EmbeddingPort`` wearing LlamaIndex's ``BaseEmbedding`` interface.

The retriever needs an embedder LlamaIndex can call; this project has one
already, behind its own port and loaded once per process. Building a second
one -- LlamaIndex's own HuggingFace embedding wrapper over the same weights --
would put two copies of a multi-gigabyte model in memory and, worse, would make
the vectors that answer a query come from a different object than the vectors
that filled the index. Those are only equal by convention, and nothing would
notice when they stopped being.

Only the async query path is implemented. The synchronous methods raise, and
that is a statement rather than an omission: this port is async all the way
down, so a working ``_get_query_embedding`` would have to either block the
event loop the request is running on or start a second one. Both are worse than
an error, because both are invisible.

Nothing embeds documents through here yet. ADR-017 assigns ingestion to
LlamaIndex too, but that is a later step with its own migration evidence; until
then the ingestion path builds its vectors directly and the text methods say so
by refusing.
"""

from __future__ import annotations

from typing import Any

from llama_index.core.base.embeddings.base import BaseEmbedding
from pydantic import PrivateAttr

from agent_workbench.ports.embedding import EmbeddingPort

#: What LlamaIndex reports this embedding as. The real identity -- model and
#: revision -- lives on the port and travels into the index identity, which is
#: what an evaluation report is compared by. This name exists so a LlamaIndex
#: callback has something to print, and must not be mistaken for the former.
MODEL_NAME = "agent-workbench-embedding-port"


class PortBackedEmbedding(BaseEmbedding):
    """LlamaIndex's embedding contract, answered by this project's embedder."""

    _port: EmbeddingPort = PrivateAttr()

    def __init__(self, port: EmbeddingPort, **kwargs: Any) -> None:
        super().__init__(model_name=MODEL_NAME, **kwargs)
        self._port = port

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return list(await self._port.embed_query(query))

    def _get_query_embedding(self, query: str) -> list[float]:
        raise NotImplementedError(
            "this embedding is async-only; use the retriever's async path"
        )

    def _get_text_embedding(self, text: str) -> list[float]:
        raise NotImplementedError(
            "documents are not embedded through the LlamaIndex adapter yet"
        )


__all__ = ["MODEL_NAME", "PortBackedEmbedding"]
