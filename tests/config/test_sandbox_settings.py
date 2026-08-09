"""The `[sandbox]` section and what enabling it does to a Task's authority.

Every widening is paired with the default it widens from. The envelope is
stored with each Task and re-applied on every resume, so "off by default" is
not caution here -- it is what stops an upgrade from granting historical Tasks
a capability their submitter never chose (ADR-029, and ADR-020 before it).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_workbench.bootstrap.projections import (
    TASK_V1_AUTHORIZATION_ENVELOPE,
    WORKSPACE_TOOLS,
    project_task,
    project_task_worker,
    task_authorization_envelope,
)
from agent_workbench.bootstrap.settings import Settings, load_settings
from agent_workbench.domain.sandbox import SANDBOX_RUN_TOOL
from tests.config.test_settings import valid_payload

ROOT = Path(__file__).resolve().parents[2]


def _payload(**sandbox: object) -> dict[str, object]:
    payload = valid_payload()
    payload["sandbox"] = sandbox
    return payload


def test_a_deployment_that_never_configured_a_sandbox_gets_none() -> None:
    settings = Settings(**valid_payload())

    assert settings.sandbox.enabled is False
    envelope = project_task(settings).default_authorization_envelope
    assert envelope == TASK_V1_AUTHORIZATION_ENVELOPE
    assert SANDBOX_RUN_TOOL not in envelope.allowed_tools
    # Not merely absent from the allowlist: the ceiling never rose either.
    assert envelope.max_tool_risk == "write"


def test_enabling_the_sandbox_grants_both_halves_of_the_permission() -> None:
    """Allowlist and risk ceiling together, or the tool is still refused.

    `sandbox_run` declares `risk="external"` and `risk_within` ranks external
    above write, so an envelope that named the tool without raising the ceiling
    would read as a bug rather than as a policy.
    """

    settings = Settings(**_payload(enabled=True))

    envelope = project_task(settings).default_authorization_envelope

    # Sorted, because the envelope normalizes its allowlist.
    assert envelope.allowed_tools == tuple(
        sorted(("export_artifact", *WORKSPACE_TOOLS, SANDBOX_RUN_TOOL))
    )
    assert envelope.max_tool_risk == "external"


def test_the_sandbox_composes_with_search_and_mcp_rather_than_replacing_them() -> None:
    """Three independent widenings, and none of them shadows another."""

    both = task_authorization_envelope(
        external_search=True,
        mcp_tools=("mcp_office_render_document",),
        sandbox=True,
    )

    assert both.allowed_tools == tuple(
        sorted(
            (
                "export_artifact",
                "external_search",
                "mcp_office_render_document",
                *WORKSPACE_TOOLS,
                SANDBOX_RUN_TOOL,
            )
        )
    )

    # Control: the same call with the sandbox off differs by exactly one name,
    # which is what says this option added a tool and changed nothing else.
    without = task_authorization_envelope(
        external_search=True,
        mcp_tools=("mcp_office_render_document",),
    )
    assert set(both.allowed_tools) - set(without.allowed_tools) == {SANDBOX_RUN_TOOL}
    assert set(without.allowed_tools) - set(both.allowed_tools) == set()
    assert without.max_tool_risk == both.max_tool_risk


def test_the_worker_is_pointed_at_a_sandbox_only_when_one_is_enabled() -> None:
    off = project_task_worker(Settings(**valid_payload()))
    assert off.sandbox is None

    on = project_task_worker(
        Settings(**_payload(enabled=True, endpoint="http://127.0.0.1:9999/mcp"))
    )
    assert on.sandbox is not None
    assert on.sandbox.endpoint == "http://127.0.0.1:9999/mcp"
    assert on.sandbox.timeout_seconds == 180


def test_stdio_transport_is_refused_rather_than_quietly_accepted() -> None:
    # Same reasoning as the MCP section: spawning a local subprocess is a
    # different threat model than calling a service, and no ADR decided it.
    with pytest.raises(ValidationError):
        Settings(**_payload(enabled=True, transport="stdio"))

    assert Settings(**_payload(enabled=True, transport="http")).sandbox.transport == (
        "http"
    )


def test_the_isolation_is_not_reachable_from_configuration() -> None:
    """ADR-029 §3.2: the network switch is the premise, not a hardening option.

    A deployment that could turn it off is one where every replay guarantee in
    this system is void, so the section is checked for the absence of anything
    shaped like a knob -- this is the test that fails when somebody adds one.
    """

    declared = set(Settings(**valid_payload()).sandbox.model_fields_set) | set(
        type(Settings(**valid_payload()).sandbox).model_fields
    )

    assert declared == {"enabled", "transport", "endpoint", "timeout_seconds"}
    for forbidden in ("network", "memory", "cpu", "image", "read_only", "user", "cap"):
        assert not any(forbidden in field for field in declared)


def test_an_unknown_sandbox_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(**_payload(enabled=True, allow_network=True))


def _load_profile(monkeypatch: pytest.MonkeyPatch, path: Path) -> Settings:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    dsn = (
        "postgresql+asyncpg://agent:local-profile-test@127.0.0.1:5433/"
        "agent_workbench_local"
    )
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", dsn)
    return load_settings(config_file=path)


def test_the_ordinary_local_profile_does_not_widen_tasks_with_a_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the profile below, and the one that matters more.

    A checked-in profile that quietly enabled this would widen every Task
    submitted by anybody who used the ordinary local setup.
    """

    settings = _load_profile(monkeypatch, ROOT / "config/config.local.toml")

    assert settings.sandbox.enabled is False
    assert SANDBOX_RUN_TOOL not in (
        project_task(settings).default_authorization_envelope.allowed_tools
    )


def test_the_explicit_sandbox_profile_enables_exactly_that_one_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load_profile(monkeypatch, ROOT / "config/config.sandbox-local.toml")

    assert settings.sandbox.enabled is True
    assert settings.sandbox.endpoint == "http://127.0.0.1:8766/mcp"
    # It does not also switch on the other opt-in capabilities.
    assert settings.mcp.servers == ()
    assert settings.research.enabled is False

    envelope = project_task(settings).default_authorization_envelope
    assert SANDBOX_RUN_TOOL in envelope.allowed_tools
    assert envelope.max_tool_risk == "external"
