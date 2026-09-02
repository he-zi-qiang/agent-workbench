"""`usable_key_present()`: the question a launcher has to answer before start.

Pinned against the validator it exists to stay ahead of. The probe is only
useful if it agrees with `load_settings` about what counts as a key -- a probe
that said yes to `replace-me-local-only` would turn on research and hand the
process a startup error, which is the exact failure it was written to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_workbench.bootstrap.provider_key import (
    ENV_VAR,
    KEY_FILE_ENV_VAR,
    usable_key_present,
)
from agent_workbench.bootstrap.settings import PLACEHOLDER_PREFIXES


def test_an_exported_key_is_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "sk-real-looking-value")
    # No file at all: the export wins, as it does in `load_settings`.
    monkeypatch.setenv(KEY_FILE_ENV_VAR, "")

    assert usable_key_present() is True


def test_a_stored_key_is_enough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "key"
    # With the trailing newline a shell `echo` leaves, which is how this file is
    # written in practice.
    key_file.write_text("sk-stored-value\n", encoding="utf-8")
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv(KEY_FILE_ENV_VAR, str(key_file))

    assert usable_key_present() is True


def test_nothing_anywhere_is_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv(KEY_FILE_ENV_VAR, str(tmp_path / "absent"))

    assert usable_key_present() is False


def test_an_empty_file_is_no(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("\n", encoding="utf-8")
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv(KEY_FILE_ENV_VAR, str(key_file))

    assert usable_key_present() is False


@pytest.mark.parametrize("prefix", PLACEHOLDER_PREFIXES)
def test_every_placeholder_the_validator_refuses_is_refused_here(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """Parameterized over the validator's own list, so a new prefix lands here.

    This is the whole point of importing the constant rather than restating it:
    the day somebody adds a fifth placeholder shape, this test covers it without
    anybody remembering that a container launcher also reads keys.
    """

    monkeypatch.setenv(ENV_VAR, f"{prefix}-whatever")
    monkeypatch.setenv(KEY_FILE_ENV_VAR, "")

    assert usable_key_present() is False
