"""What this turn has actually looked at, so a write can be refused (ADR-0078).

`project_write` replaces a whole file with the text it was handed. Nothing
until now asked whether the model had ever seen the file it was replacing, so
the failure this closes is quiet by construction: the user edits a file in
their editor, the model -- working from a copy it read three tool calls ago, or
from no copy at all -- writes its version over the top, the tool answers `ok`,
the step line says "写入项目文件", and the transcript looks exactly like a turn
that went well. The user finds out when they next open the file.

Discipline 1 of the coding prompt has always asked for this ("Read before you
write"). Prose is not a precondition, and `code_prompt.py` says so about
itself: what is not enforced somewhere else should not be claimed. This is the
somewhere else.

**Per turn, not per session.** The ledger is entered around one run, beside the
project scope, and a fresh one starts every turn. That is not a limitation to
apologise for -- a receipt from an earlier turn says the model read the file
minutes or hours ago, which is precisely the belief this exists to distrust.

**Why the mechanism is a ContextVar and not a field.** Same reason
:class:`ProjectFileScope` is one: the tool registry is built once at process
start and never changes, while what a tool operates on belongs to one turn. Two
coding sessions in one process must not see each other's receipts, and a
``ContextVar`` makes "for the duration of this turn" true rather than
approximate. The value is a mutable ledger rather than an immutable mapping
because a read in one tool call has to be visible to a write in another, and
tool calls in a parallel batch run in copied contexts -- copying a context
copies the *binding*, so the two calls keep sharing the object it points at.

**What it cannot see.** Everything that writes the directory without going
through a tool: `PUT /v1/projects/{project_id}/file` from the console, the
user's own editor, git, a formatter the model itself started with
`project_run`. Only the last of those is distinguishable from here, and it is
distinguished, because "you changed this yourself, read it again" and "somebody
else changed this" call for different next moves. The rest arrive as a refusal
that names the file and asks for a re-read, which is the correct answer to all
of them even though it cannot name the cause.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ReadReceipt:
    """One file, as this turn last saw it.

    ``size_bytes`` and ``modified_at`` are copied from what the store answered
    at read time, so they describe the file the model was actually handed
    rather than the file as it stands now -- which is the whole point.

    ``covers_whole_file`` is the field that keeps this honest. A read can
    succeed and still not hand over the file: a window (`offset`/`limit`), or a
    single line longer than the inline ceiling. Recording those as "read" would
    licence an overwrite of bytes the model provably never saw, which is
    discipline 1's exact failure mode wearing a green light.
    """

    path: str
    size_bytes: int
    modified_at: datetime
    covers_whole_file: bool


@dataclass(slots=True)
class _Ledger:
    """One turn's receipts, plus whether a command has run since."""

    receipts: dict[str, ReadReceipt] = field(default_factory=dict[str, ReadReceipt])
    #: Set once `project_run` has run anything at all. Not per file, because a
    #: command's effects are not knowable from here: `black .` rewrites files
    #: nobody named, and a build writes files that did not exist. What this
    #: buys is only the difference between two sentences -- "you changed this
    #: yourself" and "somebody else did" -- and for that, "did a command run in
    #: this turn" is exactly as much as can be said truthfully.
    commands_ran: bool = False


class ReadReceiptsUnavailableError(RuntimeError):
    """A file tool ran outside a turn that entered a receipt ledger."""


class ReadReceipts:
    """Which files this turn has read, for the tools that write them."""

    __slots__ = ("_current",)

    def __init__(self) -> None:
        self._current: ContextVar[_Ledger | None] = ContextVar(
            "file_read_receipts", default=None
        )

    @contextmanager
    def using(self) -> Generator[None]:
        """Run the block with a fresh, empty ledger.

        Restored rather than cleared on the way out, for the reason
        ``ProjectFileScope.using`` gives. Here the consequence of getting it
        wrong is milder than writing into the wrong project and worse than it
        looks: a leaked ledger is a set of receipts for files as they stood in
        somebody else's turn, which is a gate that opens for stale beliefs --
        strictly more dangerous than no gate, because the transcript now shows
        a check that passed.
        """

        token = self._current.set(_Ledger())
        try:
            yield
        finally:
            self._current.reset(token)

    def _ledger(self) -> _Ledger:
        ledger = self._current.get()
        if ledger is None:
            # A refusal, not an empty ledger made on demand. An empty ledger
            # would answer "you have not read this" to every write, which reads
            # as a working gate and is actually an unwired one; and the shape
            # one line down -- returning a ledger nothing else can see -- would
            # record every read into a table no write consults, which is an
            # unwired gate that reads as a working one. Both are worse than a
            # loud failure at the first tool call.
            raise ReadReceiptsUnavailableError(
                "no read-receipt ledger is entered for this turn"
            )
        return ledger

    def record(
        self,
        path: str,
        *,
        size_bytes: int,
        modified_at: datetime,
        covers_whole_file: bool,
    ) -> None:
        """Note that this turn has seen ``path`` as described."""

        self._ledger().receipts[path] = ReadReceipt(
            path=path,
            size_bytes=size_bytes,
            modified_at=modified_at,
            covers_whole_file=covers_whole_file,
        )

    def seen(self, path: str) -> ReadReceipt | None:
        """What this turn last saw of ``path``, if anything."""

        return self._ledger().receipts.get(path)

    def note_command_ran(self) -> None:
        """Record that a host command ran in this turn."""

        self._ledger().commands_ran = True

    def commands_ran(self) -> bool:
        """Whether a host command has run in this turn."""

        return self._ledger().commands_ran


__all__ = [
    "ReadReceipt",
    "ReadReceipts",
    "ReadReceiptsUnavailableError",
]
