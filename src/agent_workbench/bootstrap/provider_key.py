"""Where the provider key file is, when this deployment has one.

Only that. Reading the file is ``load_settings``'s business and writing it is
``application/provider_key.py``'s; what lives here is the one question that
needs the process environment, and the process environment may only be read in
this package.

``scripts/dev.sh`` has resolved this same path since long before Python did,
and the semantics below are its semantics rather than an improvement on them:

* ``AW_KEY_FILE`` unset means the default path;
* ``AW_KEY_FILE=""`` means **no key file at all** -- the shell writes
  ``${AW_KEY_FILE-...}`` with ``-`` rather than ``:-`` for exactly that, and
  three profile tests set it empty so a refusal keeps being asserted on a
  machine that does have a key sitting in the default place.

The variable was not in ``CONTROL_ENV_VARS`` until this module existed, so
exporting it -- the spelling ``.env.example`` and ``CLAUDE.md`` both describe --
made every process refuse to start with "unknown Agent Workbench environment
variable". Nobody hit it because the shell assigns it without ``export``.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_workbench.bootstrap.settings import PLACEHOLDER_PREFIXES

#: The variable the settings model reads. A value here always wins over a file.
ENV_VAR = "AW_SECRETS__DEEPSEEK_API_KEY"
#: The variable that relocates the file, spelled as ``scripts/dev.sh`` spells it.
KEY_FILE_ENV_VAR = "AW_KEY_FILE"


def key_file() -> Path | None:
    """The configured key file, or ``None`` when this deployment declares none."""
    configured = os.environ.get(KEY_FILE_ENV_VAR)
    if configured is None:
        return Path.home() / ".config" / "agent-workbench" / "key"
    if not configured:
        return None
    return Path(configured).expanduser()


def usable_key_present() -> bool:
    """Whether *some* start of this process would find a real provider key.

    Asked by a launcher, not by the application: ``research.enabled`` without a
    key is a startup error by design (settings, "configuration describes a
    system that does not exist"), so anything that turns research on has to know
    the answer *before* the process starts. A container launcher that guessed
    would trade "chat without web search" for "a stack that will not come up",
    which is a much worse failure on the machine least able to debug it.

    Deliberately weaker than what `load_settings` does. It answers "there is a
    key here", not "that key works" -- no request is made, and a wrong-but-real
    key is indistinguishable from a right one until the provider says otherwise.
    That is the same standing `dev.sh` has always had for the same test.

    The placeholder rule is imported rather than restated, because the whole
    value of this function is that it agrees with the validator it is trying to
    stay ahead of. The whitespace handling is `_read_stored_provider_key`'s, and
    `tests/config/test_provider_key_probe.py` pins the two together.
    """

    exported = os.environ.get(ENV_VAR)
    if exported is not None and _is_real(exported):
        return True
    path = key_file()
    if path is None:
        return False
    try:
        stored = path.read_text(encoding="utf-8")
    except OSError:
        # Missing or unreadable is "absent", exactly as it is for the loader
        # that will read this file a moment later.
        return False
    return _is_real(stored)


def _is_real(value: str) -> bool:
    normalized = "".join(value.split()).lower()
    return bool(normalized) and not normalized.startswith(PLACEHOLDER_PREFIXES)


__all__ = ["ENV_VAR", "KEY_FILE_ENV_VAR", "key_file", "usable_key_present"]
