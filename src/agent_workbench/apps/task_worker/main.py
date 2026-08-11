"""Console entry point for the independently deployed Task Worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress

from agent_workbench.adapters.persistence.notifications import (
    TASK_READY_CHANNEL,
    TaskReadyListener,
)
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

logger = logging.getLogger(__name__)


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

    settings = load_settings()
    config = project_task_worker(settings)
    dependencies = await build_task_worker_dependencies(config, demo=demo)
    listener: TaskReadyListener | None = None
    try:
        stop = asyncio.Event()
        _install_shutdown_handlers(stop)
        # Read off `settings` here rather than taken from `config`: the Worker
        # projection carries no listen DSN, and the file that would add one is
        # not this package's to edit. Passed as three values rather than as the
        # Settings object because `Settings` itself may not cross into an app
        # (tests/architecture/test_dependency_boundaries).
        listener = await _start_task_ready_listener(
            listen_dsn=settings.database.listen_dsn.get_secret_value(),
            application_name=f"{settings.database.application_name}-listener",
            healthcheck_seconds=float(settings.database.listener_healthcheck_seconds),
            configured_channel=settings.event_stream.task_ready_channel,
        )
        runner = TaskWorkerRunner(
            run_once=dependencies.worker.run_once,
            poll_seconds=config.task.claim_poll_seconds,
            concurrency=config.worker_concurrency,
            wakeup=None if listener is None else listener.woken,
        )
        await dependencies.startup()
        await runner.run_forever(stop)
    finally:
        if listener is not None:
            await listener.aclose()
        await dependencies.dispose()


async def _start_task_ready_listener(
    *,
    listen_dsn: str,
    application_name: str,
    healthcheck_seconds: float,
    configured_channel: str,
) -> TaskReadyListener | None:
    """The wake-up channel if it can be had, and ``None`` if it cannot.

    Fail-soft, which is the opposite of how this process treats everything else
    it assembles -- and for a reason that is specific to this one dependency.
    A missing model or artifact root makes a Worker that accepts Tasks it cannot
    run; a missing listener makes a Worker that finds work on its poll interval
    instead of immediately. Refusing to start over it would turn a notification
    the design guarantees nothing about into the thing the Worker cannot run
    without, which is the one property the channel must not have.

    So it is a warning, and the warning is the point: "claims got slower" is not
    otherwise visible from outside.
    """

    if configured_channel != TASK_READY_CHANNEL:
        # `notify_task_ready` writes the constant and reads no configuration, so
        # subscribing to the configured name instead would produce a Worker that
        # connects, subscribes, logs nothing, and is never woken again -- the
        # constant wins and the disagreement is said out loud rather than
        # silently resolved. The default configuration used to disagree here
        # (`agent_task_ready`) and now matches, so this fires only for a
        # deployment that really did configure another name; a default that
        # always warned would have taught readers to skip the line.
        logger.warning(
            "task_ready_channel_configuration_ignored",
            extra={
                "configured_channel": configured_channel,
                "listen_channel": TASK_READY_CHANNEL,
            },
        )
    listener = TaskReadyListener(
        listen_dsn,
        application_name=application_name,
        healthcheck_seconds=healthcheck_seconds,
    )
    try:
        await listener.start()
    except Exception as error:
        # Broad because the failures are: a DSN this process cannot resolve, a
        # server that is not up yet, a role without LISTEN. They differ in how
        # they are fixed and not at all in what this process does about them.
        logger.warning(
            "task_ready_listener_unavailable",
            extra={"listen_error_type": type(error).__name__},
        )
        await listener.aclose()
        return None
    return listener


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
