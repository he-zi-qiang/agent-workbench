"""The encoder service and its three clients, over a real socket (ADR-0106).

Nothing here loads a model. The server is built over the deterministic doubles
and the assertions are about the wire: that a vector survives it unchanged and
in order, that a width or an identity that disagrees with the configuration is
refused at connect time the way a mismatched local model is, and that a lean
process asked to use the service never imports the runtime it exists to avoid.

A real uvicorn on a real loopback port rather than an ASGI transport, because
the factories connect synchronously and the clients asynchronously, and the
one thing an in-memory transport cannot show is that both ends agree about a
socket.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette

from agent_workbench.adapters.embedding import (
    DeterministicEmbedder,
    DeterministicSparseEncoder,
)
from agent_workbench.adapters.encoder import (
    EncoderServiceRefusedError,
    EncoderServiceUnavailableError,
    RemoteEmbedder,
    RemoteReranker,
    RemoteSparseEncoder,
    describe_service,
)
from agent_workbench.adapters.encoder.protocol import (
    EMBED_PATH,
    HEALTH_PATH,
    MAX_TEXT_CHARS,
    MAX_TEXTS_PER_REQUEST,
    RERANK_PATH,
)
from agent_workbench.adapters.reranking import (
    LexicalOverlapReranker,
    MiscountingReranker,
)
from agent_workbench.apps.encoder.server import create_app
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.projections import EmbeddingConfig, RerankerConfig
from agent_workbench.bootstrap.reranker_factory import (
    RerankerUnavailable,
    build_reranker,
)
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.reranker import RerankerContractError, RerankerPort
from agent_workbench.ports.sparse import SparseEncoderPort, SparseVector

DIMENSION = 8
VOCABULARY = 250_002
MODEL = "test-dense"
REVISION = "rev-1"
RERANKER_MODEL = "test-reranker"


@dataclass(frozen=True, slots=True)
class _Dense:
    """The deterministic embedder, wearing a configured model's identity.

    The remote adapters refuse an identity that is not `model_id@revision`,
    which the double's own identity deliberately is not (it announces itself
    as a hash). So the test gives it the name the configuration will ask for.
    """

    inner: DeterministicEmbedder = field(
        default_factory=lambda: DeterministicEmbedder(dimension=DIMENSION)
    )

    @property
    def dimension(self) -> int:
        return self.inner.dimension

    @property
    def identity(self) -> str:
        return f"{MODEL}@{REVISION}"

    async def embed_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[tuple[float, ...], ...]:
        return await self.inner.embed_documents(texts)

    async def embed_query(self, text: str) -> tuple[float, ...]:
        return await self.inner.embed_query(text)


@dataclass(frozen=True, slots=True)
class _Sparse:
    inner: DeterministicSparseEncoder = field(
        default_factory=lambda: DeterministicSparseEncoder(vocabulary_size=VOCABULARY)
    )

    @property
    def vocabulary_size(self) -> int:
        return self.inner.vocabulary_size

    @property
    def identity(self) -> str:
        return f"{MODEL}@{REVISION}-sparse"

    async def encode_documents(
        self, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        return await self.inner.encode_documents(texts)

    async def encode_query(self, text: str) -> SparseVector:
        return await self.inner.encode_query(text)


@dataclass(frozen=True, slots=True)
class _Reranker:
    inner: RerankerPort = field(default_factory=LexicalOverlapReranker)

    @property
    def identity(self) -> str:
        return f"{RERANKER_MODEL}@{REVISION}"

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        return await self.inner.rerank(query, passages)


@contextmanager
def _serving(app: Starlette) -> Iterator[str]:
    """Run ``app`` on an ephemeral loopback port for the duration of a test."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="encoder", daemon=True
    )
    thread.start()
    try:
        deadline = 100
        while not server.started and deadline:
            threading.Event().wait(0.05)
            deadline -= 1
        assert server.started, "the encoder did not bind"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


_ABSENT = object()


