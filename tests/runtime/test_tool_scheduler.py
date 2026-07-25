"""Grouping decided before anything runs, and checkable without a loop."""

from __future__ import annotations

import pytest

from agent_workbench.domain.tools import ToolCall, ToolSpec
from agent_workbench.ports.tools import ToolBinding, ToolInvocation
from agent_workbench.runtime.tool_gateway import PreparedCall
from agent_workbench.runtime.tool_scheduler import plan_tool_batches


async def _handler(invocation: ToolInvocation) -> None:  # pragma: no cover
    raise AssertionError("the planner never runs a handler")


def _prepared(name: str, *, exclusive: bool = False) -> PreparedCall:
    spec = (
        ToolSpec(
            name=name,
            description="A side-effecting tool.",
            input_schema={"type": "object"},
            concurrency="exclusive",
            risk="write",
            idempotency="keyed",
            timeout_seconds=5,
            permission_scopes=("artifact:write",),
        )
        if exclusive
        else ToolSpec(
            name=name,
            description="A read-only tool.",
            input_schema={"type": "object"},
            concurrency="parallel",
            risk="read",
            idempotency="safe",
            timeout_seconds=5,
        )
    )
    return PreparedCall(
        binding=ToolBinding(spec=spec, handler=_handler),  # pyright: ignore[reportArgumentType]
        call=ToolCall(tool_call_id=f"toolu_{name}", tool_name=name),
    )


def _names(
    groups: tuple[tuple[PreparedCall, ...], ...],
) -> list[list[str]]:
    return [[prepared.call.tool_name for prepared in group] for group in groups]


def test_an_empty_batch_produces_no_groups() -> None:
    assert plan_tool_batches([]) == ()


def test_consecutive_reads_share_one_group() -> None:
    groups = plan_tool_batches([_prepared("a"), _prepared("b"), _prepared("c")])

    assert _names(groups) == [["a", "b", "c"]]


def test_a_group_never_exceeds_the_parallel_ceiling() -> None:
    calls = [_prepared(name) for name in "abcde"]

    groups = plan_tool_batches(calls, max_parallel=2)

    assert _names(groups) == [["a", "b"], ["c", "d"], ["e"]]


def test_an_exclusive_call_is_always_a_group_of_one() -> None:
    """Side effects cross a barrier: nothing runs beside them, either way."""

    calls = [
        _prepared("a"),
        _prepared("write", exclusive=True),
        _prepared("b"),
        _prepared("c"),
    ]

    groups = plan_tool_batches(calls)

    assert _names(groups) == [["a"], ["write"], ["b", "c"]]


def test_two_exclusive_calls_never_share_a_group() -> None:
    calls = [_prepared("w1", exclusive=True), _prepared("w2", exclusive=True)]

    assert _names(plan_tool_batches(calls)) == [["w1"], ["w2"]]


def test_grouping_preserves_the_model_order() -> None:
    """A read proposed before a write must not execute after it."""

    calls = [
        _prepared("a"),
        _prepared("b"),
        _prepared("write", exclusive=True),
        _prepared("c"),
    ]

    flattened = [name for group in _names(plan_tool_batches(calls)) for name in group]

    assert flattened == ["a", "b", "write", "c"]


def test_every_call_appears_exactly_once() -> None:
    calls = [_prepared(name) for name in "abcdef"]
    calls.insert(3, _prepared("write", exclusive=True))

    groups = plan_tool_batches(calls, max_parallel=2)
    flattened = [name for group in _names(groups) for name in group]

    assert sorted(flattened) == sorted(prepared.call.tool_name for prepared in calls)
    assert len(flattened) == len(calls)


def test_a_serial_ceiling_puts_every_call_in_its_own_group() -> None:
    calls = [_prepared("a"), _prepared("b")]

    assert _names(plan_tool_batches(calls, max_parallel=1)) == [["a"], ["b"]]


def test_a_non_positive_ceiling_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        plan_tool_batches([_prepared("a")], max_parallel=0)
