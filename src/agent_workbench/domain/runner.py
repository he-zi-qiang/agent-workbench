"""What the command runner is called, from the side that has to name it (ADR-0115).

``project_run`` has one name and, since ADR-0115, two places it can execute:
the process that holds the coding session (the native launcher -- the user's
own machine), or a separate container that holds nothing but the project
directory. The tool name, its risk and its gate do not change with the place.
What changes is a transport, and a transport needs the remote tool's name.

The names live here rather than beside the client for the reason
``domain/sandbox.py`` gives for its own: the API's assembly and the settings
validator both have to say which server this is, and neither may import an
adapter. ``apps/runner_mcp`` is an outer-boundary package and the dependency
test fails a core module that reaches into one.

There is deliberately no ``RUNNER_TOOL`` here. The model-facing tool is still
``project_run`` (``domain/project_files.py``), with the same ``destructive``
risk and the same "every call stops for a person" gate -- ADR-0115 §3.1 is
that a command in a key-less container is *less* to worry about than one on
the host, not a different kind of thing, so it is not a different tool.
"""

from __future__ import annotations

from typing import Final

RUNNER_ALIAS: Final[str] = "runner"
RUNNER_REMOTE_TOOL: Final[str] = "run_command"

__all__ = [
    "RUNNER_ALIAS",
    "RUNNER_REMOTE_TOOL",
]
