"""Which profile assembled this process, as a label a console can print.

One function, and it reads exactly one thing: the ``AW_CONFIG_FILE`` control
variable every launcher already sets (``config.demo-local.toml`` natively,
``config.compose-local.toml`` in the stack). The label is the file's stem with
its ``config.`` prefix removed -- ``demo-local``, ``compose-local`` -- or
``default`` when nothing was named, which is what ``load_settings`` falls back
to as well.

A label, not an identity: two Workers started from the same profile share it,
and that is the point. The console uses it to say "the Worker I can see was
assembled from the same profile as this API", which is the question a person
whose Task sits in ``queued`` actually has.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DEPLOYMENT_LABEL = "default"
CONFIG_FILE_VARIABLE = "AW_CONFIG_FILE"


def deployment_label() -> str:
    raw = _control_env(CONFIG_FILE_VARIABLE)
    if raw is None:
        return DEFAULT_DEPLOYMENT_LABEL
    # `load_settings` accepts several files separated by the platform's path
    # separator or a comma and layers them in order; the last one named is the
    # most specific, so it is the one worth naming.
    parts = [
        part.strip()
        for chunk in raw.split(os.pathsep)
        for part in chunk.split(",")
        if part.strip()
    ]
    if not parts:
        return DEFAULT_DEPLOYMENT_LABEL
    stem = Path(parts[-1]).stem
    label = stem.removeprefix("config.").strip()
    return label or DEFAULT_DEPLOYMENT_LABEL


def _control_env(name: str) -> str | None:
    """Case-insensitive, like ``bootstrap.settings._read_control_env``."""

    target = name.upper()
    for key, value in os.environ.items():
        if key.upper() == target:
            return value or None
    return None


__all__ = ["CONFIG_FILE_VARIABLE", "DEFAULT_DEPLOYMENT_LABEL", "deployment_label"]