def _app(
    *,
    sparse: SparseEncoderPort | None | object = _ABSENT,
    reranker: RerankerPort | None | object = _ABSENT,
    warmed: bool = True,
) -> Starlette:
    return create_app(
        embedder=_Dense(),
        sparse=_Sparse() if sparse is _ABSENT else sparse,  # pyright: ignore[reportArgumentType]
        reranker=_Reranker() if reranker is _ABSENT else reranker,  # pyright: ignore[reportArgumentType]
        absent={"sparse": "no lexical head here", "reranker": "no cross-encoder here"},
        warmed=lambda: warmed,
    )


def _embedding(url: str, **overrides: object) -> EmbeddingConfig:
    fields: dict[str, object] = {
        "model_id": MODEL,
        "revision": REVISION,
        "vector_size": DIMENSION,
        "batch_size": 4,
        "device": "auto",
        "sparse_vocabulary_size": VOCABULARY,
        "service_url": url,
    }
    fields.update(overrides)
    return EmbeddingConfig(**fields)  # pyright: ignore[reportArgumentType]


def _reranking(url: str, **overrides: object) -> RerankerConfig:
    fields: dict[str, object] = {
        "model_id": RERANKER_MODEL,
        "revision": REVISION,
        "batch_size": 4,
        "device": "auto",
        "timeout_seconds": 15.0,
        "service_url": url,
    }
    fields.update(overrides)
    return RerankerConfig(**fields)  # pyright: ignore[reportArgumentType]


