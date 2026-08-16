"""Noticing that the caller left, for any route that runs work while it waits.

A synchronous route that drives an agent loop has a problem no ordinary
handler has: the work outlives the client's patience, and ASGI does not tell a
handler that its reader is gone. It has to be asked. So a watcher polls
``request.is_disconnected()`` alongside the work, and when the answer is yes it
stops the work two ways.

Two, because they stop different things. ``CancellationSource.cancel`` is the
cooperative signal the runtime reads at its own checkpoints; it is what makes a
run end *tidily*, recording a cancelled outcome. ``Task.cancel`` is what
interrupts an ``await`` that has no checkpoint to reach -- a provider call in
flight, a wait on a human. Sending only the first leaves a run parked; sending
only the second loses the tidy ending.

This lives here rather than in a route module because a second conversational
surface needs exactly the same behaviour, and the way that usually goes wrong
is that the second one gets a copy which then drifts -- the copy keeps polling
after the first has learned to also cancel its task, and nobody notices because
each file reads correctly on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request

from agent_workbench.ports.cancellation import CancellationSource


async def watch_disconnect(
    request: Request,
    cancellation: CancellationSource,
    *,
    target: asyncio.Task[Any],
    poll_seconds: float,
) -> None:
    """Cancel actual work as well as setting the cooperative runtime signal."""

    while not cancellation.cancelled:
        if await request.is_disconnected():
            cancellation.cancel("client_disconnected")
            if not target.done():
                target.cancel()
            return
        await asyncio.sleep(poll_seconds)


@asynccontextmanager
async def watched(
    request: Request,
    cancellation: CancellationSource,
    *,
    target: asyncio.Task[Any],
    poll_seconds: float,
    name: str,
) -> AsyncGenerator[None]:
    """Run ``target`` with a disconnect watcher, and always retire the watcher.

    The cleanup is the part worth sharing. A watcher whose work has finished is
    parked on a sleep that nothing will interrupt, and one leaked watcher per
    request is a leak with a shape: it holds the request object, so it holds
    whatever the request holds.
    """

    watcher = asyncio.create_task(
        watch_disconnect(
            request,
            cancellation,
            target=target,
            poll_seconds=poll_seconds,
        ),
        name=name,
    )
    try:
        yield
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


__all__ = ["watch_disconnect", "watched"]
