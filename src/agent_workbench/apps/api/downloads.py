"""What every route that hands back bytes says about the file it is handing back.

One function, in one place, because two routes now stream stored content -- an
artifact by id, and a coding session's workspace entry by name -- and a header
that two modules spell two ways is a header that will eventually disagree with
itself. The bug that produces is not a crash: it is one download arriving with
a usable filename and the other with `attachment; filename="artifact"`, which
nobody notices until somebody has saved twenty of them.
"""

from __future__ import annotations

from urllib.parse import quote


def content_disposition(filename: str | None) -> str:
    """Encode display metadata without letting it become response syntax."""

    resolved = filename or "artifact"
    fallback = "".join(
        character
        if character.isascii()
        and (character.isalnum() or character in {".", "_", "-", " "})
        else "_"
        for character in resolved
    ).strip(" .")
    if not fallback or fallback in {".", ".."}:
        fallback = "artifact"
    encoded = quote(resolved, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


__all__ = ["content_disposition"]
