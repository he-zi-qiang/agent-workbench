"""Turning the first thing somebody asked for into the name of the session.

Its own module, and a pure function, because the alternative is a slice
expression buried in a service method -- and this has three rules that each
exist for a reason worth writing down once rather than rediscovering from a
list of sessions that all read the same.
"""

from __future__ import annotations

#: Long enough to tell two coding sessions apart, short enough to sit in a
#: sidebar column. The column behind it allows 256; this is a display decision,
#: not a storage one.
DEFAULT_TITLE_LIMIT = 120

#: Marks a name as the head of something longer. One character, so it costs the
#: title almost nothing, and unambiguous -- a name that genuinely ends in "..."
#: still reads differently from one that ends in this.
ELLIPSIS = "…"


def title_from_instruction(
    text: str, *, limit: int = DEFAULT_TITLE_LIMIT
) -> str | None:
    """A session name derived from the first thing asked of it.

    ``None`` when nothing usable is left. An instruction only has to be one
    character to be accepted, and a single space passes that check -- so a
    title taken verbatim could be whitespace, which stores fine and renders as
    a blank row somebody cannot click on with any confidence.

    Only the first line, because a multi-line instruction usually opens with
    the request and continues with the details; the request is the part that
    identifies it. Interior whitespace is collapsed for the same reason a name
    is not a document: two spaces and a newline are formatting, and formatting
    inside a one-line label is just noise.
    """

    for line in text.splitlines():
        collapsed = " ".join(line.split())
        if not collapsed:
            continue
        # Counted in characters rather than bytes: this is a label, and the
        # limit exists to bound how much of it a reader has to scan.
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit].rstrip() + ELLIPSIS
    return None


__all__ = ["DEFAULT_TITLE_LIMIT", "ELLIPSIS", "title_from_instruction"]
