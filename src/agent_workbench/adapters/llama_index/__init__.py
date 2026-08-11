"""LlamaIndex, confined to the layer ADR-017 gives it.

Three adapters and a mapping: an embedding, a vector store, a retriever, and
the conversion between this project's chunks and LlamaIndex's nodes. Together
they let LlamaIndex own ingestion-and-retrieval mechanics without owning any
of the decisions around them -- the collection layout, the one fusion (this
process's own since ADR-033), and authorization and answer release all stay
the application's.

What is absent is as deliberate as what is here. No agent executor, no query
engine, no response synthesizer, no second fusion. Those are the roles
``rag.llama_index`` declares off, and the architecture guard enforces the first
two by refusing their imports anywhere in the source tree -- a flag can be left
unread, an import cannot be left unmade.
"""

from agent_workbench.adapters.llama_index.embedding import PortBackedEmbedding
from agent_workbench.adapters.llama_index.nodes import (
    NodeMappingError,
    from_node,
    to_node,
)
from agent_workbench.adapters.llama_index.retriever import LlamaIndexCandidateRetriever
from agent_workbench.adapters.llama_index.vector_store import (
    PortBackedVectorStore,
    UnsupportedFilterError,
    build_filters,
)

__all__ = [
    "LlamaIndexCandidateRetriever",
    "NodeMappingError",
    "PortBackedEmbedding",
    "PortBackedVectorStore",
    "UnsupportedFilterError",
    "build_filters",
    "from_node",
    "to_node",
]
