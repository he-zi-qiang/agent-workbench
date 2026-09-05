"""A Worker process saying it is here, and for how long that can be believed.

**Why this exists (ADR-0110).** The control plane could not see a Worker.
``task_runs`` carries a heartbeat only while a Task is claimed, so an idle Task
Worker and a dead one were the same fact from the API's side, and the ingestion
Worker never left a row anywhere. The console's 运行状态 page answered
「任务与文档 Worker——状态未知」 and said which command to go and run instead
(known-gaps E-09). For a demo that is the wrong moment to find out: the API
answers, the database is ready, the Task sits in ``queued`` forever.

**What it is not.** Not a registry the Registry consults, not a lease, not a
claim. Nothing in the run path reads this table: a Worker that never announced
itself claims and runs Tasks exactly as before, and a stale row stops nobody.
It is a readout -- written by the process on a timer, read by the console --
and the port is kept that narrow so it cannot grow into a second source of
liveness beside the lease.

**Freshness is judged by the reader, on the database clock.** The writer says
how long its word is good for (``expires_at``); the reader compares that with
the clock every other liveness question here already uses
(``coordination.lease_time_source``), so a Worker whose own clock is wrong is
judged by the same clock as its leases. A row past its expiry is kept and
labelled stale rather than deleted: "stopped answering at 10:42" is worth more
than an absent row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import DomainModel

#: The two kinds of process that run outside the API and claim durable work.
WorkerKind = Literal["task", "ingestion"]


class WorkerPresence(DomainModel):
    """One Worker's most recent word about itself."""

    worker_id: Identifier
    kind: WorkerKind
    #: Which profile assembled it (`demo-local`, `compose-local`, ...). A label
    #: for the console, never an address.
    deployment: str = Field(min_length=1, max_length=128)
    #: What the process reported it can do at assembly -- whether it is the
    #: ``--demo`` synthetic Worker, which graphs it builds, which tools it holds.
    #: Read back as data; the console draws it and decides nothing from it.
    capabilities: dict[str, object] = Field(default_factory=dict)
    started_at: AwareDatetime
    heartbeat_at: AwareDatetime
    expires_at: AwareDatetime


class WorkerPresenceReport(DomainModel):
    """Every row, plus the clock they were read against."""

    #: The store's own clock at read time. The console computes "fresh" and
    #: "how long ago" against this, never against the browser's clock.
    observed_at: AwareDatetime
    workers: tuple[WorkerPresence, ...] = ()

    def fresh(self, worker: WorkerPresence) -> bool:
        return worker.expires_at > self.observed_at


@runtime_checkable
class WorkerPresenceStore(Protocol):
    """Announce a process; list what has announced itself."""

    async def announce(
        self,
        *,
        worker_id: Identifier,
        kind: WorkerKind,
        deployment: str,
        capabilities: dict[str, object],
        started_at: datetime,
        ttl_seconds: float,
    ) -> WorkerPresence:
        """Upsert this Worker's row with a fresh heartbeat and expiry.

        ``started_at`` is the process's own start, carried on every beat so
        that the row survives a restart with the new start time rather than
        the first one ever written. The heartbeat and expiry are stamped on
        the store's clock.
        """
        ...

    async def forget(self, worker_id: Identifier) -> None:
        """Drop the row on an orderly exit, so a clean stop reads as absent."""
        ...

    async def report(self) -> WorkerPresenceReport:
        """Every announced Worker, stale ones included, against the store clock."""
        ...


__all__ = [
    "WorkerKind",
    "WorkerPresence",
    "WorkerPresenceReport",
    "WorkerPresenceStore",
]
