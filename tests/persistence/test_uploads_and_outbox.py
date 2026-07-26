"""Uploads, document facts and the outbox that must commit with them.

These run against real PostgreSQL only. The invariants here are about
transactions, row locks and ``SKIP LOCKED``; an in-memory double would be
asserting that the double behaves, not that the database does.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.persistence import (
    PostgresDocumentStore,
    PostgresOutbox,
    create_query_engine,
)
from agent_workbench.adapters.persistence.models import documents, outbox_events
from agent_workbench.application.uploads import UploadService, UploadVerificationError
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_id
from agent_workbench.ports.documents import KnowledgeBaseMismatchError

TEST_DSN_ENV_VAR = "AGENT_WORKBENCH_TEST_DSN"

TENANT = "tenant_a"
OTHER_TENANT = "tenant_b"
OWNER = "user_1"
KNOWLEDGE_BASE = "kb_main"
CONTENT = b"Qdrant performs one dense and sparse fusion per query.\n"
DIGEST = hashlib.sha256(CONTENT).hexdigest()

TABLES = (
    "artifacts, upload_intents, document_acl, "
    "document_versions, documents, outbox_events"
)


def _dsn() -> str:
    dsn = os.environ.get(TEST_DSN_ENV_VAR)
    if not dsn:
        pytest.skip(f"{TEST_DSN_ENV_VAR} is not set")
    return dsn


@dataclass(frozen=True, slots=True)
class Harness:
    """Everything one upload scenario needs, freshly truncated."""

    service: UploadService
    documents: PostgresDocumentStore
    outbox: PostgresOutbox
    artifacts: LocalArtifactStore
    engine: Any


def _run(scenario: Callable[[Harness], Awaitable[Any]], root: Path) -> Any:
    dsn = _dsn()

    async def execute() -> Any:
        engine = create_query_engine(dsn, application_name="agent-workbench-tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"TRUNCATE {TABLES} CASCADE"))
            store = PostgresDocumentStore(engine)
            artifacts = LocalArtifactStore(root)
            harness = Harness(
                service=UploadService(documents=store, artifacts=artifacts),
                documents=store,
                outbox=PostgresOutbox(engine),
                artifacts=artifacts,
                engine=engine,
            )
            return await scenario(harness)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


async def _upload(
    harness: Harness,
    *,
    content: bytes = CONTENT,
    document_id: str = "doc_1",
    tenant_id: str = TENANT,
    granted: tuple[str, ...] = (),
    owner: str = OWNER,
    knowledge_base_id: str = KNOWLEDGE_BASE,
) -> Any:
    """Declare, transfer and complete one upload."""

    intent = await harness.service.create_upload(
        tenant_id=tenant_id,
        owner_id=owner,
        declared_size_bytes=len(content),
        declared_sha256=hashlib.sha256(content).hexdigest(),
        media_type="text/plain",
        filename="passage.txt",
    )
    stored = await harness.artifacts.put(
        tenant_id=tenant_id,
        kind="source_document",
        media_type="text/plain",
        content=content,
        filename="passage.txt",
    )
    return await harness.service.complete_upload(
        upload_id=intent.upload_id,
        tenant_id=tenant_id,
        principal_id=owner,
        artifact_id=stored.artifact_id,
        document_id=document_id,
        knowledge_base_id=knowledge_base_id,
        granted_principals=granted,
    )


def test_a_completed_upload_becomes_a_document_version(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> tuple[int, str]:
        version = await _upload(harness)
        return version.source_revision, version.content_sha256

    assert _run(scenario, tmp_path) == (1, DIGEST)


def test_the_version_and_its_outbox_event_commit_together(tmp_path: Path) -> None:
    """A document nothing will ever index is a document that silently vanishes."""

    async def scenario(harness: Harness) -> tuple[int, int, int]:
        version = await _upload(harness)
        events = await harness.outbox.claim(worker_id="worker_1")
        return (
            len(events),
            events[0].source_revision,
            version.source_revision,
        )

    count, event_revision, version_revision = _run(scenario, tmp_path)

    assert count == 1
    assert event_revision == version_revision


def test_a_failed_outbox_write_takes_the_document_with_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transaction is the guarantee; this is what happens without it.

    The outbox insert is replaced with one the CHECK constraint refuses, so it
    fails *after* the document and version rows have already been written in
    the same transaction. Neither may survive.
    """

    async def refused_outbox(
        self: PostgresDocumentStore,
        connection: Any,
        *,
        document_id: str,
        revision: int,
        kind: str,
        payload: dict[str, object],
    ) -> None:
        await connection.execute(
            insert(outbox_events).values(
                event_id=new_id("obx"),
                document_id=document_id,
                source_revision=revision,
                kind="not_a_valid_kind",
                payload=payload,
            )
        )

    monkeypatch.setattr(PostgresDocumentStore, "_record_outbox", refused_outbox)

    async def scenario(harness: Harness) -> tuple[int, int]:
        with pytest.raises(IntegrityError):
            await _upload(harness, document_id="doc_rollback")

        async with harness.engine.connect() as connection:
            document_rows = (
                await connection.execute(
                    select(documents.c.document_id).where(
                        documents.c.document_id == "doc_rollback"
                    )
                )
            ).all()
            event_rows = (
                await connection.execute(
                    select(outbox_events.c.event_id).where(
                        outbox_events.c.document_id == "doc_rollback"
                    )
                )
            ).all()
        return len(document_rows), len(event_rows)

    assert _run(scenario, tmp_path) == (0, 0)


