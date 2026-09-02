"""``load_settings(switches_file=...)``: where a stored switch ranks, and the hold.

Pinned against the real loader with the shipped ``config.default.toml``, so the
claims are about the precedence a deployment actually gets: a stored switch
beats the TOML files, an exported variable beats the stored switch, and a
stored "on" for ``research.enabled`` with no key is *held* rather than turned
into the startup error the validator would otherwise raise (ADR-103 §3).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from agent_workbench.bootstrap.projections import project_api
from agent_workbench.bootstrap.settings import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config/config.default.toml"
POSTGRES_DSN = (
    "postgresql+asyncpg://agent:switch-test@127.0.0.1:5433/agent_workbench_local"
)
RESEARCH = "research.enabled"
DELEGATION = "multi_agent.delegation_enabled"


def _load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, switches: dict[str, bool] | str
) -> Settings:
    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)
    target = tmp_path / "switches.json"
    target.write_text(
        switches if isinstance(switches, str) else json.dumps(switches),
        encoding="utf-8",
    )
    return load_settings(config_file=DEFAULT_CONFIG, switches_file=target)


def test_a_stored_switch_beats_the_shipped_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shipped file says false for all four; a switch nothing could flip is none."""

    settings = _load(monkeypatch, tmp_path, {DELEGATION: True})

    assert settings.multi_agent.delegation_enabled is True
    assert type(settings).stored_switches == {DELEGATION: True}
    assert type(settings).applied_switches == {DELEGATION: True}
    assert type(settings).held_switches == {}


def test_an_exported_variable_beats_a_stored_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator's environment is the deployment's own decision."""

    monkeypatch.setenv("AW_MULTI_AGENT__DELEGATION_ENABLED", "false")
    settings = _load(monkeypatch, tmp_path, {DELEGATION: True})
    # `_load` clears AW_* first, so set it again after: the helper's clearing
    # is what keeps the other tests honest, and this one needs the variable.
    monkeypatch.setenv("AW_MULTI_AGENT__DELEGATION_ENABLED", "false")
    settings = load_settings(
        config_file=DEFAULT_CONFIG, switches_file=tmp_path / "switches.json"
    )

    assert settings.multi_agent.delegation_enabled is False
    # Recorded as stored and applied: the *source* applied it, and a higher
    # source won. The projection below is what turns that into "overridden".
    assert type(settings).stored_switches == {DELEGATION: True}
    state = next(s for s in project_api(settings).switches if s.path == DELEGATION)
    assert state.active is False
    assert state.stored_at_start is True
    assert state.held == ""


def test_research_on_with_no_key_is_held_rather_than_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one switch whose "on" the validator refuses without a key.

    Refusing would lock the author out: the page that stored the switch lives
    in the process that would not start. So the loader holds it, says so, and
    the process comes up without web search -- the same shape ADR-102 §3 chose
    for the container launcher.
    """

    settings = _load(monkeypatch, tmp_path, {RESEARCH: True, DELEGATION: True})

    assert settings.research.enabled is False
    assert settings.multi_agent.delegation_enabled is True
    assert type(settings).stored_switches == {RESEARCH: True, DELEGATION: True}
    assert type(settings).applied_switches == {DELEGATION: True}
    assert "Provider Key" in type(settings).held_switches[RESEARCH]

    state = next(s for s in project_api(settings).switches if s.path == RESEARCH)
    assert state.active is False
    assert state.stored_at_start is True
    assert state.held != ""


def test_research_on_with_a_key_is_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _load(monkeypatch, tmp_path, {RESEARCH: True})
    monkeypatch.setenv("AW_SECRETS__DEEPSEEK_API_KEY", "sk-example-not-a-credential")
    settings = load_settings(
        config_file=DEFAULT_CONFIG, switches_file=tmp_path / "switches.json"
    )

    assert settings.research.enabled is True
    assert type(settings).held_switches == {}


def test_a_file_the_parser_dislikes_is_a_startup_error_that_names_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match=re.escape("switches.json")):
        _load(monkeypatch, tmp_path, "{not json")
    with pytest.raises(ValueError, match="不认识的开关"):
        _load(monkeypatch, tmp_path, {"policy.shell_tools_enabled": True})


def test_a_load_that_was_not_pointed_at_a_file_reads_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests and `agent-config-check` must not inherit somebody's console choices."""

    for name in tuple(os.environ):
        if name.upper().startswith("AW_"):
            monkeypatch.delenv(name, raising=False)
    for suffix in ("DSN", "GUARD_DSN", "LISTEN_DSN"):
        monkeypatch.setenv(f"AW_DATABASE__{suffix}", POSTGRES_DSN)

    settings = load_settings(config_file=DEFAULT_CONFIG)

    assert type(settings).stored_switches == {}
    assert all(
        state.stored_at_start is None for state in project_api(settings).switches
    )
