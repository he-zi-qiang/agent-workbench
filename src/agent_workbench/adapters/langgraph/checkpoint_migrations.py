"""Raising a checkpointed graph state written by an older layout to this one.

A Task's position lives in two stores and only one of them could be migrated.
Events are written as envelopes and replayed through
``EventUpcasterRegistry``; a **checkpoint** is written as LangGraph channels and
read back by ``_to_state``, which validates it into ``TaskState`` -- a
``DomainModel``, and therefore ``extra="forbid"``. Until this module existed,
changing any field of any model inside ``TaskState`` made every checkpoint
written before the change unloadable, and the Task holding it unresumable.

**The version here is deliberately not ``DOMAIN_SCHEMA_VERSION``.** That one is
global to every ``VersionedModel``: bumping it to say "the plan shape changed"
would also mean every stored *event* now claims a version its reader refuses,
and the event registry that would rescue them is empty. One axis cannot carry
two independent histories. So a checkpoint versions its own layout, and events
keep theirs.

Two refusals, both deliberate, and both are the honest answer rather than a
best-effort parse:

* a version **above** the current one is refused -- it was written by a newer
  deployment, and this process cannot know what it removed;
* a version with **no registered step** is refused by name. A gap in the chain
  is a migration somebody forgot to write, and guessing across it would hand
  validation a payload claiming a shape it does not have.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, cast

#: The layout every checkpoint this process writes is stamped with.
#:
#: **1 means "written before this field existed"**, which is what every
#: checkpoint in existence when this shipped carries -- the key is simply
#: absent, and absence reads as 1. There is no version 1 stamp anywhere.
CHECKPOINT_SCHEMA_VERSION: Final[int] = 2

#: The channel carrying it. Not a ``TaskState`` field: the domain does not know
#: that it is checkpointed, and a field on the model would be a framework
#: detail inside the contract. ``_to_state`` strips it before validating, the
#: same way it strips LangGraph's own ``__``-prefixed channels.
CHECKPOINT_VERSION_CHANNEL: Final[str] = "checkpoint_schema_version"

#: One step. Takes the channel mapping as written, returns it one version newer.
CheckpointUpcaster = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class CheckpointMigrationError(RuntimeError):
    """A checkpoint cannot be raised to the layout this process reads."""


class CheckpointUpcasterRegistry:
    """The chain from an older checkpoint layout to the current one.

    Keyed by ``from_version`` alone, and that is the difference from the event
    registry next door: an event envelope is one of many *types*, so its chain
    needs ``(event_type, from_version)``. A checkpoint is one shape -- the graph
    state -- so the type half would be a constant in every key.

    Each entry raises the payload exactly one version and **does not touch the
    version itself**: the chain owns the bump. An upcaster responsible for its
    own stamp could forget it, and the loop would then apply the same step
    forever.
    """

    __slots__ = ("_steps",)

    def __init__(self) -> None:
        self._steps: dict[int, CheckpointUpcaster] = {}

    def register(self, from_version: int, upcaster: CheckpointUpcaster) -> None:
        if from_version in self._steps:
            # Refused rather than replaced: two upcasters for one step means
            # somebody registered a migration twice, and which one runs would
            # depend on import order.
            raise ValueError(
                f"a checkpoint upcaster from version {from_version} is already "
                "registered"
            )
        if not 1 <= from_version < CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(
                f"a checkpoint upcaster must raise a version below "
                f"{CHECKPOINT_SCHEMA_VERSION}, not {from_version}"
            )
        self._steps[from_version] = upcaster

    def raise_to_current(
        self, payload: Mapping[str, Any], *, from_version: int
    ) -> Mapping[str, Any]:
        """Apply steps until the payload is at the current layout, or refuse."""

        if from_version > CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointMigrationError(
                f"this checkpoint was written at layout version {from_version} "
                f"and this process reads {CHECKPOINT_SCHEMA_VERSION}; it was "
                "written by a newer deployment and cannot be read here"
            )
        if from_version < 1:
            raise CheckpointMigrationError(
                f"a checkpoint layout version must be at least 1, not {from_version}"
            )

        current = payload
        version = from_version
        while version < CHECKPOINT_SCHEMA_VERSION:
            step = self._steps.get(version)
            if step is None:
                raise CheckpointMigrationError(
                    f"no checkpoint upcaster from layout version {version}; the "
                    f"chain to {CHECKPOINT_SCHEMA_VERSION} has a gap and this "
                    "checkpoint cannot be raised across it"
                )
            current = step(current)
            version += 1
        return current


def _drop_plan_step_dependencies(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Layout 1 → 2: ``TaskStep`` no longer carries ``depends_on``.

    The field recorded a step ordering **nothing ever executed** -- the plan is
    rendered to later prompts as a flat numbered list, and the graph's shape is
    frozen at submission -- so it was removed rather than wired. See the
    ledger's B-10.

    Dropping rather than translating, because there is nothing to translate
    into: no field replaced it. A step that never carried the key passes
    through untouched, which is what makes this safe to apply to a plan written
    by any 1-era deployment.
    """

    plan = payload.get("plan")
    if not plan:
        return payload
    steps = cast("Sequence[object]", plan)
    raised: list[object] = []
    for step in steps:
        if isinstance(step, Mapping):
            entries = cast("Mapping[str, object]", step)
            raised.append({k: v for k, v in entries.items() if k != "depends_on"})
        else:
            # Not a mapping: leave it exactly as written. This function's job is
            # to drop one key, not to decide what an unrecognised plan entry is
            # -- validation downstream is the thing that gets to refuse it.
            raised.append(step)
    return {**payload, "plan": tuple(raised)}


#: The chain this process ships with.
DEFAULT_CHECKPOINT_UPCASTERS: Final[CheckpointUpcasterRegistry] = (
    CheckpointUpcasterRegistry()
)
DEFAULT_CHECKPOINT_UPCASTERS.register(1, _drop_plan_step_dependencies)


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CHECKPOINT_VERSION_CHANNEL",
    "DEFAULT_CHECKPOINT_UPCASTERS",
    "CheckpointMigrationError",
    "CheckpointUpcaster",
    "CheckpointUpcasterRegistry",
]
