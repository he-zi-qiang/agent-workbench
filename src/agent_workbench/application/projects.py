"""Create, name and file things under a project, as the caller.

The store already scopes every statement by ``(tenant_id, owner_id)``. This
layer owns the two things above that: minting ids, and turning "no such row" into
the refusal an interface can act on.

It deliberately owns no authorization rule of its own. A project is not an ACL
(ADR-071 2.4): there is nothing here to decide beyond whether the row is this
person's, and the store answers that in SQL rather than trusting a caller to
have asked nicely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_workbench.domain.errors import NotFoundError
from agent_workbench.domain.identifiers import new_id
from agent_workbench.domain.policies import PrincipalContext
from agent_workbench.ports.project_files import (
    ProjectFileStore,
    ProjectFileStoreFactory,
)
from agent_workbench.ports.projects import (
    ProjectContents,
    ProjectRecord,
    ProjectStore,
)

PROJECT_ID_PREFIX = "prj"


@dataclass(frozen=True, slots=True)
class ProjectService:
    """Own project identity; leave membership semantics to the store."""

    store: ProjectStore
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    #: Absent where the deployment serves no project directories (ADR-072).
    #: ``None`` rather than a no-op double: a build without the capability must
    #: refuse the request rather than accept a root path and then quietly do
    #: nothing with it, which is the failure shape `--web-dir` and
    #: `--without-chat` are both written to avoid.
    files: ProjectFileStoreFactory | None = None

    async def create(
        self,
        principal: PrincipalContext,
        *,
        name: str,
        project_id: str | None = None,
    ) -> ProjectRecord:
        now = self.clock()
        record = ProjectRecord(
            project_id=project_id or new_id(PROJECT_ID_PREFIX),
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            name=name,
            created_at=now,
            updated_at=now,
        )
        return await self.store.create(record)

    async def get(self, principal: PrincipalContext, project_id: str) -> ProjectRecord:
        record = await self.store.get(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
        )
        if record is None:
            raise NotFoundError(f"project {project_id!r} is not readable")
        return record

    async def list(
        self, principal: PrincipalContext, *, include_archived: bool = False
    ) -> tuple[ProjectRecord, ...]:
        return await self.store.list_for_owner(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            include_archived=include_archived,
        )

    async def rename(
        self, principal: PrincipalContext, project_id: str, *, name: str
    ) -> ProjectRecord:
        record = await self.store.rename(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
            name=name,
        )
        if record is None:
            raise NotFoundError(f"project {project_id!r} is not readable")
        return record

    async def set_root_path(
        self, principal: PrincipalContext, project_id: str, *, root_path: str | None
    ) -> ProjectRecord:
        """Register the directory this project is, or clear it.

        The path is *checked before it is stored*, by building a store over it
        and discarding that store. Storing first and validating on first use
        would put a root nobody can open into the database, and the person who
        mistyped it would learn about it from an agent's error days later --
        the same argument ``resolve_web_directory`` makes for checking the
        console directory at startup.

        Clearing skips the check, and must: the whole reason to clear a root is
        often that it has become unopenable -- the directory was moved, the
        volume unmounted -- and a validation on the way out would trap the
        project pointing at something it can no longer reach.
        """

        if root_path is not None:
            if self.files is None:
                raise NotFoundError(
                    "this deployment does not serve project directories"
                )
            # Built and dropped. The store is not kept because the next request
            # will build its own: a cached one would hold a root resolved at
            # registration time, and the answer to "where does this project
            # point" would then be two different things depending on which code
            # path asked.
            self.files.open(root_path)
        record = await self.store.set_root_path(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
            root_path=root_path,
        )
        if record is None:
            raise NotFoundError(f"project {project_id!r} is not readable")
        return record

    async def open_files(
        self, principal: PrincipalContext, project_id: str
    ) -> ProjectFileStore:
        """A store over this project's directory.

        Reads the project first, which is what makes this owner-scoped: the root
        path comes out of a row the store already refused to hand to anybody
        else. A version that took a path from the request would be an open
        directory-read endpoint with a project id decorating it.
        """

        record = await self.get(principal, project_id)
        if record.root_path is None:
            raise NotFoundError(f"project {project_id!r} has no directory")
        if self.files is None:
            raise NotFoundError("this deployment does not serve project directories")
        return self.files.open(record.root_path)

    async def set_archived(
        self, principal: PrincipalContext, project_id: str, *, archived: bool
    ) -> ProjectRecord:
        record = await self.store.set_archived(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
            archived=archived,
        )
        if record is None:
            raise NotFoundError(f"project {project_id!r} is not readable")
        return record

    async def delete(self, principal: PrincipalContext, project_id: str) -> None:
        """Delete the project. Everything filed under it is released, not removed."""

        removed = await self.store.delete(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
        )
        if not removed:
            raise NotFoundError(f"project {project_id!r} is not readable")

    async def contents(
        self, principal: PrincipalContext, project_id: str
    ) -> ProjectContents:
        # Read the project first so a missing one is a 404 rather than an empty
        # list -- "this project has nothing in it" and "there is no such
        # project" are different answers and an interface draws them differently.
        await self.get(principal, project_id)
        return await self.store.contents(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
        )

    async def assign_session(
        self, principal: PrincipalContext, session_id: str, *, project_id: str | None
    ) -> None:
        changed = await self.store.assign_session(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            session_id=session_id,
            project_id=project_id,
        )
        if not changed:
            # One refusal for both causes, and deliberately so: telling a caller
            # apart "that session is not yours" from "that project is not yours"
            # would answer a question about somebody else's data.
            raise NotFoundError(f"session {session_id!r} cannot be filed there")

    async def assign_task(
        self, principal: PrincipalContext, task_id: str, *, project_id: str | None
    ) -> None:
        changed = await self.store.assign_task(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            task_id=task_id,
            project_id=project_id,
        )
        if not changed:
            raise NotFoundError(f"task {task_id!r} cannot be filed there")

    async def link_knowledge_base(
        self, principal: PrincipalContext, project_id: str, *, knowledge_base_id: str
    ) -> None:
        linked = await self.store.link_knowledge_base(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        if not linked:
            raise NotFoundError(f"project {project_id!r} is not readable")

    async def unlink_knowledge_base(
        self, principal: PrincipalContext, project_id: str, *, knowledge_base_id: str
    ) -> None:
        unlinked = await self.store.unlink_knowledge_base(
            tenant_id=principal.tenant_id,
            owner_id=principal.principal_id,
            project_id=project_id,
            knowledge_base_id=knowledge_base_id,
        )
        if not unlinked:
            raise NotFoundError(
                f"knowledge base {knowledge_base_id!r} is not linked there"
            )


__all__ = ["PROJECT_ID_PREFIX", "ProjectService"]
