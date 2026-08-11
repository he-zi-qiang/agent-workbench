"""Kill a Task Worker process outright, and watch a second one finish its Task.

Every recovery test in this repository until now has staged the crash inside
one pytest process: build an engine and a Worker, drop the references, build
another pair, assert the second finished what the first started. That shows the
durable state is sufficient, and it is genuinely worth showing -- but it cannot
show what a *process* death does, because the interpreter that was supposedly
dead ran every ``finally`` on the way out. The engine disposed. The guard
released its advisory lock. Nothing was left behind for recovery to find.

So this file uses ``SIGKILL`` on a real operating-system process, and
deliberately not ``SIGTERM``: an orderly shutdown proves the shutdown path, not
the recovery path. The Worker is a subprocess running
``tests/support/task_worker_process.py``, which is the deployed
``--demo`` assembly -- the same settings load, the same composition root, the
same Registry, session-pinned execution guard, fenced checkpointer and poll
loop -- with its node handlers wrapped so that each execution leaves a line in a
file and one chosen node hangs instead of returning.

Three things make this a test rather than a demonstration that a process can
die:

* the first Worker is killed only after three facts agree: the database says
  the Task is ``running`` under *its* lease, the effects file shows the graph
  reached the node that hangs, and the checkpoint durably says that node is
  what runs next. Killing on a timer would test an empty queue on a slow
  machine, and killing on the first two alone turned out to test something
  else again -- see below;
* the kill is checked -- ``returncode == -9`` -- and so is the wreck it left:
  a Task still marked ``running``, still naming a process that no longer
  exists. That is the "permanent fake running" the recovery has to undo, and
  asserting it exists is what stops the recovery assertions from passing
  vacuously; and
* the control group runs the identical Task through the identical Worker with
  nothing killed. Without it, an implementation that always took the recovery
  path -- or one that simply restarted the graph from the top every time --
  would look the same from here.

The third condition on the kill is worth stating plainly, because writing this
file is how it was found. LangGraph's default ``durability="async"`` does not
await a checkpoint put before starting the next superstep. The demo graph runs
end to end in under half a second, so a kill fired the moment the node started
executing landed while *every* put was still in flight: measured on this
machine, zero rows in ``workflow_checkpoints`` at that instant and seven of them
half a second later. A Worker killed there leaves nothing to resume, the next
one starts the graph over, and the Task still succeeds -- correct, and not what
this file claims to be about. A real Worker spends seconds per node and is not
normally in that window; a test with instantaneous nodes is always in it unless
it waits.

Bounded timeouts appear below only as a guard against a hung test. Nothing is
scheduled by sleeping: every wait polls a real fact (a Registry row, a line in
the effects file, a durable checkpoint position, a process return code) and
fails with what it last saw.

Real PostgreSQL only. No model provider: the demo graph contacts nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.artifacts.local import METADATA_SUFFIX
from agent_workbench.adapters.langgraph import (
    LangGraphTaskWorkflow,
    PostgresCheckpointSaver,
)
from agent_workbench.adapters.persistence import (
    PostgresEventLog,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import events
from agent_workbench.application.task_inputs import TaskInputStore
from agent_workbench.bootstrap.paths import PROJECT_ROOT, TEST_CONFIG_FILE
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.domain.task_inputs import TaskInput
from agent_workbench.ports.task_registry import TaskRun, TaskSubmission

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = (
    "approvals, task_runs, events, event_streams, workflow_checkpoints, "
    "workflow_checkpoint_blobs, workflow_checkpoint_writes, tool_executions"
)

TENANT = "tenant_crash"
OWNER = "user_crash"

#: Where the first Worker is stopped. ``critic`` is chosen rather than the first
#: node because everything before it -- both research branches and the draft --
#: has by then completed *and* been checkpointed. A recovery that restarted the
#: graph instead of resuming it would therefore produce a second draft and a
#: second pair of research runs, which the per-node counts below would see.
BLOCK_NODE = "critic"

#: Every node the demo v1 graph runs a handler for. ``route`` and
#: ``quality_gate`` are absent on purpose: the graph builder gives those its own
#: pass-through, so no wrapper of ours is ever installed on them.
V1_NODES = (
    "understand",
    "plan",
    "research_internal",
    "research_external",
    "synthesize",
    "critic",
    "approval",
    "export",
)

FIRST_WORKER_ID = "worker_crash_first"
SECOND_WORKER_ID = "worker_crash_second"

#: The one status a killed Worker must not leave permanently behind.
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "dead_letter", "waiting_migration"}
)

#: The shortest lease Settings accepts (``lease_duration_seconds >= 10``), which
#: is what the recovery has to wait out before the second Worker may reclaim the
#: Task. It bounds how long this file takes and is not otherwise interesting.
LEASE_SECONDS = 10

#: Ceilings, not schedules. Each one is "this observation should have happened
#: by now, so stop rather than hang", and every wait underneath them polls.
BOOT_TIMEOUT_SECONDS = 60.0
RECOVERY_TIMEOUT_SECONDS = 120.0
EXIT_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.05


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _engine() -> Any:
    return create_query_engine(_dsn(), application_name="agent-workbench-tests")


# --------------------------------------------------------------------------
# What one run of the scenario produced
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Effect:
    """One node execution, as the process that ran it recorded it."""

    label: str
    node: str
    worker_id: str
    epoch: str


@dataclass(frozen=True, slots=True)
class _Observed:
    """Everything one scenario left behind, read after both processes ended."""

    effects: tuple[_Effect, ...]
    final: TaskRun
    lifecycle: tuple[tuple[str, int, int], ...]
    reports: int
    #: ``None`` for the control group, which is never killed.
    killed: tuple[str, int, int] | None = None
    #: The Registry row immediately after the kill: status and lease owner.
    orphaned: tuple[str, str | None] | None = None
    #: Whether the durable checkpoint still pointed at the blocked node once
    #: the process holding it was gone.
    paused_at: bool = False
    first_returncode: int | None = None

    def count(self, node: str) -> int:
        return sum(1 for effect in self.effects if effect.node == node)

    def nodes_run_by(self, label: str) -> tuple[str, ...]:
        return tuple(effect.node for effect in self.effects if effect.label == label)


# --------------------------------------------------------------------------
# The child process
# --------------------------------------------------------------------------


def _child_environment(*, artifact_root: Path) -> dict[str, str]:
    """The environment one Worker subprocess boots from.

    Built from this process's own, then overridden: the DSN the suite was given
    becomes all three the Worker asks for, and the test overlay is selected
    explicitly rather than inherited from whatever the developer exported.

    ``AW_ENV_FILE`` points at a path that does not exist on purpose. Settings
    would otherwise read the checkout's ``.env``, and a Worker in this test must
    be configured by this test alone.

    The coordination timings are the only substantive change, and they exist to
    bound the test: a Task whose Worker died is reclaimable once its lease
    expires, and the shipped 90-second lease would make that the runtime of this
    file. Ten seconds is the floor Settings allows, and the heartbeat and grace
    below it are set to keep ``lease > heartbeat * (missed + 1) + grace`` true.
    """

    dsn = _dsn()
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                (str(PROJECT_ROOT / "src"), environment.get("PYTHONPATH", ""))
            ).rstrip(os.pathsep),
            "AW_CONFIG_FILE": str(TEST_CONFIG_FILE),
            "AW_ENV_FILE": str(artifact_root / "absent.env"),
            "AW_DATABASE__DSN": dsn,
            "AW_DATABASE__GUARD_DSN": dsn,
            "AW_DATABASE__LISTEN_DSN": dsn,
            "AW_ARTIFACT_STORE__LOCAL_ROOT": str(artifact_root),
            "AW_COORDINATION__LEASE_DURATION_SECONDS": str(LEASE_SECONDS),
            "AW_COORDINATION__HEARTBEAT_INTERVAL_SECONDS": "1",
            "AW_COORDINATION__LEASE_GRACE_SECONDS": "0",
            "AW_COORDINATION__MAX_MISSED_HEARTBEATS": "2",
            "AW_COORDINATION__CLAIM_POLL_INTERVAL_MS": "200",
            "AW_DATABASE__GUARD_HEALTHCHECK_SECONDS": "1",
        }
    )
    return environment


@dataclass(slots=True)
class _Worker:
    """A Worker subprocess and the two files its output goes to."""

    process: subprocess.Popen[bytes]
    label: str
    log: Path

    def diagnostics(self) -> str:
        """What the child said, for an assertion that has to explain itself."""

        text_output = self.log.read_text(encoding="utf-8", errors="replace")
        return f"--- {self.label} (pid {self.process.pid}) output ---\n{text_output}"


def _spawn(
    *,
    label: str,
    worker_id: str,
    effects: Path,
    artifact_root: Path,
    logs: Path,
    block_at: str | None = None,
) -> _Worker:
    """Start one Worker as a real child process of this one."""

    log = logs / f"{label}.log"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "tests" / "support" / "task_worker_process.py"),
        "--label",
        label,
        "--worker-id",
        worker_id,
        "--effects",
        str(effects),
        "--tenant",
        TENANT,
        "--owner",
        OWNER,
    ]
    if block_at is not None:
        command += ["--block-at", block_at]
    handle = log.open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=_child_environment(artifact_root=artifact_root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            # No shell and no wrapper: the pid this test signals has to be the
            # interpreter running the Worker, not something that would forward
            # the signal or outlive it.
            shell=False,
        )
    finally:
        handle.close()
    return _Worker(process=process, label=label, log=log)


def _stop(worker: _Worker) -> None:
    """End a Worker the ordinary way, and make sure it is gone."""

    if worker.process.poll() is None:
        worker.process.send_signal(signal.SIGTERM)
    try:
        worker.process.wait(timeout=EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - teardown safety net
        worker.process.kill()
        worker.process.wait(timeout=EXIT_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------


def _read_effects(effects: Path) -> tuple[_Effect, ...]:
    """Every node execution recorded so far, by any Worker."""

    if not effects.is_file():
        return ()
    parsed: list[_Effect] = []
    for line in effects.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        label, node, worker_id, epoch = line.split("\t")
        parsed.append(_Effect(label=label, node=node, worker_id=worker_id, epoch=epoch))
    return tuple(parsed)


async def _wait_until(
    condition: Callable[[], Awaitable[Any]],
    *,
    timeout: float,
    what: str,
    worker: _Worker,
) -> Any:
    """Poll a real fact until it is true, or fail saying what was last seen.

    The interval is an observation granularity, not a schedule: nothing here
    assumes that waiting a fixed time moved anything along, and the value the
    condition returns is what the caller acts on.

    A Worker that exits while this is waiting ends the wait immediately. Sitting
    out the full timeout would report "the Task never settled" for a child that
    died at boot on a misconfiguration, and the child's traceback is the answer.
    """

    deadline = time.monotonic() + timeout
    observed: Any = None
    while time.monotonic() < deadline:
        observed = await condition()
        if observed:
            return observed
        if worker.process.poll() is not None:
            raise AssertionError(
                f"{worker.label} exited with {worker.process.returncode} while "
                f"waiting for {what}\n{worker.diagnostics()}"
            )
        await asyncio.sleep(POLL_SECONDS)
    raise AssertionError(
        f"timed out after {timeout:.0f}s waiting for {what}; "
        f"last observation: {observed!r}\n{worker.diagnostics()}"
    )


def _positions(engine: Any) -> LangGraphTaskWorkflow:
    """A read-only view of the durable graph position, over the same database.

    No handlers and no fence: nothing here runs a node or writes a checkpoint.
    It is the same adapter the Worker inspects with, so what this test calls
    "where the graph is" is what a recovering Worker will call it.
    """

    return LangGraphTaskWorkflow(checkpointer=PostgresCheckpointSaver(engine))


async def _pending(positions: LangGraphTaskWorkflow, thread_id: str) -> bool:
    """Whether the durable checkpoint says the blocked node is what runs next.

    ``ValidationError`` is not an error here. Between the initial checkpoint
    landing and the first node's writes landing, LangGraph reports ``__start__``
    as the pending node, and ``CheckpointPosition.pending_nodes`` accepts only
    real graph nodes -- so ``inspect`` raises for a window this poll walks
    straight through. Treated as "not there yet" rather than swallowed: the wait
    is bounded, and a position that never settles fails the test with the last
    thing it saw.
    """

    try:
        position = await positions.inspect(thread_id)
    except ValidationError:
        return False
    return position is not None and position.pending_nodes == (BLOCK_NODE,)


async def _still_there(registry: PostgresTaskRegistry, task_id: str) -> TaskRun:
    """The Task row, or a loud failure -- never a quiet "not yet".

    A submitted Task cannot stop existing: nothing in this scenario deletes it,
    and neither Worker can. So ``None`` here means something outside the test
    truncated ``task_runs`` -- another suite sharing the database, most likely.
    Folding that into "the condition is not true yet" would spend the whole
    recovery timeout and then report that recovery failed, which is a false
    accusation against the code under test.
    """

    current = await registry.get(task_id)
    if current is None:
        raise AssertionError(
            f"task {task_id} vanished from task_runs while the scenario was "
            "running; nothing in this test deletes it, so another process "
            "truncated the table"
        )
    return current


async def _lifecycle(engine: Any, task_id: str) -> tuple[tuple[str, int, int], ...]:
    """The Task's own event stream, reduced to the fencing facts it records."""

    async with engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    select(events.c.event_type, events.c.payload)
                    .where(events.c.task_id == task_id)
                    .order_by(events.c.sequence)
                )
            )
            .mappings()
            .all()
        )
    return tuple(
        (
            str(row["event_type"]),
            int(row["payload"].get("epoch", -1)),
            int(row["payload"].get("attempt", -1)),
        )
        for row in rows
    )


