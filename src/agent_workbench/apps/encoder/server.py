"""The encoder service: three ports behind five routes (ADR-0106).

This process exists so that the API, both Task Workers and the ingestion
worker do not each load the same three models. It is deliberately not an MCP
server -- nothing here is a tool a model proposes; it is a model runtime that
other processes of *this* system call, the way they call Qdrant -- so it
speaks plain JSON over HTTP and carries no tool catalogue, no session and no
Host-header dance.

Every route is a thin shell over a port. The batching, the device, the
off-loop execution and the width checks all live in the in-process adapters
this server was built over; what this file adds is a body ceiling, a schema,
and an honest 503 for a component that did not load.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_workbench.adapters.concurrency.call_runner import (
    BlockingCallQueueTimeoutError,
)
from agent_workbench.adapters.encoder.protocol import (
    EMBED_PATH,
    HEALTH_PATH,
    IDENTITY_PATH,
    MAX_REQUEST_BYTES,
    RERANK_PATH,
    SPARSE_PATH,
    DenseDescription,
    EncodeRequest,
    RerankerDescription,
    RerankRequest,
    ServiceDescription,
    SparseDescription,
)
from agent_workbench.ports.embedding import EmbeddingPort
from agent_workbench.ports.reranker import RerankerPort
from agent_workbench.ports.sparse import SparseEncoderPort

#: Why a component is absent, keyed by the route that would have used it.
#: Carried so a 503 can say what the in-process factory would have logged.
AbsenceReasons = dict[str, str]


class _RequestTooLarge(Exception):
    pass


def create_app(
    *,
    embedder: EmbeddingPort,
    sparse: SparseEncoderPort | None,
    reranker: RerankerPort | None,
    absent: AbsenceReasons | None = None,
    warmed: Callable[[], bool] = lambda: True,
) -> Starlette:
    """Build the app over already-loaded ports.

    The ports are parameters rather than built here, for the reason every
    other server in ``apps/`` takes its dependencies: the routes can then be
    exercised with the deterministic doubles, in-process, without weights --
    and the three client adapters can be tested against *this* app rather
    than against a copy of its behaviour.

    ``warmed`` is read on every health answer. It is a callable because the
    warm-up runs after the app is constructed and before the server binds, and
    a boolean captured at construction would report cold forever.
    """

    reasons: Final[AbsenceReasons] = dict(absent or {})

    def describe() -> ServiceDescription:
        return ServiceDescription(
            service="agent-workbench-encoder",
            dense=DenseDescription(
                identity=embedder.identity, dimension=embedder.dimension
            ),
            sparse=(
                None
                if sparse is None
                else SparseDescription(
                    identity=sparse.identity, vocabulary_size=sparse.vocabulary_size
                )
            ),
            reranker=(
                None
                if reranker is None
                else RerankerDescription(identity=reranker.identity)
            ),
            warmed=warmed(),
        )

    async def identity(request: Request) -> JSONResponse:
        del request
        return JSONResponse(describe().model_dump())

    async def health(request: Request) -> JSONResponse:
        """200 only once the models are loaded *and* warm.

        A Compose `depends_on: service_healthy` reads this, and a 200 from a
        loaded-but-cold encoder would hand the first request the 29-second
        kernel compile the warm-up exists to absorb (`bootstrap/encoder_warmup`).
        """

        del request
        described = describe()
        payload: dict[str, Any] = {
            "status": "ok" if described.warmed else "warming",
            **described.model_dump(),
        }
        return JSONResponse(payload, status_code=200 if described.warmed else 503)

    async def embed(request: Request) -> JSONResponse:
        try:
            body = EncodeRequest.model_validate(await _read_json(request))
            body.check_lengths()
        except _RequestTooLarge:
            return _error(413, "the request body is above the transfer ceiling")
        except (ValueError, ValidationError) as invalid:
            return _error(400, _validation_message(invalid))
        try:
            vectors = (
                await embedder.embed_documents(tuple(body.texts))
                if body.kind == "document"
                else tuple([await embedder.embed_query(text) for text in body.texts])
            )
        except BlockingCallQueueTimeoutError:
            return _busy()
        return JSONResponse({"vectors": [list(vector) for vector in vectors]})

    async def sparse_route(request: Request) -> JSONResponse:
        if sparse is None:
            return _error(
                503, reasons.get("sparse", "this encoder loaded no sparse model")
            )
        try:
            body = EncodeRequest.model_validate(await _read_json(request))
            body.check_lengths()
        except _RequestTooLarge:
            return _error(413, "the request body is above the transfer ceiling")
        except (ValueError, ValidationError) as invalid:
            return _error(400, _validation_message(invalid))
        try:
            vectors = (
                await sparse.encode_documents(tuple(body.texts))
                if body.kind == "document"
                else tuple([await sparse.encode_query(text) for text in body.texts])
            )
        except BlockingCallQueueTimeoutError:
            return _busy()
        return JSONResponse(
            {
                "vectors": [
                    {"indices": list(vector.indices), "values": list(vector.values)}
                    for vector in vectors
                ]
            }
        )

    async def rerank(request: Request) -> JSONResponse:
        if reranker is None:
            return _error(
                503, reasons.get("reranker", "this encoder loaded no reranker")
            )
        try:
            body = RerankRequest.model_validate(await _read_json(request))
            body.check_lengths()
        except _RequestTooLarge:
            return _error(413, "the request body is above the transfer ceiling")
        except (ValueError, ValidationError) as invalid:
            return _error(400, _validation_message(invalid))
        try:
            scores = await reranker.rerank(body.query, tuple(body.passages))
        except BlockingCallQueueTimeoutError:
            return _busy()
        return JSONResponse({"scores": list(scores)})

    return Starlette(
        routes=[
            Route(IDENTITY_PATH, endpoint=identity, methods=["GET"]),
            Route(HEALTH_PATH, endpoint=health, methods=["GET"]),
            Route(EMBED_PATH, endpoint=embed, methods=["POST"]),
            Route(SPARSE_PATH, endpoint=sparse_route, methods=["POST"]),
            Route(RERANK_PATH, endpoint=rerank, methods=["POST"]),
        ]
    )


async def _read_json(request: Request) -> Any:
    """The body, bounded before it is held.

    Read from the stream rather than with `request.body()`, because that reads
    everything first and checks nothing; a ceiling applied after the fact is
    a ceiling on what is reported, not on what was held.
    """

    held = bytearray()
    async for chunk in request.stream():
        held.extend(chunk)
        if len(held) > MAX_REQUEST_BYTES:
            raise _RequestTooLarge
    import json

    try:
        return json.loads(bytes(held))
    except ValueError as error:
        raise ValueError("the request body is not JSON") from error


def _validation_message(error: Exception) -> str:
    """One line naming the location, never the value.

    pydantic's own rendering quotes the offending input, and the input here is
    somebody's text; the location and the rule are all an operator needs.
    """

    if isinstance(error, ValidationError):
        parts = [
            ".".join(str(piece) for piece in held["loc"]) + ": " + str(held["msg"])
            for held in error.errors(include_input=False, include_url=False)
        ]
        return "invalid request: " + "; ".join(parts)
    return f"invalid request: {error}"


def _busy() -> JSONResponse:
    # The blocking-call queue timed out before the work started (ADR-042).
    # Nothing ran, so the caller may retry; 503 is the status that says so.
    return _error(503, "the encoder is busy; no slot became free in time")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


__all__ = ["AbsenceReasons", "create_app"]
