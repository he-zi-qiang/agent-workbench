"""Turning one document version into indexed chunks.

Parse, chunk, embed, write. The order matters in one place: everything is
embedded before anything is written, so a failure halfway through the model
leaves the index untouched rather than holding some chunks of a version and
none of the rest. A partially indexed document is worse than an unindexed one
-- retrieval finds the half that exists and answers as though that were all
there was.

Chunk ids are derived, not generated. The same document version chunked the
same way must land on the same ids every time, or a re-index writes a second
copy of every chunk beside the first. The derivation therefore includes
everything that decides what a chunk *is*: the version it came from, its
position, and the index identity, which carries the chunker and the embedder.
Change the embedder and the ids change, because the vectors are no longer
comparable and the old points are no longer answers.

Authorization is copied in, never decided here. The caller passes the ACL it
read from PostgreSQL; this service writes it into the payload so a query can
narrow on it, and the retrieval side re-checks it against PostgreSQL before
anything reaches a context packet.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agent_workbench.application.chunking import Chunker
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.ingestion import DocumentParser
from agent_workbench.ports.sparse import SparseEncoderPort, SparseVector
from agent_workbench.ports.vector_index import IndexedChunk, VectorIndexPort

CHUNK_ID_PREFIX = "chk"


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    """One document version to index, with the facts PostgreSQL holds about it."""

    tenant_id: str
    knowledge_base_id: str
    document_id: str
    document_version: str
    owner_id: str
    authorized_principals: tuple[str, ...]
    source_revision: int
    media_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class IngestionService:
    """Parse, chunk, embed and index one document version."""

    parser: DocumentParser
    chunker: Chunker
    embedder: EmbeddingPort
    index: VectorIndexPort
    # Absent when the process has no sparse runtime. Points are then written
    # dense-only, and they stay retrievable -- what must not happen is a
    # collection where some versions carry term weights and some do not
    # without anything recording which, so the identity below says.
    sparse_encoder: SparseEncoderPort | None = None

    @property
    def index_identity(self) -> str:
        """What an index built by this service is.

        Two services that disagree on either half produce vectors and
        boundaries that are not interchangeable, so they must not share a
        collection -- and must not silently share chunk ids either.
        """

        dense_and_chunks = f"{self.embedder.identity}+{self.chunker.identity}"
        if self.sparse_encoder is None:
            return dense_and_chunks
        # Sparse changes what a point *is*, not just what else it carries: a
        # hybrid query over a half-sparse collection ranks the dense-only
        # points by one arm and the rest by two. Different identity, different
        # chunk ids, so the two never share a point.
        return f"{dense_and_chunks}+{self.sparse_encoder.identity}"

    def chunk_id(self, document_version: str, ordinal: int) -> str:
        """The stable id for one chunk of one version under this identity."""

        digest = hashlib.sha256(
            f"{self.index_identity}|{document_version}|{ordinal}".encode()
        ).hexdigest()
        return f"{CHUNK_ID_PREFIX}_{digest[:32]}"

    async def _sparse_for(self, texts: tuple[str, ...]) -> tuple[SparseVector, ...]:
        """Term weights for each chunk, or empty ones when there is no encoder.

        Encoded in the same all-or-nothing way as the dense vectors: a failure
        must not leave a version whose first chunks match terms and whose rest
        do not, because that version would rank against itself.
        """

        if self.sparse_encoder is None:
            return tuple(SparseVector() for _ in texts)
        weights = await self.sparse_encoder.encode_documents(texts)
        if len(weights) != len(texts):
            raise ValueError(
                f"the sparse encoder returned {len(weights)} vectors "
                f"for {len(texts)} chunks"
            )
        return weights

    async def ingest(self, request: IngestionRequest) -> tuple[IndexedChunk, ...]:
        """Index one version, returning what was written."""

        parsed = self.parser.parse(request.content, media_type=request.media_type)
        # Pages travel with the text they were extracted from. A chunk whose
        # locator has no page is a chunk from a format that has none, never a
        # chunk whose page nobody passed along.
        chunks = self.chunker.split(parsed.text, page_starts=parsed.page_starts)
        if not chunks:
            # An empty document is not a failure -- an upload can legitimately
            # be blank -- but it has nothing to retrieve, and writing a point
            # for it would put an empty passage in somebody's context.
            return ()

        # Embedded first, all of it. A model failure mid-document must not
        # leave a half-indexed version behind.
        vectors = await self.embedder.embed_documents(
            tuple(chunk.text for chunk in chunks)
        )
        if len(vectors) != len(chunks):
            raise ValueError(
                f"the embedder returned {len(vectors)} vectors for {len(chunks)} chunks"
            )

        sparse = await self._sparse_for(tuple(chunk.text for chunk in chunks))

        indexed = tuple(
            IndexedChunk(
                chunk_id=self.chunk_id(request.document_version, chunk.ordinal),
                document_id=request.document_id,
                document_version=request.document_version,
                tenant_id=request.tenant_id,
                knowledge_base_id=request.knowledge_base_id,
                owner_id=request.owner_id,
                authorized_principals=request.authorized_principals,
                source_revision=request.source_revision,
                text=chunk.text,
                ordinal=chunk.ordinal,
                page=chunk.locator.page,
                vector=vector,
                sparse_indices=weights.indices,
                sparse_values=weights.values,
            )
            for chunk, vector, weights in zip(chunks, vectors, sparse, strict=True)
        )
        await self.index.upsert(indexed)
        return indexed


__all__ = ["CHUNK_ID_PREFIX", "IngestionRequest", "IngestionService"]
