"""Draining the outbox into the index.

Without this the upload path stops halfway: a completed upload writes a
document, a version and an outbox event, and nothing ever indexes any of it.
Everything that appeared to work called IngestionService directly.

The ordering rule is the whole design. A stable point id stops a redelivered
event from writing a second copy; it does nothing about two events arriving out
of order, because the later write simply wins. So an event is not applied from
its own payload. The worker acquires a document-scoped, session-pinned guard,
then locks and re-reads what PostgreSQL says *now* before indexing that -- an
event is a wake-up, not a source of truth. Holding the guard across the slow
model and Qdrant calls is what prevents an older snapshot from landing after a
newer Worker; the row lock alone ends with the short snapshot transaction.

An event older than what the index already has is marked superseded rather than
applied. A crash between indexing and acknowledging replays the same event, and
replaying re-reads the same current snapshot, so the second run converges on
what the first one wrote instead of undoing it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import (
    document_acl,
    document_versions,
    documents,
)
from agent_workbench.application.ingestion import IngestionRequest, IngestionService
from agent_workbench.domain.errors import StaleExecutionError
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.execution_guard import (
    ExecutionGuard,
    GuardFactory,
    GuardUnavailableError,
)
from agent_workbench.ports.outbox import OutboxEvent, OutboxPort


@dataclass(frozen=True, slots=True)
class DrainResult:
    """What one pass over the queue did."""

    indexed: int = 0
    superseded: int = 0
    skipped: int = 0
    deferred: int = 0


@dataclass(frozen=True, slots=True)
class IngestionWorker:
    """One pass: claim, re-read, index, record, acknowledge."""

    engine: AsyncEngine
    outbox: OutboxPort
    ingestion: IngestionService
    artifacts: ArtifactStore
    worker_id: str
    guards: GuardFactory | None = None
    lease_seconds: float = 90.0
    heartbeat_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if self.heartbeat_seconds >= self.lease_seconds:
            raise ValueError("heartbeat_seconds must be shorter than the lease")

    async def drain(self, *, limit: int = 32) -> DrainResult:
        """Handle every event currently claimable, and report what happened."""

        claimed = await self.outbox.claim(
            worker_id=self.worker_id,
            limit=limit,
            lease_seconds=self.lease_seconds,
        )
        indexed = superseded = skipped = deferred = 0

        for event in claimed:
            guard = None
            if self.guards is not None:
                try:
                    # Namespaced away from Task ids. The event revision is
                    # diagnostic metadata; exclusivity belongs to the stable
                    # document key held by the pinned PostgreSQL session.
                    guard = await self.guards.acquire(
                        task_id=f"document:{event.document_id}",
                        worker_id=self.worker_id,
                        epoch=event.source_revision,
                    )
                except GuardUnavailableError:
                    await self.outbox.release(
                        event_id=event.event_id,
                        claim_token=event.claim_token,
                    )
                    deferred += 1
                    continue
            try:
                outcome = await self._apply_with_heartbeat(event, guard)
            finally:
                if guard is not None:
                    await guard.release()
            if outcome == "indexed":
                indexed += 1
            elif outcome == "superseded":
                superseded += 1
            else:
                skipped += 1
            # Acknowledged in every case, including superseded: an event that
            # describes a past state will still describe it next time, so
            # leaving it queued would make the worker rediscover it forever.
            await self.outbox.ack(
                event_id=event.event_id, claim_token=event.claim_token
            )

        return DrainResult(
            indexed=indexed,
            superseded=superseded,
            skipped=skipped,
            deferred=deferred,
        )

    async def _apply_with_heartbeat(
        self,
        event: OutboxEvent,
        guard: ExecutionGuard | None,
    ) -> str:
        """Keep the outbox token live while parsing, embedding and writing."""

        if guard is not None and not await guard.healthcheck():
            raise StaleExecutionError("the document writer guard is no longer held")
        apply = asyncio.create_task(
            self._apply(event),
            name=f"ingestion-apply:{event.event_id}",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat(event),
            name=f"ingestion-heartbeat:{event.event_id}",
        )
        guard_lost = (
            asyncio.create_task(
                guard.lost.wait(),
                name=f"ingestion-guard:{event.event_id}",
            )
            if guard is not None
            else None
        )
        try:
            waiting: set[asyncio.Task[object]] = {apply, heartbeat}
            if guard_lost is not None:
                waiting.add(guard_lost)
            done, _ = await asyncio.wait(
                waiting,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if guard_lost is not None and guard_lost in done:
                raise StaleExecutionError("the document writer guard was lost")
            if heartbeat in done:
                # A healthy heartbeat loop never returns. Losing the fenced
                # claim cancels the external write before this Worker can ack.
                heartbeat.result()
                raise AssertionError("ingestion heartbeat stopped without an error")
            outcome = await apply
            if guard is not None and not await guard.healthcheck():
                raise StaleExecutionError("the document writer guard was lost")
            return outcome
        finally:
            if not apply.done():
                apply.cancel()
            heartbeat.cancel()
            cleanup: list[asyncio.Task[object]] = [apply, heartbeat]
            if guard_lost is not None:
                guard_lost.cancel()
                cleanup.append(guard_lost)
            await asyncio.gather(*cleanup, return_exceptions=True)

    async def _heartbeat(self, event: OutboxEvent) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await self.outbox.heartbeat(
                event_id=event.event_id,
                claim_token=event.claim_token,
                lease_seconds=self.lease_seconds,
            )

    async def _apply(self, event: OutboxEvent) -> str:
        """Index the document's current state, if this event still describes it."""

        async with self.engine.begin() as connection:
            # Held for the whole snapshot decision. The advisory guard above
            # spans the later model/index work; this row lock only makes these
            # related PostgreSQL reads describe one committed document state.
            row = (
                await connection.execute(
                    select(
                        documents.c.tenant_id,
                        documents.c.knowledge_base_id,
                        documents.c.owner_id,
                        documents.c.source_revision,
                        documents.c.last_applied_revision,
                        documents.c.deleted,
                    )
                    .where(documents.c.document_id == event.document_id)
                    .with_for_update()
                )
            ).first()
            if row is None:
                # The document is gone. Nothing to index, and nothing to
                # retry -- the event outlived its subject on purpose.
                return "skipped"
            if row.deleted:
                return "skipped"
            if int(row.source_revision) <= int(row.last_applied_revision):
                return "superseded"

            version = (
                await connection.execute(
                    select(
                        document_versions.c.version_id,
                        document_versions.c.artifact_id,
                        document_versions.c.source_revision,
                    )
                    .where(document_versions.c.document_id == event.document_id)
                    .order_by(document_versions.c.source_revision.desc())
                    .limit(1)
                )
            ).first()
            if version is None:
                return "skipped"

            granted = [
                str(acl.principal_id)
                for acl in (
                    await connection.execute(
                        select(document_acl.c.principal_id).where(
                            document_acl.c.document_id == event.document_id
                        )
                    )
                ).all()
            ]
            snapshot = (
                str(row.tenant_id),
                str(row.knowledge_base_id),
                str(row.owner_id),
                int(row.source_revision),
                str(version.version_id),
                str(version.artifact_id),
                tuple(sorted({str(row.owner_id), *granted})),
            )

        # Outside the transaction: fetching bytes and running a model are slow,
        # and a database transaction held across them is a lock somebody else
        # is waiting on.
        tenant, kb, owner, revision, version_id, artifact_id, principals = snapshot
        content = await self.artifacts.get(
            tenant_id=tenant, artifact_id=artifact_id, principal_id=owner
        )
        stored = await self.artifacts.head(
            tenant_id=tenant, artifact_id=artifact_id, principal_id=owner
        )
        await self.ingestion.ingest(
            IngestionRequest(
                tenant_id=tenant,
                knowledge_base_id=kb,
                document_id=event.document_id,
                document_version=version_id,
                owner_id=owner,
                authorized_principals=principals,
                source_revision=revision,
                media_type=stored.media_type,
                content=content,
            )
        )

        async with self.engine.begin() as connection:
            # Recorded only after the index has it. The other order would let a
            # crash mark work done that never happened, and nothing would
            # notice -- the document would simply never be searchable.
            await connection.execute(
                update(documents)
                .where(documents.c.document_id == event.document_id)
                .where(documents.c.last_applied_revision < revision)
                .values(last_applied_revision=revision)
            )
        return "indexed"


__all__ = ["DrainResult", "IngestionWorker"]
