"""One piece of work, with a name, belonging to one person.

A project is a membership, not a container (ADR-071). The sidebar groups by
*product* -- chat, task, code, knowledge -- and people do not think in products.
They think in one piece of work: a quarterly review has three conversations, two
tasks and a coding session in it at once. The product answers "which tool is
this", a project answers "what was this for". Two axes, no repetition.

Three things worth knowing before reading further:

**Membership is nullable, and NULL is the normal state.** No migration invented a
project for anybody, and no endpoint refuses a conversation for not belonging to
one. Something with no membership is shown as having none.

**A project is not an ACL, in any part.** Filing a conversation under a project
does not make it visible to anybody else, and taking it out does not make it
invisible to you. Every read and write here is scoped by ``(tenant_id,
owner_id)``, with no second path, no membership table and no permission bit.

**Deleting a project does not delete what is in it.** ON DELETE SET NULL: the
label goes, the thing it labelled stays. That conversation holds questions
somebody asked; that task produced a file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import StringConstraints

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import VersionedModel

#: A project's name. Stripped by the type, so a blank one is refused here rather
#: than becoming an invisible row in a list.
ProjectName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]

#: What a member is. ``chat`` and ``code`` are separate because they are two
#: products to a reader, even though they share ``conversation_sessions``
#: (ADR-047).
ProjectItemKind = Literal["chat", "code", "task", "knowledge_base"]


class ProjectRecord(VersionedModel):
    """The durable identity of one project."""

    project_id: Identifier
    tenant_id: Identifier
    owner_id: Identifier
    name: ProjectName
    created_at: datetime
    updated_at: datetime
    #: Archiving is not a soft delete: an archived project leaves the sidebar but
    #: stays readable, its deep link still opens, and its members keep their
    #: ``project_id`` (ADR-071 2.5).
    archived_at: datetime | None = None
    #: The directory this project *is*, on the machine the API runs on
    #: (ADR-072). ``None`` is the normal state and always was: a project without
    #: one behaves exactly as every project did before the column existed.
    #:
    #: Stored as the caller registered it, not resolved. Resolution is what
    #: ``ProjectSandbox`` does at construction, and doing it here would put a
    #: second, staler answer in the database -- the symlink a path went through
    #: can be repointed, and a resolved copy in a row would then disagree with
    #: the disk while looking authoritative.
    #:
    #: Not validated by this type either. A ``StringConstraints`` pattern could
    #: only check spelling, and spelling is the half of the rules that does not
    #: keep anybody safe; the half that does needs the filesystem. A constraint
    #: here would read as a guarantee it cannot give.
    root_path: str | None = None


class ProjectItem(VersionedModel):
    """One member, in enough detail to draw a row and no more."""

    kind: ProjectItemKind
    item_id: Identifier
    #: A session is named by its first instruction (ADR-047), and one that has
    #: been opened but not spoken in has no name. Left ``None`` rather than
    #: invented from the id.
    title: str | None = None
    #: What this row sorts by: last activity for a session, creation for a task,
    #: link time for a knowledge base. The name says "this is what orders it",
    #: not "this is when it happened" -- each product's own endpoint answers the
    #: second question properly.
    ordered_at: datetime


class ProjectContents(VersionedModel):
    """Everything filed under one project."""

    project_id: Identifier
    items: tuple[ProjectItem, ...] = ()


@runtime_checkable
class ProjectStore(Protocol):
    """Persist projects, and project membership into a readable list."""

    async def create(self, record: ProjectRecord) -> ProjectRecord:
        """Persist one new project."""

        ...

    async def get(
        self, *, tenant_id: str, owner_id: str, project_id: str
    ) -> ProjectRecord | None:
        """Read one project, owner-scoped, or ``None``.

        An archived project *is* returned here: archiving affects the list, not
        the deep link.
        """

        ...

    async def list_for_owner(
        self, *, tenant_id: str, owner_id: str, include_archived: bool = False
    ) -> tuple[ProjectRecord, ...]:
        """This person's projects, most recently touched first."""

        ...

    async def rename(
        self, *, tenant_id: str, owner_id: str, project_id: str, name: str
    ) -> ProjectRecord | None:
        """Rename, returning the record as it now stands, or ``None``."""

        ...

    async def set_archived(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        archived: bool,
    ) -> ProjectRecord | None:
        """Archive or unarchive, returning the record as it now stands."""

        ...

    async def set_root_path(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        root_path: str | None,
    ) -> ProjectRecord | None:
        """Register the directory this project is, or (``None``) unregister it.

        ``None`` means *no directory*, and it is a distinct thing from *the
        field was not sent* -- the same distinction ``assign_session`` draws and
        for the same reason: without it, "stop pointing this project at that
        folder" has no way of being said (ADR-071 §4).

        Unregistering removes the project's access to the tree and touches not
        one file in it. That asymmetry is deliberate and matches ``delete``:
        this store manages labels and pointers, and nothing it offers deletes
        somebody's work.

        Whether the path is a directory an agent may be handed is **not** decided
        here. This method records what it was told; the check that the path
        exists and resolves inside itself happens where a sandbox is built over
        it, because that is the only place holding the filesystem it is a claim
        about.
        """

        ...

    async def delete(self, *, tenant_id: str, owner_id: str, project_id: str) -> bool:
        """Delete the project itself and *release* everything filed under it.

        Returns whether a row was actually removed. Not one conversation, task
        or knowledge base disappears -- their ``project_id`` becomes ``NULL``,
        and that is all (ADR-071 2.2).
        """

        ...

    async def contents(
        self, *, tenant_id: str, owner_id: str, project_id: str
    ) -> ProjectContents:
        """The conversations, coding sessions, tasks and bases filed here."""

        ...

    async def assign_session(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        session_id: str,
        project_id: str | None,
    ) -> bool:
        """File a session under a project, or (``project_id=None``) take it out.

        Returns whether a row was changed. ``None`` means *no membership*, which
        is a different thing from *the field was not sent* -- and one PATCH has
        to be able to express both, or "take it out of the project" has no way
        of being said at all (ADR-071 4).
        """

        ...

    async def assign_task(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        task_id: str,
        project_id: str | None,
    ) -> bool:
        """File a task, or take it out. Same semantics as ``assign_session``."""

        ...

    async def link_knowledge_base(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        knowledge_base_id: str,
    ) -> bool:
        """Link a knowledge base. Linking twice is idempotent, not an error."""

        ...

    async def unlink_knowledge_base(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        project_id: str,
        knowledge_base_id: str,
    ) -> bool:
        """Unlink one. The knowledge base itself is untouched."""

        ...


__all__ = [
    "ProjectContents",
    "ProjectItem",
    "ProjectItemKind",
    "ProjectName",
    "ProjectRecord",
    "ProjectStore",
]
