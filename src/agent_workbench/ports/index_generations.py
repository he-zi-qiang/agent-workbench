"""Framework-neutral boundary for a Qdrant index generation's later life.

A generation is created when an index is built and reserved when a Task is
submitted against it. This is the other end: retiring one so it stops taking
reservations, releasing a finished Task's hold, and deleting what nothing needs
any more.

Release is a separate step from reaching a terminal status on purpose. The
architecture baseline says a terminal Task must not block collection
reclamation, and the foreign key from ``task_runs`` blocks deletion
unconditionally -- so something has to let go, explicitly, and be seen to. What
is released is only the *reservation*: which concrete index the Task ran against
survives inside its own semantics snapshot, so the audit does not depend on the
generation row still existing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_workbench.domain.identifiers import Identifier


class GenerationStillReferencedError(RuntimeError):
    """Deletion was asked for while a Task still holds the generation.

    Carries the count rather than the identities: the caller is a collector
    deciding whether to move on, and enumerating which Tasks hold an index is a
    different question with a different authorization boundary.
    """

    def __init__(self, *, generation_id: str, references: int) -> None:
        self.generation_id = generation_id
        self.references = references
        super().__init__(
            f"index generation {generation_id} is still held by {references} task(s)"
        )


@runtime_checkable
class IndexGenerationStore(Protocol):
    """Retire, release and collect concrete index generations."""

    async def retire(self, generation_id: str) -> None:
        """Stop the generation taking new reservations.

        Existing reservations stay valid -- that is the difference between
        retiring an index and deleting it, and it is what lets an alias switch
        drain rather than cut.
        """
        ...

    async def release(self, task_id: Identifier) -> bool:
        """Drop a terminal Task's hold on its generation.

        Returns whether anything was released, so a sweep can tell "let go" from
        "had nothing to let go of" without a second read. Refuses a Task that is
        not terminal: a running Task's reservation is the only thing standing
        between it and an index disappearing mid-run.
        """
        ...

    async def collect(self, generation_id: str) -> None:
        """Delete a retired generation that nothing references.

        Raises ``GenerationStillReferencedError`` rather than deleting when a
        Task still holds it, so a collector that has not released everything
        first fails loudly instead of relying on the foreign key to refuse it
        with a constraint name.
        """
        ...


__all__ = ["GenerationStillReferencedError", "IndexGenerationStore"]
