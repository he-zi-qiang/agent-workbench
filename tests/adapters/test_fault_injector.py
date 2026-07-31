"""Deterministic, framework-neutral fault controller contracts."""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.adapters.testing import (
    FailpointController,
    InjectedCrash,
    InjectedFaultError,
)

POINT = "after_claim_commit_before_advisory_lock"


def test_pause_waits_for_explicit_release_after_observable_arrival() -> None:
    async def scenario() -> None:
        controller = FailpointController(frozenset({POINT}))
        controller.arm(POINT)
        blocked = asyncio.create_task(controller.hit(POINT))

        await controller.wait_until_hit(POINT)
        assert not blocked.done()
        controller.release(POINT)
        await blocked
        assert controller.hits == [POINT]

    asyncio.run(scenario())


def test_raise_and_crash_are_distinct_controlled_outcomes() -> None:
    async def scenario() -> None:
        controller = FailpointController(frozenset({POINT}))
        controller.arm(POINT, mode="raise")
        with pytest.raises(InjectedFaultError, match=POINT):
            await controller.hit(POINT)
        await controller.wait_until_hit(POINT)

        controller.arm(POINT, mode="crash")
        with pytest.raises(InjectedCrash, match=POINT):
            await controller.hit(POINT)
        await controller.wait_until_hit(POINT)

    asyncio.run(scenario())


def test_unallowed_or_unarmed_points_fail_closed() -> None:
    controller = FailpointController(frozenset({POINT}))
    with pytest.raises(ValueError, match="not allowed"):
        controller.arm("inside_checkpoint_put")
    with pytest.raises(RuntimeError, match="not armed"):
        controller.release(POINT)
