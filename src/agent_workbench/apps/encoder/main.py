"""Console entry point for the encoder service (ADR-0106).

Loads the three retrieval models once, warms them, and serves them to the
other processes of this deployment. Refuses to start without the dense model:
a process whose entire job is to hold the weights has nothing to serve
without them, and a server that came up and answered 503 to everything would
be one an operator diagnoses by reading logs.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import replace

import uvicorn

from agent_workbench.adapters.concurrency.call_runner import BlockingCallRunner
from agent_workbench.apps.encoder.server import AbsenceReasons, create_app
from agent_workbench.bootstrap import load_settings
from agent_workbench.bootstrap.embedding_factory import (
    EmbeddingUnavailable,
    build_embedder,
)
from agent_workbench.bootstrap.encoder_warmup import warm_encoders
from agent_workbench.bootstrap.projections import (
    EncoderServiceConfig,
    project_encoder_service,
)
from agent_workbench.bootstrap.reranker_factory import (
    RerankerUnavailable,
    build_reranker,
)
from agent_workbench.bootstrap.sparse_factory import (
    SparseEncodingUnavailable,
    build_sparse_encoder,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8769
#: Loopback by default, like every other project-owned server, plus the one
#: address a container topology needs. This server is not an MCP server and
#: not a control plane: it holds no identity, takes no tool proposal and
#: answers to the other processes of *this* deployment, the way Qdrant does.
#: In Compose those processes are other containers, and a service that only
#: listened on its own loopback would be reachable by nobody. `0.0.0.0` is
#: therefore a choice a deployment makes on the command line, and the
#: Compose file is where it is made; the native path never passes it.
_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-encoder",
        description=(
            "Load the retrieval models once and serve dense, sparse and "
            "reranking to the other processes of this deployment."
        ),
    )
    parser.add_argument("--host", choices=_HOSTS, default=DEFAULT_HOST)
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT)
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    config = loads_in_process(project_encoder_service(load_settings()))

    # ADR-042: one pool per process, so the bound is a ceiling rather than
    # three private ones -- and in this process every request is a blocking
    # call, so the pool is the whole concurrency story.
    blocking = BlockingCallRunner(
        slots=config.blocking_calls.slots,
        queue_timeout_seconds=config.blocking_calls.queue_timeout_seconds,
    )

    built_embedder = build_embedder(config.embedding, runner=blocking)
    if isinstance(built_embedder, EmbeddingUnavailable):
        parser.exit(2, f"agent-encoder: no dense model: {built_embedder.reason}\n")
        return  # pragma: no cover - parser.exit raises

    absent: AbsenceReasons = {}
    built_sparse = build_sparse_encoder(config.embedding, runner=blocking)
    sparse = None
    if isinstance(built_sparse, SparseEncodingUnavailable):
        # Served anyway, and said so: the ingestion worker, which cannot write a
        # hybrid collection without lexical weights, reads the absence off
        # `/identity` and refuses on its own terms, exactly as it would have
        # with the weights missing from its own process.
        absent["sparse"] = built_sparse.reason
        logger.warning(
            "encoder_sparse_unavailable", extra={"reason": built_sparse.reason}
        )
    else:
        sparse = built_sparse

    built_reranker = build_reranker(config.reranker, runner=blocking)
    reranker = None
    if isinstance(built_reranker, RerankerUnavailable):
        absent["reranker"] = built_reranker.reason
        logger.warning(
            "encoder_reranker_unavailable", extra={"reason": built_reranker.reason}
        )
    else:
        reranker = built_reranker

    warmed = False

    def is_warm() -> bool:
        return warmed

    app = create_app(
        embedder=built_embedder,
        sparse=sparse,
        reranker=reranker,
        absent=absent,
        warmed=is_warm,
    )
    # Before binding, so a health probe never sees a loaded-but-cold encoder
    # (`bootstrap/encoder_warmup`: 29.4 s for the first forward pass on MPS,
    # 0.06 s for every later one).
    asyncio.run(warm_encoders(built_embedder, sparse, reranker))
    warmed = True
    logger.info(
        "encoder_serving",
        extra={
            "host": arguments.host,
            "port": arguments.port,
            "dense": built_embedder.identity,
            "sparse": None if sparse is None else sparse.identity,
            "reranker": None if reranker is None else reranker.identity,
        },
    )
    try:
        uvicorn.run(app, host=arguments.host, port=arguments.port, access_log=False)
    finally:
        blocking.close()


def loads_in_process(config: EncoderServiceConfig) -> EncoderServiceConfig:
    """This process loads; the two service URLs describe its *clients*.

    The Compose profile is one file read by five processes, and it names
    `http://encoder:8769` in both leaves because four of them are clients. The
    fifth is this one, and until 2026-09-03 it refused to start on seeing its
    own address -- a guard against "an encoder forwarding to itself" that
    fired on the one deployment the encoder exists for, and would have taken
    the whole stack down with it (every other service waits on this one's
    health). Found by review before it ever ran.

    So the leaves are cleared here rather than refused: whatever the profile
    says, an `agent-encoder` builds its three ports through the in-process
    branch of the factories, and says so once when the profile had named a
    URL, so a reader of the log is not left wondering which branch it took.
    """

    named = config.embedding.service_url or config.reranker.service_url
    if named:
        logger.info(
            "encoder_ignores_service_url",
            extra={
                "service_url": named,
                "reason": "this process is the one that loads the weights",
            },
        )
    return replace(
        config,
        embedding=replace(config.embedding, service_url=""),
        reranker=replace(config.reranker, service_url=""),
    )


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return parsed


if __name__ == "__main__":  # pragma: no cover - console script owns this branch
    main()


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "loads_in_process", "main"]
