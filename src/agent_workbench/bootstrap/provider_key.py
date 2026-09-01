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


__all__ = ["ENV_VAR", "KEY_FILE_ENV_VAR", "key_file"]
