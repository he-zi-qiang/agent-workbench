"""The durable, user-supplied input to one Task.

Task input is an artifact rather than a ``task_runs`` JSON column: it is an
immutable request payload with its own schema boundary, while the registry
only needs the small stable reference that identifies which input a Task was
accepted with.  Keeping it separate also makes the write order explicit -- an
unreferenced input artifact is safe to clean up, but a Task never points to
bytes that were not stored successfully.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.schema import VersionedModel
from agent_workbench.domain.tasks import MAX_REVISIONS, TaskObjective


class TaskInput(VersionedModel):
    """The bounded input accepted for one general-purpose Task."""

    objective: TaskObjective
    max_revisions: int = Field(default=2, ge=0, le=MAX_REVISIONS)
    # Optional because a general-purpose Task may work only from a prompt or
    # from future non-knowledge tools. When present, later graph nodes use it
    # to select the knowledge scope; it is not a client-supplied ACL.
    knowledge_base_id: Identifier | None = None
    # Whether this Task is expected to produce a downloadable file. False means
    # the Task still runs to completion and still writes a draft -- it just
    # stops after the critic instead of asking a human to approve an export
    # nobody wanted. Default False, because a Task that was not asked for a
    # file should not interrupt somebody to authorize one.
    wants_report: bool = False

    def canonical_bytes(self) -> bytes:
        """Serialize the exact bytes that identify one input request.

        Pydantic's JSON representation is intentionally not used as an
        idempotency key by convention alone. Sorting keys and fixing separators
        makes the digest stable across retries and processes while retaining
        the explicit schema version in the payload.
        """

        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def fingerprint(self) -> str:
        """The SHA-256 identity submitted beside an artifact reference."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()


__all__ = ["TaskInput"]
