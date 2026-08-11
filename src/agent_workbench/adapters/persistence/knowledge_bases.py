"""PostgreSQL authority for knowledge-base discovery and write ownership."""

from __future__ import annotations

from typing import Any, Literal, cast

from sqlalchemy import and_, case, exists, func, insert, null, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import (
    document_acl,
    document_versions,
    documents,
    knowledge_bases,
    outbox_events,
    upload_intents,
)
from agent_workbench.ports.knowledge_bases import (
    KnowledgeBaseRecord,
    KnowledgeBaseSummary,
    KnowledgeDocument,
)

#: The three states a readable document can be in, as this module counts them.
_DocumentState = Literal["ready", "processing", "failed"]


class PostgresKnowledgeBaseStore:
    """Store bases and derive caller-specific document projections."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, record: KnowledgeBaseRecord) -> KnowledgeBaseRecord:
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(knowledge_bases).values(
                    knowledge_base_id=record.knowledge_base_id,
                    tenant_id=record.tenant_id,
                    owner_id=record.owner_id,
                    name=record.name,
                    description=record.description,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
        return record

    async def get(
        self, *, tenant_id: str, knowledge_base_id: str
    ) -> KnowledgeBaseRecord | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(knowledge_bases)
                        .where(knowledge_bases.c.tenant_id == tenant_id)
                        .where(knowledge_bases.c.knowledge_base_id == knowledge_base_id)
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _record(row)

    async def describe_readable(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
    ) -> KnowledgeBaseSummary | None:
        query = _summary_query(principal_id).where(
            knowledge_bases.c.tenant_id == tenant_id,
            knowledge_bases.c.knowledge_base_id == knowledge_base_id,
            _knowledge_base_readable(principal_id),
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).mappings().first()
        return None if row is None else _summary(row)

    async def list_readable(
        self, *, tenant_id: str, principal_id: str
    ) -> tuple[KnowledgeBaseSummary, ...]:
        query = (
            _summary_query(principal_id)
            .where(
                knowledge_bases.c.tenant_id == tenant_id,
                _knowledge_base_readable(principal_id),
            )
            .order_by(
                # The label is the effective update time, including document
                # events, not merely edits to the base's display metadata.
                _effective_updated_at(principal_id).desc(),
                knowledge_bases.c.knowledge_base_id,
            )
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()
        return tuple(_summary(row) for row in rows)

    async def list_readable_documents(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        knowledge_base_id: str,
    ) -> tuple[KnowledgeDocument, ...]:
        latest_version_id = (
            select(document_versions.c.version_id)
            .where(document_versions.c.document_id == documents.c.document_id)
            .order_by(document_versions.c.source_revision.desc())
            .limit(1)
            .correlate(documents)
            .scalar_subquery()
        )

        def upload_value(column: Any) -> Any:
            # More than one idempotent upload may name the same version. The
            # latest completed intent is the current display metadata, and the
            # completion path verified its size and digest before committing.
            return (
                select(column)
                .where(upload_intents.c.version_id == latest_version_id)
                .where(upload_intents.c.status == "completed")
                .order_by(
                    upload_intents.c.created_at.desc(),
                    upload_intents.c.upload_id.desc(),
                )
                .limit(1)
                .correlate(documents)
                .scalar_subquery()
            )

        last_event_at = (
            select(func.max(outbox_events.c.created_at))
            .where(outbox_events.c.document_id == documents.c.document_id)
            .correlate(documents)
            .scalar_subquery()
        )
        query = (
            select(
                documents.c.document_id,
                upload_value(upload_intents.c.filename).label("filename"),
                upload_value(upload_intents.c.media_type).label("media_type"),
                upload_value(upload_intents.c.declared_size_bytes).label("size_bytes"),
                documents.c.source_revision,
                documents.c.last_applied_revision,
                _document_status().label("status"),
                _reported_failure_code().label("failure_code"),
                documents.c.created_at,
                func.coalesce(last_event_at, documents.c.created_at).label(
                    "updated_at"
                ),
            )
            .where(
                documents.c.tenant_id == tenant_id,
                documents.c.knowledge_base_id == knowledge_base_id,
                documents.c.deleted.is_(False),
                _document_readable(principal_id),
            )
            .order_by(
                func.coalesce(last_event_at, documents.c.created_at).desc(),
                documents.c.document_id,
            )
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).mappings().all()
        return tuple(_document(row) for row in rows)


def _document_indexed() -> Any:
    """The index has been told about the revision that currently exists."""

    return documents.c.last_applied_revision == documents.c.source_revision


def _document_refused() -> Any:
    """An attempt at the revision that currently exists came back refused.

    Scoped to ``source_revision`` on both sides, so a marker left by an older
    revision does not follow a re-upload that has not been tried yet.

    ``IS NOT DISTINCT FROM`` rather than ``=`` because the column is null for
    every document that has never failed, and a three-valued comparison cannot
    be negated: ``NOT (x = NULL)`` is NULL, so the processing count -- which is
    exactly that negation -- would have dropped every healthy document.
    """

    return and_(
        documents.c.last_applied_revision < documents.c.source_revision,
        documents.c.failed_revision.is_not_distinct_from(documents.c.source_revision),
    )


def _document_status() -> Any:
    """Ready, failed, or still waiting -- decided in that order.

    Indexed first, and not merely for tidiness: a retry that succeeds is
    supposed to clear the marker, and if one ever does not, the reader is told
    the document is searchable, which is the fact they can act on.
    """

    return case(
        (_document_indexed(), "ready"),
        (_document_refused(), "failed"),
        else_="processing",
    )


def _reported_failure_code() -> Any:
    """The refusal code, and only while it describes the current revision."""

    return case((_document_refused(), documents.c.failure_code), else_=null())


def _document_readable(principal_id: str) -> Any:
    granted = exists(
        select(document_acl.c.document_id).where(
            document_acl.c.document_id == documents.c.document_id,
            document_acl.c.principal_id == principal_id,
        )
    )
    return or_(documents.c.owner_id == principal_id, granted)


def _readable_document_in_base(principal_id: str) -> Any:
    return and_(
        documents.c.tenant_id == knowledge_bases.c.tenant_id,
        documents.c.knowledge_base_id == knowledge_bases.c.knowledge_base_id,
        documents.c.deleted.is_(False),
        _document_readable(principal_id),
    )


def _knowledge_base_readable(principal_id: str) -> Any:
    return or_(
        knowledge_bases.c.owner_id == principal_id,
        exists(
            select(documents.c.document_id).where(
                _readable_document_in_base(principal_id)
            )
        ),
    )


def _document_count(principal_id: str, *, state: _DocumentState | None = None) -> Any:
    conditions = [_readable_document_in_base(principal_id)]
    if state == "ready":
        conditions.append(_document_indexed())
    elif state == "failed":
        conditions.append(_document_refused())
    elif state == "processing":
        # Everything not indexed and not refused. Subtracting the refused ones
        # is the whole change here: they used to be counted as progress.
        conditions.append(
            and_(
                documents.c.last_applied_revision < documents.c.source_revision,
                ~_document_refused(),
            )
        )
    return (
        select(func.count())
        .select_from(documents)
        .where(*conditions)
        .correlate(knowledge_bases)
        .scalar_subquery()
    )


def _latest_readable_document_event(principal_id: str) -> Any:
    return (
        select(func.max(outbox_events.c.created_at))
        .select_from(
            outbox_events.join(
                documents,
                documents.c.document_id == outbox_events.c.document_id,
            )
        )
        .where(_readable_document_in_base(principal_id))
        .correlate(knowledge_bases)
        .scalar_subquery()
    )


def _effective_updated_at(principal_id: str) -> Any:
    return func.greatest(
        knowledge_bases.c.updated_at,
        func.coalesce(
            _latest_readable_document_event(principal_id),
            knowledge_bases.c.updated_at,
        ),
    )


def _summary_query(principal_id: str) -> Any:
    return select(
        knowledge_bases.c.knowledge_base_id,
        knowledge_bases.c.name,
        knowledge_bases.c.description,
        # The same owner-only rule ``KnowledgeBaseService.require_writable``
        # enforces, expressed once more in SQL because the projection cannot
        # call it per row. The two must move together: widening write authority
        # without widening this leaves an interface that hides a control the
        # server would have accepted, and widening this alone offers one it
        # will refuse.
        (knowledge_bases.c.owner_id == principal_id).label("can_write"),
        _document_count(principal_id).label("document_count"),
        _document_count(principal_id, state="ready").label("ready_document_count"),
        _document_count(principal_id, state="processing").label(
            "processing_document_count"
        ),
        _document_count(principal_id, state="failed").label("failed_document_count"),
        knowledge_bases.c.created_at,
        _effective_updated_at(principal_id).label("updated_at"),
    )


def _record(row: Any) -> KnowledgeBaseRecord:
    return KnowledgeBaseRecord(
        knowledge_base_id=cast(str, row["knowledge_base_id"]),
        tenant_id=cast(str, row["tenant_id"]),
        owner_id=cast(str, row["owner_id"]),
        name=cast(str, row["name"]),
        description=cast(str | None, row["description"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _summary(row: Any) -> KnowledgeBaseSummary:
    return KnowledgeBaseSummary(
        knowledge_base_id=cast(str, row["knowledge_base_id"]),
        name=cast(str, row["name"]),
        description=cast(str | None, row["description"]),
        can_write=cast(bool, row["can_write"]),
        document_count=cast(int, row["document_count"]),
        ready_document_count=cast(int, row["ready_document_count"]),
        processing_document_count=cast(int, row["processing_document_count"]),
        failed_document_count=cast(int, row["failed_document_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _document(row: Any) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=cast(str, row["document_id"]),
        filename=cast(str | None, row["filename"]),
        media_type=cast(str, row["media_type"]),
        size_bytes=cast(int, row["size_bytes"]),
        source_revision=cast(int, row["source_revision"]),
        last_applied_revision=cast(int, row["last_applied_revision"]),
        status=cast(str, row["status"]),  # pyright: ignore[reportArgumentType]
        failure_code=cast(  # pyright: ignore[reportArgumentType]
            str | None, row["failure_code"]
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


__all__ = ["PostgresKnowledgeBaseStore"]
