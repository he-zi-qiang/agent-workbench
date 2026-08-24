"""What a command started by this process inherits (ADR-077).

One decision, so one test file: everything in the ``AW_*`` namespace is removed
and nothing else is. Both halves matter and they fail differently -- leaving the
namespace in hands a model the provider key, and scrubbing anything else hands
the operator a ``git push`` that cannot reach their agent socket and no reason
why.
"""

from __future__ import annotations

import pytest

from agent_workbench.bootstrap.child_environment import command_environment


def test_the_provider_key_is_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    # The concrete failure. `env` is an ordinary thing to run while looking
    # around a project, and the command is written by a model.
    monkeypatch.setenv("AW_SECRETS__DEEPSEEK_API_KEY", "sk-live-should-never-leave")
    assert "AW_SECRETS__DEEPSEEK_API_KEY" not in command_environment()


def test_connection_strings_are_not_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `settings.py` treats a DSN as a credential even when today's carries no
    # password, and this has to agree with it rather than judge each string.
    monkeypatch.setenv("AW_DATABASE__DSN", "postgresql+asyncpg://agent:pw@h/db")
    monkeypatch.setenv("AW_DATABASE__GUARD_DSN", "postgresql+asyncpg://agent:pw@h/db")
    monkeypatch.setenv("AW_KEY_FILE", "/home/someone/.config/agent-workbench/key")
    inherited = command_environment()
    assert not [name for name in inherited if name.startswith("AW_")]


def test_a_future_setting_is_covered_without_anybody_adding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reason the rule is a namespace rather than a list. A list would have
    # to be revisited every time a setting is added, by somebody who is thinking
    # about the setting rather than about this function.
    monkeypatch.setenv("AW_SOMETHING__NOBODY_HAS_WRITTEN_YET", "value")
    assert "AW_SOMETHING__NOBODY_HAS_WRITTEN_YET" not in command_environment()


def test_the_operators_own_environment_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half, and it is a decision rather than an omission: a command
    # run inside somebody's project is meant to see their toolchain and their
    # own credentials. A model that can run commands at all is not made safer
    # by making them fail.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_the_operators_own")
    inherited = command_environment()
    assert inherited["PATH"] == "/usr/bin:/bin"
    assert inherited["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
    assert inherited["GITHUB_TOKEN"] == "ghp_the_operators_own"


def test_a_name_merely_containing_the_prefix_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `startswith`, not `in`. A project with a `MY_AW_SETTING` is not this
    # platform's configuration, and over-scrubbing is the failure mode that
    # produces a bug report nobody can reproduce.
    monkeypatch.setenv("MY_AW_SETTING", "kept")
    assert command_environment()["MY_AW_SETTING"] == "kept"
