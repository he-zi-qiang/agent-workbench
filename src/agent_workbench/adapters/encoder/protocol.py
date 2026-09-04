"""What crosses the wire between a lean process and the encoder (ADR-0106).

One module, imported by both ends, so the server and its three clients cannot
drift into two opinions about a field name. Nothing here is a port: the ports
are ``EmbeddingPort``, ``SparseEncoderPort`` and ``RerankerPort``, and this is
the shape one implementation of each of them happens to speak.

The ceilings are ceilings on a *request*, not on the work. A caller with more
texts than ``MAX_TEXTS_PER_REQUEST`` sends several requests; the client does
that itself, so no caller above it has to know the number. They exist because
a JSON body is read into memory whole before anything can be validated, and
a process that accepted an unbounded one would be a process that could be
made to hold an unbounded one.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

SERVICE_NAME: Final[str] = "agent-workbench-encoder"

IDENTITY_PATH: Final[str] = "/identity"
HEALTH_PATH: Final[str] = "/health"
EMBED_PATH: Final[str] = "/embed"
SPARSE_PATH: Final[str] = "/sparse"
RERANK_PATH: Final[str] = "/rerank"

#: How many texts one request may carry. Sized to the ingestion batch
#: (`rag.ingestion.embedding_batch_size`, shipped as 16) with room above it,
#: and well under anything a single JSON body should be.
MAX_TEXTS_PER_REQUEST: Final[int] = 64
#: The longest single text. A 512-token chunk is a few thousand characters;
#: this is two orders of magnitude above that, and a bound rather than a size.
MAX_TEXT_CHARS: Final[int] = 100_000
#: Passages one rerank call may score. `rag.retrieval.fused_top_k` is capped
#: at 1000 by settings, so this is the most the retrieval funnel can ask for.
MAX_PASSAGES_PER_RERANK: Final[int] = 1024
#: What the server reads off the socket before answering 413. Above the
#: largest legal request by a comfortable margin, so a legal call is refused
#: by the schema and never by the transport -- which is the harder refusal to
#: read (the same reasoning `apps/sandbox_mcp/server.py` gives for its own).
MAX_REQUEST_BYTES: Final[int] = 32 * 1024 * 1024

#: A query and a passage are different inputs to most embedding models -- BGE
#: prepends different instructions to each -- which is why the ports split
#: them into two methods and why the wire keeps the distinction rather than
#: letting a server guess.
TextKind = Literal["query", "document"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EncodeRequest(_Strict):
    kind: TextKind
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS_PER_REQUEST)

    def check_lengths(self) -> None:
        """Refuse a text past the ceiling, without echoing it.

        Not a `max_length` on the item type: pydantic's error for that would
        quote the offending value, and these strings are somebody's document.
        """

        for index, text in enumerate(self.texts):
            if len(text) > MAX_TEXT_CHARS:
                raise ValueError(
                    f"texts[{index}] is {len(text)} characters, above the "
                    f"{MAX_TEXT_CHARS}-character limit"
                )


class DenseResponse(_Strict):
    vectors: list[list[float]]


class SparseVectorPayload(_Strict):
    indices: list[int]
    values: list[float]


class SparseResponse(_Strict):
    vectors: list[SparseVectorPayload]


class RerankRequest(_Strict):
    query: str = Field(min_length=1)
    passages: list[str] = Field(min_length=1, max_length=MAX_PASSAGES_PER_RERANK)

    def check_lengths(self) -> None:
        if len(self.query) > MAX_TEXT_CHARS:
            raise ValueError(
                f"query is {len(self.query)} characters, above the "
                f"{MAX_TEXT_CHARS}-character limit"
            )
        for index, passage in enumerate(self.passages):
            if len(passage) > MAX_TEXT_CHARS:
                raise ValueError(
                    f"passages[{index}] is {len(passage)} characters, above the "
                    f"{MAX_TEXT_CHARS}-character limit"
                )


class RerankResponse(_Strict):
    scores: list[float]


class DenseDescription(_Strict):
    #: The loaded model's own identity, verbatim. It becomes part of the index
    #: identity in every process that reads it, so the wire carries the string
    #: the in-process adapter would have produced and never a name of its own.
    identity: str
    dimension: int


class SparseDescription(_Strict):
    identity: str
    vocabulary_size: int


class RerankerDescription(_Strict):
    identity: str


class ServiceDescription(_Strict):
    """What this encoder loaded, and what it did not.

    ``None`` for a component is an honest absence -- the weights or the runtime
    behind them did not load -- and each client turns it into the same typed
    absence the in-process factory would have returned. The dense model is
    never ``None`` on a running server: `apps/encoder/main.py` refuses to start
    without it, because a process whose whole job is to hold the weights has
    nothing to serve without them.
    """

    service: Literal["agent-workbench-encoder"]
    dense: DenseDescription | None
    sparse: SparseDescription | None
    reranker: RerankerDescription | None
    #: Whether the first forward pass has been paid (`bootstrap/encoder_warmup`).
    #: Reported so a health probe can wait for it: a loaded model is not a
    #: ready model, and the difference was measured at 29 seconds on MPS.
    warmed: bool


class ErrorResponse(_Strict):
    error: str


__all__ = [
    "EMBED_PATH",
    "HEALTH_PATH",
    "IDENTITY_PATH",
    "MAX_PASSAGES_PER_RERANK",
    "MAX_REQUEST_BYTES",
    "MAX_TEXTS_PER_REQUEST",
    "MAX_TEXT_CHARS",
    "RERANK_PATH",
    "SERVICE_NAME",
    "SPARSE_PATH",
    "DenseDescription",
    "DenseResponse",
    "EncodeRequest",
    "ErrorResponse",
    "RerankRequest",
    "RerankResponse",
    "RerankerDescription",
    "ServiceDescription",
    "SparseDescription",
    "SparseResponse",
    "SparseVectorPayload",
    "TextKind",
]