def _report_count(artifact_root: Path) -> int:
    """How many report artifacts exist on disk, counted as files."""

    tenant_directory = artifact_root / TENANT
    if not tenant_directory.is_dir():
        return 0
    total = 0
    for metadata in tenant_directory.glob(f"*{METADATA_SUFFIX}"):
        envelope = json.loads(metadata.read_text(encoding="utf-8"))
        if envelope["reference"]["kind"] == "report":
            total += 1
    return total


# --------------------------------------------------------------------------
# Setting one Task up
# --------------------------------------------------------------------------


async def _truncate(engine: Any) -> None:
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))


async def _submit(engine: Any, artifact_root: Path) -> TaskRun:
    """Store a real input artifact and open the Task that references it.

    The Worker loads its state through the deployed ``TaskInputStore``, which
    re-reads the artifact and checks its fingerprint, so the input has to be
    real bytes in the store the Worker was pointed at rather than a synthetic
    reference.
    """

    task_input = TaskInput(
        objective="Summarise what a killed Worker leaves behind.",
        # No revision budget: the demo critic always passes, and a graph that
        # could loop would make the per-node counts below a range instead of a
        # number.
        max_revisions=0,
        # So the graph runs approval and export, which is where the one product
        # this test counts gets written.
        wants_report=True,
    )
    stored = await TaskInputStore(LocalArtifactStore(artifact_root)).store(
        principal=PrincipalContext(principal_id=OWNER, tenant_id=TENANT),
        task_input=task_input,
    )
    return await PostgresTaskRegistry(engine, events=PostgresEventLog(engine)).submit(
        TaskSubmission.model_validate(
            {
                "tenant_id": TENANT,
                "owner_id": OWNER,
                "thread_id": "thr_crash_1",
                "graph_version": "v1",
                "input_ref": stored.artifact_id,
                "input_fingerprint": task_input.fingerprint,
                "submission_dedup_key": "dedup_crash_1",
                "run_semantics_snapshot": {"model": {"provider": "fake"}},
                "run_semantics_revision": "1.2:v1.3:"
                + hashlib.sha256(b"crash").hexdigest()[:16],
                "submitted_policy_revision": "policy-1",
                "submitted_policy_fingerprint": "f" * 16,
                "submitted_authorization_envelope": {},
            }
        )
    )


