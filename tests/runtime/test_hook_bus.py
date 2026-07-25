"""One pass over the hooks, in order, failing closed."""

from __future__ import annotations

import asyncio

import pytest

from agent_workbench.domain.policies import (
    AuthorizationEnvelope,
    ExecutionContext,
    PrincipalContext,
)
from agent_workbench.domain.schema import JsonObject
from agent_workbench.domain.tools import ToolCall
from agent_workbench.ports.hooks import HookOutcome
from agent_workbench.runtime.hook_bus import HookBus, HookBusOutcome

CONTEXT = ExecutionContext(
    principal=PrincipalContext(principal_id="user_1", tenant_id="tenant_a"),
    envelope=AuthorizationEnvelope(allowed_tools=("search",)),
    agent_run_id="run_1",
    policy_identity="policy-test:0000000000000000",
)
CALL = ToolCall(
    tool_call_id="toolu_1",
    tool_name="search",
    arguments={"query": "fusion"},
)


class _Recorder:
    """A hook that records what it saw and returns a fixed outcome."""

    def __init__(self, name: str, outcome: HookOutcome | None = None) -> None:
        self.name = name
        self.seen: list[ToolCall] = []
        self._outcome = outcome if outcome is not None else HookOutcome.unchanged()

    async def before_tool(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> HookOutcome:
        self.seen.append(call)
        return self._outcome


class _Raiser:
    def __init__(self, name: str) -> None:
        self.name = name

    async def before_tool(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> HookOutcome:
        raise RuntimeError("connection to policy service failed: sk-ant-canary")


class _Staller:
    def __init__(self, name: str) -> None:
        self.name = name

    async def before_tool(
        self,
        call: ToolCall,
        context: ExecutionContext,
    ) -> HookOutcome:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


def _run(bus: HookBus, call: ToolCall = CALL) -> HookBusOutcome:
    return asyncio.run(bus.before_tool(call, CONTEXT))


def test_an_outcome_cannot_both_rewrite_and_block() -> None:
    with pytest.raises(ValueError, match="not both"):
        HookOutcome(arguments={"query": "x"}, blocked_reason="no")


def test_an_empty_bus_changes_nothing() -> None:
    bus = HookBus()

    outcome = _run(bus)

    assert bus.is_empty is True
    assert outcome.call == CALL
    assert outcome.rewritten is False
    assert outcome.blocked is False


def test_hooks_run_in_registration_order() -> None:
    first = _Recorder("first")
    second = _Recorder("second")

    bus = HookBus([first, second])
    _run(bus)

    assert bus.names == ("first", "second")
    assert len(first.seen) == 1
    assert len(second.seen) == 1


def test_a_later_hook_sees_what_an_earlier_one_produced() -> None:
    rewriter = _Recorder("rewriter", HookOutcome.rewrite({"query": "clamped"}))
    observer = _Recorder("observer")

    outcome = _run(HookBus([rewriter, observer]))

    assert observer.seen[0].arguments == {"query": "clamped"}
    assert outcome.rewritten is True
    assert outcome.call.arguments == {"query": "clamped"}


def test_a_rewrite_cannot_change_the_call_identity() -> None:
    """The id and the tool belong to the model's request, not to a hook."""

    rewriter = _Recorder("rewriter", HookOutcome.rewrite({"query": "other"}))

    outcome = _run(HookBus([rewriter]))

    assert outcome.call.tool_call_id == CALL.tool_call_id
    assert outcome.call.tool_name == CALL.tool_name


def test_a_blocking_hook_stops_the_pass() -> None:
    blocker = _Recorder("blocker", HookOutcome.block("path outside the workspace"))
    later = _Recorder("later")

    outcome = _run(HookBus([blocker, later]))

    assert outcome.blocked is True
    assert outcome.blocked_by == "blocker"
    assert outcome.reason == "path outside the workspace"
    assert later.seen == []


def test_a_raising_hook_blocks_instead_of_disappearing() -> None:
    """A broken safety rule must not become permission."""

    outcome = _run(HookBus([_Raiser("fragile")]))

    assert outcome.blocked is True
    assert outcome.blocked_by == "fragile"
    assert outcome.reason is not None
    assert "RuntimeError" in outcome.reason
    assert "sk-ant-canary" not in outcome.reason


def test_a_hanging_hook_blocks_when_its_time_runs_out() -> None:
    outcome = _run(HookBus([_Staller("slow")], timeout_seconds=0.05))

    assert outcome.blocked is True
    assert outcome.blocked_by == "slow"
    assert outcome.reason is not None
    assert "timeout" in outcome.reason


def test_a_rewrite_made_before_a_block_is_reported_with_it() -> None:
    rewriter = _Recorder("rewriter", HookOutcome.rewrite({"query": "clamped"}))
    blocker = _Recorder("blocker", HookOutcome.block("still not allowed"))

    outcome = _run(HookBus([rewriter, blocker]))

    assert outcome.blocked_by == "blocker"
    assert outcome.rewritten is True


def test_duplicate_hook_names_are_rejected() -> None:
    """An audit line has to say which hook refused a call."""

    with pytest.raises(ValueError, match="unique"):
        HookBus([_Recorder("same"), _Recorder("same")])


def test_a_non_positive_timeout_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        HookBus([], timeout_seconds=0)


def test_arbitrary_json_arguments_survive_a_rewrite() -> None:
    nested: JsonObject = {"filters": {"tags": ["a", "b"], "limit": 3}}
    rewriter = _Recorder("rewriter", HookOutcome.rewrite(nested))

    assert _run(HookBus([rewriter])).call.arguments == nested
