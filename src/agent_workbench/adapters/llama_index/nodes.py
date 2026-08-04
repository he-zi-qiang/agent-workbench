"""Converting between this project's chunks and LlamaIndex's nodes.

Both directions live here, in one file, because they are one contract. A
forward mapping that stops writing a field and a reverse mapping that starts
defaulting it are individually harmless and jointly a silent data loss, and
splitting them across two modules is how that pair gets written by two people
who each read only their own side.

The reverse direction is strict on purpose. Every field it reads is load
bearing somewhere downstream -- ``source_revision`` is what the publish fence
compares, ``document_id`` is what authorization is decided on, ``ordinal`` and
``page`` are what a citation points at -- so a missing or mistyped value is a
defect to raise at the boundary, not a default to invent. A defaulted
``source_revision`` would not fail loudly: it would compare unequal to the row
PostgreSQL holds, every candidate would be dropped as stale, and retrieval
would return nothing while looking like it merely found nothing.

``page`` is the one field that is legitimately absent, and the round trip has
to keep absence distinguishable from a value. A format without pages has no
page 1; writing one would send a reader to a location this system invented.
"""

from __future__ import annotations

from typing import Any

from llama_index.core.schema import MetadataMode, NodeWithScore, TextNode

from agent_workbench.ports.vector_index import ScoredChunk

#: Metadata keys a node must carry for a chunk to be rebuilt from it. ``page``
#: is deliberately not here: it is required to be *present*, but its value may
#: be ``None``.
REQUIRED_METADATA_KEYS = (
    "document_id",
    "document_version",
    "tenant_id",
    "knowledge_base_id",
    "source_revision",
    "ordinal",
)


class NodeMappingError(ValueError):
    """A node did not carry what a chunk needs, so no chunk was invented."""


def to_node(chunk: ScoredChunk) -> TextNode:
    """Present one stored chunk as a LlamaIndex node."""

    return TextNode(
        id_=chunk.chunk_id,
        text=chunk.text,
        metadata={
            "document_id": chunk.document_id,
            "document_version": chunk.document_version,
            "tenant_id": chunk.tenant_id,
            "knowledge_base_id": chunk.knowledge_base_id,
            "source_revision": chunk.source_revision,
            "ordinal": chunk.ordinal,
            "page": chunk.page,
        },
    )


def from_node(scored: NodeWithScore) -> ScoredChunk:
    """Rebuild the stored chunk a node was made from, or refuse.

    The score comes from ``NodeWithScore`` rather than from metadata because
    that is where the retriever puts it, and carrying it twice would let the
    two disagree.
    """

    node = scored.node
    metadata: dict[str, Any] = dict(node.metadata)
    missing = [key for key in REQUIRED_METADATA_KEYS if key not in metadata]
    if missing:
        raise NodeMappingError(
            f"node {node.node_id} is missing {', '.join(missing)}; "
            "it was not produced by this adapter"
        )
    if "page" not in metadata:
        raise NodeMappingError(
            f"node {node.node_id} does not say whether it has a page; "
            "absence must be recorded, not assumed"
        )

    # A node the retriever returned without a score is not a candidate with
    # score zero -- it is a candidate whose rank is unknown, and everything
    # downstream reads position as meaning.
    if scored.score is None:
        raise NodeMappingError(f"node {node.node_id} came back without a score")

    try:
        return ScoredChunk(
            chunk_id=node.node_id,
            document_id=metadata["document_id"],
            document_version=metadata["document_version"],
            tenant_id=metadata["tenant_id"],
            knowledge_base_id=metadata["knowledge_base_id"],
            source_revision=metadata["source_revision"],
            # NONE explicitly, not by default. LlamaIndex can render a node's
            # metadata into its content -- that is what the other metadata
            # modes are for -- and this project's metadata is tenant ids and
            # revision numbers. Inheriting whichever mode a future version
            # defaults to would prepend those to the passage a model reads and
            # a citation quotes, without failing anything.
            text=node.get_content(metadata_mode=MetadataMode.NONE),
            ordinal=metadata["ordinal"],
            page=metadata["page"],
            score=scored.score,
        )
    except ValueError as invalid:
        raise NodeMappingError(
            f"node {node.node_id} carries a value no chunk can hold: {invalid}"
        ) from invalid


__all__ = ["REQUIRED_METADATA_KEYS", "NodeMappingError", "from_node", "to_node"]