# --------------------------------------------------------------------------
# The two scenarios
# --------------------------------------------------------------------------


async def _crash_scenario(workspace: Path) -> _Observed:
    """Start a Worker, kill it mid-graph, and let a second one recover."""

    effects = workspace / "effects.tsv"
    artifacts = workspace / "artifacts"
    logs = workspace / "logs"
    artifacts.mkdir()
    logs.mkdir()

    engine = _engine()
    try:
        await _truncate(engine)
        task = await _submit(engine, artifacts)
        registry = PostgresTaskRegistry(engine)

        first = _spawn(
            label="w1",
            worker_id=FIRST_WORKER_ID,
            effects=effects,
            artifact_root=artifacts,
            logs=logs,
            block_at=BLOCK_NODE,
        )
        positions = _positions(engine)
        try:
            # Three facts, not one. The row says this process holds the lease;
            # the effects file says its graph got as far as the node that hangs;
            # and the checkpoint says the durable position is "next: that node".
            #
            # The third is the one that had to be measured rather than assumed.
            # LangGraph's default ``durability="async"`` does not await a
            # checkpoint put before starting the next superstep, and the demo
            # graph is fast enough that a kill fired the instant the node
            # started found *zero* checkpoint rows: the whole graph was still
            # in flight. That is honest behaviour and not a defect, but it is
            # not the crash this file is about -- recovering from nothing is
            # just starting -- so the wait is for a position a resume can
            # actually stand on.
            async def executing() -> TaskRun | None:
                current = await _still_there(registry, task.task_id)
                if current.status != "running":
                    return None
                if current.lease_owner != FIRST_WORKER_ID:
                    return None
                reached = any(
                    effect.label == "w1" and effect.node == BLOCK_NODE
                    for effect in _read_effects(effects)
                )
                if not reached:
                    return None
                return current if await _pending(positions, task.thread_id) else None

            running = await _wait_until(
                executing,
                timeout=BOOT_TIMEOUT_SECONDS,
                what=(
                    f"the first Worker to reach {BLOCK_NODE} under its own lease, "
                    f"with the checkpoint durably positioned at {BLOCK_NODE}"
                ),
                worker=first,
            )
            killed = (
                str(running.lease_owner),
                running.lease_epoch,
                running.attempt_count,
            )
            os.kill(first.process.pid, signal.SIGKILL)
            first.process.wait(timeout=EXIT_TIMEOUT_SECONDS)
        finally:
            _stop(first)

        orphan = await _still_there(registry, task.task_id)
        paused_at = await _pending(positions, task.thread_id)
        orphaned = (orphan.status, orphan.lease_owner)

        second = _spawn(
            label="w2",
            worker_id=SECOND_WORKER_ID,
            effects=effects,
            artifact_root=artifacts,
            logs=logs,
        )
        try:

            async def settled() -> TaskRun | None:
                current = await _still_there(registry, task.task_id)
                return current if current.status in TERMINAL_STATUSES else None

            final = await _wait_until(
                settled,
                timeout=RECOVERY_TIMEOUT_SECONDS,
                what="the second Worker to settle the Task",
                worker=second,
            )
        finally:
            _stop(second)

        return _Observed(
            effects=_read_effects(effects),
            final=final,
            lifecycle=await _lifecycle(engine, task.task_id),
            reports=_report_count(artifacts),
            killed=killed,
            orphaned=orphaned,
            paused_at=paused_at,
            first_returncode=first.process.returncode,
        )
    finally:
        await engine.dispose()


