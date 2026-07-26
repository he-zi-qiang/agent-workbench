"""Running one contract scenario against one implementation of a port.

A port with two implementations and two test suites has two contracts. The
suites are parameterized over implementations instead, so the in-memory store
and the real one answer the same questions and any difference between them
shows up as a failure rather than as a surprise in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StoreHarness:
    """Runs one scenario against one implementation of a port."""

    name: str
    factory: Callable[[], Any]

    def run(self, scenario: Callable[[Any], Awaitable[Any]]) -> Any:
        async def execute() -> Any:
            async with self.factory() as store:
                return await scenario(store)

        return asyncio.run(execute())


__all__ = ["StoreHarness"]
