"""Deciding what may run together, before anything runs.

Scheduling is a pure function here on purpose. Concurrency bugs are hard to
observe and harder to reproduce, so the decision -- which calls share a group,
which one runs alone -- is made in code that takes a list and returns a list,
and can be checked without an event loop.

Two rules produce the groups.

Calls keep the model's order. A batch is scanned left to right and consecutive
parallel-safe calls accumulate into one group, so a read proposed before a
write is never executed after it. Reordering side effects relative to the reads
around them would change what the model asked for.

An exclusive call is a group of one. Write, external and destructive tools are
exclusive by construction -- the domain refuses to build a ToolSpec that says
otherwise -- so this is where "side effects cross a barrier" stops being a
sentence in the baseline and becomes a shape in memory: nothing is in flight
beside them, in either direction.

Execution order still says nothing about submission order. Results go back to
the model in the order it called for them, whichever group finished first.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_workbench.runtime.tool_gateway import PreparedCall

DEFAULT_MAX_PARALLEL_READS = 4


def plan_tool_batches(
    calls: Sequence[PreparedCall],
    *,
    max_parallel: int = DEFAULT_MAX_PARALLEL_READS,
) -> tuple[tuple[PreparedCall, ...], ...]:
    """Group one batch of authorized calls into execution groups.

    Every group may run concurrently within itself; groups run one after
    another, in order.
    """

    if max_parallel < 1:
        raise ValueError("max_parallel must be positive")

    groups: list[tuple[PreparedCall, ...]] = []
    pending: list[PreparedCall] = []

    def flush() -> None:
        if pending:
            groups.append(tuple(pending))
            pending.clear()

    for prepared in calls:
        if prepared.binding.spec.concurrency == "exclusive":
            flush()
            groups.append((prepared,))
            continue

        pending.append(prepared)
        if len(pending) == max_parallel:
            flush()

    flush()
    return tuple(groups)


__all__ = ["DEFAULT_MAX_PARALLEL_READS", "plan_tool_batches"]