async def _control_scenario(workspace: Path) -> _Observed:
    """The same Task and the same Worker, with nothing killed."""

    effects = workspace / "effects.tsv"
    artifacts = workspace / "artifacts"
    logs = workspace / "logs"
    artifacts.mkdir()
    logs.mkdir()

    engine = _engine()
    try:
        await _truncate(engine)
        task = await _submit(engine, artifacts)
        registry = PostgresTaskRegistry(engine)
        worker = _spawn(
            label="w1",
            worker_id=FIRST_WORKER_ID,
            effects=effects,
            artifact_root=artifacts,
            logs=logs,
        )
        try:

            async def settled() -> TaskRun | None:
                current = await _still_there(registry, task.task_id)
                return current if current.status in TERMINAL_STATUSES else None

            final = await _wait_until(
                settled,
                timeout=BOOT_TIMEOUT_SECONDS,
                what="the only Worker to settle the Task",
                worker=worker,
            )
        finally:
            _stop(worker)

        return _Observed(
            effects=_read_effects(effects),
            final=final,
            lifecycle=await _lifecycle(engine, task.task_id),
            reports=_report_count(artifacts),
        )
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def crashed(tmp_path_factory: pytest.TempPathFactory) -> _Observed:
    """One kill-and-recover run, shared by the assertions that read it.

    Module scoped because the scenario spends most of a lease waiting for the
    dead Worker's claim to expire, and re-running that for each property would
    buy nothing: they are all statements about the same single run.
    """

    _dsn()
    return asyncio.run(_crash_scenario(tmp_path_factory.mktemp("crash")))


