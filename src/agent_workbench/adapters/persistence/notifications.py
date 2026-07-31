"""Telling a Worker that a Task became claimable, without telling it anything.

``NOTIFY`` is a wake-up and nothing else. It carries a task id so a listener
knows where to look, and it carries no status, no reason and no payload text --
a listener that acted on this message instead of querying would be trusting a
delivery PostgreSQL is allowed to coalesce, and would still be wrong the moment
a second transition landed between the send and the read.

Correctness therefore never depends on it arriving. Every transition it
accompanies is already a durable row, and the Worker's claim loop polls the same
rows on its own; this only shortens the gap between "a Task is claimable" and
"somebody claimed it". Losing a notification, receiving it twice, or having two
identical ones merged all leave the same outcome.

It is sent inside the caller's transaction on purpose. PostgreSQL delivers
notifications at commit, so a wake-up for a transition that rolled back is never
sent -- which is what makes "no listener ever hears about work that did not
happen" a property of the database rather than of remembering to guard the send.
"""

from __future__ import annotations

import json
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_workbench.domain.identifiers import Identifier

#: One shared channel for every Task that became claimable. Deliberately not one
#: per Task or per tenant: a channel per object is a listener that has to
#: subscribe before it can be told anything, which is the problem it was
#: supposed to solve.
TASK_READY_CHANNEL: Final[str] = "task_ready"


async def notify_task_ready(
    connection: AsyncConnection,
    *,
    task_id: Identifier,
) -> None:
    """Announce, at commit, that ``task_id`` is claimable.

    The payload is a JSON object with one key rather than a bare id, so a
    listener parses one shape now and keeps parsing it if a second locator is
    ever added. Its size is bounded by the identifier constraint -- 128
    characters -- which is two orders of magnitude below PostgreSQL's own 8000
    byte limit, so there is nothing here to truncate or check.
    """

    await connection.execute(
        select(func.pg_notify(TASK_READY_CHANNEL, json.dumps({"task_id": task_id})))
    )


__all__ = ["TASK_READY_CHANNEL", "notify_task_ready"]
