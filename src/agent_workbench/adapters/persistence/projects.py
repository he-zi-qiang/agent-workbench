"""PostgreSQL authority for project membership.

Membership lives on the row that *is* a member, not on the project:
``conversation_sessions.project_id`` and ``task_runs.project_id`` are foreign
keys, and knowledge bases join through a link table (ADR-071 2.3). So there is
no membership table here -- ``contents()`` is a union of three selects,
``assign_*`` is one update, and deleting a project releases membership through
ON DELETE SET NULL rather than through cleanup code in this module.

Every statement carries ``owner_id``. Not defensive repetition: a project is
owner-private, and an owner scope written only in the application layer is a
convention the next caller can walk around.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    Table,
    and_,
    delete,
    insert,
    literal,
    select,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_workbench.adapters.persistence.models import (
    conversation_sessions,
    knowledge_bases,
    project_knowledge_bases,
    projects,
    task_runs,
)
from agent_workbench.ports.projects import (
    ProjectContents,
    ProjectItem,
    ProjectRecord,
)


class PostgresProjectStore:
    """Projects, and the membership between them and everything else."""

    __slots__ = ("_engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, record: ProjectRecord) -> ProjectRecord:
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(projects).values(
                    project_id=record.project_id,
                    tenant_id=record.tenant_id,
                    owner_id=record.owner_id,
                    name=record.name,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    archived_at=record.archived_at,
                    # Listed explicitly like every other column rather than
                    # spread from `model_dump()`: an insert that enumerates its
                    # columns fails loudly when a field is added and forgotten,
                    # and one that spreads silently starts writing whatever the
                    # model grew next.
                    root_path=record.root_path,
                )
            )
        return record

    async def get(
        self, *, tenant_id: str, owner_id: str, project_id: str
    ) -> ProjectRecord | None:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(projects).where(
                            projects.c.tenant_id == tenant_id,
                            projects.c.owner_id == owner_id,
                            projects.c.project_id == project_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else ProjectRecord.model_validate(dict(row))

    async def list_for_owner(
        self, *, tenant_id: str, owner_id: str, include_archived: bool = False
    ) -> tuple[ProjectRecord, ...]:
        statement = select(projects).where(
            projects.c.tenant_id == tenant_id,
            projects.c.owner_id == owner_id,
        )
        if not include_archived:
            statement = statement.where(projects.c.archived_at.is_(None))
        # Unarchived first, then most recently touched. Archived ones sort
        # last rather than vanish, so the include_archived=True call gets an
        # order that means something too.
        statement = statement.order_by(
            projects.c.archived_at.is_(None).desc(),
            projects.c.updated_at.desc(),
            projects.c.project_id,
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return tuple(ProjectRecord.model_validate(dict(row)) for row in rows)

    async def rename(
        self, *, tenant_id: str, owner_id: str, project_id: str, name: str
    ) -> ProjectRecord | None:
        return await self._update(
            tenant_id=tenant_id,
            owner_id=owner_id,
            project_id=project_id,
            values={"name": name},
        )

    async def set_archived(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        archived: bool,
    ) -> ProjectRecord | None:
        return await self._update(
            tenant_id=tenant_id,
            owner_id=owner_id,
            project_id=project_id,
            values={"archived_at": datetime.now(UTC) if archived else None},
        )

    async def set_root_path(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        root_path: str | None,
    ) -> ProjectRecord | None:
        # `None` reaches the column as SQL NULL, which is the whole point: it is
        # how "stop pointing this project at that folder" is said. `_update`
        # writes the dict as given, so nothing here has to special-case it.
        return await self._update(
            tenant_id=tenant_id,
            owner_id=owner_id,
            project_id=project_id,
            values={"root_path": root_path},
        )

    async def _update(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        values: dict[str, object],
    ) -> ProjectRecord | None:
        async with self._engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        update(projects)
                        .where(
                            projects.c.tenant_id == tenant_id,
                            projects.c.owner_id == owner_id,
                            projects.c.project_id == project_id,
                        )
                        .values(updated_at=datetime.now(UTC), **values)
                        .returning(*projects.c)
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else ProjectRecord.model_validate(dict(row))

    async def delete(self, *, tenant_id: str, owner_id: str, project_id: str) -> bool:
        # Only the project row. Sessions and tasks are released by ON DELETE
        # SET NULL; the link rows go by ON DELETE CASCADE -- the knowledge bases
        # they pointed at are untouched.
        async with self._engine.begin() as connection:
            result = await connection.execute(
                delete(projects).where(
                    projects.c.tenant_id == tenant_id,
                    projects.c.owner_id == owner_id,
                    projects.c.project_id == project_id,
                )
            )
        return result.rowcount > 0

    async def contents(
        self, *, tenant_id: str, owner_id: str, project_id: str
    ) -> ProjectContents:
        chat_and_code = select(
            conversation_sessions.c.mode.label("kind"),
            conversation_sessions.c.session_id.label("item_id"),
            conversation_sessions.c.title.label("title"),
            conversation_sessions.c.last_activity_at.label("ordered_at"),
        ).where(
            conversation_sessions.c.tenant_id == tenant_id,
            conversation_sessions.c.owner_id == owner_id,
            conversation_sessions.c.project_id == project_id,
        )
        tasks = select(
            literal("task").label("kind"),
            task_runs.c.task_id.label("item_id"),
            task_runs.c.objective_preview.label("title"),
            task_runs.c.created_at.label("ordered_at"),
        ).where(
            task_runs.c.tenant_id == tenant_id,
            task_runs.c.owner_id == owner_id,
            task_runs.c.project_id == project_id,
        )
        bases = (
            select(
                literal("knowledge_base").label("kind"),
                knowledge_bases.c.knowledge_base_id.label("item_id"),
                knowledge_bases.c.name.label("title"),
                project_knowledge_bases.c.linked_at.label("ordered_at"),
            )
            .select_from(
                project_knowledge_bases.join(
                    knowledge_bases,
                    and_(
                        knowledge_bases.c.knowledge_base_id
                        == project_knowledge_bases.c.knowledge_base_id,
                        knowledge_bases.c.tenant_id
                        == project_knowledge_bases.c.tenant_id,
                    ),
                )
            )
            .where(
                project_knowledge_bases.c.tenant_id == tenant_id,
                project_knowledge_bases.c.project_id == project_id,
            )
        )
        combined = union_all(chat_and_code, tasks, bases).subquery()
        statement = select(combined).order_by(
            combined.c.ordered_at.desc(), combined.c.item_id
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return ProjectContents(
            project_id=project_id,
            items=tuple(ProjectItem.model_validate(dict(row)) for row in rows),
        )

    async def assign_session(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        session_id: str,
        project_id: str | None,
    ) -> bool:
        return await self._assign(
            table=conversation_sessions,
            id_column=conversation_sessions.c.session_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            item_id=session_id,
            project_id=project_id,
        )

    async def assign_task(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        project_id: str | None,
    ) -> bool:
        return await self._assign(
            table=task_runs,
            id_column=task_runs.c.task_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            item_id=task_id,
            project_id=project_id,
        )

    async def _assign(
        self,
        *,
        table: Table,
        id_column: Column[str],
        tenant_id: str,
        owner_id: str,
        item_id: str,
        project_id: str | None,
    ) -> bool:
        # Establish that the project is this person's before writing. The
        # foreign key would admit "file my session under somebody else's
        # project": it constrains tenant, and knows nothing about owner.
        if (
            project_id is not None
            and (
                await self.get(
                    tenant_id=tenant_id, owner_id=owner_id, project_id=project_id
                )
            )
            is None
        ):
            return False
        async with self._engine.begin() as connection:
            result = await connection.execute(
                update(table)
                .where(
                    table.c.tenant_id == tenant_id,
                    table.c.owner_id == owner_id,
                    id_column == item_id,
                )
                .values(project_id=project_id)
            )
        return result.rowcount > 0

    async def link_knowledge_base(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        knowledge_base_id: str,
    ) -> bool:
        if (
            await self.get(
                tenant_id=tenant_id, owner_id=owner_id, project_id=project_id
            )
        ) is None:
            return False
        # Linking twice is idempotent rather than an error: a reader pressing
        # "add to this project" a second time wants it to be in there, not a
        # 409.
        async with self._engine.begin() as connection:
            await connection.execute(
                pg_insert(project_knowledge_bases)
                .values(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                    linked_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing()
            )
        return True

    async def unlink_knowledge_base(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        knowledge_base_id: str,
    ) -> bool:
        if (
            await self.get(
                tenant_id=tenant_id, owner_id=owner_id, project_id=project_id
            )
        ) is None:
            return False
        async with self._engine.begin() as connection:
            result = await connection.execute(
                delete(project_knowledge_bases).where(
                    project_knowledge_bases.c.tenant_id == tenant_id,
                    project_knowledge_bases.c.project_id == project_id,
                    project_knowledge_bases.c.knowledge_base_id == knowledge_base_id,
                )
            )
        return result.rowcount > 0


__all__ = ["PostgresProjectStore"]