@pytest.fixture(scope="module")
def uninterrupted(tmp_path_factory: pytest.TempPathFactory) -> _Observed:
    """The control group: the same Task, never killed."""

    _dsn()
    return asyncio.run(_control_scenario(tmp_path_factory.mktemp("control")))


# --------------------------------------------------------------------------
# The kill itself
# --------------------------------------------------------------------------


def test_the_first_worker_was_killed_rather_than_asked_to_stop(
    crashed: _Observed,
) -> None:
    """``-9``, and nothing else.

    A negative return code is the signal that ended the process. Were this
    ``0`` or ``-15``, the Worker would have unwound its own resources on the
    way out and everything below would be a test of orderly shutdown.
    """

    assert crashed.first_returncode == -signal.SIGKILL


def test_the_killed_worker_left_the_task_running_under_a_dead_owner(
    crashed: _Observed,
) -> None:
    """The wreck the recovery has to clear up, asserted before it is cleared.

    Without this, "the Task ended up succeeded" would also be true of a run
    where the first Worker had quietly settled it before dying, and the second
    process had nothing to recover.
    """

    assert crashed.orphaned == ("running", FIRST_WORKER_ID)
    assert crashed.killed is not None
    _, epoch, attempt = crashed.killed
    assert (epoch, attempt) == (1, 1)
    # And the checkpoint the dead process wrote outlived it, still pointing at
    # the node it never finished. Without this the Task would be recoverable
    # only by starting over, which is a different property.
    assert crashed.paused_at is True


