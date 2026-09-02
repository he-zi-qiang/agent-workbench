"""Where the switches file is, and the one decision a launcher makes about it.

The same shape as ``bootstrap/provider_key.py``, for the same reason: the
process environment may only be read in this package, and "where does the
console's file live" is a question that needs it. The answer is *beside the
key* -- ``switches.json`` in the key file's directory -- so a deployment that
declared no key file (``AW_KEY_FILE=""``) has declared no switches file either.
One variable, one directory, one volume in Compose; a second location would be
a second thing to mount, own and forget.
"""

from __future__ import annotations

from pathlib import Path

from agent_workbench.application.switches import (
    FILE_NAME,
    SwitchRefused,
    SwitchStore,
)
from agent_workbench.bootstrap.provider_key import key_file, usable_key_present

RESEARCH_SWITCH = "research.enabled"


def switches_file() -> Path | None:
    """The console's switches file, or ``None`` when this deployment has none."""

    key = key_file()
    return None if key is None else key.parent / FILE_NAME


def launcher_decides_web_search() -> bool:
    """Whether a launcher should turn research on for this start.

    ADR-102 §3 made the container launcher decide Chat's web search by probing
    for a key, because the switch cannot be written statically. ADR-103 puts a
    stored choice above that probe: when the console has recorded a value for
    ``research.enabled`` -- either one -- the launcher exports nothing, and the
    settings loader applies the stored value (or holds it, when "on" meets no
    key). Only when nobody decided does the probe's answer stand.

    Two launchers ask, through ``docker/decide_web_search.py``:
    ``docker/run-api-local.sh`` since ADR-102, and the ``demo-api`` /
    ``demo-worker`` arms of ``scripts/dev.sh`` since ADR-104. The native arms
    used to export ``true`` unconditionally, which ranked above the stored
    switch and made the System page report every native start as
    ``overridden``.

    A switches file this process cannot parse is answered ``False`` rather
    than by a second opinion: the loader is about to refuse it out loud, with
    the file's name, and a launcher that enabled research on top of that
    refusal would only add a second error in front of the real one.
    """

    path = switches_file()
    if path is not None:
        try:
            stored = SwitchStore(path=path, checkout_root=None).read()
        except SwitchRefused:
            return False
        if RESEARCH_SWITCH in stored:
            return False
    return usable_key_present()


__all__ = ["RESEARCH_SWITCH", "launcher_decides_web_search", "switches_file"]
