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

:class:`TaskReadyListener` is the receiving half, and it is deliberately the
dumbest possible one: it sets an :class:`asyncio.Event` and discards the
payload. Everything above holds only while nothing reads that payload, so this
module does not offer it -- a listener cannot come to depend on a delivery that
may be coalesced if it is never handed anything to depend on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from agent_workbench.adapters.persistence.engine import ASYNCPG_PREFIX
from agent_workbench.domain.identifiers import Identifier

logger = logging.getLogger(__name__)

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


#: The other spelling ``database.listen_dsn`` is allowed to arrive in.
_PLAIN_PREFIX: Final[str] = "postgresql://"

#: How long a quiet session may go unverified. A backend that is terminated
#: says so through the driver's termination callback; this is for the failure
#: that says nothing -- a half-open TCP session, which stays "open" locally and
#: delivers nothing forever.
DEFAULT_LISTENER_HEALTHCHECK_SECONDS: Final[float] = 30.0

#: Spacing between reconnection attempts, flat rather than exponential. What is
#: being reconnected is an accelerator, not a data path: a Worker that backs off
#: for ten minutes after a one-second blip has punished itself with polling for
#: no reason, and the reconnect costs one connection attempt.
DEFAULT_RECONNECT_SECONDS: Final[float] = 5.0


