"""Documents, versions, ACL and their outbox events, in PostgreSQL.

``commit_version`` is the method this whole package exists for. It writes the
document, its version, its ACL and the outbox event inside one transaction, so
the index is told about exactly the documents that exist. Splitting it into two
commits would create both of the failures that ordering cannot fix: a document
nothing will ever index, and an index entry for content that was rolled back.

Revisions are taken while the document row is locked, which is what makes them
monotonic under concurrent uploads rather than merely distinct. Ownership is
checked under that same lock, and for the same reason: a check that released
the row before writing would authorize against a document that no longer looks
like the one being written to.

Completion is idempotent twice over. Completing the same upload again returns
the version it already produced, and re-uploading content identical to the
current version does not mint a new revision -- a re-sent request should not
make the index redo work that would produce the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_workbench.adapters.persistence.models import (
    document_acl,
    document_versions,
    documents,
    outbox_events,
    upload_intents,
)
from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_id
from agent_workbench.ports.documents import (
    Document,
    DocumentVersion,
    KnowledgeBaseMismatchError,
    UploadIntent,
)

OUTBOX_EVENT_PREFIX = "obx"


@dataclass(frozen=True, slots=True)
class _LockedDocument:
    """A document row held under ``FOR UPDATE``, with what decides the write."""

    revision: int
    owner_id: str
    knowledge_base_id: str


class PostgresDocumentStore:
    """The authority on documents; the index is a copy of what is here."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_upload(
        self,
        *,
        upload_id: str,
        tenant_id: str,
        owner_id: str,
        declared_size_bytes: int,
        declared_sha256: str,
        media_type: str,
        filename: str | None = None,
    ) -> UploadIntent:
        intent = UploadIntent(
            upload_id=upload_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            declared_size_bytes=declared_size_bytes,
            declared_sha256=declared_sha256,
            media_type=media_type,
            filename=filename,
        )
        async with self._engine.begin() as connection:
            try:
                await connection.execute(
                    insert(upload_intents).values(
                        upload_id=intent.upload_id,
                        tenant_id=intent.tenant_id,
                        owner_id=intent.owner_id,
                        declared_size_bytes=intent.declared_size_bytes,
                        declared_sha256=intent.declared_sha256.lower(),
                        media_type=intent.media_type,
                        filename=intent.filename,
                        status="pending",
                    )
                )
            except IntegrityError as exc:
                raise ValueError(f"upload {upload_id} already exists") from exc
        return intent

    async def upload_intent(
        self, *, upload_id: str, tenant_id: str, principal_id: str
    ) -> UploadIntent:
        async with self._engine.connect() as connection:
            return await self._require_intent(
                connection, upload_id, tenant_id, principal_id
            )

    async def commit_version(
        self,
        *,
        upload_id: str,
        tenant_id: str,
        principal_id: str,
        document_id: str,
        knowledge_base_id: str,
        version_id: str,
        artifact_id: str,
        content_sha256: str,
        granted_principals: tuple[str, ...] = (),
    ) -> DocumentVersion:
        digest = content_sha256.lower()

        async with self._engine.begin() as connection:
            intent = await self._require_intent(
                connection,
                upload_id,
                tenant_id,
                principal_id,
                for_update=True,
            )

            if intent.status == "completed":
                # The same upload, completed again. Return what it produced
                # rather than minting a second version of the same bytes.
                assert intent.version_id is not None
                return await self._version(connection, intent.version_id)

            current = await self._locked_document(connection, document_id, tenant_id)
            if current is None:
                # Another transaction may be creating this same document right
                # now, and its row is not visible yet. Insert conditionally,
                # then lock whatever ended up there -- ours, or the one that
                # got there first. A plain insert would lose that race with a
                # duplicate-key error.
                await connection.execute(
                    pg_insert(documents)
                    .values(
                        document_id=document_id,
                        tenant_id=tenant_id,
                        owner_id=principal_id,
                        knowledge_base_id=knowledge_base_id,
                        source_revision=0,
                    )
                    .on_conflict_do_nothing(index_elements=["document_id"])
                )
                current = await self._locked_document(
                    connection,
                    document_id,
                    tenant_id,
                )
                if current is None:  # pragma: no cover - the row exists by now
                    raise NotFoundError("document not found")

            # Checked once, on whichever row is now held: the one that was
            # already there, or the one this transaction lost the race to
            # create. Losing that race must not be a way to acquire a write.
            self._require_writable(current, principal_id, knowledge_base_id)

            revision_before = current.revision
            latest = await self._latest_version(connection, document_id)
            if latest is not None and latest.content_sha256 == digest:
                # Identical content. Advancing the revision would make the
                # index redo work that produces exactly the same rows.
                await self._mark_completed(connection, upload_id, latest.version_id)
                return latest

            revision = revision_before + 1
            await connection.execute(
                update(documents)
                .where(documents.c.document_id == document_id)
                .values(source_revision=revision)
            )

            version = DocumentVersion(
                version_id=version_id,
                document_id=document_id,
                source_revision=revision,
                artifact_id=artifact_id,
                content_sha256=digest,
            )
            await connection.execute(
                insert(document_versions).values(
                    version_id=version.version_id,
                    document_id=version.document_id,
                    source_revision=version.source_revision,
                    artifact_id=version.artifact_id,
                    content_sha256=version.content_sha256,
                )
            )
            await self._replace_acl(connection, document_id, granted_principals)
            await self._record_outbox(
                connection,
                document_id=document_id,
                revision=revision,
                kind="document_upserted",
                payload={
                    "tenant_id": tenant_id,
                    "knowledge_base_id": knowledge_base_id,
                    "version_id": version.version_id,
                    "artifact_id": artifact_id,
                    "content_sha256": digest,
                    "authorized_principals": sorted(
                        {intent.owner_id, *granted_principals}
                    ),
                },
            )
            await self._mark_completed(connection, upload_id, version.version_id)
            return version

    async def document(
        self, *, document_id: str, tenant_id: str, principal_id: str
    ) -> Document:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(
                        documents.c.document_id,
                        documents.c.tenant_id,
                        documents.c.owner_id,
                        documents.c.knowledge_base_id,
                        documents.c.source_revision,
                        documents.c.deleted,
                    )
                    .where(documents.c.document_id == document_id)
                    .where(documents.c.tenant_id == tenant_id)
                )
            ).first()
        if row is None:
            raise NotFoundError("document not found")
        document = Document(
            document_id=cast(str, row.document_id),
            tenant_id=cast(str, row.tenant_id),
            owner_id=cast(str, row.owner_id),
            knowledge_base_id=cast(str, row.knowledge_base_id),
            source_revision=cast(int, row.source_revision),
            deleted=cast(bool, row.deleted),
        )
        if document.owner_id != principal_id and not await self._is_granted(
            document_id, principal_id
        ):
            raise NotFoundError("document not found")
        return document

    async def versions(
        self,
        *,
        document_id: str,
        tenant_id: str,
        principal_id: str,
    ) -> tuple[DocumentVersion, ...]:
        await self.document(
            document_id=document_id, tenant_id=tenant_id, principal_id=principal_id
        )
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        document_versions.c.version_id,
                        document_versions.c.source_revision,
                        document_versions.c.artifact_id,
                        document_versions.c.content_sha256,
                    )
                    .where(document_versions.c.document_id == document_id)
                    .order_by(document_versions.c.source_revision)
                )
            ).all()
        return tuple(
            DocumentVersion(
                version_id=cast(str, row.version_id),
                document_id=document_id,
                source_revision=cast(int, row.source_revision),
                artifact_id=cast(str, row.artifact_id),
                content_sha256=cast(str, row.content_sha256),
            )
            for row in rows
        )

    async def authorized_principals(
        self,
        *,
        document_id: str,
        tenant_id: str,
        principal_id: str,
    ) -> tuple[str, ...]:
        owned = await self.document(
            document_id=document_id, tenant_id=tenant_id, principal_id=principal_id
        )
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(document_acl.c.principal_id).where(
                        document_acl.c.document_id == document_id
                    )
                )
            ).all()
        granted = {cast(str, row.principal_id) for row in rows}
        return tuple(sorted({owned.owner_id, *granted}))

    async def _is_granted(self, document_id: str, principal_id: str) -> bool:
        """Whether the ACL names this principal.

        Reading answers to the owner or a granted principal; writing answers to
        the owner alone. Keeping them separate is the point -- a grant to read
        a document that also conferred the right to overwrite it would be a
        permission nobody chose to hand out.
        """

        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(document_acl.c.principal_id)
                    .where(document_acl.c.document_id == document_id)
                    .where(document_acl.c.principal_id == principal_id)
                )
            ).first()
        return row is not None

    @staticmethod
    def _require_writable(
        current: _LockedDocument,
        principal_id: str,
        knowledge_base_id: str,
    ) -> None:
        """Whether this principal may commit a new version to this document."""

        if current.owner_id != principal_id:
            # Someone else's document. Committing here would overwrite its
            # content and replace its ACL -- the write the tenant boundary
            # alone never stopped. Only the owner writes; a read grant is a
            # grant to read.
            raise NotFoundError("document not found")
        if current.knowledge_base_id != knowledge_base_id:
            # The caller owns it, so this is not a refusal to answer -- it is a
            # claim that contradicts the row. Accepting it would leave the row
            # in one knowledge base and tell the index another.
            raise KnowledgeBaseMismatchError(
                "the document is not in the named knowledge base"
            )

    async def _require_intent(
        self,
        connection: AsyncConnection,
        upload_id: str,
        tenant_id: str,
        principal_id: str,
        *,
        for_update: bool = False,
    ) -> UploadIntent:
        query = (
            select(
                upload_intents.c.upload_id,
                upload_intents.c.tenant_id,
                upload_intents.c.owner_id,
                upload_intents.c.declared_size_bytes,
                upload_intents.c.declared_sha256,
                upload_intents.c.media_type,
                upload_intents.c.filename,
                upload_intents.c.status,
                upload_intents.c.version_id,
            )
            .where(upload_intents.c.upload_id == upload_id)
            .where(upload_intents.c.tenant_id == tenant_id)
        )
        if for_update:
            query = query.with_for_update()
        row = (await connection.execute(query)).first()
        if row is None:
            raise NotFoundError("upload not found")
        if cast(str, row.owner_id) != principal_id:
            # Another principal's upload. Its filename and media type are that
            # principal's, and completing it would attribute their document to
            # a transfer they did not make.
            raise NotFoundError("upload not found")
        return UploadIntent(
            upload_id=cast(str, row.upload_id),
            tenant_id=cast(str, row.tenant_id),
            owner_id=cast(str, row.owner_id),
            declared_size_bytes=cast(int, row.declared_size_bytes),
            declared_sha256=cast(str, row.declared_sha256),
            media_type=cast(str, row.media_type),
            filename=cast(str | None, row.filename),
            status=cast(str, row.status),  # pyright: ignore[reportArgumentType]
            version_id=cast(str | None, row.version_id),
        )

    async def _locked_document(
        self,
        connection: AsyncConnection,
        document_id: str,
        tenant_id: str,
    ) -> _LockedDocument | None:
        """Hold the document row and return it, or ``None`` if absent.

        A freshly created row carries revision 0 and is raised to 1 before the
        transaction commits, so a committed document is always at 1 or above.
        The lock is what makes revisions monotonic under concurrent uploads
        rather than merely distinct -- and it is also what makes the ownership
        check meaningful, since the row cannot change between the two.
        """

        row = (
            await connection.execute(
                select(
                    documents.c.source_revision,
                    documents.c.tenant_id,
                    documents.c.owner_id,
                    documents.c.knowledge_base_id,
                )
                .where(documents.c.document_id == document_id)
                .with_for_update()
            )
        ).first()
        if row is None:
            return None
        if cast(str, row.tenant_id) != tenant_id:
            # Another tenant's document id. Answering anything but "not found"
            # would confirm it exists.
            raise NotFoundError("document not found")
        return _LockedDocument(
            revision=cast(int, row.source_revision),
            owner_id=cast(str, row.owner_id),
            knowledge_base_id=cast(str, row.knowledge_base_id),
        )

    async def _latest_version(
        self,
        connection: AsyncConnection,
        document_id: str,
    ) -> DocumentVersion | None:
        row = (
            await connection.execute(
                select(
                    document_versions.c.version_id,
                    document_versions.c.source_revision,
                    document_versions.c.artifact_id,
                    document_versions.c.content_sha256,
                )
                .where(document_versions.c.document_id == document_id)
                .order_by(document_versions.c.source_revision.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return DocumentVersion(
            version_id=cast(str, row.version_id),
            document_id=document_id,
            source_revision=cast(int, row.source_revision),
            artifact_id=cast(str, row.artifact_id),
            content_sha256=cast(str, row.content_sha256),
        )

    async def _version(
        self,
        connection: AsyncConnection,
        version_id: str,
    ) -> DocumentVersion:
        row = (
            await connection.execute(
                select(
                    document_versions.c.version_id,
                    document_versions.c.document_id,
                    document_versions.c.source_revision,
                    document_versions.c.artifact_id,
                    document_versions.c.content_sha256,
                ).where(document_versions.c.version_id == version_id)
            )
        ).first()
        if row is None:
            raise NotFoundError("document version not found")
        return DocumentVersion(
            version_id=cast(str, row.version_id),
            document_id=cast(str, row.document_id),
            source_revision=cast(int, row.source_revision),
            artifact_id=cast(str, row.artifact_id),
            content_sha256=cast(str, row.content_sha256),
        )

    async def _replace_acl(
        self,
        connection: AsyncConnection,
        document_id: str,
        granted_principals: tuple[str, ...],
    ) -> None:
        await connection.execute(
            document_acl.delete().where(document_acl.c.document_id == document_id)
        )
        if granted_principals:
            await connection.execute(
                insert(document_acl),
                [
                    {"document_id": document_id, "principal_id": principal}
                    for principal in sorted(set(granted_principals))
                ],
            )

    async def _record_outbox(
        self,
        connection: AsyncConnection,
        *,
        document_id: str,
        revision: int,
        kind: str,
        payload: dict[str, object],
    ) -> None:
        await connection.execute(
            insert(outbox_events).values(
                event_id=new_id(OUTBOX_EVENT_PREFIX),
                document_id=document_id,
                source_revision=revision,
                kind=kind,
                payload=payload,
            )
        )

    async def _mark_completed(
        self,
        connection: AsyncConnection,
        upload_id: str,
        version_id: str,
    ) -> None:
        await connection.execute(
            update(upload_intents)
            .where(upload_intents.c.upload_id == upload_id)
            .values(status="completed", version_id=version_id)
        )

    async def pending_outbox(self) -> int:
        async with self._engine.connect() as connection:
            result = await connection.execute(
                select(func.count()).where(outbox_events.c.acked_at.is_(None))
            )
            return result.scalar_one()


__all__ = ["OUTBOX_EVENT_PREFIX", "PostgresDocumentStore"]
