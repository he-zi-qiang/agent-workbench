"""Contract shared by the memory and PostgreSQL ``WorkerPresenceStore`` (ADR-0110).

Three facts, the same for both: an announcement is visible at once and reads
fresh; a second announcement replaces the first row rather than adding one; a
row whose expiry has passed is still listed and reads stale, because "stopped
answering at HH:MM" is the fact a person debugging a queued Task needs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from harness import StoreHarness

from agent_workbench.ports.worker_presence import WorkerPresenceStore

STARTED = datetime(2026, 9, 5, 1, 0, tzinfo=UTC)


def test_an_announced_worker_is_listed_and_fresh(worker_presence: StoreHarness) -> None:
    async def scenario(store: WorkerPresenceStore) -> None:
        row = await store.announce(
            worker_id="worker_task_1",
            kind="task",
            deployment="demo-local",
            capabilities={"demo": False, "tools": ["export_artifact"]},
            started_at=STARTED,
            ttl_seconds=60,
        )
        report = await store.report()
        assert [held.worker_id for held in report.workers] == ["worker_task_1"]
        [held] = report.workers
        assert held.kind == "task"
        assert held.deployment == "demo-local"
        assert held.capabilities == {"demo": False, "tools": ["export_artifact"]}
        assert held.started_at == STARTED
        assert held.expires_at > held.heartbeat_at
        assert report.fresh(held)
        assert row.worker_id == held.worker_id

    worker_presence.run(scenario)


def test_a_second_beat_replaces_the_row(worker_presence: StoreHarness) -> None:
    async def scenario(store: WorkerPresenceStore) -> None:
        first = await store.announce(
            worker_id="worker_task_1",
            kind="task",
            deployment="demo-local",
            capabilities={"demo": True},
            started_at=STARTED,
            ttl_seconds=60,
        )
        await asyncio.sleep(0.01)
        await store.announce(
            worker_id="worker_task_1",
            kind="task",
            deployment="demo-local",
            capabilities={"demo": False},
            started_at=STARTED,
            ttl_seconds=60,
        )
        report = await store.report()
        assert len(report.workers) == 1
        [held] = report.workers
        assert held.capabilities == {"demo": False}
        assert held.heartbeat_at >= first.heartbeat_at

    worker_presence.run(scenario)


def test_an_expired_row_is_kept_and_reads_stale(worker_presence: StoreHarness) -> None:
    async def scenario(store: WorkerPresenceStore) -> None:
        await store.announce(
            worker_id="worker_ingest_1",
            kind="ingestion",
            deployment="demo-local",
            capabilities={},
            started_at=STARTED,
            ttl_seconds=0.2,
        )
        await asyncio.sleep(0.35)
        report = await store.report()
        [held] = report.workers
        assert held.worker_id == "worker_ingest_1"
        assert not report.fresh(held)

    worker_presence.run(scenario)


def test_forget_removes_the_row(worker_presence: StoreHarness) -> None:
    async def scenario(store: WorkerPresenceStore) -> None:
        await store.announce(
            worker_id="worker_task_2",
            kind="task",
            deployment="demo-local",
            capabilities={},
            started_at=STARTED,
            ttl_seconds=60,
        )
        await store.forget("worker_task_2")
        await store.forget("worker_never_seen")
        report = await store.report()
        assert report.workers == ()

    worker_presence.run(scenario)
