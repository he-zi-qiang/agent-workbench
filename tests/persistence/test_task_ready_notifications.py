"""The wake-up that says a Task became claimable.

A real ``LISTEN`` on a real connection, because what is under test is a property
of PostgreSQL rather than of this code: notifications are delivered at commit,
so a transition that rolled back cannot wake anybody. Asserting that against a
fake would be asserting it against the fake.

Every test here also has its control group, and the same one: a notification is
absent for a great many uninteresting reasons -- a listener that never
subscribed, a channel name typo, a race the test lost. So "nothing arrived" is
only ever asserted beside a case where something did.

The listener is deliberately not this project's own: none exists yet, and one
written here would be a second implementation to keep in step. What the Worker
does with these messages is separate work; this establishes that they are sent,
transactionally, for exactly the transitions that make a Task claimable.

Real PostgreSQL only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text

from agent_workbench.adapters.persistence import (
    TASK_READY_CHANNEL,
    PostgresApprovalStore,
    PostgresTaskRegistry,
    create_query_engine,
)
from agent_workbench.ports.approvals import ApprovalNotDecidableError
from agent_workbench.ports.task_registry import TaskSubmission
from agent_workbench.workflows.approval import APPROVAL_OPERATION_ID

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TABLES = "approvals, task_runs, events, event_streams"

#: The channel this listener subscribes to, written out rather than imported.
#: A test that took the name from ``TASK_READY_CHANNEL`` would subscribe to
#: whatever the sender happened to be sending on, and would stay green through a
#: rename that every real listener -- configured, deployed, in another language
#: -- would miss. The constant is pinned against this literal once, below.
CHANNEL = "task_ready"

#: How long a committed notification is given to arrive. Generous, and only ever
#: waited out in full by a test asserting silence -- the positive cases return as
#: soon as the message lands.
DELIVERY_TIMEOUT_SECONDS = 2.0


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


def _raw_dsn() -> str:
    """The same database, addressed the way the driver wants it."""

    return _dsn().replace("postgresql+asyncpg://", "postgresql://")


class _Listener:
    """A dedicated session holding ``LISTEN``, and what it heard."""

    def __init__(self) -> None:
        self._connection: Any = None
        self.received: list[dict[str, Any]] = []
        self._arrived = asyncio.Event()

    async def __aenter__(self) -> _Listener:
        self._connection = await asyncpg.connect(_raw_dsn())
        await self._connection.add_listener(CHANNEL, self._on_notify)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._connection is not None:
            await self._connection.remove_listener(CHANNEL, self._on_notify)
            await self._connection.close()

    def _on_notify(self, _: Any, __: int, ___: str, payload: str) -> None:
        self.received.append(json.loads(payload))
        self._arrived.set()

    async def wait(self, *, expected: int = 1) -> list[dict[str, Any]]:
        """Wait for ``expected`` messages, or return whatever arrived in time."""

        async def collect() -> None:
            while len(self.received) < expected:
                self._arrived.clear()
                await self._arrived.wait()

        # A timeout is an answer, not a failure: the tests that assert
        # silence reach it every time, and the ones that assert delivery
        # would rather report what did arrive than a bare TimeoutError.
        with suppress(TimeoutError):
            await asyncio.wait_for(collect(), timeout=DELIVERY_TIMEOUT_SECONDS)
        return list(self.received)


def _run(scenario: Callable[[Any, _Listener], Awaitable[Any]]) -> Any:
    _dsn()

    async def execute() -> Any:
        engine = create_query_engine(_dsn(), application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            # Subscribed before anything is written. A listener that attached
            # afterwards would miss the message it is here to observe, and the
            # test would read as "not sent".
            async with _Listener() as listener:
                return await scenario(engine, listener)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _submission(**overrides: Any) -> TaskSubmission:
    base: dict[str, Any] = {
        "tenant_id": "tenant_a",
        "owner_id": "user_1",
        "thread_id": "thr_1",
        "graph_version": "v1",
        "input_ref": "input_1",
        "input_fingerprint": hashlib.sha256(b"input_1").hexdigest(),
        "submission_dedup_key": "dedup_1",
        "run_semantics_snapshot": {"model": {"provider": "deepseek"}},
        "run_semantics_revision": "1.2:v1.3:abc0123456789def",
        "submitted_policy_revision": "policy-1",
        "submitted_policy_fingerprint": "f" * 16,
        "submitted_authorization_envelope": {},
    }
    base.update(overrides)
    return TaskSubmission.model_validate(base)


# --------------------------------------------------------------------------
# What is sent, and what it says
# --------------------------------------------------------------------------


def test_the_channel_name_is_the_one_a_listener_would_be_configured_with() -> None:
    """Renaming the channel is a protocol change, not a refactor.

    Every other test here subscribes to the literal, so a rename fails them all.
    This one says why the literal is correct: it is the name the architecture
    baseline gives, and a deployed listener has it written down somewhere this
    repository cannot rename.
    """

    assert TASK_READY_CHANNEL == CHANNEL


def test_a_submitted_task_announces_itself_by_id_and_nothing_else() -> None:
    async def scenario(engine: Any, listener: _Listener) -> tuple[Any, ...]:
        task = await PostgresTaskRegistry(engine).submit(_submission())
        return task.task_id, await listener.wait()

    task_id, received = _run(scenario)

    assert received == [{"task_id": task_id}]
    # A wake-up, not a message. Nothing here tells a listener what happened, so
    # nothing here can be acted on without reading the row.
    assert set(received[0]) == {"task_id"}


def test_an_approval_decision_wakes_the_task_it_requeued() -> None:
    async def scenario(engine: Any, listener: _Listener) -> tuple[Any, ...]:
        registry = PostgresTaskRegistry(engine)
        store = PostgresApprovalStore(engine)
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.await_approval(claim.lease)
        approval = await store.request(
            task_id=task.task_id,
            graph_node_operation_id=APPROVAL_OPERATION_ID,
            tenant_id="tenant_a",
            owner_id="user_1",
        )
        # The submission's own wake-up is already in flight; wait past it.
        before = await listener.wait()
        await store.decide(
            approval.approval_id,
            decision="approved",
            decision_version=1,
            decided_by="reviewer_1",
        )
        return task.task_id, before, await listener.wait(expected=len(before) + 1)

    task_id, before, after = _run(scenario)

    assert before == [{"task_id": task_id}]
    assert after == [{"task_id": task_id}, {"task_id": task_id}]


def test_a_released_claim_announces_that_the_task_is_back() -> None:
    async def scenario(engine: Any, listener: _Listener) -> tuple[Any, ...]:
        registry = PostgresTaskRegistry(engine)
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        before = await listener.wait()
        await registry.release_for_retry(claim.lease, delay_seconds=0)
        return task.task_id, before, await listener.wait(expected=2)

    task_id, before, after = _run(scenario)

    assert before == [{"task_id": task_id}]
    assert after == [{"task_id": task_id}, {"task_id": task_id}]


# --------------------------------------------------------------------------
# What is not sent
# --------------------------------------------------------------------------


def test_a_rolled_back_submission_wakes_nobody() -> None:
    """The control group is the first test: the same submission, committed.

    Here the transaction is deliberately abandoned. PostgreSQL sends
    notifications at commit, so the wake-up for work that did not happen is
    never delivered -- which is the reason the send is inside the transaction
    rather than after it.
    """

    async def scenario(engine: Any, listener: _Listener) -> tuple[Any, ...]:
        connection = await engine.connect()
        transaction = await connection.begin()
        await connection.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL, "payload": '{"task_id":"task_rollback"}'},
        )
        await transaction.rollback()
        await connection.close()

        silent = await listener.wait()
        # Then commit one, so "nothing arrived" is distinguishable from a
        # listener that was never going to hear anything at all.
        task = await PostgresTaskRegistry(engine).submit(_submission())
        return silent, await listener.wait(), task.task_id

    silent, heard, task_id = _run(scenario)

    assert silent == []
    assert heard == [{"task_id": task_id}]


def test_a_refused_decision_does_not_wake_a_task_nobody_released() -> None:
    """A late approval on a cancelled Task rolls its whole transaction back.

    Without the rollback carrying the notification with it, a Worker would be
    sent to look at a Task a human never released -- it would find a cancelled
    row and stop, but the wake-up would still be a lie the database told.
    """

    async def scenario(engine: Any, listener: _Listener) -> tuple[Any, ...]:
        registry = PostgresTaskRegistry(engine)
        store = PostgresApprovalStore(engine)
        task = await registry.submit(_submission())
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.await_approval(claim.lease)
        approval = await store.request(
            task_id=task.task_id,
            graph_node_operation_id=APPROVAL_OPERATION_ID,
            tenant_id="tenant_a",
            owner_id="user_1",
        )
        await registry.cancel(task.task_id, reason="the owner asked")
        before = await listener.wait()

        with pytest.raises(ApprovalNotDecidableError):
            await store.decide(
                approval.approval_id,
                decision="approved",
                decision_version=1,
                decided_by="reviewer_1",
            )
        return task.task_id, before, await listener.wait(expected=2)

    task_id, before, after = _run(scenario)

    # Only the submission's. The refused decision added nothing.
    assert before == [{"task_id": task_id}]
    assert after == before


def test_a_dead_lettered_task_is_not_announced_as_claimable() -> None:
    """The control group is inside the same test: one reclaim of each kind.

    A reclaimed Task goes back on the queue and is announced; one whose attempt
    budget ran out is terminal, and announcing it would send a Worker to look at
    something it must not pick up.
    """

    async def scenario(engine: Any, listener: _Listener) -> tuple[Any, ...]:
        registry = PostgresTaskRegistry(engine)
        retried = await registry.submit(_submission())
        doomed = await registry.submit(
            _submission(thread_id="thr_2", submission_dedup_key="dedup_2")
        )
        for _ in range(2):
            claim = await registry.claim_next("worker_1", lease_seconds=60)
            assert claim is not None
        # Expire both leases, and exhaust one Task's attempt budget.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE task_runs SET lease_until = now() - interval '1 minute'")
            )
            await connection.execute(
                text("UPDATE task_runs SET attempt_count = 9 WHERE task_id = :t"),
                {"t": doomed.task_id},
            )
        submissions = await listener.wait(expected=2)

        recovered = await registry.reclaim_expired(
            limit=10, max_attempts=5, retry_base_seconds=1, retry_max_seconds=60
        )
        statuses = {task.task_id: task.status for task in recovered}
        return (
            retried.task_id,
            doomed.task_id,
            statuses,
            len(submissions),
            await listener.wait(expected=3),
        )

    retried_id, doomed_id, statuses, submitted, received = _run(scenario)

    assert statuses == {retried_id: "queued", doomed_id: "dead_letter"}
    assert submitted == 2
    # Two submissions and exactly one reclaim -- the dead-lettered Task is not
    # announced, and the timeout above gave it every chance to be.
    assert received == [
        {"task_id": retried_id},
        {"task_id": doomed_id},
        {"task_id": retried_id},
    ]


def test_claiming_a_task_announces_nothing() -> None:
    """The queue only ever gets a wake-up when something enters it.

    A claim takes a Task *out*, and a notification there would wake every
    listener for work none of them can have.
    """

    async def scenario(engine: Any, listener: _Listener) -> tuple[Any, ...]:
        registry = PostgresTaskRegistry(engine)
        task = await registry.submit(_submission())
        before = await listener.wait()
        claim = await registry.claim_next("worker_1", lease_seconds=60)
        assert claim is not None
        await registry.mark_succeeded(claim.lease)
        return task.task_id, before, await listener.wait(expected=2)

    task_id, before, after = _run(scenario)

    assert before == [{"task_id": task_id}]
    assert after == before
