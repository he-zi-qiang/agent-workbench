"""What a command started by this process is allowed to inherit (ADR-077).

Here rather than beside the tool that uses it, and not by preference:
``tests/architecture/test_dependency_boundaries.py`` allows ``os.environ`` in
``bootstrap`` and nowhere else, because a business module that reads the
environment is a module whose behaviour cannot be determined from the validated
settings it was handed. The rule is right, and it lands well here -- deciding
what a subprocess may see *is* a configuration decision, and this is where
configuration decisions are made.

The decision itself is one sentence: a command inherits everything except this
platform's own configuration.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

#: The namespace this platform's settings live in.
#:
#: ``bootstrap/settings.py`` reads ``AW_*`` and rejects unknown members of it,
#: so this prefix is exactly "configuration belonging to this system" and
#: nothing else.
_SETTINGS_PREFIX: Final[str] = "AW_"


def command_environment() -> Mapping[str, str]:
    """The parent environment, minus everything this platform put in it.

    The whole ``AW_*`` namespace is removed rather than a list of the sensitive
    members. ``AW_SECRETS__DEEPSEEK_API_KEY`` is the provider key;
    ``AW_DATABASE__DSN`` and its two siblings are connection strings, which
    ``settings.py`` treats as credentials even when today's happen to carry no
    password; ``AW_KEY_FILE`` names a file holding the first. A list would have
    to be revisited every time a setting is added, by somebody who is thinking
    about the setting rather than about this function. A namespace does not.

    This matters here in a way it does not for ``sandbox_run``, which inherits
    nothing because a fresh container has nothing to inherit. A ``project_run``
    command runs as this process's own user, and the command was written by a
    model: ``env`` is an ordinary thing to run while looking around a project,
    and without this it would print the key this process authenticates with.

    **Nothing else is scrubbed, and that is also a decision.** A command run
    inside somebody's project is meant to see their ``PATH``, their toolchain,
    their ``SSH_AUTH_SOCK`` and their own credentials -- a ``git push`` that
    cannot reach the agent socket is a broken tool, not a safe one, and a model
    that can run commands at all is not made safer by making them fail. What it
    may not have is the platform's own configuration, because that is the one
    thing in the environment the operator did not put there for the project's
    benefit.

    Called once at assembly rather than per call, like everything else built
    from settings. A variable exported into this process after startup is not
    seen by a command -- the same rule the tool registry follows, and for the
    same reason: what a run was able to do should be answerable from what the
    process was started with.
    """

    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_SETTINGS_PREFIX)
    }


__all__ = ["command_environment"]
