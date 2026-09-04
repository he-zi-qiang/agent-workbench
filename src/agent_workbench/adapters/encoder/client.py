"""The HTTP half of asking another process for a vector (ADR-0106).

Two kinds of failure, kept apart because they send an operator to different
places. :class:`EncoderServiceUnavailableError` is *transport*: nothing at the
URL answered, or the answer never arrived. :class:`EncoderServiceRefusedError`
is a server that answered and said no -- a component it did not load, a
request past a ceiling, a queue that was full. At connect time the first one
becomes the same typed absence a missing runtime would have been, so a lean
process degrades exactly the way a heavy one does; at call time both are
raised, because a retrieval that silently returned nothing would be worse than
one that failed.

The texts never appear in an error. They are somebody's document, and an error
string travels into logs and, from a tool, back into a model's context.
"""

from __future__ import annotations

from typing import Any, Final, cast

import httpx
from pydantic import ValidationError

from agent_workbench.adapters.encoder.protocol import (
    EMBED_PATH,
    IDENTITY_PATH,
    MAX_TEXTS_PER_REQUEST,
    RERANK_PATH,
    SPARSE_PATH,
    DenseResponse,
    EncodeRequest,
    RerankRequest,
    RerankResponse,
    ServiceDescription,
    SparseResponse,
    TextKind,
)
from agent_workbench.ports.embedding import Vector
from agent_workbench.ports.sparse import SparseVector

#: How long a connect-time description may take. The server answers this from
#: memory, so a slow answer is a server still loading its models -- which the
#: caller should wait out at the deployment level (`depends_on` with a health
#: condition), not here.
DESCRIBE_TIMEOUT_SECONDS: Final[float] = 10.0
#: How long one encode request may take. A batch of documents on a CPU that is
#: also serving three other processes is tens of seconds; this is the backstop
#: for an encoder that has stopped answering, not a budget for a slow one.
REQUEST_TIMEOUT_SECONDS: Final[float] = 300.0


class EncoderServiceUnavailableError(RuntimeError):
    """Nothing at the configured URL answered, or the answer never arrived."""


class EncoderServiceRefusedError(RuntimeError):
    """The encoder answered and would not, or could not, do what was asked."""


def describe_service(
    service_url: str, *, timeout_seconds: float = DESCRIBE_TIMEOUT_SECONDS
) -> ServiceDescription:
    """Ask a running encoder what it loaded. Synchronous, for the factories.

    The composition roots build their ports synchronously and this is the one
    network call a build needs, so it is made with a synchronous client and
    closed before returning. Everything after it is ``await``-ed.
    """

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            answer = client.get(_join(service_url, IDENTITY_PATH))
    except httpx.HTTPError as error:
        raise EncoderServiceUnavailableError(
            f"the encoder service at {service_url} did not answer "
            f"({type(error).__name__}). Start it with `agent-encoder`, or clear "
            "rag.embedding.service_url / rag.reranker.service_url to load the "
            "weights in this process instead"
        ) from error
    if answer.status_code != 200:
        raise EncoderServiceUnavailableError(
            f"the encoder service at {service_url} answered {answer.status_code} "
            "to a request for its identity; it is not an encoder, or not one "
            "that has finished starting"
        )
    try:
        return ServiceDescription.model_validate(answer.json())
    except (ValueError, ValidationError) as error:
        raise EncoderServiceUnavailableError(
            f"the process at {service_url} did not describe itself the way an "
            f"encoder does ({type(error).__name__})"
        ) from error