def test_completing_the_same_upload_twice_returns_one_version(
    tmp_path: Path,
) -> None:
    async def scenario(harness: Harness) -> tuple[str, str, int]:
        intent = await harness.service.create_upload(
            tenant_id=TENANT,
            owner_id=OWNER,
            declared_size_bytes=len(CONTENT),
            declared_sha256=DIGEST,
            media_type="text/plain",
        )
        stored = await harness.artifacts.put(
            tenant_id=TENANT,
            kind="source_document",
            media_type="text/plain",
            content=CONTENT,
        )
        first = await harness.service.complete_upload(
            upload_id=intent.upload_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            artifact_id=stored.artifact_id,
            document_id="doc_1",
            knowledge_base_id=KNOWLEDGE_BASE,
        )
        second = await harness.service.complete_upload(
            upload_id=intent.upload_id,
            tenant_id=TENANT,
            principal_id=OWNER,
            artifact_id=stored.artifact_id,
            document_id="doc_1",
            knowledge_base_id=KNOWLEDGE_BASE,
        )
        return first.version_id, second.version_id, await harness.outbox.pending_count()

    first_id, second_id, pending = _run(scenario, tmp_path)

    assert first_id == second_id
    assert pending == 1


def test_re_uploading_identical_content_does_not_advance_the_revision(
    tmp_path: Path,
) -> None:
    """The index should not redo work that produces exactly the same rows."""

    async def scenario(harness: Harness) -> tuple[int, int, int]:
        first = await _upload(harness)
        second = await _upload(harness)
        return (
            first.source_revision,
            second.source_revision,
            await harness.outbox.pending_count(),
        )

    first_revision, second_revision, pending = _run(scenario, tmp_path)

    assert (first_revision, second_revision) == (1, 1)
    assert pending == 1


