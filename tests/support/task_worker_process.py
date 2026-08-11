"""One Task Worker in an operating-system process of its own, killable mid-graph.

``tests/e2e/test_worker_process_crash_recovery.py`` needs a Worker that really
is a separate OS process, so that ``SIGKILL`` can take it away with nothing
unwinding: no ``finally``, no ``dispose``, no engine returning its connections,
and no object from the dead attempt surviving into the recovery. Rebuilding an
engine and a Worker inside one pytest process -- which is what every existing
recovery test does -- cannot show that, because the thing being tested is
precisely what a process teardown would have cleaned up on the way out.

This module is that process's entry point. It is deliberately a near-copy of
:func:`agent_workbench.apps.task_worker.main.serve`, and the two differences are
the whole of what makes the test possible:

* every node execution appends one line to a file the parent can read *while
  this process is still alive*, so "the graph reached node X here" is an
  observation rather than an inference from a sleep; and
* exactly one node may be told to append its line and then never return, which
  is what holds this process in a provable mid-graph state to be killed in.

Everything else is the deployed path. ``build_task_worker_dependencies(config,
handlers=...)`` is the same branch ``--demo`` takes -- ``demo=True`` is literally
``handlers = build_demo_handlers()`` and then this call -- so the settings load,
the Registry, the session-pinned execution guard, the fenced checkpointer and
the poll loop are the real ones. Nothing here reimplements recovery; the point
is only to have a real process worth destroying.

The demo handlers are used because a Worker must be startable with no model
provider. They contact nothing and their node identifiers are the deployed
graph's, so what is exercised is LangGraph's real control flow over the real
checkpointer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.langgraph.workflow import NodeHandler
from agent_workbench.apps.task_worker.composition import (
    build_task_worker_dependencies,
)
from agent_workbench.apps.task_worker.runner import TaskWorkerRunner
from agent_workbench.bootstrap import load_settings
from agent_workbench.bootstrap.projections import project_task_worker
from agent_workbench.domain.tasks import TaskNodeId, TaskState
from agent_workbench.ports.task_registry import ExecutionLease
from agent_workbench.workflows.demo_handlers import build_demo_handlers
from agent_workbench.workflows.execution_scope import TaskExecutionScope

#: Stood in for a real product by the ``export`` node, through the real local
#: artifact store. A second export would be a second file, which is a fact on
#: disk rather than a counter this test keeps.
REPORT_MEDIA_TYPE = "text/markdown"
REPORT_FILENAME = "report.md"

#: What an effect line records when no lease was published into the execution
#: scope. Never expected: the Worker enters the scope around every graph
#: invocation. It is written rather than raised so the *test* fails on the
#: evidence, instead of the graph failing for a reason the evidence cannot show.
NO_LEASE = "-"


class _LeaseSource:
    """The Worker's execution scope, which only exists after composition.

    Handlers have to be built before ``build_task_worker_dependencies`` is
    called, and the scope they must read is created inside it. Going through
    this indirection is what lets an effect line record the lease the node
    actually ran under, rather than whatever the Registry reports at that
    moment -- which, for the process that replaced a killed Worker, is a
    different claim entirely. That distinction is the whole of the fencing
    story, so the log has to carry the first one.
    """

    __slots__ = ("scope",)

    def __init__(self) -> None:
        self.scope: TaskExecutionScope | None = None

    def lease(self) -> ExecutionLease | None:
        return None if self.scope is None else self.scope.current()


def _append_effect(
    path: Path, *, label: str, node: str, lease: ExecutionLease | None
) -> None:
    """Record one node execution, durably enough to survive a ``SIGKILL``.

    One tab-separated line per execution -- ``label``, ``node``, ``worker_id``,
    ``epoch`` -- so the parent's reader is a ``split`` and its counter is a
    count. Written *before* the handler runs, so a line means "this node began
    executing in this process", which is the fact the parent has to see before
    it may kill anything.

    Opened and closed per line, in append mode. ``SIGKILL`` destroys the
    process but not the page cache, so a completed ``write`` is readable by the
    parent afterwards; a buffered stream held open across the block would not
    be.
    """

    fields = (
        label,
        node,
        NO_LEASE if lease is None else lease.worker_id,
        NO_LEASE if lease is None else str(lease.epoch),
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\t".join(fields) + "\n")


def _instrumented(
    *,
    label: str,
    effects: Path,
    block_at: TaskNodeId | None,
    leases: _LeaseSource,
    artifacts: LocalArtifactStore,
    tenant_id: str,
    owner_id: str,
) -> dict[TaskNodeId, NodeHandler]:
    """The demo handler set, each node wrapped to leave a trace and maybe hang."""

    demo = build_demo_handlers()
    if block_at is not None and block_at not in demo:
        # Loudly, at boot. A typo that silently blocked nothing would let the
        # parent kill a Worker that had already finished, and the test would
        # then be measuring a race rather than a crash.
        raise ValueError(f"no demo handler to block at: {block_at}")

    def wrap(node: TaskNodeId, handler: NodeHandler) -> NodeHandler:
        async def run(state: TaskState) -> Mapping[str, Any]:
            _append_effect(effects, label=label, node=node, lease=leases.lease())
            if node == block_at:
                # Never returns. The parent polls for the line written above and
                # then kills this process outright, which is the only way to
                # leave behind the state a graceful shutdown would have tidied.
                await asyncio.Event().wait()
            result = await handler(state)
            if node == "export":
                await artifacts.put(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    kind="report",
                    media_type=REPORT_MEDIA_TYPE,
                    content=json.dumps(
                        {"task_id": state.task_id, "draft_ref": state.draft_ref}
                    ).encode("utf-8"),
                    filename=REPORT_FILENAME,
                )
            return result

        return run

    return {node: wrap(node, handler) for node, handler in demo.items()}


async def serve(
    *,
    label: str,
    worker_id: str,
    effects: Path,
    block_at: TaskNodeId | None,
    tenant_id: str,
    owner_id: str,
) -> None:
    """Assemble, run and dispose one Worker process, as ``main.serve`` does."""

    config = project_task_worker(load_settings(), worker_id=worker_id)
    leases = _LeaseSource()
    dependencies = await build_task_worker_dependencies(
        config,
        handlers=_instrumented(
            label=label,
            effects=effects,
            block_at=block_at,
            leases=leases,
            # The same root the composition root gives its own store, so the
            # report lands where a deployed Worker's would.
            artifacts=LocalArtifactStore(Path(config.artifacts.local_root)),
            tenant_id=tenant_id,
            owner_id=owner_id,
        ),
    )
    leases.scope = dependencies.scope
    try:
        stop = asyncio.Event()
        # Mirrors main._install_shutdown_handlers. SIGTERM is how the parent
        # ends a Worker it did *not* kill -- the control group, and the second
        # process once the Task has settled -- so that an orderly stop stays
        # distinguishable from the crash this file exists to stage.
        loop = asyncio.get_running_loop()
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        runner = TaskWorkerRunner(
            run_once=dependencies.worker.run_once,
            poll_seconds=config.task.claim_poll_seconds,
            concurrency=config.worker_concurrency,
        )
        await dependencies.startup()
        # Printed after startup so the parent can tell "did not boot" from "booted
        # and found nothing to claim" without guessing from a timeout.
        print(f"worker-ready {label} {worker_id}", flush=True)
        await runner.run_forever(stop)
    finally:
        await dependencies.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-task-worker-test",
        description="Run one instrumented Task Worker process for a crash test.",
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--effects", required=True, type=Path)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument(
        "--block-at",
        default=None,
        help="node that appends its effect line and then hangs forever",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(
            serve(
                label=args.label,
                worker_id=args.worker_id,
                effects=args.effects,
                block_at=args.block_at,
                tenant_id=args.tenant,
                owner_id=args.owner,
            )
        )
    except KeyboardInterrupt:  # pragma: no cover - the parent sends SIGTERM
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
