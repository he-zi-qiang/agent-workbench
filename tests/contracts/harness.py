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

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.ports.chat_release import ChatReleaseCoordinator
from agent_workbench.ports.conversation_store import ChatTurnStore
from agent_workbench.ports.event_log import EventLogPort, EventScope, EventSink


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


@dataclass(frozen=True, slots=True)
class ChatReleaseHarness:
    """One release coordinator with the Turn ledger and log it publishes into.

    A release contract cannot be stated against the coordinator alone: the
    candidate has to be prepared in a Turn ledger and the terminal answer read
    back out of an event log, and both must be the ones this coordinator
    actually writes through. Bundling the three is what lets the identical
    scenario run against the deterministic double and against PostgreSQL.
    """

    conversations: ChatTurnStore
    coordinator: ChatReleaseCoordinator
    events: EventLogPort

    def sink(self, scope: EventScope) -> EventSink:
        """The sink a single-process coordinator publishes its answer through.

        The PostgreSQL coordinator ignores it and appends inside the same
        transaction as the authorization fence -- emitting here would open a
        second transaction and reopen the race. It is still part of the port,
        so the contract supplies one either way.
        """

        return ScopedEventSink(log=self.events, scope=scope)


__all__ = ["ChatReleaseHarness", "StoreHarness"]
