from __future__ import annotations

import asyncio

from agent_workbench.apps.ingestion_worker.runner import IngestionWorkerRunner
from agent_workbench.workers.ingestion import DrainResult


def test_runner_drains_backlog_without_polling_between_non_empty_batches() -> None:
    async def scenario() -> int:
        calls = 0
        stop = asyncio.Event()

        async def drain() -> DrainResult:
            nonlocal calls
            calls += 1
            if calls == 3:
                stop.set()
            return DrainResult(indexed=1)

        runner = IngestionWorkerRunner(
            drain=drain,
            poll_seconds=60,
            error_backoff_seconds=60,
        )
        await asyncio.wait_for(runner.run_forever(stop), timeout=1)
        return calls

    assert asyncio.run(scenario()) == 3


def test_runner_survives_a_failed_drain_and_obeys_shutdown_during_backoff() -> None:
    async def scenario() -> int:
        calls = 0
        stop = asyncio.Event()

        async def drain() -> DrainResult:
            nonlocal calls
            calls += 1
            stop.set()
            raise RuntimeError("transient")

        runner = IngestionWorkerRunner(
            drain=drain,
            poll_seconds=60,
            error_backoff_seconds=60,
        )
        await asyncio.wait_for(runner.run_forever(stop), timeout=1)
        return calls

    assert asyncio.run(scenario()) == 1


def test_runner_rejects_non_positive_intervals() -> None:
    async def drain() -> DrainResult:
        return DrainResult()

    for poll, backoff in ((0, 1), (1, 0)):
        try:
            IngestionWorkerRunner(
                drain=drain,
                poll_seconds=poll,
                error_backoff_seconds=backoff,
            )
        except ValueError:
            continue
        raise AssertionError("non-positive runner interval was accepted")