class EncoderClient:
    """One connection pool to one encoder, shared by the three ports over it.

    The pool is opened on the first request, not in the constructor, and the
    difference is what makes the three adapters safe to build from the
    synchronous composition roots. An ``httpx.AsyncClient`` binds to the event
    loop it first runs on, and the roots build their ports before any loop is
    running -- so a pool opened here would belong to no loop yet and could be
    torn down by a later refusal (`RerankerRequiredError`, an ingestion worker
    whose sparse arm did not load) with nothing to close it. Opened lazily,
    an adapter that was built and never used holds no socket at all, and
    ``aclose`` on it is a no-op rather than a leak.
    """

    def __init__(
        self,
        service_url: str,
        *,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.service_url = service_url
        self._timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    def _pool(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.service_url.rstrip("/"), timeout=self._timeout_seconds
            )
        return self._client

    async def embed(self, kind: TextKind, texts: tuple[str, ...]) -> tuple[Vector, ...]:
        vectors: list[Vector] = []
        for batch in _batches(texts):
            body = await self._post(
                EMBED_PATH, _request(EncodeRequest, kind=kind, texts=list(batch))
            )
            parsed = DenseResponse.model_validate(body)
            _require_count(len(parsed.vectors), len(batch), what="vectors")
            vectors.extend(
                tuple(float(value) for value in row) for row in parsed.vectors
            )
        return tuple(vectors)

    async def sparse(
        self, kind: TextKind, texts: tuple[str, ...]
    ) -> tuple[SparseVector, ...]:
        vectors: list[SparseVector] = []
        for batch in _batches(texts):
            body = await self._post(
                SPARSE_PATH, _request(EncodeRequest, kind=kind, texts=list(batch))
            )
            parsed = SparseResponse.model_validate(body)
            _require_count(len(parsed.vectors), len(batch), what="vectors")
            vectors.extend(
                SparseVector(indices=tuple(held.indices), values=tuple(held.values))
                for held in parsed.vectors
            )
        return tuple(vectors)

    async def rerank(self, query: str, passages: tuple[str, ...]) -> tuple[float, ...]:
        body = await self._post(
            RERANK_PATH, _request(RerankRequest, query=query, passages=list(passages))
        )
        parsed = RerankResponse.model_validate(body)
        # Not `_require_count`: the reranker port has its own contract error
        # for exactly this, and the adapter above raises it so a short answer
        # is the same failure whichever adapter produced it.
        return tuple(float(score) for score in parsed.scores)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            answer = await self._pool().post(path, json=payload)
        except httpx.HTTPError as error:
            raise EncoderServiceUnavailableError(
                f"the encoder service at {self.service_url} did not answer "
                f"{path} ({type(error).__name__})"
            ) from error
        if answer.status_code != 200:
            raise EncoderServiceRefusedError(
                f"the encoder service at {self.service_url} answered "
                f"{answer.status_code} to {path}: {_detail(answer)}"
            )
        try:
            return answer.json()
        except ValueError as error:
            raise EncoderServiceRefusedError(
                f"the encoder service at {self.service_url} answered {path} "
                "with something that is not JSON"
            ) from error


def _request[M: (EncodeRequest, RerankRequest)](
    model: type[M], **fields: Any
) -> dict[str, Any]:
    """Build one request body against the shared schema, or refuse here.

    The same ceilings the server enforces, applied before a body is sent --
    so a caller past them hears the client's own sentence instead of a 400,
    and never pays for the transfer. The location and the rule are reported;
    the value is not, for the reason the server's own messages omit it.
    """

    try:
        request = model(**fields)
        # The per-text ceiling is a method rather than a field constraint so
        # that its refusal never quotes the text (`protocol.py`); it therefore
        # has to be called, and until 2026-09-03 this function did not -- the
        # docstring promised a client-side refusal that only the server made.
        request.check_lengths()
        return request.model_dump()
    except ValueError as invalid:
        if not isinstance(invalid, ValidationError):
            raise EncoderServiceRefusedError(
                f"this request is past the encoder's ceiling: {invalid}"
            ) from None
        parts = [
            ".".join(str(piece) for piece in held["loc"]) + ": " + str(held["msg"])
            for held in invalid.errors(include_input=False, include_url=False)
        ]
        raise EncoderServiceRefusedError(
            "this request is past the encoder's ceiling: " + "; ".join(parts)
        ) from None


def _batches(texts: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        texts[start : start + MAX_TEXTS_PER_REQUEST]
        for start in range(0, len(texts), MAX_TEXTS_PER_REQUEST)
    )


def _require_count(got: int, wanted: int, *, what: str) -> None:
    """Positional alignment is the whole contract; a short answer is a defect.

    The caller pairs results with the texts it sent by position, so an answer
    of the wrong length would attach every vector to the wrong text without
    failing anything -- the same argument `ports/embedding.py` makes about
    order.
    """

    if got != wanted:
        raise EncoderServiceRefusedError(
            f"the encoder returned {got} {what} for {wanted} texts"
        )


def _detail(answer: httpx.Response) -> str:
    """The server's own sentence, when it wrote one; the status text otherwise.

    The server composes its errors without echoing request content, so
    forwarding its sentence is safe. Anything else that answered on this port
    is not trusted to have been that careful, hence the fallback.
    """

    try:
        body: object = answer.json()
    except ValueError:
        return answer.reason_phrase or "no detail"
    if isinstance(body, dict):
        detail: object = cast("dict[str, object]", body).get("error")
        if isinstance(detail, str):
            return detail
    return answer.reason_phrase or "no detail"


def _join(service_url: str, path: str) -> str:
    return service_url.rstrip("/") + path


__all__ = [
    "DESCRIBE_TIMEOUT_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "EncoderClient",
    "EncoderServiceRefusedError",
    "EncoderServiceUnavailableError",
    "describe_service",
]
