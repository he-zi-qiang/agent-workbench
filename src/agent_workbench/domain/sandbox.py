"""What the ephemeral sandbox is called, and what it costs to call it (ADR-029).

The names live in the domain rather than beside the handler for the same reason
the workspace ones do: an agent profile and the authorization envelope both have
to name this tool, and neither may import an adapter.

``risk="external"`` is the substantive declaration here, and it is not a
formality. The sandbox cannot reach the network and leaves nothing behind, so it
is repeatable -- but the content does leave this process for an execution
environment outside it, and that is what ``external`` describes. A deployment
that enables the sandbox therefore raises its Task envelope's risk ceiling, and
that widening is exactly the thing the envelope exists to make visible.
"""

from __future__ import annotations

from typing import Final

#: The tool an agent calls. Named for what it does to the caller's world -- it
#: runs code over the working set -- rather than for the language, because the
#: language is an implementation fact of the server behind it.
SANDBOX_RUN_TOOL: Final[str] = "sandbox_run"

#: What a principal must hold before it may be dispatched.
SANDBOX_RUN_SCOPE: Final[str] = "sandbox:run"

#: The remote tool this project's own sandbox server publishes. It is named
#: here, not discovered, because the Task-side tool is written against this one
#: contract rather than against whatever a directory happens to advertise.
SANDBOX_REMOTE_TOOL: Final[str] = "run_python"

__all__ = [
    "SANDBOX_REMOTE_TOOL",
    "SANDBOX_RUN_SCOPE",
    "SANDBOX_RUN_TOOL",
]
