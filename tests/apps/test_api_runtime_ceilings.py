"""The API process is bound by the same deployment ceilings the Worker is.

ADR-098. Four numbers -- ``runtime.model_timeout_seconds``,
``runtime.tool_timeout_seconds``, ``runtime.max_parallel_read_tools`` and
``policy.max_tool_argument_bytes`` -- were validated at startup, written into
shipped profiles, explained in the configuration document, and read by exactly
one process. The API was the other one, and it is the process that runs Code
sessions, which is where ``project_run`` and ``sandbox_run`` live.

The projection test below is the cheap half. The source guard is the half that
matters: the defect was never "somebody chose the wrong number", it was five
construction sites and nothing that noticed a sixth arriving without them.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from agent_workbench.adapters.tools import StaticToolRegistry
from agent_workbench.apps.api.dependencies import (
    _api_gateway,  # pyright: ignore[reportPrivateUsage]
)
from agent_workbench.bootstrap.paths import DEFAULT_CONFIG_FILE
from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = PROJECT_ROOT / "src/agent_workbench/apps/api/dependencies.py"

TEST_DSN = "postgresql+asyncpg://unit:test@postgres:5432/agent_workbench"


def _settings(**runtime_overrides: object) -> Settings:
    with Path(DEFAULT_CONFIG_FILE).open("rb") as handle:
        payload = tomllib.load(handle)
    payload["database"] = {
        **payload["database"],
        "dsn": TEST_DSN,
        "guard_dsn": TEST_DSN,
        "listen_dsn": TEST_DSN,
    }
    payload["model"]["main"]["model_id"] = "unit-main"
    payload["model"]["compact"]["model_id"] = "unit-compact"
    payload["secrets"] = {"deepseek_api_key": "unit-test-key"}
    payload["runtime"] = {**payload["runtime"], **runtime_overrides}
    return Settings(**payload)


def test_api_projection_carries_the_four_deployment_ceilings() -> None:
    settings = _settings()

    api = project_api(settings)

    assert api.model_timeout_seconds == float(settings.runtime.model_timeout_seconds)
    assert api.max_parallel_read_tools == settings.runtime.max_parallel_read_tools
    assert api.tool_timeout_seconds is None  # what config.default.toml ships
    assert api.max_tool_argument_bytes == settings.policy.max_tool_argument_bytes


def test_a_raised_model_timeout_reaches_the_api_projection() -> None:
    """The exact shape ``config.code-local.toml`` was written to produce.

    That profile raises the envelope to 300 to settle a measured incident, and
    it is only ever loaded by ``code-api`` -- a process that used to drop the
    number on the floor and run every Code turn at the runtime's own 120.0.
    """

    api = project_api(_settings(model_timeout_seconds=300, tool_timeout_seconds=90))

    assert api.model_timeout_seconds == 300.0
    assert api.tool_timeout_seconds == 90.0


def test_the_gateway_helper_hands_both_ceilings_to_what_it_builds() -> None:
    config = project_api(_settings(tool_timeout_seconds=45))

    gateway = _api_gateway(config, StaticToolRegistry([]))

    # Private on purpose: these are the two values the gateway would otherwise
    # have silently defaulted, and there is no public reader for either.
    assert gateway._max_argument_bytes == config.max_tool_argument_bytes  # pyright: ignore[reportPrivateUsage]
    executor = gateway._executor  # pyright: ignore[reportPrivateUsage]
    assert executor._deployment_ceiling_seconds == 45.0  # pyright: ignore[reportPrivateUsage]


#: Parsed once. Two parses give two sets of node objects, and the guard below
#: compares identities across the file and one function within it.
DEPENDENCIES_AST = ast.parse(DEPENDENCIES.read_text(encoding="utf-8"))


def _calls(name: str, *, within: ast.AST = DEPENDENCIES_AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(within)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def test_every_gateway_in_this_process_is_built_by_the_one_helper() -> None:
    """Every ``ToolGateway(`` in the file is inside ``_api_gateway``.

    A direct construction anywhere else is exactly the regression: it compiles,
    it runs, and it quietly reverts this process to the gateway's own defaults.
    """

    helper = next(
        node
        for node in ast.walk(DEPENDENCIES_AST)
        if isinstance(node, ast.FunctionDef) and node.name == "_api_gateway"
    )
    inside = {id(call) for call in _calls("ToolGateway", within=helper)}

    stray = [node.lineno for node in _calls("ToolGateway") if id(node) not in inside]
    assert not stray, (
        f"ToolGateway constructed directly at line(s) {stray} in dependencies.py; "
        "build it through _api_gateway so it carries the deployment's ceilings"
    )
    assert len(_calls("_api_gateway")) >= 4


@pytest.mark.parametrize(
    "keyword", ["model_timeout_seconds", "max_parallel_read_tools"]
)
def test_every_runtime_in_this_process_receives_the_runtime_ceilings(
    keyword: str,
) -> None:
    constructions = _calls("ClaudeLikeAgentRuntime")

    assert constructions, "the API assembles at least one runtime"
    missing = [
        node.lineno
        for node in constructions
        if keyword not in {kw.arg for kw in node.keywords if kw.arg is not None}
    ]
    assert not missing, (
        f"ClaudeLikeAgentRuntime at line(s) {missing} in dependencies.py does not "
        f"pass {keyword}; it will silently use the runtime's compiled-in default"
    )
