"""What a file written into a working set is called on the wire.

One table, because there were two and they disagreed. ``workspace.py`` typed
what the model wrote and ``sandbox.py`` typed what a script wrote, each with its
own suffix list and its own fallback, and neither knew about the other. What the
divergence cost, precisely:

* ``.png`` was in the sandbox's list and not in the workspace tool's.
* ``.jpg``, ``.jpeg``, ``.gif`` and ``.webp`` were in **neither**. A matplotlib
  script calling ``savefig("chart.png")`` produced a file the console showed;
  the same script calling ``savefig("chart.jpg")`` produced one it could only
  offer as a download, because ``application/octet-stream`` reaches no viewer.
  Nothing about the picture changed -- only the three letters after the dot.
* ``.pdf`` was in the sandbox's list and not the workspace tool's, and the
  workspace tool's fallback was ``text/plain``, so a PDF it somehow typed would
  have been offered to a text viewer.

The general form of the bug is the one ADR-066 is about: **a file's fate should
not depend on which half of the system wrote it.** A reader looking at
``config.yaml`` in a working set has no way to know, and no reason to care,
whether ``workspace_write`` or a script put it there -- yet that was what
decided whether they could read it in place.

The media type is decided **once, when the file is written, and stored in the
manifest** (``ArtifactRef`` is immutable and the ``ArtifactStore`` port has
neither an update nor a delete). So a guess made here is not a display default
that a better guess can override later: it is written down. Widening this table
does not retroactively fix a ``.jpg`` written before it.
"""

from __future__ import annotations

#: Suffix to media type, longest-suffix-wins is not needed because no entry here
#: is a suffix of another.
#:
#: The ``.svg`` row carries its own history and it is the argument for why this
#: table needs an owner: typed ``text/plain`` by one writer and
#: ``application/octet-stream`` by the other, a diagram an agent had drawn
#: reached neither the image viewer nor anything else -- it opened as its own
#: markup, or not at all (ADR-062 §2). Rasterised through ``<img>`` a scripted
#: SVG's scripts never run, which is why admitting it here is safe.
_SUFFIX_MEDIA_TYPES: dict[str, str] = {
    # Text a person or a model writes.
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".ndjson": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".py": "text/x-python",
    # Source and configuration, all as plain text. Deliberately not
    # `text/javascript` or `application/toml`: the console's only question about
    # these is "can it be read", the answer is yes, and a more specific type
    # buys a label while risking a viewer nobody wrote.
    ".js": "text/plain",
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".jsx": "text/plain",
    ".css": "text/plain",
    ".xml": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".toml": "text/plain",
    ".ini": "text/plain",
    ".sh": "text/plain",
    ".sql": "text/plain",
    # Pictures. The four that were in neither table are the reason this module
    # exists; a plotting script picks its format from the filename it is given.
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    # Documents a script can produce even though `workspace_write` may not
    # declare them (`_UNWRITABLE_SUFFIXES` refuses model-synthesised bytes
    # claiming these formats; a script that ran reportlab or openpyxl really
    # did produce one).
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".zip": "application/zip",
}

#: How much of an unrecognised file is examined to decide whether it is text.
#:
#: 8 KiB rather than the whole thing: a file large enough for the difference to
#: matter is one where reading all of it to answer a labelling question is the
#: wrong trade, and a payload whose first 8 KiB is clean UTF-8 with no NUL and
#: whose tail is not was not going to be served correctly by either answer.
_SNIFF_BYTES = 8 * 1024


def media_type_for(name: str, content: bytes) -> str:
    """The media type to record for ``name``, sniffing the bytes if unknown.

    The fallback is a function of the **bytes**, not of which caller asked --
    and that is the whole reason the two tables could be merged at all. The old
    fallbacks were both right for their own caller and wrong for the other:
    ``workspace_write``'s content arrives as a JSON string and is therefore
    always UTF-8 text, so ``text/plain`` was correct there; a sandbox output
    arrives base64-decoded and can be anything, so ``application/octet-stream``
    was correct there. Asking the bytes gives each caller its old answer without
    either one having to know who it is.

    A single fallback of ``text/plain`` was considered and rejected: a ``.rar``,
    or an ``.xlsx`` written by a script under a name this table does not list,
    would go from "download only" -- honest -- to being fetched, truncated at
    512 KiB and rendered into a ``<pre>`` as mojibake. Turning *I do not know*
    into a confident wrong answer is worse than the gap it closes, especially
    for a value that is written into a manifest and cannot be revised.
    """

    lowered = name.lower()
    for suffix, media_type in _SUFFIX_MEDIA_TYPES.items():
        if lowered.endswith(suffix):
            return media_type
    return "text/plain" if _looks_like_text(content) else "application/octet-stream"


def _looks_like_text(content: bytes) -> bool:
    """Whether these bytes decode as UTF-8 and carry no NUL.

    Two checks rather than one. UTF-8 alone admits a file whose first bytes
    happen to decode and which is plainly binary; a NUL is the single most
    reliable marker that something is not text, and no text this project
    produces contains one. Empty content is text: an empty file has no evidence
    against it, and the readable answer is the one a reader can do something
    with.
    """

    head = content[:_SNIFF_BYTES]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character straddling the cut is not evidence of binary.
        # Retry one character's worth shorter before concluding anything; UTF-8
        # sequences are at most four bytes.
        for trim in (1, 2, 3):
            if trim >= len(head):
                return False
            try:
                head[:-trim].decode("utf-8")
            except UnicodeDecodeError:
                continue
            return True
        return False
    return True


__all__ = ["media_type_for"]
