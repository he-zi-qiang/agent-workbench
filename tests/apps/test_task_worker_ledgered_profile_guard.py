"""The startup half of "a ledgered effect is issued, not proposed" (ADR-075 §4).

``ToolGateway.advertise`` refuses to show a model a tool whose binding carries
an ``operation_key``, and that refusal is the one that closes the path. But it
runs per agent run, and the ``PolicyDeniedError`` it raises becomes a failed
run -- so a deployment that wired a ledgered tool into a profile would stay up
and fail that node on every Task. ADR-075 §4 named that shape as wrong and left
it open, because the gateway is assembled before anything hands it the
profiles. ``_assert_no_profile_offers_a_ledgered_tool`` closes it at the point
where both are known, and this file is what makes deleting it visible.

Nothing in the repository can reach the guard on its own: ADR-025 §2.6 pins
every MCP binding to ``idempotency="safe"``, ``ToolBinding`` refuses to combine
that with an operation key, and ``export_artifact`` -- the only ledgered tool
there is -- is issued by the ``export`` node and named in no profile. So each
test here builds the wiring mistake deliberately, and the control group below
is what stops "it raised" from being satisfied by a guard that raises always.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_workbench.adapters.artifacts import LocalArtifactStore
from agent_workbench.adapters.tools import ExportArtifactTool, StaticToolRegistry
from agent_workbench.adapters.tools.export_artifact import TOOL_NAME as EXPORT_TOOL
from agent_workbench.adapters.tools.workspace import WorkspaceReadTool, WorkspaceScope
from agent_workbench.apps.task_worker.composition import (
    LedgeredToolInAgentProfileError,
    _assert_no_profile_offers_a_ledgered_tool,
)
from agent_workbench.domain.tools import ToolName
from agent_workbench.workflows.agent_profiles import (
    AGENT_ROSTERS,
    WORKSPACE_READ_TOOL,
    AgentProfile,
    DynamicToolSource,
)


def _registry(tmp_path: Path) -> StaticToolRegistry:
    """The two bindings that matter: one ledgered, one not.

    The ledgered one is the real ``export_artifact`` rather than a stand-in,
    because what the guard reads is ``ToolBinding.operation_key`` and a
    hand-built double could carry one the production tool does not.
    """

    return StaticToolRegistry(
        (
            ExportArtifactTool(artifacts=LocalArtifactStore(root=tmp_path)).binding(),
            WorkspaceReadTool(WorkspaceScope()).binding(),
        )
    )


def _profile(
    *tool_names: ToolName, dynamic: frozenset[DynamicToolSource] = frozenset()
) -> AgentProfile:
    return AgentProfile(
        name="writer",
        node="synthesize",
        system_prompt="",
        admits=frozenset({"objective"}),
        tool_names=tool_names,
        dynamic_tool_sources=dynamic,
    )


def test_the_real_rosters_start_against_the_real_registry(tmp_path: Path) -> None:
    """The control group, and the claim ADR-075 §4 makes about today.

    Without it every other test here would be satisfied by a guard that raised
    unconditionally -- and this one also states the thing the ADR asserts: the
    repository as it stands wires no ledgered tool into any profile, so the
    Worker that registers ``export_artifact`` still starts.
    """

    _assert_no_profile_offers_a_ledgered_tool(
        _registry(tmp_path),
        dynamic_tools={},
        rosters=AGENT_ROSTERS,
    )


def test_a_profile_naming_a_ledgered_tool_stops_the_process(tmp_path: Path) -> None:
    with pytest.raises(LedgeredToolInAgentProfileError) as raised:
        _assert_no_profile_offers_a_ledgered_tool(
            _registry(tmp_path),
            dynamic_tools={},
            rosters=(("v2_general", (_profile(EXPORT_TOOL),)),),
        )

    # The message has to name all three, because the reader arriving at it has
    # a process that will not start and no run to look at: which graph, which
    # agent, which tool.
    assert "v2_general" in str(raised.value)
    assert "writer" in str(raised.value)
    assert EXPORT_TOOL in str(raised.value)


def test_a_dynamic_catalog_carrying_one_stops_it_too(tmp_path: Path) -> None:
    """The half a check over the declarations alone would miss.

    A profile's static ``tool_names`` is not what a run is offered. The Worker
    widens it from the catalogs it assembled, so a server that started serving
    a ledgered tool would reach the model through a profile whose own
    declaration never changed -- and the guard has to read what the run will
    actually see, not what the roster says.
    """

    with pytest.raises(LedgeredToolInAgentProfileError) as raised:
        _assert_no_profile_offers_a_ledgered_tool(
            _registry(tmp_path),
            dynamic_tools={"synthesis": (EXPORT_TOOL,)},
            rosters=(("v2_general", (_profile(dynamic=frozenset({"synthesis"})),)),),
        )

    assert EXPORT_TOOL in str(raised.value)


def test_a_source_the_profile_does_not_declare_is_not_its_problem(
    tmp_path: Path,
) -> None:
    """Widening is per-source, and the guard must not flatten it.

    A catalog holding a ledgered tool is only a mistake for the profiles that
    asked for that catalog. Reading every catalog for every profile would fail
    a deployment for a tool no agent could ever be shown -- and the guard would
    then be teaching people to delete it.
    """

    _assert_no_profile_offers_a_ledgered_tool(
        _registry(tmp_path),
        dynamic_tools={"sandbox": (EXPORT_TOOL,)},
        rosters=(("v2_general", (_profile(dynamic=frozenset({"synthesis"})),)),),
    )


def test_a_registered_tool_that_is_not_ledgered_is_left_alone(
    tmp_path: Path,
) -> None:
    """The workspace tools are the reason this guard is not "no tools at all".

    ADR-028 lets a profile hold them precisely because they have no external
    effect -- they bind names inside the Task's own versioned artifact store.
    A guard that refused every registered tool in a profile would take those
    with it and leave the writer unable to work.
    """

    _assert_no_profile_offers_a_ledgered_tool(
        _registry(tmp_path),
        dynamic_tools={},
        rosters=(("v2_general", (_profile(WORKSPACE_READ_TOOL),)),),
    )
