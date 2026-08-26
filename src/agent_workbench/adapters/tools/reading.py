"""What both read tools do once they are holding the text.

The flat workspace and the project directory are different worlds -- one has
names, the other has paths; one versions every write, the other writes the
user's disk -- but "hand this file to a model without spending its whole
context" is the same problem in both, and it was being solved twice. Two
solutions meant two ceilings declared separately, two sentences for the same
event, and one shared defect: above the ceiling the head of the file came back
as a *success* and no argument existed that could reach the rest of it. A model
that asked for a 60,000-character file got 48,000 of it and no way to ask for
the other 12,000 -- on the project side, where `sandbox_run` cannot see the
directory, the tail was not reachable by any tool the turn was holding.

So the window and its sentence live in `domain/workspace.py`, and this module
is the part that has to know about tool results.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import JsonValue

from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.tools import ToolResult
from agent_workbench.domain.workspace import describe_read_window, read_window
from agent_workbench.ports.tools import ToolInvocation

#: The two arguments that make a long file reachable. Optional, so every read
#: written before they existed still means what it meant.
OFFSET_SCHEMA: dict[str, JsonValue] = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "The line to start at, counting from 1. Omit it to start at the top."
    ),
}

LIMIT_SCHEMA: dict[str, JsonValue] = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "At most this many lines. The context ceiling still applies and may "
        "stop the window sooner."
    ),
}


def _line_number(value: JsonValue | None) -> int | None:
    """A schema-validated integer, or ``None``.

    Checked again here rather than trusted, because the ceiling this feeds is
    the one thing standing between one tool call and the whole context budget,
    and `bool` is an `int` in Python: ``offset=true`` would arrive as 1 and
    read as though somebody meant it.
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def windowed_result(
    invocation: ToolInvocation,
    *,
    label: str,
    text: str,
    arguments: Mapping[str, JsonValue],
    note_read: Callable[[bool], None] | None = None,
) -> ToolResult:
    """One read's answer: the window, and the sentence that places it.

    A whole file that fits comes back exactly as it always did -- bare text,
    no header. The header is for the reads that are not the whole file, and it
    is worth the two lines it costs there: without it the model cannot tell a
    file that ends at line 400 from one that was cut at line 400, and it has
    been observed to write the second one as though it were the first.

    ``note_read`` is called on a successful read with whether the window was
    the whole file, and never on a refusal. It exists because that answer is
    computed here and needed by `ProjectWriteTool` (ADR-0078), and the only
    other way for a caller to learn it is to window the text a second time --
    two windowings that must agree, where the interesting bug is them
    disagreeing and licensing an overwrite of bytes nobody read. The flat
    workspace passes nothing: it versions every write, so "did you read this
    first" is a question about a file that can be recovered either way.
    """

    offset = _line_number(arguments.get("offset")) or 1
    window = read_window(
        text, offset=offset, limit=_line_number(arguments.get("limit"))
    )
    described = describe_read_window(label, window)
    if not window.text:
        # Only reachable by asking past the end: an empty file never gets here,
        # because both handlers answer that before there is a window to take.
        # A refusal rather than the last line, so the model corrects the
        # argument instead of building on an answer to a different question.
        return ToolResult.failed(
            invocation.call,
            ErrorInfo(
                code="invalid_tool_input",
                message=described or f"{label} has no line {offset}",
                retryable=False,
            ),
        )
    if note_read is not None:
        # `described is None` is exactly "this window was the whole file" --
        # the same condition that decides whether a header is worth printing,
        # asked once and used for both.
        note_read(described is None)
    if described is None:
        return ToolResult.succeeded(invocation.call, content=window.text)
    return ToolResult.succeeded(
        invocation.call, content=f"{described}\n\n{window.text}"
    )


__all__ = ["LIMIT_SCHEMA", "OFFSET_SCHEMA", "windowed_result"]