def _closed_port() -> str:
    """A loopback URL nothing listens on."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# --- what the service says about itself --------------------------------------


def test_the_service_describes_exactly_what_it_loaded() -> None:
    with _serving(_app(reranker=None)) as url:
        described = describe_service(url)

    assert described.dense is not None
    assert described.dense.identity == f"{MODEL}@{REVISION}"
    assert described.dense.dimension == DIMENSION
    assert described.sparse is not None
    assert described.sparse.vocabulary_size == VOCABULARY
    # An honest absence, not a placeholder.
    assert described.reranker is None
    assert described.warmed is True


def test_health_refuses_until_the_models_are_warm() -> None:
    """A loaded model is not a ready model, and a health probe must not say so."""

    with _serving(_app(warmed=False)) as url:
        cold = httpx.get(url + HEALTH_PATH)
    with _serving(_app(warmed=True)) as url:
        warm = httpx.get(url + HEALTH_PATH)

    assert cold.status_code == 503 and cold.json()["status"] == "warming"
    assert warm.status_code == 200 and warm.json()["status"] == "ok"


# --- the three ports, over the wire ------------------------------------------


def test_dense_vectors_survive_the_wire_unchanged_and_in_order() -> None:
    local = _Dense()
    texts = ("alpha", "beta", "gamma")

    async def exercise(remote: RemoteEmbedder) -> tuple[object, object]:
        try:
            return (
                await remote.embed_documents(texts),
                await remote.embed_query("alpha"),
            )
        finally:
            await remote.aclose()

    with _serving(_app()) as url:
        remote = RemoteEmbedder.connect(_embedding(url))
        documents, query = asyncio.run(exercise(remote))

    assert isinstance(remote, EmbeddingPort)
    assert remote.identity == local.identity
    assert remote.dimension == DIMENSION
    assert documents == asyncio.run(local.embed_documents(texts))
    assert query == asyncio.run(local.embed_query("alpha"))
    # The two paths differ, as the port promises they must.
    assert query != documents[0]


def test_more_texts_than_one_request_carries_come_back_in_order() -> None:
    """The client batches; nothing above it has to know the ceiling."""

    texts = tuple(f"text {index}" for index in range(MAX_TEXTS_PER_REQUEST * 2 + 3))

    async def exercise(remote: RemoteEmbedder) -> object:
        try:
            return await remote.embed_documents(texts)
        finally:
            await remote.aclose()

    with _serving(_app()) as url:
        vectors = asyncio.run(exercise(RemoteEmbedder.connect(_embedding(url))))

    assert vectors == asyncio.run(_Dense().embed_documents(texts))


def test_sparse_vectors_survive_the_wire_unchanged() -> None:
    local = _Sparse()
    texts = ("the quick fox", "the slow fox")

    async def exercise(remote: RemoteSparseEncoder) -> tuple[object, object]:
        try:
            return (
                await remote.encode_documents(texts),
                await remote.encode_query("fox"),
            )
        finally:
            await remote.aclose()

    with _serving(_app()) as url:
        remote = RemoteSparseEncoder.connect(_embedding(url))
        documents, query = asyncio.run(exercise(remote))

    assert isinstance(remote, SparseEncoderPort)
    assert remote.identity == local.identity
    assert remote.vocabulary_size == VOCABULARY
    assert documents == asyncio.run(local.encode_documents(texts))
    assert query == asyncio.run(local.encode_query("fox"))
    assert len(documents[0]) > 0


def test_reranker_scores_survive_the_wire_and_the_contract_is_still_checked() -> None:
    passages = ("fox and hound", "nothing relevant", "a fox")

    async def exercise(remote: RemoteReranker) -> object:
        try:
            return await remote.rerank("fox", passages)
        finally:
            await remote.aclose()

    with _serving(_app()) as url:
        scores = asyncio.run(exercise(RemoteReranker.connect(_reranking(url))))
    assert scores == asyncio.run(_Reranker().rerank("fox", passages))

    with _serving(_app(reranker=_Reranker(inner=MiscountingReranker()))) as url:
        short = RemoteReranker.connect(_reranking(url))
        with pytest.raises(RerankerContractError):
            asyncio.run(exercise(short))


# --- refusals at connect time ------------------------------------------------


def test_a_service_serving_another_model_is_refused_by_name() -> None:
    """Two processes reading two files is how an index gets two models."""

    with _serving(_app()) as url:
        with pytest.raises(ValueError, match="other-model@rev-1") as refused:
            RemoteEmbedder.connect(_embedding(url, model_id="other-model"))
        assert f"{MODEL}@{REVISION}" in str(refused.value)
        with pytest.raises(ValueError, match="-sparse"):
            RemoteSparseEncoder.connect(_embedding(url, revision="rev-2"))
        with pytest.raises(ValueError, match=RERANKER_MODEL):
            RemoteReranker.connect(_reranking(url, model_id="another-reranker"))


def test_a_width_that_disagrees_with_the_configuration_is_refused() -> None:
    with _serving(_app()) as url:
        with pytest.raises(ValueError, match="vector_size"):
            RemoteEmbedder.connect(_embedding(url, vector_size=DIMENSION + 1))
        with pytest.raises(ValueError, match="ADR-013"):
            RemoteSparseEncoder.connect(_embedding(url, sparse_vocabulary_size=4096))


def test_a_component_the_service_did_not_load_is_a_typed_absence() -> None:
    """The same absence the in-process factory returns, with the same shape."""

    with _serving(_app(sparse=None, reranker=None)) as url:
        with pytest.raises(EncoderServiceUnavailableError):
            RemoteSparseEncoder.connect(_embedding(url))
        sparse = build_sparse_encoder(_embedding(url))
        reranker = build_reranker(_reranking(url))
        embedder = build_embedder(_embedding(url))

    assert isinstance(sparse, SparseEncodingUnavailable)
    assert "no sparse model" in sparse.reason
    assert isinstance(reranker, RerankerUnavailable)
    assert "no reranker" in reranker.reason
    assert isinstance(embedder, RemoteEmbedder)
    asyncio.run(embedder.aclose())


def test_an_encoder_that_is_not_answering_is_an_absence_that_names_the_fix() -> None:
    url = _closed_port()

    embedder = build_embedder(_embedding(url))
    sparse = build_sparse_encoder(_embedding(url))
    reranker = build_reranker(_reranking(url))

    assert isinstance(embedder, EmbeddingUnavailable)
    assert url in embedder.reason
    assert "agent-encoder" in embedder.reason
    assert "service_url" in embedder.reason
    assert isinstance(sparse, SparseEncodingUnavailable)
    assert isinstance(reranker, RerankerUnavailable)


def test_a_lean_process_never_imports_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the service, stated as an assertion.

    With the runtime made unimportable, the in-process branch would return an
    absence naming `--extra embedding`; the remote branch must not even try.
    """

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", None)
    monkeypatch.setitem(sys.modules, "torch", None)

    with _serving(_app()) as url:
        embedder = build_embedder(_embedding(url))
        sparse = build_sparse_encoder(_embedding(url))
        reranker = build_reranker(_reranking(url))
        assert isinstance(embedder, RemoteEmbedder)
        assert isinstance(sparse, RemoteSparseEncoder)
        assert isinstance(reranker, RemoteReranker)
        asyncio.run(embedder.aclose())
        asyncio.run(sparse.aclose())
        asyncio.run(reranker.aclose())