# --------------------------------------------------------------------------
# What the second process did about it
# --------------------------------------------------------------------------


def test_a_second_process_finishes_the_task_the_killed_one_abandoned(
    crashed: _Observed,
) -> None:
    """No permanent fake ``running``: the Task reaches a real terminal state."""

    assert crashed.final.status == "succeeded"
    assert crashed.final.lease_owner is None


def test_recovery_re_runs_only_the_node_that_died(crashed: _Observed) -> None:
    """Every other node executed exactly once, across both processes.

    This is the assertion a "recovery" that simply restarted the graph would
    fail: understand, both research branches and the draft were finished and
    checkpointed before the kill, and running them again would be doing the
    work twice while still ending in ``succeeded``.
    """

    assert crashed.count(BLOCK_NODE) == 2
    for node in V1_NODES:
        if node == BLOCK_NODE:
            continue
        assert crashed.count(node) == 1, f"{node} ran {crashed.count(node)} times"


def test_the_two_processes_split_the_graph_at_the_node_that_died(
    crashed: _Observed,
) -> None:
    """Who ran what, rather than only how many times."""

    first = crashed.nodes_run_by("w1")
    assert first[:2] == ("understand", "plan")
    # The two research branches share one superstep, so which of them appends
    # its line first is LangGraph's business and not a property of recovery.
    assert sorted(first[2:4]) == ["research_external", "research_internal"]
    assert first[4:] == ("synthesize", BLOCK_NODE)
    assert crashed.nodes_run_by("w2") == (BLOCK_NODE, "approval", "export")


