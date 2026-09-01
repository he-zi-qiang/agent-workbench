"""A Task in flight survives a change to the shape of its own state.

ADR-100. Before this, a checkpoint was written as LangGraph channels and read
back through `_to_state`, which validates it into `TaskState` -- a `DomainModel`,
and therefore `extra="forbid"`. Any field change to any model inside that state
made every checkpoint written before the change unloadable, and the Task holding
it unresumable. Nothing migrated it: the upcaster registry next door runs on
stored *event envelopes* during replay, and a checkpoint never reaches it.

The first thing this path carries is the removal of `TaskStep.depends_on`, which
is not a coincidence -- a migration mechanism with an empty registry is the
failure it is supposed to prevent, one layer up.
"""

from __future__ import annotations

import pytest

from agent_workbench.adapters.langgraph.checkpoint_migrations import (
    CHECKPOINT_SCHEMA_VERSION,
    CHECKPOINT_VERSION_CHANNEL,
    DEFAULT_CHECKPOINT_UPCASTERS,
    CheckpointMigrationError,
    CheckpointUpcasterRegistry,
)
from agent_workbench.adapters.langgraph.workflow import (
    _to_state,  # pyright: ignore[reportPrivateUsage]
)

#: One real plan step, copied from a checkpoint this repository's own dev
#: database was holding: 305 of these across 85 checkpoints, every one carrying
#: `depends_on`. Kept verbatim rather than invented, because what has to survive
#: is the bytes a previous deployment actually wrote.
REAL_PLAN_STEP = {
    "step_id": "step_46b94dafc2aa98dd323b5f5b2127ecec",
    "sequence": 1,
    "objective": "Produce a concise, evidence-oriented answer to the objective.",
    "depends_on": [],
}


def _checkpoint(**overrides: object) -> dict[str, object]:
    """A checkpoint as an older deployment wrote it: no version stamp at all."""

    return {
        "schema_version": 1,
        "task_id": "task_0000000000000000000000000000001",
        "objective": "Answer the question.",
        "plan": (REAL_PLAN_STEP,),
        **overrides,
    }


def test_a_checkpoint_written_before_the_removal_still_resumes() -> None:
    """The whole point: an in-flight Task is not stranded by a field change."""

    state = _to_state(_checkpoint())

    assert len(state.plan) == 1
    assert state.plan[0].step_id == REAL_PLAN_STEP["step_id"]
    assert not hasattr(state.plan[0], "depends_on")


def test_the_same_checkpoint_is_refused_without_the_migration() -> None:
    """The control. Without it the test above proves only that a dict parses.

    Feeds the identical payload straight to the model, which is what
    `_to_state` did before ADR-100.
    """

    from agent_workbench.domain.tasks import TaskState

    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        TaskState.model_validate(_checkpoint())


def test_an_unstamped_checkpoint_is_read_as_layout_one() -> None:
    """Absence is the version, not an error.

    Every checkpoint in existence when this shipped has no stamp, so a reader
    that treated the missing key as a fault would refuse all of them.
    """

    assert CHECKPOINT_VERSION_CHANNEL not in _checkpoint()

    state = _to_state(_checkpoint())

    assert state.task_id == "task_0000000000000000000000000000001"


def test_a_checkpoint_this_process_wrote_needs_no_migration() -> None:
    stamped = _checkpoint(
        plan=({"step_id": "s1", "sequence": 1, "objective": "x"},),
        **{CHECKPOINT_VERSION_CHANNEL: CHECKPOINT_SCHEMA_VERSION},
    )

    state = _to_state(stamped)

    assert len(state.plan) == 1


def test_a_checkpoint_from_a_newer_deployment_is_refused() -> None:
    """Fail closed. This process cannot know what a later layout removed."""

    with pytest.raises(CheckpointMigrationError, match="newer deployment"):
        _to_state(
            _checkpoint(**{CHECKPOINT_VERSION_CHANNEL: CHECKPOINT_SCHEMA_VERSION + 1})
        )


def test_a_gap_in_the_chain_is_named_rather_than_guessed_across() -> None:
    empty = CheckpointUpcasterRegistry()

    with pytest.raises(CheckpointMigrationError, match="has a gap"):
        empty.raise_to_current({"plan": ()}, from_version=1)


def test_the_chain_applies_one_step_at_a_time_and_owns_the_bump() -> None:
    """Each entry raises exactly one version; the loop counts, not the step.

    An upcaster responsible for its own stamp could forget it, and the loop
    would then apply the same step forever.
    """

    seen: list[int] = []
    registry = CheckpointUpcasterRegistry()

    def _step(index: int):
        def upcast(payload: dict[str, object]) -> dict[str, object]:
            seen.append(index)
            return payload

        return upcast

    for version in range(1, CHECKPOINT_SCHEMA_VERSION):
        registry.register(version, _step(version))  # pyright: ignore[reportArgumentType]

    registry.raise_to_current({}, from_version=1)

    assert seen == list(range(1, CHECKPOINT_SCHEMA_VERSION))


def test_a_duplicate_registration_is_refused() -> None:
    registry = CheckpointUpcasterRegistry()
    registry.register(1, lambda payload: payload)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(1, lambda payload: payload)


def test_a_step_may_not_claim_to_raise_past_the_current_layout() -> None:
    registry = CheckpointUpcasterRegistry()

    with pytest.raises(ValueError, match="must raise a version below"):
        registry.register(CHECKPOINT_SCHEMA_VERSION, lambda payload: payload)


def test_the_shipped_chain_reaches_the_current_layout_from_every_version() -> None:
    """No hole between 1 and today. A gap here is a Task nobody can resume."""

    for version in range(1, CHECKPOINT_SCHEMA_VERSION + 1):
        DEFAULT_CHECKPOINT_UPCASTERS.raise_to_current(
            {"plan": (REAL_PLAN_STEP,)}, from_version=version
        )


def test_a_plan_that_never_carried_the_field_passes_through_untouched() -> None:
    """Idempotence, which is what makes the step safe on any 1-era plan."""

    without = {"plan": ({"step_id": "s1", "sequence": 1, "objective": "x"},)}

    raised = DEFAULT_CHECKPOINT_UPCASTERS.raise_to_current(without, from_version=1)

    assert raised["plan"] == without["plan"]


def test_an_empty_plan_is_left_alone() -> None:
    payload = {"plan": (), "task_id": "task_1"}

    assert (
        DEFAULT_CHECKPOINT_UPCASTERS.raise_to_current(payload, from_version=1)
        == payload
    )