def test_new_content_takes_the_next_revision(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> tuple[list[int], list[int]]:
        await _upload(harness)
        await _upload(harness, content=b"Revised passage.\n")
        versions = await harness.documents.versions(
            document_id="doc_1",
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        events = await harness.outbox.claim(worker_id="worker_1")
        return (
            [version.source_revision for version in versions],
            [event.source_revision for event in events],
        )

    revisions, event_revisions = _run(scenario, tmp_path)

    assert revisions == [1, 2]
    assert event_revisions == [1, 2]


def test_concurrent_uploads_take_distinct_monotonic_revisions(
    tmp_path: Path,
) -> None:
    """The document row lock is what makes revisions monotonic, not distinct."""

    async def scenario(harness: Harness) -> list[int]:
        await asyncio.gather(
            *(
                _upload(harness, content=f"revision {index}\n".encode())
                for index in range(4)
            )
        )
        versions = await harness.documents.versions(
            document_id="doc_1",
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return [version.source_revision for version in versions]

    assert _run(scenario, tmp_path) == [1, 2, 3, 4]


def test_a_transfer_that_delivered_other_bytes_is_refused(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> int:
        intent = await harness.service.create_upload(
            tenant_id=TENANT,
            owner_id=OWNER,
            declared_size_bytes=len(CONTENT),
            declared_sha256=DIGEST,
            media_type="text/plain",
        )
        other = await harness.artifacts.put(
            tenant_id=TENANT,
            kind="source_document",
            media_type="text/plain",
            # Same length, different bytes: the digest check is what has to
            # catch this, not the size check.
            content=bytes(len(CONTENT)),
        )
        with pytest.raises(UploadVerificationError, match="digest"):
            await harness.service.complete_upload(
                upload_id=intent.upload_id,
                tenant_id=TENANT,
                principal_id=OWNER,
                artifact_id=other.artifact_id,
                document_id="doc_1",
                knowledge_base_id=KNOWLEDGE_BASE,
            )
        return await harness.outbox.pending_count()

    assert _run(scenario, tmp_path) == 0


def test_another_tenant_cannot_complete_the_upload(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> None:
        intent = await harness.service.create_upload(
            tenant_id=TENANT,
            owner_id=OWNER,
            declared_size_bytes=len(CONTENT),
            declared_sha256=DIGEST,
            media_type="text/plain",
        )
        stored = await harness.artifacts.put(
            tenant_id=TENANT,
            kind="source_document",
            media_type="text/plain",
            content=CONTENT,
        )
        await harness.service.complete_upload(
            upload_id=intent.upload_id,
            tenant_id=OTHER_TENANT,
            principal_id=OWNER,
            artifact_id=stored.artifact_id,
            document_id="doc_1",
            knowledge_base_id=KNOWLEDGE_BASE,
        )

    with pytest.raises(NotFoundError):
        _run(scenario, tmp_path)


def test_another_tenant_cannot_read_the_document(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> None:
        await _upload(harness)
        await harness.documents.document(
            document_id="doc_1", tenant_id=OTHER_TENANT, principal_id=OWNER
        )

    with pytest.raises(NotFoundError):
        _run(scenario, tmp_path)


def test_the_outbox_payload_carries_who_may_see_the_document(
    tmp_path: Path,
) -> None:
    """The index filters on this, so it has to travel with the event."""

    async def scenario(harness: Harness) -> tuple[Any, tuple[str, ...]]:
        await _upload(harness, granted=("user_2", "user_3"))
        events = await harness.outbox.claim(worker_id="worker_1")
        principals = await harness.documents.authorized_principals(
            document_id="doc_1",
            tenant_id=TENANT,
            principal_id=OWNER,
        )
        return events[0].payload["authorized_principals"], principals

    payload_principals, stored_principals = _run(scenario, tmp_path)

    assert payload_principals == ["user_1", "user_2", "user_3"]
    assert stored_principals == ("user_1", "user_2", "user_3")


def test_two_workers_never_claim_the_same_event(tmp_path: Path) -> None:
    """SKIP LOCKED is what lets several workers drain one queue."""

    async def scenario(harness: Harness) -> tuple[int, int, int]:
        for index in range(6):
            await _upload(
                harness,
                content=f"document {index}\n".encode(),
                document_id=f"doc_{index}",
            )
        first, second = await asyncio.gather(
            harness.outbox.claim(worker_id="worker_1", limit=3),
            harness.outbox.claim(worker_id="worker_2", limit=3),
        )
        overlap = {event.event_id for event in first} & {
            event.event_id for event in second
        }
        return len(first), len(second), len(overlap)

    claimed_first, claimed_second, overlap = _run(scenario, tmp_path)

    assert claimed_first + claimed_second == 6
    assert overlap == 0


def test_claimed_events_are_not_claimed_again(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> tuple[int, int]:
        await _upload(harness)
        first = await harness.outbox.claim(worker_id="worker_1")
        second = await harness.outbox.claim(worker_id="worker_2")
        return len(first), len(second)

    assert _run(scenario, tmp_path) == (1, 0)


def test_acknowledging_clears_the_pending_work(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> tuple[int, int]:
        await _upload(harness)
        before = await harness.outbox.pending_count()
        events = await harness.outbox.claim(worker_id="worker_1")
        await harness.outbox.ack(event_id=events[0].event_id)
        return before, await harness.outbox.pending_count()

    assert _run(scenario, tmp_path) == (1, 0)


def test_events_are_ordered_by_the_database(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> list[int]:
        for index in range(4):
            await _upload(
                harness,
                content=f"document {index}\n".encode(),
                document_id=f"doc_{index}",
            )
        events = await harness.outbox.claim(worker_id="worker_1", limit=10)
        return [event.sequence for event in events]

    sequences = _run(scenario, tmp_path)

    assert sequences == sorted(sequences)
    assert len(set(sequences)) == 4


def test_a_neighbour_cannot_read_the_document(tmp_path: Path) -> None:
    """P1-1. Same tenant, no grant. A tenant is not a principal."""

    async def scenario(harness: Harness) -> None:
        await _upload(harness)
        await harness.documents.document(
            document_id="doc_1", tenant_id=TENANT, principal_id="user_neighbour"
        )

    with pytest.raises(NotFoundError):
        _run(scenario, tmp_path)


def test_a_granted_principal_can_read_the_document(tmp_path: Path) -> None:
    """The control: reading answers to the owner *or* the ACL."""

    async def scenario(harness: Harness) -> str:
        await _upload(harness, granted=("user_neighbour",))
        document = await harness.documents.document(
            document_id="doc_1", tenant_id=TENANT, principal_id="user_neighbour"
        )
        return document.owner_id

    assert _run(scenario, tmp_path) == OWNER


def test_a_neighbour_cannot_list_who_may_see_the_document(tmp_path: Path) -> None:
    """Who else can see it is itself something only readers may ask."""

    async def scenario(harness: Harness) -> None:
        await _upload(harness, granted=("user_2",))
        await harness.documents.authorized_principals(
            document_id="doc_1", tenant_id=TENANT, principal_id="user_neighbour"
        )

    with pytest.raises(NotFoundError):
        _run(scenario, tmp_path)


def test_a_neighbour_cannot_list_the_versions(tmp_path: Path) -> None:
    async def scenario(harness: Harness) -> None:
        await _upload(harness)
        await harness.documents.versions(
            document_id="doc_1", tenant_id=TENANT, principal_id="user_neighbour"
        )

    with pytest.raises(NotFoundError):
        _run(scenario, tmp_path)


def test_a_granted_principal_still_cannot_commit_a_version(tmp_path: Path) -> None:
    """Read and write are separate rules, checked at the store itself."""

    async def scenario(harness: Harness) -> None:
        await _upload(harness, granted=("user_neighbour",))
        await _upload(harness, content=b"Rewritten.\n", owner="user_neighbour")

    with pytest.raises(NotFoundError):
        _run(scenario, tmp_path)


def test_the_knowledge_base_of_an_existing_document_cannot_change(
    tmp_path: Path,
) -> None:
    """The row and the outbox event must not describe different knowledge bases."""

    async def scenario(harness: Harness) -> None:
        await _upload(harness)
        await _upload(harness, content=b"Moved.\n", knowledge_base_id="kb_other")

    with pytest.raises(KnowledgeBaseMismatchError):
        _run(scenario, tmp_path)


def test_losing_the_creation_race_does_not_grant_a_write(tmp_path: Path) -> None:
    """Two principals create the same new document id at once.

    The conditional insert means one of them ends up locking a row it did not
    create. That is the interleaving where an ownership check placed before the
    insert would pass and then write into somebody else's document, so the
    check sits after the lock instead. Exactly one may win, and the surviving
    document must belong to whoever that was.
    """

    async def scenario(harness: Harness) -> tuple[int, str, int]:
        outcomes = await asyncio.gather(
            _upload(harness, content=b"from the owner\n", owner=OWNER),
            _upload(harness, content=b"from the neighbour\n", owner="user_neighbour"),
            return_exceptions=True,
        )
        succeeded = [
            outcome for outcome in outcomes if not isinstance(outcome, BaseException)
        ]
        refused = [
            outcome for outcome in outcomes if isinstance(outcome, NotFoundError)
        ]
        async with harness.engine.connect() as connection:
            owner = (
                await connection.execute(select(documents.c.owner_id))
            ).scalar_one()
        return len(succeeded), cast(str, owner), len(refused)

    winners, owner_id, refusals = _run(scenario, tmp_path)

    assert (winners, refusals) == (1, 1)
    assert owner_id in {OWNER, "user_neighbour"}
