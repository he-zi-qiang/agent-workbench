"""The beacon announces first, keeps beating, forgets on exit, and never raises."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from agent_workbench.adapters.memory import InMemoryWorkerPresenceStore
from agent_workbench.application.worker_presence import TTL_BEATS, WorkerPresenceBeacon


def test_the_beacon_announces_at_once_and_keeps_beating() -> None:
    async def scenario() -> None:
        store = InMemoryWorkerPresenceStore()
        beacon = WorkerPresenceBeacon(
            store,
            worker_id="worker_task_1",
            kind="task",
            deployment="demo-local",
            capabilities={"demo": True},
            interval_seconds=0.05,
        )
        async with beacon:
            first = (await store.report()).workers
            assert [row.worker_id for row in first] == ["worker_task_1"]
            ttl = (first[0].expires_at - first[0].heartbeat_at).total_seconds()
            assert ttl == pytest.approx(0.05 * TTL_BEATS)
            assert beacon.ttl_seconds == pytest.approx(0.05 * TTL_BEATS)
            await asyncio.sleep(0.18)
            later = (await store.report()).workers
            assert later[0].heartbeat_at > first[0].heartbeat_at
        # An orderly exit leaves no row: a clean stop reads as absent, a crash
        # reads as stale -- and the console can tell the two apart.
        assert (await store.report()).workers == ()

    asyncio.run(scenario())


class _RefusingStore:
    """A store that cannot be written -- the migration not applied, say."""

    async def announce(self, **kwargs: object) -> object:
        del kwargs
        raise RuntimeError("relation worker_presence does not exist")

    async def forget(self, worker_id: str) -> None:
        del worker_id
        raise RuntimeError("relation worker_presence does not exist")

    async def report(self) -> object:
        raise AssertionError("the beacon never reads")


def test_a_store_that_refuses_costs_a_warning_never_the_worker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        beacon = WorkerPresenceBeacon(
            _RefusingStore(),  # pyright: ignore[reportArgumentType]
            worker_id="worker_task_1",
            kind="task",
            deployment="demo-local",
            capabilities={},
            interval_seconds=0.05,
        )
        async with beacon:
            assert await beacon.announce() is False
            await asyncio.sleep(0.08)

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert "worker_presence_announce_failed" in messages
    assert "worker_presence_forget_failed" in messages


def test_the_started_at_is_the_process_start_not_the_beat() -> None:
    async def scenario() -> None:
        store = InMemoryWorkerPresenceStore()
        before = datetime.now(UTC)
        beacon = WorkerPresenceBeacon(
            store,
            worker_id="worker_task_1",
            kind="task",
            deployment="demo-local",
            capabilities={},
            interval_seconds=0.05,
        )
        async with beacon:
            await asyncio.sleep(0.12)
            [row] = (await store.report()).workers
            assert before <= row.started_at <= row.heartbeat_at

    asyncio.run(scenario())
