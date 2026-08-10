"""Who decided a Task's shape, recorded beside the Task rather than inside it.

ADR-036 lets a model propose the two submission-time decisions -- which graph
runs and whether the Task should end in a file -- so "what was decided" stops
being the whole story and "who decided it" becomes worth recording. This block
is that record. It is provenance, never authority: the binding facts stay
where they were (``task_runs.graph_version`` and ``TaskInput.wants_report``),
and nothing reads this block to run the Task.

Deliberately not part of :mod:`agent_workbench.domain.task_inputs`. The input
artifact's bytes participate in ``input_fingerprint``, whose canonical form
does not exclude defaults -- any field added there would change the recomputed
digest of every stored input and fail every existing Task's fingerprint check
on load. Provenance therefore travels with the *submission* and lands in the
``TaskSubmitted`` event, which is where "why is this Task shaped like this"
questions are already answered.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agent_workbench.domain.schema import DomainModel

#: Who made a submission-time decision. ``user`` is an explicit choice a human
#: made in a client; ``model`` is a triage verdict the client adopted; and
#: ``default`` is nobody -- the deployment's configured value applied because
#: triage was unavailable, timed out, or is disabled.
IntentDecidedBy = Literal["user", "model", "default"]


class TaskIntent(DomainModel):
    """The provenance of one submission's two shape decisions."""

    graph_decided_by: IntentDecidedBy
    wants_report_decided_by: IntentDecidedBy
    # The triage verdict's own words, shown on the timeline. Bounded because
    # it is caller-supplied text that every timeline reader will download.
    reason: str | None = Field(default=None, max_length=500)


__all__ = ["IntentDecidedBy", "TaskIntent"]
