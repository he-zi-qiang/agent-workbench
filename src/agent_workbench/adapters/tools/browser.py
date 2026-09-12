"""The one browser tool that has to know where a project session's files are.

The six browser tools reach a coding session through ordinary MCP discovery
(ADR-0113 §4): the server's own schema, the server's own names, nothing local
in between. One of them, ``browser_open``, takes a ``workspace_path`` that the
*server* resolves against the one root it was started with -- the artifact
store natively, ``/workspace`` under Compose. That is right for a flat
workspace session, whose files land there, and wrong for a project session,
whose files are in the user's own directory: ``workspace_path="mario.html"``
opened ``<artifact root>/mario.html``, which does not exist, and
``ERR_FILE_NOT_FOUND`` was the only clue.

The model cannot route around this itself. A project turn is told paths are
relative to the project root and that absolute ones are refused
(``_PROJECT_WORLD``), so it never learns where the root *is* -- and the same
sentence that keeps it from writing outside the directory keeps it from
naming a ``file://`` URL inside one.

So the API side does the one thing it knows and the server does not: when a
turn has entered a project (the ``ProjectFileScope`` is set, the same
ContextVar every ``project_*`` tool reads), a ``workspace_path`` is resolved
against *that* directory and sent on as a ``file://`` URL. The path is judged
by the store first, which is what refuses ``..`` and absolute paths for every
other tool, so this cannot be the one way to name a file outside the root.

**The two sides have to see the directory at the same path**, and that is a
deployment fact this module cannot check. Natively both processes are on one
machine. Under Compose the browser container mounts the host folder at the
path the API mounts it (``/projects``, read-only on the browser's side);
``tests/deployment/test_compose.py`` pins that the two mounts share a source.
"""

from __future__ import annotations

from dataclasses import replace

from agent_workbench.adapters.mcp.naming import tool_name_for
from agent_workbench.application.project_file_scope import ProjectFileScope
from agent_workbench.domain.browser import BROWSER_ALIAS
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.project_files import ProjectPathError
from agent_workbench.domain.tools import ToolResult
from agent_workbench.ports.tools import ToolBinding, ToolInvocation

#: The local name discovery gives the server's ``browser_open`` -- derived by
#: the same function that names every discovered tool, so a change to the
#: naming scheme cannot leave this module wrapping a name nothing is bound to.
_open_name = tool_name_for(BROWSER_ALIAS, "browser_open")
# `tool_name_for` answers a `SkipReason` for a name it cannot normalise, and
# "browser_open" is seven ASCII letters and an underscore -- so the branch is
# unreachable, and the assertion is what tells the type checker so.
assert isinstance(_open_name, str), _open_name
OPEN_TOOL_LOCAL_NAME: str = _open_name


def open_within_project(binding: ToolBinding, scope: ProjectFileScope) -> ToolBinding:
    """``binding`` with a project turn's ``workspace_path`` resolved for it.

    Any binding that is not the open tool is returned as it was: the caller
    maps this over every discovered binding, and five of the six have no path
    to resolve. A turn that entered no project -- a flat workspace session --
    passes straight through as well, and the server resolves the path against
    its own root exactly as before.
    """

    if binding.spec.name != OPEN_TOOL_LOCAL_NAME:
        return binding
    inner = binding.handler

    async def handle(invocation: ToolInvocation) -> ToolResult:
        store = scope.current()
        arguments = invocation.call.arguments
        path = arguments.get("workspace_path")
        # `url` given alongside is the server's refusal to make, not this
        # module's: it will answer "give exactly one" in its own words.
        if store is None or not isinstance(path, str) or "url" in arguments:
            return await inner(invocation)
        try:
            present = await store.exists(path)
        except ProjectPathError as error:
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="invalid_tool_input",
                    message=str(error),
                    retryable=False,
                ),
            )
        if not present:
            # Said here, in the tool's own vocabulary, rather than left to the
            # browser's `ERR_FILE_NOT_FOUND`: the model wrote this path a
            # moment ago with `project_write`, and the sentence it needs is
            # the one every other project tool would give it.
            return ToolResult.failed(
                invocation.call,
                ErrorInfo(
                    code="not_found",
                    message=f"{path} is not in the project directory",
                    retryable=False,
                ),
            )
        target = (store.working_directory / path).as_uri()
        rewritten = invocation.call.model_copy(
            update={
                "arguments": {
                    **{
                        key: value
                        for key, value in arguments.items()
                        if key != "workspace_path"
                    },
                    "url": target,
                }
            }
        )
        return await inner(replace(invocation, call=rewritten))

    return ToolBinding(spec=binding.spec, handler=handle)


__all__ = ["OPEN_TOOL_LOCAL_NAME", "open_within_project"]
