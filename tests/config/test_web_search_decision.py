"""``launcher_decides_web_search()``: the probe steps aside for a stored choice.

ADR-102 made the container launcher decide Chat's web search by probing for a
key. ADR-103 puts the console's stored switch above that probe. These pin the
order: a stored value -- either one -- means the launcher exports nothing and
the loader decides; only when nothing is stored does the key probe stand.
Since ADR-104 the native ``scripts/dev.sh`` console arms ask the same function
through the same probe; ``test_dev_script_web_search.py`` holds them to it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_workbench.bootstrap.provider_key import ENV_VAR, KEY_FILE_ENV_VAR
from agent_workbench.bootstrap.switches import (
    launcher_decides_web_search,
    switches_file,
)


@pytest.fixture
def key_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    (tmp_path / "key").write_text("sk-stored-not-a-credential\n", encoding="utf-8")
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv(KEY_FILE_ENV_VAR, str(tmp_path / "key"))
    return tmp_path


def test_the_switches_file_sits_beside_the_key(key_dir: Path) -> None:
    assert switches_file() == key_dir / "switches.json"


def test_no_key_file_means_no_switches_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_FILE_ENV_VAR, "")
    assert switches_file() is None


def test_a_key_and_no_stored_choice_is_yes(key_dir: Path) -> None:
    assert launcher_decides_web_search() is True


@pytest.mark.parametrize("stored", [True, False])
def test_a_stored_choice_either_way_takes_the_decision_away(
    key_dir: Path, stored: bool
) -> None:
    (key_dir / "switches.json").write_text(
        json.dumps({"research.enabled": stored}), encoding="utf-8"
    )
    assert launcher_decides_web_search() is False


def test_a_stored_choice_about_something_else_leaves_the_probe_alone(
    key_dir: Path,
) -> None:
    (key_dir / "switches.json").write_text(
        json.dumps({"multi_agent.delegation_enabled": True}), encoding="utf-8"
    )
    assert launcher_decides_web_search() is True


def test_no_key_is_no_whatever_is_stored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv(KEY_FILE_ENV_VAR, str(tmp_path / "absent"))
    assert launcher_decides_web_search() is False


def test_an_unreadable_file_is_no_rather_than_a_second_opinion(key_dir: Path) -> None:
    """The loader is about to refuse it by name; the launcher adds nothing."""

    (key_dir / "switches.json").write_text("{", encoding="utf-8")
    assert launcher_decides_web_search() is False
