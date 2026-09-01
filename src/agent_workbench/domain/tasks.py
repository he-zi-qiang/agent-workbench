"""Framework-neutral state carried by the fixed Task workflow.

The graph checkpoint is allowed to persist this module's values, so every
field is deliberately small and JSON serializable.  Execution position belongs
to the checkpointer, product status and graph version belong to the Task
Registry, and large inputs and outputs belong to their stores.  In particular,
this state must never grow a ``current_step``, a message transcript, document
contents, or a provider/framework object.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, model_validator

from agent_workbench.domain.identifiers import Identifier
from agent_workbench.domain.runs import BudgetUsage
from agent_workbench.domain.schema import DomainModel, VersionedModel
from agent_workbench.domain.task_registry import ApprovalDecision

#: Every node id either graph may sit on. One union rather than one per graph,
#: because a node id is durable checkpoint metadata and the recovery path reads
#: it without yet knowing which graph wrote it (ADR-031). Which subset is legal
#: is a property of a graph, and each graph declares its own tuple below.
TaskNodeId = Literal[
    "understand",
    "plan",
    "route",
    "research_internal",
    "research_external",
    "synthesize",
    "critic",
    "quality_gate",
    "approval",
    "export",
    # v2 only. `understand` and `export` are shared with v1 deliberately: they
    # mean the same thing in both graphs, and giving them separate names would
    # make an operator reading a timeline decide twice whether two identically
    # described nodes are the same step.
    "work",
    "review",
]

# Node ids are durable checkpoint metadata.  Their order is the canonical v1
# declaration order, not a claim that the graph is linear (the two research
# nodes fan out, and quality_gate may route back to synthesize).
CANONICAL_V1_NODE_IDS: Final[tuple[TaskNodeId, ...]] = (
    "understand",
    "plan",
    "route",
    "research_internal",
    "research_external",
    "synthesize",
    "critic",
    "quality_gate",
    "approval",
    "export",
)

#: v2's declaration order (ADR-031). Separate from the v1 tuple rather than
#: derived from it: the two graphs share three of four node ids and none of
#: their shape, and a tuple computed from the other would make a change to one
#: silently move the other.
#:
#: ``approval`` is in here for the same reason v1's routing nodes are: this
#: tuple is every node id a checkpoint of this graph may sit on, and a v2 thread
#: paused for a human sits on exactly that one. ADR-031 §2.1 counts four nodes
#: because four is the shape; the gate on the export path is the fifth thing a
#: recovery has to be able to name.
CANONICAL_V2_NODE_IDS: Final[tuple[TaskNodeId, ...]] = (
    "understand",
    "work",
    "review",
    "approval",
    "export",
)

TaskObjective = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
ReviewSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]

#: One thing the next attempt has to fix, in the reviewer's own words.
#:
#: A type of its own rather than ``ShortText``, which is what it used to be.
#: That type is 256 characters and every other thing wearing it is an
#: identifier -- ``graph_version``, ``model_id``, ``reason_code``,
#: ``profile_name``. This is the one place it was carrying free prose, and
#: prose that another agent then works from: the reviewer is told to name
#: something "specific enough to act on", and specific instructions about a
#: document do not fit in an identifier's budget.
#:
#: 512 is measured, not doubled for luck. Across the review outputs recorded on
#: this machine the issues ran 65 / 181 / 376 characters (shortest, median,
#: longest) -- a distribution already sitting at 71% of the old ceiling, where
#: one ordinary sentence of context pushed it over and failed the whole node.
#: This clears the longest observed by a third and stays an eighth of
#: ``ReviewSummary``, so "a short actionable item" is still what the shape says.
#:
#: The prompts quote 500 rather than 512, and 4000 rather than 4096. That is
#: deliberate: a model does not count characters, it aims at the number it was
#: given, so a round target with headroom under the real ceiling fails less
#: often than the exact boundary would. The limit enforced here is the true
#: one; the prompt states a goal.
ReviewIssue = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
ReviewDecision = Literal["pass", "revise"]

MAX_PLAN_STEPS: Final[int] = 64
MAX_STATE_REFS: Final[int] = 256
MAX_REVISIONS: Final[int] = 20


class TaskStep(DomainModel):
    """One stable item in the plan, without transient execution status."""

    step_id: Identifier
    sequence: int = Field(ge=1, le=MAX_PLAN_STEPS)
    objective: TaskObjective
    #: **Recorded and validated; never executed.** Stated plainly because the
    #: four checks below, plus `TaskPlan`'s "only preceding steps" rule, plus
    #: the planner contract that asks the model for this field, together read
    #: like a dependency graph something walks. Nothing walks it: the plan is
    #: rendered to later prompts as a flat numbered list
    #: (`agent_profiles.py`), and the research graph's shape is frozen at
    #: submission -- it does not branch per step. **The plan is advice to the
    #: model, not a DAG this system schedules.**
    #:
    #: So why is it still here. Deleting it is not free: `TaskState.plan` is a
    #: `tuple[TaskStep, ...]` reconstructed from the checkpointed graph channel
    #: by `_to_state`, and `DomainModel` is `extra="forbid"` -- so a checkpoint
    #: written before the removal stops loading, and every Task in flight at
    #: deploy time fails to resume. Measured, not assumed: feeding `_to_state`
    #: a plan dict with one unknown key raises `extra_forbidden`.
    #:
    #: **And nothing in this repository would fix that** -- which is the part
    #: worth reading twice. The upcaster registry is applied by
    #: `PostgresEventLog` to stored *event envelopes* during replay; a
    #: checkpoint never goes through it, so known gap B-05 is a different path
    #: and does not gate this. Bumping `schema_version` makes it worse rather
    #: than better: `VersionedModel` fails closed on a version it does not
    #: recognise, so an old checkpoint is then refused outright.
    #:
    #: **Removal is gated on B-12**, the absence of any migration path for
    #: checkpointed graph state. Until then the honest thing is this paragraph:
    #: the validation is real, and what it validates is a claim the model made,
    #: not an order anything follows.
    depends_on: tuple[Identifier, ...] = Field(default=(), max_length=MAX_PLAN_STEPS)

    @model_validator(mode="after")
    def validate_dependencies(self) -> TaskStep:
        if self.step_id in self.depends_on:
            raise ValueError("a task step cannot depend on itself")
        if tuple(sorted(self.depends_on)) != self.depends_on:
            raise ValueError("depends_on must be sorted")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on must not contain duplicate step ids")
        return self


class ReviewResult(DomainModel):
    """The critic's bounded decision about one exact draft revision."""

    decision: ReviewDecision
    reviewed_draft_ref: Identifier
    revision_number: int = Field(ge=0, le=MAX_REVISIONS)
    summary: ReviewSummary
    issues: tuple[ReviewIssue, ...] = Field(default=(), max_length=32)
    score: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_decision(self) -> ReviewResult:
        if self.decision == "revise" and not self.issues:
            raise ValueError("a revise decision must identify at least one issue")
        if len(set(self.issues)) != len(self.issues):
            raise ValueError("review issues must not repeat")
        return self