class TaskReadyListener:
    """A dedicated session holding ``LISTEN task_ready``, and the flag it sets.

    Its own engine on ``database.listen_dsn``, never a connection borrowed from
    the query pool. Three reasons, in order of how badly they end: ``dsn`` may
    address PgBouncer in transaction pooling, where ``LISTEN`` is accepted and
    then never delivers anything; a checkout held for the life of the process
    is a pool slot that never comes back, which on the default pool of ten is a
    tenth of the Worker's query capacity and on a pool of one is a deadlock
    (measured -- the next query blocked until its timeout); and the
    architecture already fixed this shape, in ``listen_connection_scope =
    "process_pinned"`` and a third DSN that exists for nothing else.

    What it exposes is one :class:`asyncio.Event`, not a queue of task ids. A
    waiter learns "something may be claimable" and goes to the table, which is
    the only thing a coalescible delivery can honestly support. The payload is
    read by nobody here on purpose: an id in hand is an invitation to skip the
    query, and the query is what makes a lost, duplicated or merged
    notification indistinguishable from a delivered one.

    A dropped connection is therefore not an incident. Nothing is missed and
    nothing is caught up on afterwards -- the rows were always the source, and
    the Worker's poll is always running underneath. It reconnects because an
    accelerator that stays off after one blip is an accelerator nobody can rely
    on being there, and it logs each transition because "the Worker got slower
    three days ago" is otherwise invisible.
    """

    __slots__ = (
        "_closing",
        "_connection",
        "_driver",
        "_engine",
        "_healthcheck_seconds",
        "_lost",
        "_reconnect_seconds",
        "_sessions",
        "_supervisor",
        "_woken",
    )

    def __init__(
        self,
        listen_dsn: str,
        *,
        application_name: str = "agent-workbench-listener",
        healthcheck_seconds: float = DEFAULT_LISTENER_HEALTHCHECK_SECONDS,
        reconnect_seconds: float = DEFAULT_RECONNECT_SECONDS,
    ) -> None:
        if healthcheck_seconds <= 0:
            raise ValueError("healthcheck_seconds must be positive")
        if reconnect_seconds <= 0:
            raise ValueError("reconnect_seconds must be positive")
        self._healthcheck_seconds = healthcheck_seconds
        self._reconnect_seconds = reconnect_seconds
        # NullPool for the same reason the execution guard uses one: this
        # connection is pinned to one physical backend and must not be recycled
        # under it. AUTOCOMMIT so the healthcheck round trip cannot leave a
        # transaction open for the life of the process.
        self._engine: AsyncEngine = create_async_engine(
            _asyncpg_dsn(listen_dsn),
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=False,
            connect_args={
                "server_settings": {"application_name": application_name},
            },
        )
        self._connection: AsyncConnection | None = None
        # The raw asyncpg connection. Untyped -- asyncpg ships no stubs -- and
        # reached through SQLAlchemy's adapter rather than by importing the
        # driver, which is what keeps the rest of this module type-checked.
        self._driver: Any = None
        self._woken = asyncio.Event()
        self._lost = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._sessions = 0
        self._closing = False

    @property
    def woken(self) -> asyncio.Event:
        """Set when a Task may have become claimable. Never cleared here.

        Clearing belongs to whoever waits on it, and it has to happen *before*
        that waiter's next look at the table -- see ``TaskWorkerRunner._lane``.
        A listener that cleared the flag itself would be deciding, on behalf of
        code it cannot see, that the wake-up had already been acted on.
        """

        return self._woken

    @property
    def connected(self) -> bool:
        """Whether a live session is currently listening.

        False means the Worker is running on its poll interval alone. It is not
        an error state and nothing recovers from it except this class.

        It is also not a substitute for :attr:`sessions` when what is being
        waited for is a *reconnection*: for the first moments after a backend
        goes away this still reports ``True``, because nothing local has
        noticed yet.
        """

        return self._driver is not None and not bool(self._driver.is_closed())

    @property
    def sessions(self) -> int:
        """How many sessions this listener has put into ``LISTEN``.

        ``1`` after :meth:`start`, and one more for every reconnection. The only
        externally visible difference between a listener that has held one
        connection all week and one that has been quietly reconnecting every
        thirty seconds, which is a thing worth being able to see from outside --
        and the only way anything can wait for *the next* session rather than
        for a wall-clock guess at when the last one died.
        """

        return self._sessions

    async def start(self) -> None:
        """Subscribe now, and keep the subscription up from here on.

        The first attempt's failure is raised, not swallowed: whether an
        unavailable accelerator is worth refusing to start over is the caller's
        decision, and for the Task Worker the answer is no -- refusing would
        have made a notification that is explicitly allowed to be lost into a
        thing the Worker cannot run without.
        """

        if self._closing:
            raise RuntimeError("this listener has been closed")
        if self._supervisor is not None:
            raise RuntimeError("this listener is already started")
        await self._attach()
        self._supervisor = asyncio.create_task(
            self._supervise(),
            name="task-ready-listener",
        )

    async def aclose(self) -> None:
        """Stop supervising, close the session, and dispose the engine."""

        if self._closing:
            return
        self._closing = True
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor
        await self._detach()
        await self._engine.dispose()

    async def _attach(self) -> None:
        """Open one session and put it into ``LISTEN``."""

        connection = await self._engine.connect()
        try:
            driver: Any = (await connection.get_raw_connection()).driver_connection
            # The driver issues the ``LISTEN`` and dispatches per channel, so
            # this call is the entire subscription.
            await driver.add_listener(TASK_READY_CHANNEL, self._on_notify)
            # After the subscription, not before: a session that died during it
            # is already reported by the failure above, and a termination
            # callback on a connection that never listened would start a
            # reconnect loop for a subscription that was never there.
            driver.add_termination_listener(self._on_terminated)
        except BaseException:
            await _close_quietly(connection)
            raise
        self._connection = connection
        self._driver = driver
        # Last, after the subscription is established rather than while it is
        # being set up: anything watching this counter is waiting to be able to
        # send something the new session will hear.
        self._sessions += 1

    async def _detach(self) -> None:
        """Drop the current session, whatever state it is in."""

        connection, self._connection = self._connection, None
        self._driver = None
        if connection is not None:
            await _close_quietly(connection)

    def _on_notify(
        self,
        connection: object,
        pid: object,
        channel: object,
        payload: object,
    ) -> None:
        """Raise the flag. The four arguments are the driver's, and unused.

        ``payload`` in particular: it carries the task id, and reading it here
        is how a wake-up quietly turns into a message. See the module docstring.
        """

        self._woken.set()

    def _on_terminated(self, connection: object) -> None:
        """The driver noticed *a* session is gone. Wake the supervisor for ours.

        The identity check is the whole point. asyncpg fires this callback for a
        graceful ``close()`` exactly as it does for a backend that died --
        measured -- and it fires it a tick late, through ``loop.call_soon``. So
        the session :meth:`_detach` closes on the way to a reconnection reports
        its own loss *after* the replacement is already listening, and a
        supervisor that believed it would tear down a healthy connection, then
        the next one, forever: a reconnect every ``reconnect_seconds`` for the
        life of the process, each one dropping whatever arrived in between.
        Measured, from one injected healthcheck failure: 67 sessions in six
        seconds, and the healthcheck never ran again because the loop never
        reached its timeout.

        Moving :attr:`_lost`'s ``clear`` after the ``_detach`` does not fix
        this, because the callback is not synchronous with ``close()`` -- it
        lands a tick later, past any ordering this method's caller can arrange.
        Identity is what the ordering was reaching for anyway: what matters is
        not *when* the report arrived but *which* session it is about, and a
        report about a session that has already been replaced is news about
        nothing. ``connection`` is the same object ``_attach`` registered on
        (asyncpg hands the callback its own ``Connection``), so the comparison
        needs nothing kept on the side.
        """

        if connection is not self._driver:
            return
        self._lost.set()

    async def _supervise(self) -> None:
        """Keep the session listening, or leave the Worker on its poll alone."""

        while not self._closing:
            try:
                await asyncio.wait_for(
                    self._lost.wait(),
                    timeout=self._healthcheck_seconds,
                )
            except TimeoutError:
                # Nothing reported a loss. Verify rather than believe it.
                if await self._still_listening():
                    continue
            if self._closing:
                return
            logger.warning(
                "task_ready_listener_disconnected",
                extra={"listen_channel": TASK_READY_CHANNEL},
            )
            await self._reconnect()

    async def _reconnect(self) -> None:
        """Re-subscribe, retrying until it works or the process shuts down."""

        while not self._closing:
            # Before the attempt, including the first one. It is a floor between
            # two consecutive sessions, not a penalty for a failure: an endpoint
            # that accepts a connection and immediately drops it would otherwise
            # be answered with a connect loop at whatever rate the network
            # allows, and a warning line per turn. The Worker is polling
            # throughout, so the wait costs nothing anybody is waiting on.
            await asyncio.sleep(self._reconnect_seconds)
            # Cleared before the attempt rather than after it. A session that
            # dies between attaching and clearing would have its loss erased,
            # and the supervisor would then sit on a dead connection until the
            # next healthcheck -- or forever, since a termination callback for
            # that session has already fired and will not fire twice.
            #
            # What makes that safe is `_on_terminated`'s identity check, not
            # this ordering: the session `_detach` is about to close reports
            # itself lost a tick from now, after the replacement exists, and no
            # position for this line can be early or late enough to matter.
            self._lost.clear()
            await self._detach()
            try:
                await self._attach()
            except Exception as error:
                # Broad on purpose. Everything from the raw driver arrives as
                # an asyncpg exception this module deliberately does not import,
                # and every failure here has the same answer: the Worker is
                # polling, so try again in a moment.
                logger.warning(
                    "task_ready_listener_reconnect_failed",
                    extra={"listen_error_type": type(error).__name__},
                )
                continue
            logger.info(
                "task_ready_listener_reconnected",
                extra={"listen_channel": TASK_READY_CHANNEL},
            )
            return

    async def _still_listening(self) -> bool:
        """One round trip, because ``is_closed()`` cannot see a half-open pipe."""

        driver = self._driver
        if driver is None or bool(driver.is_closed()):
            return False
        try:
            async with asyncio.timeout(self._healthcheck_seconds):
                await driver.fetchval("SELECT 1")
        except Exception:
            return False
        return True


async def _close_quietly(connection: AsyncConnection) -> None:
    """Close a session that may already be gone, without a second failure."""

    with suppress(Exception):
        await connection.close()


def _asyncpg_dsn(dsn: str) -> str:
    """The listen DSN spelled the way SQLAlchemy needs to see it.

    ``database.listen_dsn`` validates both PostgreSQL URL forms and only one of
    them names a driver, so the bare form is completed rather than refused --
    refusing it would reject a configuration Settings already accepted. Anything
    else is refused, because a DSN naming some other driver would put a blocking
    client inside the event loop.
    """

    if dsn.startswith(ASYNCPG_PREFIX):
        return dsn
    if dsn.startswith(_PLAIN_PREFIX):
        return ASYNCPG_PREFIX + dsn[len(_PLAIN_PREFIX) :]
    raise ValueError(f"the task_ready listener requires a {ASYNCPG_PREFIX} DSN")


__all__ = [
    "DEFAULT_LISTENER_HEALTHCHECK_SECONDS",
    "DEFAULT_RECONNECT_SECONDS",
    "TASK_READY_CHANNEL",
    "TaskReadyListener",
    "notify_task_ready",
]
