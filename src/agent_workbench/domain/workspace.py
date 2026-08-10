"""A Task's working set: mutable names over immutable bytes (ADR-028).

An artifact is written once and named by its content. That is what makes a
tool result safe to reference from an event and a checkpoint, and it is not
negotiable. But an agent that is *working* needs the opposite property: it puts
something down, picks it up two steps later, and replaces it with a better
version.

Both are available at once by moving the mutability up one layer. A workspace
is a mapping from a name to an ``ArtifactRef``; writing a name stores new bytes
and produces a *new manifest*. The bytes never change and nothing is deleted --
what changes is which bytes a name points at. Git resolves the same tension the
same way, and for the same reason: a tree that can be rewritten on top of blobs
that cannot is what lets history stay checkable while work stays editable.

The manifest is itself stored as an artifact, so "which version of the
workspace" is one identifier a checkpoint can hold. A node reads the version
pinned at its entry and a replay therefore sees exactly what the first attempt
saw -- not the half-finished writes of the attempt that died.

Names are names, not paths. ``ArtifactRef`` refuses to carry a filesystem path
because "a client-supplied path is exactly how path traversal and cross-tenant
reads enter a system", and re-introducing one here as a *key* would give that
back. There are no directories; a model that wants hierarchy writes
``draft-v2.md``.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field, StringConstraints

from agent_workbench.domain.artifacts import ArtifactRef
from agent_workbench.domain.schema import VersionedModel

#: Flat, printable, no separator of any kind. The character class excludes ``/``
#: and ``\`` so a path cannot be spelled, and requires an alphanumeric first
#: character so ``.`` and ``..`` are not names.
WorkspaceName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$"),
]

#: Ceilings, as constants rather than configuration. They bound a checkpoint
#: row and a model's listing at the same time, and a deployment that could
#: raise them would be one where those two things are unbounded.
MAX_WORKSPACE_ENTRIES: Final[int] = 256
MAX_WORKSPACE_TOTAL_BYTES: Final[int] = 64 * 1024 * 1024

#: The names these tools are known by. They live in the domain rather than
#: beside the handlers because both an agent profile and the authorization
#: envelope have to name them, and neither may import an adapter.
WORKSPACE_LIST_TOOL: Final[str] = "workspace_list"
WORKSPACE_READ_TOOL: Final[str] = "workspace_read"
WORKSPACE_WRITE_TOOL: Final[str] = "workspace_write"
WORKSPACE_EDIT_TOOL: Final[str] = "workspace_edit"
#: What a principal must hold before the write tool may be dispatched. Shared
#: with the edit tool: both replace what a name points at, and a permission
#: that let you rewrite a file only if you rewrote all of it would be a
#: distinction nobody could act on.
WORKSPACE_WRITE_SCOPE: Final[str] = "workspace:write"


class WorkspaceOverflowError(ValueError):
    """A write would take the workspace past a ceiling.

    Raised rather than returned, and never partially applied: the manifest the
    caller already holds is unchanged, so a refused write leaves no trace to
    reconcile.
    """


class WorkspaceEditMatchError(ValueError):
    """``old_text`` did not occur in the file exactly once.

    Both directions are errors, and neither is recoverable by guessing
    (ADR-030 §2.3). Zero matches means the model believes the file says
    something it does not -- editing anything would be editing a file it has
    not read. More than one means the model believes there is one occurrence
    and there is not; "the first one" is a position it never chose, and an edit
    applied there is silent corruption that leaves no trace to find later.
    """

    def __init__(self, *, name: str, matches: int) -> None:
        self.name = name
        self.matches = matches
        super().__init__(
            f"old_text occurs {matches} times in {name!r}, and an edit needs "
            "exactly one occurrence"
            + (
                "; include more surrounding text to identify which one"
                if matches > 1
                else "; read the file and copy the text to replace from it"
            )
        )


def replace_exactly_once(text: str, old: str, new: str, *, name: str) -> str:
    """``old`` -> ``new`` in ``text``, or refuse.

    Placed in the domain rather than beside the tool handler because the rule
    -- not the plumbing -- is the decision ADR-030 recorded, and because a
    second caller (a future v2 node, an editor over a different store) must not
    get to re-answer it.

    An empty ``old`` is refused by the same count: it "occurs" between every
    pair of characters, so ``str.count`` reports ``len(text) + 1`` and the edit
    is rejected as ambiguous rather than inserting at a position nobody named.
    """

    matches = text.count(old)
    if matches != 1:
        raise WorkspaceEditMatchError(name=name, matches=matches)
    return text.replace(old, new, 1)


class WorkspaceManifest(VersionedModel):
    """One version of a Task's working set.

    Immutable, like every other domain object here. :meth:`with_entry` returns
    the next version instead of mutating this one, which is what lets a node
    hold its entry version while producing later ones.
    """

    entries: dict[WorkspaceName, ArtifactRef] = Field(
        default_factory=dict,
        max_length=MAX_WORKSPACE_ENTRIES,
    )

    @property
    def total_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.entries.values())

    def names(self) -> tuple[str, ...]:
        """Every name, sorted.

        Sorted rather than insertion-ordered because a listing is shown to a
        model and recorded in events: two runs that wrote the same files in a
        different order should not read as two different workspaces.
        """

        return tuple(sorted(self.entries))

    def with_entry(self, name: str, ref: ArtifactRef) -> WorkspaceManifest:
        """Return the next version, with ``name`` bound to ``ref``.

        Replacing an existing name is not growth: neither ceiling counts the
        bytes or the slot it is giving up. Without that, a workspace that once
        held a large file could never be written to again even after the file
        was replaced -- the budget would ratchet rather than track.
        """

        replaced = self.entries.get(name)
        if replaced is None and len(self.entries) >= MAX_WORKSPACE_ENTRIES:
            raise WorkspaceOverflowError(
                f"workspace already holds {MAX_WORKSPACE_ENTRIES} entries"
            )

        freed = replaced.size_bytes if replaced is not None else 0
        if self.total_bytes - freed + ref.size_bytes > MAX_WORKSPACE_TOTAL_BYTES:
            raise WorkspaceOverflowError(
                f"workspace would exceed {MAX_WORKSPACE_TOTAL_BYTES} bytes"
            )

        return self.model_copy(update={"entries": {**self.entries, name: ref}})


__all__ = [
    "MAX_WORKSPACE_ENTRIES",
    "MAX_WORKSPACE_TOTAL_BYTES",
    "WORKSPACE_EDIT_TOOL",
    "WORKSPACE_LIST_TOOL",
    "WORKSPACE_READ_TOOL",
    "WORKSPACE_WRITE_SCOPE",
    "WORKSPACE_WRITE_TOOL",
    "WorkspaceEditMatchError",
    "WorkspaceManifest",
    "WorkspaceName",
    "WorkspaceOverflowError",
    "replace_exactly_once",
]
