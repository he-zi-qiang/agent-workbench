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

**Where a session's membership lives.** On the session, when there is a session
to put it on. Pass an ``InMemoryConversationStore`` as ``sessions=`` and this
store stops holding chat and code membership itself: existence, owner, title,
ordering and ``project_id`` all come from that store's row, exactly as they
come from ``conversation_sessions`` over PostgreSQL. ``_Member`` then covers
only what has no store in this package -- Tasks -- and the unwired double.

The rule is one fact held by whichever store owns the row, never a copy in
each. The copy-in-each version is what shipped, and it is why the PostgreSQL
projection bug was uncatchable by contract: a session filed through
``assign_session`` had its project recorded in ``_members`` here and *not* on
the ``ConversationSession``, so the double answered ``project_id=None`` from
the field default -- agreeing, by construction, with the projection that had
dropped the column.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from agent_workbench.ports.conversation_store import ConversationSession
from agent_workbench.ports.projects import (
    ProjectContents,
    ProjectItem,
    ProjectItemKind,
    ProjectRecord,
)

#: The kinds whose row is a ``conversation_sessions`` row (ADR-047).
_SESSION_KINDS: frozenset[ProjectItemKind] = frozenset({"chat", "code"})


class SessionRows(Protocol):
    """The session rows this store files under projects but does not own.

    Two methods, one per thing ``ProjectStore`` does to that table: read the
    rows filed under a project, and write the column. Structural rather than an
    import of ``InMemoryConversationStore`` so the dependency between two
    doubles is a stated contract instead of a direct reach into a sibling
    adapter.
    """

    async def sessions_in_project(
        self, *, tenant_id: str, owner_id: str, project_id: str
    ) -> tuple[ConversationSession, ...]: ...

    async def file_under_project(
        self,
        *,
        session_id: str,
        tenant_id: str,
        owner_id: str,
        project_id: str | None,
    ) -> bool: ...


@dataclass(slots=True)
class _Member:
    """One row another store owns, as much of it as membership needs."""

    owner_id: str
    kind: ProjectItemKind
    title: str | None
    ordered_at: datetime
    #: Read and written only for members whose row lives nowhere else -- Tasks
    #: always, sessions only while no ``sessions=`` store was given. When one
    #: was, the session object holds the membership and this field is dead for
    #: chat and code kinds: not a stale copy of it, no copy at all.
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
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sessions: SessionRows | None = None,
    ) -> None:
        self._sessions = sessions
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
        """Stand in for a session row, for a store given no ``sessions=``.

        With one, this is not the way to make a session known: that store's
        ``create_session`` is, and a session remembered here is invisible to
        every session path below.
        """

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

        Answers for the rows *this* store stands in for. A wired store's
        sessions are the conversation store's, and outlive a deletion there.
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

    async def set_root_path(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        root_path: str | None,
    ) -> ProjectRecord | None:
        # Recorded as given. Whether the path exists, resolves inside itself, or
        # is a directory anybody should be handed is decided where a sandbox is
        # built over it -- this store has no filesystem to ask.
        return await self._update(
            tenant_id=tenant_id,
            owner_id=owner_id,
            project_id=project_id,
            change=lambda record: record.model_copy(update={"root_path": root_path}),
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
            # membership goes. Wired, the sessions are rows in another store
            # and are released there -- one release per fact, wherever the
            # fact is kept.
            if self._sessions is not None:
                for session in await self._sessions.sessions_in_project(
                    tenant_id=tenant_id, owner_id=owner_id, project_id=project_id
                ):
                    await self._sessions.file_under_project(
                        session_id=session.session_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        project_id=None,
                    )
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
            # Wired, a session remembered here is not a member -- the row that
            # decides that is in the other store, and this arm would answer
            # from a `project_id` nothing writes.
            and not (self._sessions is not None and member.kind in _SESSION_KINDS)
        ]
        if self._sessions is not None:
            for session in await self._sessions.sessions_in_project(
                tenant_id=tenant_id, owner_id=owner_id, project_id=project_id
            ):
                if session.last_activity_at is None:  # pragma: no cover - stamped
                    continue
                items.append(
                    ProjectItem(
                        # `mode` *is* the kind: chat and code are two products
                        # to a reader sharing one table (ADR-047), which is the
                        # same union the SQL builds from `mode` as a label.
                        kind=session.mode,
                        item_id=session.session_id,
                        title=session.title,
                        ordered_at=session.last_activity_at,
                    )
                )
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
        if self._sessions is None:
            return await self._assign(
                tenant_id=tenant_id,
                owner_id=owner_id,
                item_id=session_id,
                project_id=project_id,
                kinds=("chat", "code"),
            )
        # Same two steps as the SQL, in the same order: establish the project is
        # this person's -- the foreign key over there constrains tenant and
        # knows nothing about owner -- then one write carrying the same three
        # predicates, whose miss is `False` rather than an exception.
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
        return await self._sessions.file_under_project(
            session_id=session_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            project_id=project_id,
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


__all__ = ["InMemoryProjectStore", "SessionRows"]