# --- the server's own refusals -----------------------------------------------


def test_the_server_refuses_a_text_past_the_ceiling_without_echoing_it() -> None:
    secret = "s" * (MAX_TEXT_CHARS + 1)
    with _serving(_app()) as url:
        answer = httpx.post(
            url + EMBED_PATH, json={"kind": "document", "texts": [secret]}
        )

    assert answer.status_code == 400
    assert "texts[0]" in answer.json()["error"]
    assert secret[:64] not in answer.text


def test_the_server_refuses_a_malformed_request_and_the_client_says_so() -> None:
    with _serving(_app()) as url:
        too_many = httpx.post(
            url + EMBED_PATH,
            json={"kind": "document", "texts": ["x"] * (MAX_TEXTS_PER_REQUEST + 1)},
        )
        not_json = httpx.post(url + EMBED_PATH, content=b"not json")
        unknown_kind = httpx.post(
            url + EMBED_PATH, json={"kind": "passage", "texts": ["x"]}
        )
        remote = RemoteReranker.connect(_reranking(url))

        async def past_the_ceiling() -> None:
            # Past the server's ceiling: the client sends it whole, the server
            # says no, and the caller hears the server's sentence.
            try:
                await remote.rerank("q", tuple("p" for _ in range(2000)))
            finally:
                await remote.aclose()

        with pytest.raises(EncoderServiceRefusedError, match="passages"):
            asyncio.run(past_the_ceiling())

    assert too_many.status_code == 400
    assert not_json.status_code == 400
    assert unknown_kind.status_code == 400


def test_a_missing_component_answers_503_with_the_factorys_own_reason() -> None:
    with _serving(_app(reranker=None)) as url:
        answer = httpx.post(url + RERANK_PATH, json={"query": "q", "passages": ["p"]})

    assert answer.status_code == 503
    assert answer.json()["error"] == "no cross-encoder here"


def test_the_client_refuses_a_text_past_the_ceiling_before_sending_it() -> None:
    """The promise `_request` makes, and did not keep until a review read it:
    a caller past the per-text ceiling hears the client's own sentence and
    never pays for the transfer. Asserted through the adapter, not raw HTTP."""

    secret = "s" * (MAX_TEXT_CHARS + 1)

    async def exercise(remote: RemoteEmbedder) -> None:
        try:
            await remote.embed_documents((secret,))
        finally:
            await remote.aclose()

    with _serving(_app()) as url:
        remote = RemoteEmbedder.connect(_embedding(url))
        with pytest.raises(EncoderServiceRefusedError, match="texts\\[0\\]") as refused:
            asyncio.run(exercise(remote))

    assert secret[:64] not in str(refused.value)


def test_an_adapter_that_was_built_and_never_used_holds_no_pool() -> None:
    """Built from a synchronous root, before any loop runs, and possibly torn
    down by a later refusal with nothing to close it: the pool opens on the
    first request, so this is not a leak and `aclose` is a no-op."""

    with _serving(_app()) as url:
        remote = RemoteEmbedder.connect(_embedding(url))
        assert remote.client._client is None  # pyright: ignore[reportPrivateUsage]
        asyncio.run(remote.aclose())
        assert remote.client._client is None  # pyright: ignore[reportPrivateUsage]
