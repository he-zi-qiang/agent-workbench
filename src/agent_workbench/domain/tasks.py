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
from agent_workbench.domain.schema import DomainModel, ShortText, VersionedModel
from agent_workbench.domain.task_registry import ApprovalDecision

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

TaskObjective = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
ReviewSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
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
    issues: tuple[ShortText, ...] = Field(default=(), max_length=32)
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
    review_result: ReviewResult | None = None
    approval_id: Identifier | None = None
    # Which way the graph went at the approval gate. This is *not* a second copy
    # of the ledger's answer: the ledger records what a human decided, and this
    # records what this thread routed on when it resumed, read from the ledger at
    # that moment. Routing is a pure function of state, so a decision the state
    # does not carry is one no edge can depend on.
    approval_decision: ApprovalDecision | None = None
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
        if self.approval_id is not None and (
            review is None or review.decision != "pass"
        ):
            raise ValueError("approval_id requires a passing review_result")
        if (self.approval_id is None) != (self.approval_decision is None):
            # Both or neither. An approval_id without a decision would be a gate
            # the graph walked past without an answer -- which is exactly what a
            # human approval exists to prevent -- and a decision without the
            # approval it answers names nothing an auditor can look up.
            raise ValueError("approval_id and approval_decision travel together")
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
        """Whether quality_gate may route back to synthesize."""

        return self.revision_count < self.max_revisions


__all__ = [
    "CANONICAL_V1_NODE_IDS",
    "MAX_PLAN_STEPS",
    "MAX_REVISIONS",
    "MAX_STATE_REFS",
    "ReviewDecision",
    "ReviewResult",
    "ReviewSummary",
    "TaskNodeId",
    "TaskObjective",
    "TaskState",
    "TaskStep",
]
