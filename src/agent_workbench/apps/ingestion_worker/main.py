"""Console entry point for the independently deployed ingestion Worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress

from agent_workbench.adapters.persistence import PostgresWorkerPresenceStore
from agent_workbench.application.worker_presence import WorkerPresenceBeacon
from agent_workbench.apps.ingestion_worker.composition import (
    IngestionBackendUnavailableError,
    build_ingestion_worker_dependencies,
)
from agent_workbench.apps.ingestion_worker.runner import IngestionWorkerRunner
from agent_workbench.bootstrap import load_settings
from agent_workbench.bootstrap.deployment import deployment_label
from agent_workbench.bootstrap.projections import project_ingestion_worker

EXIT_OK = 0
EXIT_CONFIGURATION_ERROR = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-ingestion-worker",
        description="Drain the durable document outbox into Qdrant.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "use deterministic dense vectors for local infrastructure smoke tests; "
            "never use this to build a real knowledge index"
        ),
    )
    return parser


async def serve(*, demo: bool) -> None:
    """Assemble, validate, run and dispose one ingestion process."""

    config = project_ingestion_worker(load_settings())
    dependencies = build_ingestion_worker_dependencies(config, demo=demo)
    stop = asyncio.Event()
    _install_shutdown_handlers(stop)
    runner = IngestionWorkerRunner(
        drain=lambda: dependencies.worker.drain(limit=config.claim_limit),
        poll_seconds=config.poll_seconds,
        error_backoff_seconds=config.error_backoff_seconds,
    )
    try:
        await dependencies.startup()
        # Same readout the Task Worker writes (ADR-0110); same fail-soft rule.
        beacon = WorkerPresenceBeacon(
            PostgresWorkerPresenceStore(dependencies.engine),
            worker_id=config.worker_id,
            kind="ingestion",
            deployment=deployment_label(),
            capabilities={
                "demo": demo,
                "sparse": bool(config.embedding.sparse_enabled),
                "collection": str(config.qdrant.write_collection),
            },
            interval_seconds=float(config.heartbeat_seconds),
        )
        async with beacon:
            await runner.run_forever(stop)
    finally:
        await dependencies.dispose()


def _install_shutdown_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(serve(demo=args.demo))
    except (IngestionBackendUnavailableError, ValueError) as error:
        print(f"agent-ingestion-worker: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())


__all__ = [
    "EXIT_CONFIGURATION_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "build_parser",
    "main",
    "serve",
]