class TaskState(VersionedModel):
    """Checkpoint-safe business state for one Task graph.

    Tuple fields use canonical ordering so parallel fan-in has one deterministic
    representation.  Reducers must sort before constructing the next state;
    silently sorting or de-duplicating here would hide a broken reducer.
    """

    task_id: Identifier
    objective: TaskObjective
    knowledge_base_id: Identifier | None = None
    plan: tuple[TaskStep, ...] = Field(default=(), max_length=MAX_PLAN_STEPS)
    evidence_refs: tuple[Identifier, ...] = Field(
        default=(),
        max_length=MAX_STATE_REFS,
    )
    draft_ref: Identifier | None = None
    # Which version of this Task's working set (ADR-028). One identifier stands
    # for the whole set because it names a stored manifest, and it lives in the
    # checkpoint for the property the design exists for: a node reads the
    # version pinned at its entry, so the attempt replacing one that died
    # mid-write sees the same inputs the first attempt saw rather than its
    # half-finished output. `None` is where every Task starts.
    workspace_version: Identifier | None = None
    review_result: ReviewResult | None = None
    # Whether this Task was asked for a downloadable file, copied from its
    # TaskInput at load. **Defaults True, unlike TaskInput's own default**, and
    # the asymmetry is deliberate: a checkpoint written before this field
    # existed has no value for it, and every such Task was submitted under a
    # graph that always exported. Defaulting False here would silently change
    # what an in-flight Task does when it resumes.
    wants_report: bool = True
    # Whether this Task's export waits for a human (ADR-031 §2.4 made it always
    # do). Copied from configuration at load, then frozen here for the same
    # reason `wants_report` is: routing is a pure function of state, so a Task
    # in flight keeps the gate it was submitted under even if the deployment's
    # setting changes underneath it -- a checkpoint that consulted live config
    # would resume onto a different graph than the one it paused in.
    #
    # **Defaults True**, matching both the shipped configuration and every
    # checkpoint written before this field existed. What the gate is *for* is
    # narrower than it looks, which is why it can be turned off at all: export
    # writes a versioned artifact into this Task's own store, and nothing
    # leaves the deployment until a human clicks download. The gate guards a
    # file appearing in a list, not a side effect anyone outside can see.
    export_requires_approval: bool = True
    approval_id: Identifier | None = None
    # Which way the graph went at the approval gate. This is *not* a second copy
    # of the ledger's answer: the ledger records what a human decided, and this
    # records what this thread routed on when it resumed, read from the ledger at
    # that moment. Routing is a pure function of state, so a decision the state
    # does not carry is one no edge can depend on.
    approval_decision: ApprovalDecision | None = None
    # The report the export node produced. A reference, like everything else
    # here: the bytes are in the artifact store, and the ledger -- not this
    # field -- is what makes the export happen once.
    export_ref: Identifier | None = None
    agent_outcome_refs: tuple[Identifier, ...] = Field(
        default=(),
        max_length=MAX_STATE_REFS,
    )
    budget_usage: BudgetUsage = BudgetUsage()
    revision_count: int = Field(default=0, ge=0, le=MAX_REVISIONS)
    max_revisions: int = Field(default=2, ge=0, le=MAX_REVISIONS)

    @model_validator(mode="after")
    def validate_checkpoint_state(self) -> TaskState:
        self._validate_plan()
        self._validate_refs("evidence_refs", self.evidence_refs)
        self._validate_refs("agent_outcome_refs", self.agent_outcome_refs)

        if self.revision_count > self.max_revisions:
            raise ValueError("revision_count must not exceed max_revisions")

        review = self.review_result
        if review is not None:
            if self.draft_ref is None:
                raise ValueError("a review result requires a draft_ref")
            if review.reviewed_draft_ref != self.draft_ref:
                raise ValueError("review_result must refer to the current draft_ref")
            if review.revision_number != self.revision_count:
                raise ValueError("review revision_number must equal revision_count")
        if self.approval_id is not None and review is None:
            # A review, not a *passing* review, since ADR-060: an exhausted
            # reviewer's verdict stands recorded and the draft goes to the gate
            # with it, so the human decides about the work as reviewed. What
            # stays unrepresentable is a gate with no review at all -- an
            # approval about a draft nobody examined.
            raise ValueError("approval_id requires a review_result")
        if (self.approval_id is None) != (self.approval_decision is None):
            # Both or neither. An approval_id without a decision would be a gate
            # the graph walked past without an answer -- which is exactly what a
            # human approval exists to prevent -- and a decision without the
            # approval it answers names nothing an auditor can look up.
            raise ValueError("approval_id and approval_decision travel together")
        if (
            self.export_ref is not None
            and self.export_requires_approval
            and self.approval_decision != "approved"
        ):
            # The gate is the point of the gate -- *where there is one*. A
            # state carrying an export nobody approved would otherwise be
            # reachable only by a graph that walked past its own interrupt,
            # and this is the cheapest place to say so: the checkpoint that
            # recorded it would not load.
            #
            # Conditioned on the Task's own frozen `export_requires_approval`
            # rather than dropped. A deployment that runs without the gate has
            # no approval to point at, so demanding one would make its exports
            # unrepresentable; a deployment that runs *with* it keeps exactly
            # the invariant it had, on the value that was fixed when the Task
            # was submitted rather than on live configuration.
            raise ValueError("export_ref requires an approved approval_decision")
        return self

    def _validate_plan(self) -> None:
        sequences = tuple(step.sequence for step in self.plan)
        if sequences != tuple(range(1, len(self.plan) + 1)):
            raise ValueError(
                "plan steps must be sorted with contiguous sequence numbers"
            )

        step_ids = tuple(step.step_id for step in self.plan)
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("plan step ids must be unique")

        seen: set[str] = set()
        for step in self.plan:
            unknown = set(step.depends_on) - seen
            if unknown:
                raise ValueError(
                    "step dependencies must refer only to preceding plan steps"
                )
            seen.add(step.step_id)

    @staticmethod
    def _validate_refs(name: str, refs: tuple[str, ...]) -> None:
        if refs != tuple(sorted(refs)):
            raise ValueError(f"{name} must be sorted")
        if len(set(refs)) != len(refs):
            raise ValueError(f"{name} must not contain duplicate references")

    @property
    def can_revise(self) -> bool:
        """Whether a reviewing node may send the work back one more time.

        One budget for both graphs: v1's ``quality_gate -> synthesize`` and
        v2's ``review -> work`` ask the same question, and two counters would
        be two places to change it (ADR-031 §2.1).
        """

        return self.revision_count < self.max_revisions

    @property
    def unresolved_review(self) -> ReviewResult | None:
        """The review this Task ships with unanswered, if there is one.

        Non-``None`` exactly when the reviewer asked for changes and the
        revision budget cannot pay for them (ADR-060): the verdict stands
        recorded, the work proceeds, and whoever reads the result is owed the
        list of what the reviewer still wanted. One property rather than the
        same three-clause test in both graphs and the adapter.
        """

        review = self.review_result
        if review is None or review.decision != "revise" or self.can_revise:
            return None
        return review


__all__ = [
    "CANONICAL_V1_NODE_IDS",
    "CANONICAL_V2_NODE_IDS",
    "MAX_PLAN_STEPS",
    "MAX_REVISIONS",
    "MAX_STATE_REFS",
    "ReviewDecision",
    "ReviewIssue",
    "ReviewResult",
    "ReviewSummary",
    "TaskNodeId",
    "TaskObjective",
    "TaskState",
    "TaskStep",
]
