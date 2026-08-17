"""Put an MCP tool's file into the working set, so the rest of the Task can see it.

An MCP server hands back bytes and the result mapping stores them as an
artifact (ADR-026 §2.2: the server receives no path and no owner, so the Worker
is what assigns both). That artifact is real and downloadable, and until now it
was also invisible to everything except the attachment rail: the workspace
manifest never learned a name for it.

That gap is what made a Word Task fail. v2's ``review`` node judges the working
set -- it holds ``workspace_list``, ``workspace_read`` and ``workspace_grep``
and nothing else -- so a run whose whole product was a rendered ``.docx``
looked like this from the reviewer's chair:

    decision: revise
    summary: The workspace is empty -- no Word document or any file exists to
             review.

The writer had called the renderer, the gateway had allowed it, a 37 KB Word
package was sitting in the artifact store, and the Task still burned its two
revisions and failed. Three round trips to a model, each one asked to fix
something that was never wrong.

So the binding happens here, once, at the seam where an MCP result comes back
inside a node that has entered a session. Not in the agent runtime, which knows
nothing about workspaces and should not; not left to the model, which would
make a file's existence depend on remembering a second call.

``write_ref`` rather than a copy: the bytes are already stored under this
Task's tenant and owner, and a manifest entry is a name pointing at them
(ADR-028). Nothing is duplicated and nothing changes hands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from agent_workbench.application.workspace_scope import WorkspaceScope
from agent_workbench.domain.tools import ToolResult
from agent_workbench.domain.workspace import WorkspaceOverflowError
from agent_workbench.ports.tools import ToolBinding, ToolHandler, ToolInvocation

#: The flat-name rule, the same one ``_NAME_SCHEMA`` states to the model. A
#: server is free to call its file anything at all, including nothing, so the
#: name it suggests is a candidate rather than an answer.
_LEGAL_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class _WorkspaceBound:
    """One MCP handler, with its file bound into the working set afterwards."""

    inner: ToolHandler
    scope: WorkspaceScope

    async def __call__(self, invocation: ToolInvocation) -> ToolResult:
        result = await self.inner(invocation)
        if result.status != "ok" or result.artifact is None:
            return result
        session = self.scope.current()
        if session is None:
            # A node that entered no session has no working set to bind into --
            # the researcher, for one. The artifact is stored either way and the
            # result is returned untouched, because a tool that started
            # reporting a workspace name only some of the time would be worse
            # than one that never does.
            return result

        name = _workspace_name(result)
        try:
            session.version = await session.workspace.write_ref(
                session.version, name, result.artifact
            )
        except (ValueError, WorkspaceOverflowError) as error:
            # A full workspace does not undo a rendering that already happened.
            # The model is told the file exists and where it does not, which is
            # the only version of this it can act on.
            return result.model_copy(
                update={
                    "content": _appended(
                        result.content,
                        f"It could not be added to the workspace: {error}",
                    )
                }
            )
        # Said out loud, because the model's next move depends on it: the file
        # is now something `workspace_list` shows and `workspace_read` opens,
        # and the reviewer will be looking for exactly that.
        #
        # And said twice, in two registers, because the two readers are not the
        # same reader. The sentence is for the model. `workspace_writes` is for
        # everything that is not a model (ADR-063): it survives
        # `record_step_inputs=False`, where the sentence does not, because the
        # sentence reaches a console only by way of `output_preview`.
        #
        # This binding is the case that argues hardest for the field. The
        # docstring at the top of this module records why it exists at all -- a
        # Word Task whose entire product was a rendered .docx failed review
        # with "The workspace is empty", because the manifest never learned a
        # name for it. Leaving that one file type reachable only through a
        # sentence would have rebuilt the same hole one layer up.
        return result.model_copy(
            update={
                "content": _appended(
                    result.content,
                    f"It is in the workspace as {name}.",
                ),
                # One name, not `(*result.workspace_writes, name)`: an MCP tool
                # runs out of process and cannot touch this workspace, so
                # anything already in that tuple did not come from a write and
                # would be a claim this binding cannot support.
                "workspace_writes": (name,),
            }
        )


def bind_results_into_workspace(
    binding: ToolBinding, scope: WorkspaceScope
) -> ToolBinding:
    """Wrap ``binding`` so a file it returns lands in the working set.

    The spec is untouched. What the model is offered, what the envelope names
    and what the gateway checks are all the same tool; only what happens after
    a successful result changes.
    """

    return ToolBinding(
        spec=binding.spec,
        handler=_WorkspaceBound(binding.handler, scope),
        operation_key=binding.operation_key,
    )


def _workspace_name(result: ToolResult) -> str:
    """What to call the file, preferring what the server called it.

    Deterministic, and deliberately not unique-ified with a counter or a
    timestamp: a second render under the same name replaces the first, which is
    what "the current document" means to a reviewer reading the working set. The
    superseded version is not lost -- the workspace is versioned and the earlier
    artifact still exists under its own id.
    """

    artifact = result.artifact
    suggested = None if artifact is None else artifact.filename
    if suggested is not None and _LEGAL_NAME.match(suggested):
        return suggested
    return f"{result.tool_name}.bin"


def _appended(content: str, sentence: str) -> str:
    return sentence if not content else f"{content}\n\n{sentence}"


__all__ = ["bind_results_into_workspace"]
