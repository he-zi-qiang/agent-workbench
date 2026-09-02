"""The optional parts a console may switch, and the file the choice lives in.

**A switch is a stored choice for the next start, never a live change.** This
is the discipline ADR-101 set for the provider key, applied to four booleans
(ADR-103): a process reads its configuration once, at composition, and what it
assembled from that read is what it serves until it exits. So the console does
not "turn web search on"; it records that the next start should, and the
capability report says -- in two separate fields -- what is stored and what is
running. Collapsing those is how a settings page comes to claim that a switch
it flipped a second ago is already in effect.

**Why exactly these four.** Each is a plain ``bool`` in the settings model
whose every other prerequisite is already inside the image: flipping it changes
what the next start assembles and nothing else has to be installed. The other
optional parts (MCP tools, the sandbox) and the retrieval half of Chat need a
server, a socket or a different image, and a switch for those would be a
promise the deployment cannot keep. Those rows say "install" instead.

**Why the file mirrors the settings paths.** ``switches.json`` holds a flat map
from the settings path (``research.enabled``) to a boolean. That is the same
spelling ``AW_RESEARCH__ENABLED`` has after the prefix and delimiter are read
off, so a person who finds this file knows exactly which knob it moves and can
grep the settings model for it. The loader turns the map into a settings
source ranked above the TOML files and below every environment source -- an
operator's exported value still wins, which is what the capability report
reports as "overridden".

**Unknown keys are refused, in both directions.** The parser is shared by this
store and by ``load_settings``, and both reject a path that is not in
``SWITCHES``. A newer console writing a switch an older process does not know
would otherwise be silently dropped by that process, and the page would show a
choice nothing honours.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from agent_workbench.application.provider_key import DIRECTORY_MODE, FILE_MODE

#: The file's name beside the provider key. Where that directory is remains
#: ``bootstrap``'s decision (``bootstrap/switches.py``); this store is handed
#: the path and never works one out.
FILE_NAME: Final[str] = "switches.json"


class SwitchRefused(ValueError):
    """The switch was not stored or could not be read; the message says why."""


@dataclass(frozen=True, slots=True)
class SwitchSpec:
    """One switch: the settings path it moves and what "on" depends on."""

    #: The dotted settings path, exactly as the settings model spells it.
    path: str
    #: Whether "on" is a state the process can only *assemble* with a provider
    #: key. Informational for the console -- a stored "on" without a key is
    #: still stored, and the capability row says why it is not in effect.
    needs_model: bool
    #: Whether the loader must *hold* a stored "on" when no key is present.
    #: Only ``research.enabled`` today: the settings validator refuses that
    #: combination as a startup error, and a switch that could make the next
    #: start refuse -- from a page inside the process that refuses -- would be
    #: the trap ADR-102 §3 exists to avoid. Held means: not applied at this
    #: start, recorded as held, reported on the row.
    held_without_key: bool


SWITCHES: Final[tuple[SwitchSpec, ...]] = (
    SwitchSpec("research.enabled", needs_model=True, held_without_key=True),
    SwitchSpec("triage.enabled", needs_model=True, held_without_key=False),
    SwitchSpec("code.enabled", needs_model=True, held_without_key=False),
    SwitchSpec(
        "multi_agent.delegation_enabled", needs_model=False, held_without_key=False
    ),
)
SWITCH_PATHS: Final[frozenset[str]] = frozenset(spec.path for spec in SWITCHES)


def spec_for(path: str) -> SwitchSpec | None:
    return next((spec for spec in SWITCHES if spec.path == path), None)


def parse_switches(text: str, *, source: str) -> dict[str, bool]:
    """The one parser both the store and the loader use.

    Strict on purpose: a JSON object, every key a known switch path, every value
    a boolean. ``source`` names the file in the refusal so a person reading a
    startup error knows which file to open.
    """

    try:
        raw: object = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise SwitchRefused(
            f"{source} 不是合法的 JSON：{exc.msg}（第 {exc.lineno} 行）"
        ) from exc
    if not isinstance(raw, dict):
        raise SwitchRefused(f"{source} 的顶层必须是一个对象")
    decoded = cast(dict[object, object], raw)
    switches: dict[str, bool] = {}
    for key, value in decoded.items():
        if not isinstance(key, str) or key not in SWITCH_PATHS:
            raise SwitchRefused(
                f"{source} 里有一个不认识的开关 {key!r}；认识的只有 "
                + ", ".join(sorted(SWITCH_PATHS))
            )
        if not isinstance(value, bool):
            raise SwitchRefused(f"{source} 里 {key!r} 的值必须是 true 或 false")
        switches[key] = value
    return switches


@dataclass(frozen=True, slots=True)
class SwitchStore:
    """Reads and rewrites one small JSON file, and refuses the paths it must."""

    path: Path | None
    checkout_root: Path | None

    def read(self) -> dict[str, bool]:
        """What is stored for the next start. Missing file means nothing is."""

        if self.path is None:
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SwitchRefused(f"{_display(self.path)} 读不了：{exc}") from exc
        return parse_switches(raw, source=_display(self.path))

    def set(self, path: str, value: bool | None) -> dict[str, bool]:
        """Store one choice, or with ``None`` withdraw it, and return the file."""

        if path not in SWITCH_PATHS:
            raise SwitchRefused(
                f"没有叫 {path!r} 的开关；有的只是 " + ", ".join(sorted(SWITCH_PATHS))
            )
        target = self._writable_target()
        # Read strictly rather than overwrite: a file this store cannot parse
        # was edited by hand, and replacing it would destroy whatever the
        # person meant by it. They get the parser's sentence instead.
        current = self.read()
        if value is None:
            current.pop(path, None)
        else:
            current[path] = value

        target.parent.mkdir(parents=True, exist_ok=True)
        _chmod(target.parent, DIRECTORY_MODE)
        # Neighbour-and-rename, as the key store does: a crash halfway through
        # would otherwise leave a file that is half a JSON object, which the
        # next start refuses to load -- with the switch page inside it.
        handle, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=".switches-"
        )
        scratch = Path(temporary)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(current, indent=2, sort_keys=True) + "\n")
            _chmod(scratch, FILE_MODE)
            scratch.replace(target)
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise
        return current

    def _writable_target(self) -> Path:
        if self.path is None:
            raise SwitchRefused(
                "这台部署声明了「没有 key 文件」（AW_KEY_FILE 是空串），开关和 key "
                "放在同一个目录下，所以也没有可写的地方；把它取消设置再试"
            )
        if self.checkout_root is not None:
            try:
                self.path.resolve().relative_to(self.checkout_root.resolve())
            except ValueError:
                pass
            else:
                raise SwitchRefused(
                    f"拒绝把开关写进 checkout 里（{self.path}）：它和 key 同一个目录，"
                    f"而打包工具不认 .gitignore"
                )
        return self.path


def _chmod(path: Path, mode: int) -> None:
    # Best effort, for the reason the key store gives: on Windows this moves
    # only the read-only bit, and a failure here would report a stored switch
    # as a failed one.
    with contextlib.suppress(OSError):
        path.chmod(mode)


def _display(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home()).as_posix()}"
    except (ValueError, RuntimeError):
        return str(path)


def switch_paths_as_nested(switches: Mapping[str, bool]) -> dict[str, object]:
    """``{"research.enabled": True}`` as ``{"research": {"enabled": True}}``.

    What a pydantic-settings source has to return. Two levels is all the four
    paths need, but written generally so a fifth switch does not have to be.
    """

    nested: dict[str, object] = {}
    for path, value in switches.items():
        cursor: dict[str, object] = nested
        *sections, leaf = path.split(".")
        for section in sections:
            found = cursor.get(section)
            child: dict[str, object]
            if isinstance(found, dict):
                child = cast(dict[str, object], found)
            else:
                child = {}
                cursor[section] = child
            cursor = child
        cursor[leaf] = value
    return nested


__all__ = [
    "FILE_NAME",
    "SWITCHES",
    "SWITCH_PATHS",
    "SwitchRefused",
    "SwitchSpec",
    "SwitchStore",
    "parse_switches",
    "spec_for",
    "switch_paths_as_nested",
]
