"""Console entry point for the independently deployed Task Worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress

from agent_workbench.apps.task_worker.composition import (
    RealTaskHandlersUnavailableError,
    build_task_worker_dependencies,
)
from agent_workbench.apps.task_worker.runner import TaskWorkerRunner
from agent_workbench.bootstrap import load_settings
from agent_workbench.bootstrap.projections import project_task_worker

EXIT_OK = 0
EXIT_CONFIGURATION_ERROR = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-task-worker",
        description="Run the single-process durable Task Worker.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "use deterministic synthetic graph handlers for a local smoke test; "
            "never use this in a deployed worker"
        ),
    )
    return parser


async def serve(*, demo: bool) -> None:
    """Assemble, run, and dispose one Worker process."""

    config = project_task_worker(load_settings())
    dependencies = build_task_worker_dependencies(config, demo=demo)
    stop = asyncio.Event()
    _install_shutdown_handlers(stop)
    runner = TaskWorkerRunner(
        run_once=dependencies.worker.run_once,
        poll_seconds=config.task.claim_poll_seconds,
        concurrency=config.worker_concurrency,
    )
    try:
        await dependencies.startup()
        await runner.run_forever(stop)
    finally:
        await dependencies.dispose()


def _install_shutdown_handlers(stop: asyncio.Event) -> None:
    """Make SIGTERM request an orderly stop where the platform supports it.

    SIGINT is intentionally left to ``asyncio.run``: it becomes
    ``KeyboardInterrupt`` in :func:`main`, which returns the conventional 130
    after ``serve`` has disposed its engine in ``finally``.
    """

    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Worker until a normal shutdown signal or Ctrl-C."""

    args = build_parser().parse_args(argv)
    try:
        asyncio.run(serve(demo=args.demo))
    except RealTaskHandlersUnavailableError as error:
        print(f"agent-task-worker: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
