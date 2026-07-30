"""Deterministic failpoints for concurrency and recovery tests.

Each arm owns an ``AsyncBarrier``. Tests first await ``arrived`` and only then
expire a lease, terminate a guard session, start a competing Worker, or release
the paused operation. This replaces timing guesses with a declared interleave.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from agent_workbench.ports.fault_injector import FailpointName

FailpointMode = Literal["pause", "raise", "crash"]


class InjectedFaultError(RuntimeError):
    """A controlled ordinary failure from an allowlisted test failpoint."""


class InjectedCrash(BaseException):
    """A controlled abrupt-stop signal which bypasses ordinary error handling."""


class AsyncBarrier:
    """One-shot async barrier with observable arrival and explicit release."""

    def __init__(self) -> None:
        self.arrived = asyncio.Event()
        self._released = asyncio.Event()

    async def wait(self) -> None:
        self.arrived.set()
        await self._released.wait()

    async def wait_until_arrived(self, *, timeout_seconds: float = 1.0) -> None:
        """Bounded only to fail a broken test; never used to create an ordering."""

        await asyncio.wait_for(self.arrived.wait(), timeout=timeout_seconds)

    def release(self) -> None:
        self._released.set()


@dataclass(slots=True)
class _Arm:
    mode: FailpointMode
    barrier: AsyncBarrier = field(default_factory=AsyncBarrier)


class NoopFaultInjector:
    """The safe production default: every declared point is inert."""

    async def hit(self, name: FailpointName) -> None:
        del name


class FailpointController:
    """Allowlisted controller injected directly by deterministic tests.

    Calling ``arm`` replaces a previous arm for that point. A ``pause`` waits
    until the test calls :meth:`release`; ``raise`` and ``crash`` expose their
    arrival immediately and then raise different controlled signals.
    """

    def __init__(self, allowed: frozenset[FailpointName]) -> None:
        self._allowed = allowed
        self._arms: dict[FailpointName, _Arm] = {}
        self.hits: list[FailpointName] = []

    def arm(self, name: FailpointName, *, mode: FailpointMode = "pause") -> None:
        self._require_allowed(name)
        self._arms[name] = _Arm(mode=mode)

    async def wait_until_hit(
        self, name: FailpointName, *, timeout_seconds: float = 1.0
    ) -> None:
        self._require_allowed(name)
        arm = self._arms.get(name)
        if arm is None:
            raise RuntimeError(f"failpoint {name} is not armed")
        await arm.barrier.wait_until_arrived(timeout_seconds=timeout_seconds)

    def release(self, name: FailpointName) -> None:
        self._require_allowed(name)
        arm = self._arms.get(name)
        if arm is None:
            raise RuntimeError(f"failpoint {name} is not armed")
        arm.barrier.release()

    async def hit(self, name: FailpointName) -> None:
        self._require_allowed(name)
        self.hits.append(name)
        arm = self._arms.get(name)
        if arm is None:
            return
        arm.barrier.arrived.set()
        if arm.mode == "pause":
            await arm.barrier.wait()
            return
        if arm.mode == "raise":
            raise InjectedFaultError(f"injected fault at {name}")
        raise InjectedCrash(f"injected crash at {name}")

    def _require_allowed(self, name: FailpointName) -> None:
        if name not in self._allowed:
            raise ValueError(f"failpoint {name} is not allowed")


__all__ = [
    "AsyncBarrier",
    "FailpointController",
    "FailpointMode",
    "InjectedCrash",
    "InjectedFaultError",
    "NoopFaultInjector",
]
