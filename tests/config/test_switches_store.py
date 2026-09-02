"""``SwitchStore`` and the parser it shares with the loader (ADR-103).

Real files under ``tmp_path`` rather than a mock, for the reason the key
store's tests give: every claim here is about what reaches the disk -- the
bytes, the atomicity, the paths refused. A mock would let a store that wrote
nothing pass, and one that wrote into the checkout pass too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_workbench.application.switches import (
    SWITCH_PATHS,
    SWITCHES,
    SwitchRefused,
    SwitchStore,
    parse_switches,
    spec_for,
    switch_paths_as_nested,
)


def test_the_four_switches_are_the_four_booleans_the_adr_names() -> None:
    """A fifth switch is a product decision and should have to edit this."""

    assert {
        "research.enabled",
        "triage.enabled",
        "code.enabled",
        "multi_agent.delegation_enabled",
    } == SWITCH_PATHS
    # Exactly one is held without a key: the one the validator refuses.
    assert [spec.path for spec in SWITCHES if spec.held_without_key] == [
        "research.enabled"
    ]
    assert spec_for("research.enabled") is not None
    assert spec_for("policy.shell_tools_enabled") is None


def test_parsing_is_strict_in_every_direction() -> None:
    assert parse_switches("", source="f") == {}
    assert parse_switches('{"research.enabled": true}', source="f") == {
        "research.enabled": True
    }
    with pytest.raises(SwitchRefused, match="不是合法的 JSON"):
        parse_switches("{", source="f")
    with pytest.raises(SwitchRefused, match="顶层必须是一个对象"):
        parse_switches("[]", source="f")
    with pytest.raises(
        SwitchRefused, match=re.escape("不认识的开关 'policy.shell_tools_enabled'")
    ):
        parse_switches('{"policy.shell_tools_enabled": true}', source="f")
    with pytest.raises(SwitchRefused, match="必须是 true 或 false"):
        parse_switches('{"research.enabled": "yes"}', source="f")


def test_a_missing_file_stores_nothing_and_a_write_creates_it(tmp_path: Path) -> None:
    store = SwitchStore(path=tmp_path / "switches.json", checkout_root=None)
    assert store.read() == {}

    assert store.set("research.enabled", True) == {"research.enabled": True}
    assert store.set("multi_agent.delegation_enabled", False) == {
        "multi_agent.delegation_enabled": False,
        "research.enabled": True,
    }
    on_disk = json.loads((tmp_path / "switches.json").read_text(encoding="utf-8"))
    assert on_disk == {
        "multi_agent.delegation_enabled": False,
        "research.enabled": True,
    }
    # Withdrawn, not set to False: "nobody decided" is a state of its own.
    assert store.set("research.enabled", None) == {
        "multi_agent.delegation_enabled": False
    }
    # Nothing half-written left beside it.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["switches.json"]


def test_an_unknown_switch_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    store = SwitchStore(path=tmp_path / "switches.json", checkout_root=None)
    with pytest.raises(
        SwitchRefused, match=re.escape("没有叫 'policy.shell_tools_enabled' 的开关")
    ):
        store.set("policy.shell_tools_enabled", True)
    assert not (tmp_path / "switches.json").exists()


def test_a_hand_edited_file_is_refused_rather_than_overwritten(
    tmp_path: Path,
) -> None:
    """The person meant something by it; the parser's sentence is the answer."""

    target = tmp_path / "switches.json"
    target.write_text("{not json", encoding="utf-8")
    store = SwitchStore(path=target, checkout_root=None)
    with pytest.raises(SwitchRefused, match="不是合法的 JSON"):
        store.read()
    with pytest.raises(SwitchRefused, match="不是合法的 JSON"):
        store.set("research.enabled", True)
    assert target.read_text(encoding="utf-8") == "{not json"


def test_no_file_declared_means_nowhere_to_write() -> None:
    store = SwitchStore(path=None, checkout_root=None)
    assert store.read() == {}
    with pytest.raises(SwitchRefused, match="AW_KEY_FILE"):
        store.set("research.enabled", True)


def test_the_checkout_is_refused_as_a_home_for_the_file(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    store = SwitchStore(path=checkout / "var" / "switches.json", checkout_root=checkout)
    with pytest.raises(SwitchRefused, match="checkout"):
        store.set("research.enabled", True)
    assert not (checkout / "var").exists()


def test_paths_nest_the_way_a_settings_source_needs() -> None:
    assert switch_paths_as_nested(
        {"research.enabled": True, "multi_agent.delegation_enabled": False}
    ) == {
        "research": {"enabled": True},
        "multi_agent": {"delegation_enabled": False},
    }