def test_the_recovered_task_produced_exactly_one_report(crashed: _Observed) -> None:
    """One logical product, counted as files in the artifact store.

    A count of rows or of handler calls could be maintained by the test itself.
    A file either exists on disk or does not.
    """

    assert crashed.reports == 1


# --------------------------------------------------------------------------
# The fencing ledger
# --------------------------------------------------------------------------


def test_every_node_ran_under_the_lease_its_own_process_was_granted(
    crashed: _Observed,
) -> None:
    """The epoch each node saw, read from inside the graph.

    A node reads its claim from the execution scope the Worker published, not
    from the Registry -- so this is the value every fenced write downstream is
    checked against. The dead Worker's nodes must all carry epoch 1 and its own
    id; the survivor's must all carry epoch 2 and its own. A node that saw the
    Registry's current answer instead would show the successor's epoch on the
    predecessor's work, which is precisely the write a fence exists to refuse.
    """

    by_label = {
        "w1": (FIRST_WORKER_ID, "1"),
        "w2": (SECOND_WORKER_ID, "2"),
    }
    for effect in crashed.effects:
        assert (effect.worker_id, effect.epoch) == by_label[effect.label], effect

    # And the two attempts at the killed node are distinguishable by epoch,
    # which is the whole point of it being monotonic.
    epochs = [effect.epoch for effect in crashed.effects if effect.node == BLOCK_NODE]
    assert epochs == ["1", "2"]


def test_the_registry_records_a_second_claim_at_a_higher_epoch(
    crashed: _Observed,
) -> None:
    """The row's own account of the recovery."""

    assert crashed.final.lease_epoch == 2
    assert crashed.final.attempt_count == 2


def test_the_event_log_says_why_the_task_was_claimed_twice(
    crashed: _Observed,
) -> None:
    """Submitted, claimed, expired, claimed again, succeeded -- in that order.

    The reclaim is the load-bearing entry. Without it the log would show two
    claims of one Task and no reason, which reads like a double dispatch rather
    than like a lease that ran out because nobody was alive to renew it.
    """

    kinds = [kind for kind, _, _ in crashed.lifecycle]
    assert kinds == [
        "TaskSubmitted",
        "TaskClaimed",
        "TaskRetryScheduled",
        "TaskClaimed",
        "TaskSucceeded",
    ]
    assert crashed.lifecycle[1][1:] == (1, 1)
    assert crashed.lifecycle[3][1:] == (2, 2)
    assert crashed.lifecycle[4][1:] == (2, 2)


# --------------------------------------------------------------------------
# The control group
# --------------------------------------------------------------------------


def test_the_same_task_completes_in_one_process_when_nothing_kills_it(
    uninterrupted: _Observed,
) -> None:
    """Everything once, one claim, one report, no reclaim.

    An implementation that always recovered -- or a test whose Worker could only
    ever finish a Task on the second attempt -- would fail here while passing
    everything above.
    """

    assert uninterrupted.final.status == "succeeded"
    assert uninterrupted.final.lease_epoch == 1
    assert uninterrupted.final.attempt_count == 1
    assert uninterrupted.reports == 1
    for node in V1_NODES:
        assert uninterrupted.count(node) == 1, (
            f"{node} ran {uninterrupted.count(node)} times"
        )
    kinds = [kind for kind, _, _ in uninterrupted.lifecycle]
    assert kinds == ["TaskSubmitted", "TaskClaimed", "TaskSucceeded"]


def test_the_control_group_uses_the_same_worker_the_crash_test_kills(
    uninterrupted: _Observed,
) -> None:
    """Same process, same handlers, same lease plumbing -- only the kill differs.

    Asserted rather than left to the reader of ``_spawn``: a control group that
    had drifted onto a different assembly would stop being a control.
    """

    labels = {effect.label for effect in uninterrupted.effects}
    assert labels == {"w1"}
    assert {effect.worker_id for effect in uninterrupted.effects} == {FIRST_WORKER_ID}
