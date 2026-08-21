"""In-memory project store.

The one structural difference from PostgreSQL, stated rather than hidden:
membership there lives on rows this store does not own -- a column on
``conversation_sessions`` and one on ``task_runs``, released by ON DELETE SET
NULL. There is no such row here, so this double is *told* about the members it
should know, through ``remember_session`` / ``remember_task`` /
``remember_knowledge_base``.

Everything the contract asks about is then answered the same way by both: an
assignment against an item nobody knows returns ``False``; deleting a project
releases its members instead of removing them; a project belonging to somebody
else is not readable, assignable or deletable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_workbench.ports.projects import (
    ProjectContents,
    ProjectItem,
    ProjectItemKind,
    ProjectRecord,
)


@dataclass(slots=True)
class _Member:
    """One row another store owns, as much of it as membership needs."""

    owner_id: str
    kind: ProjectItemKind
    title: str | None
    ordered_at: datetime
    project_id: str | None = None


@dataclass(slots=True)
class _Base:
    """A knowledge base: linked through a table, so no ``project_id`` on it."""

    owner_id: str
    name: str


@dataclass(slots=True)
class _Link:
    project_id: str
    knowledge_base_id: str
    linked_at: datetime


class InMemoryProjectStore:
    """Projects and their membership, held in process memory."""

    def __init__(
        self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._projects: dict[tuple[str, str], ProjectRecord] = {}
        self._members: dict[tuple[str, str], _Member] = {}
        self._bases: dict[tuple[str, str], _Base] = {}
        self._links: dict[str, list[_Link]] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    # -- the double's stand-in for rows another store owns -----------------

    def remember_session(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        session_id: str,
        mode: ProjectItemKind = "chat",
        title: str | None = None,
        last_activity_at: datetime | None = None,
    ) -> None:
        self._members[(tenant_id, session_id)] = _Member(
            owner_id=owner_id,
            kind=mode,
            title=title,
            ordered_at=last_activity_at or self._clock(),
        )

    def remember_task(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        objective_preview: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self._members[(tenant_id, task_id)] = _Member(
            owner_id=owner_id,
            kind="task",
            title=objective_preview,
            ordered_at=created_at or self._clock(),
        )

    def remember_knowledge_base(
        self, *, tenant_id: str, owner_id: str, knowledge_base_id: str, name: str
    ) -> None:
        self._bases[(tenant_id, knowledge_base_id)] = _Base(
            owner_id=owner_id, name=name
        )

    def knows(self, *, tenant_id: str, item_id: str) -> bool:
        """Whether this double still holds a row for that item.

        The port has no such question because PostgreSQL answers it with a
        SELECT against a table this store does not own. Here it is the only way
        to assert what ON DELETE SET NULL guarantees: the member outlives the
        project it was filed under.
        """

        return (tenant_id, item_id) in self._members

    # -- the port ----------------------------------------------------------

    async def create(self, record: ProjectRecord) -> ProjectRecord:
        async with self._lock:
            self._projects[(record.tenant_id, record.project_id)] = record
        return record

    async def get(
        self, *, tenant_id: str, owner_id: str, project_id: str
    ) -> ProjectRecord | None:
        record = self._projects.get((tenant_id, project_id))
        if record is None or record.owner_id != owner_id:
            return None
        return record

    async def list_for_owner(
        self, *, tenant_id: str, owner_id: str, include_archived: bool = False
    ) -> tuple[ProjectRecord, ...]:
        found = [
            record
            for (held_tenant, _), record in self._projects.items()
            if held_tenant == tenant_id and record.owner_id == owner_id
        ]
        if not include_archived:
            found = [record for record in found if record.archived_at is None]
        # Same order as the SQL: unarchived first, then most recently touched,
        # then by id so equal timestamps do not shuffle between calls. Three
        # stable passes, least significant first.
        found.sort(key=lambda record: record.project_id)
        found.sort(key=lambda record: record.updated_at, reverse=True)
        found.sort(key=lambda record: record.archived_at is not None)
        return tuple(found)

    async def rename(
        self, *, tenant_id: str, owner_id: str, project_id: str, name: str
    ) -> ProjectRecord | None:
        return await self._update(
            tenant_id=tenant_id,
            owner_id=owner_id,
            project_id=project_id,
            change=lambda record: record.model_copy(update={"name": name}),
        )

    async def set_archived(
        self, *, tenant_id: str, owner_id: str, project_id: str, archived: bool
    ) -> ProjectRecord | None:
        stamp = self._clock() if archived else None
        return await self._update(
            tenant_id=tenant_id,
            owner_id=owner_id,
            project_id=project_id,
            change=lambda record: record.model_copy(update={"archived_at": stamp}),
        )

    async def _update(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        change: Callable[[ProjectRecord], ProjectRecord],
    ) -> ProjectRecord | None:
        async with self._lock:
            record = self._projects.get((tenant_id, project_id))
            if record is None or record.owner_id != owner_id:
                return None
            updated = change(record).model_copy(update={"updated_at": self._clock()})
            self._projects[(tenant_id, project_id)] = updated
            return updated

    async def delete(self, *, tenant_id: str, owner_id: str, project_id: str) -> bool:
        async with self._lock:
            record = self._projects.get((tenant_id, project_id))
            if record is None or record.owner_id != owner_id:
                return False
            del self._projects[(tenant_id, project_id)]
            # What ON DELETE SET NULL does over there: the members stay, their
            # membership goes.
            for (held_tenant, _), member in self._members.items():
                if held_tenant == tenant_id and member.project_id == project_id:
                    member.project_id = None
            self._links.pop(f"{tenant_id}/{project_id}", None)
            return True

    async def contents(
        self, *, tenant_id: str, owner_id: str, project_id: str
    ) -> ProjectContents:
        items: list[ProjectItem] = [
            ProjectItem(
                kind=member.kind,
                item_id=item_id,
                title=member.title,
                ordered_at=member.ordered_at,
            )
            for (held_tenant, item_id), member in self._members.items()
            if held_tenant == tenant_id
            and member.owner_id == owner_id
            and member.project_id == project_id
        ]
        for link in self._links.get(f"{tenant_id}/{project_id}", []):
            base = self._bases.get((tenant_id, link.knowledge_base_id))
            if base is None:
                continue
            items.append(
                ProjectItem(
                    kind="knowledge_base",
                    item_id=link.knowledge_base_id,
                    title=base.name,
                    ordered_at=link.linked_at,
                )
            )
        items.sort(key=lambda item: item.item_id)
        items.sort(key=lambda item: item.ordered_at, reverse=True)
        return ProjectContents(project_id=project_id, items=tuple(items))

    async def assign_session(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        session_id: str,
        project_id: str | None,
    ) -> bool:
        return await self._assign(
            tenant_id=tenant_id,
            owner_id=owner_id,
            item_id=session_id,
            project_id=project_id,
            kinds=("chat", "code"),
        )

    async def assign_task(
        self, *, tenant_id: str, owner_id: str, task_id: str, project_id: str | None
    ) -> bool:
        return await self._assign(
            tenant_id=tenant_id,
            owner_id=owner_id,
            item_id=task_id,
            project_id=project_id,
            kinds=("task",),
        )

    async def _assign(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        item_id: str,
        project_id: str | None,
        kinds: tuple[ProjectItemKind, ...],
    ) -> bool:
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
        async with self._lock:
            member = self._members.get((tenant_id, item_id))
            if (
                member is None
                or member.owner_id != owner_id
                or member.kind not in kinds
            ):
                return False
            member.project_id = project_id
            return True

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
        async with self._lock:
            links = self._links.setdefault(f"{tenant_id}/{project_id}", [])
            if any(link.knowledge_base_id == knowledge_base_id for link in links):
                return True
            links.append(
                _Link(
                    project_id=project_id,
                    knowledge_base_id=knowledge_base_id,
                    linked_at=self._clock(),
                )
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
        async with self._lock:
            key = f"{tenant_id}/{project_id}"
            links = self._links.get(key, [])
            remaining = [
                link for link in links if link.knowledge_base_id != knowledge_base_id
            ]
            self._links[key] = remaining
            return len(remaining) != len(links)


__all__ = ["InMemoryProjectStore"]
