"""The ledger itself: what it refuses, and what it does not carry between turns.

The tool-level behaviour is in `tests/adapters/test_project_tools.py`; this is
the two properties that make the gate above it mean anything -- that a ledger
nobody entered is a loud failure rather than a quiet pass, and that a receipt
never outlives the turn that earned it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_workbench.application.file_read_receipts import (
    ReadReceipts,
    ReadReceiptsUnavailableError,
)

SEEN_AT = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _record(receipts: ReadReceipts, path: str, *, whole: bool = True) -> None:
    receipts.record(path, size_bytes=42, modified_at=SEEN_AT, covers_whole_file=whole)


class TestOutsideATurn:
    """Every method refuses, and that is the design rather than an oversight.

    The two shapes this could have taken instead are both worse, and both look
    fine in a transcript. Making an empty ledger on demand answers "you have
    not read this" to every write -- a gate that refuses everything reads as a
    working one and is an unwired one. Returning a private ledger records every
    read into a table no write consults -- an unwired gate that reads as a
    working one, which is the failure ADR-0078 exists to remove.
    """

    def test_recording_refuses(self) -> None:
        with pytest.raises(ReadReceiptsUnavailableError):
            _record(ReadReceipts(), "README.md")

    def test_asking_refuses(self) -> None:
        with pytest.raises(ReadReceiptsUnavailableError):
            ReadReceipts().seen("README.md")

    def test_noting_a_command_refuses(self) -> None:
        with pytest.raises(ReadReceiptsUnavailableError):
            ReadReceipts().note_command_ran()


class TestInsideATurn:
    def test_a_receipt_answers_what_was_recorded(self) -> None:
        receipts = ReadReceipts()

        with receipts.using():
            _record(receipts, "README.md")
            receipt = receipts.seen("README.md")

        assert receipt is not None
        assert receipt.path == "README.md"
        assert receipt.size_bytes == 42
        assert receipt.modified_at == SEEN_AT
        assert receipt.covers_whole_file

    def test_a_file_nobody_read_has_none(self) -> None:
        receipts = ReadReceipts()

        with receipts.using():
            assert receipts.seen("never-opened.md") is None

    def test_a_later_read_replaces_the_earlier_one(self) -> None:
        # The model re-reads a file it has been told changed; the receipt that
        # matters is the one describing what it just saw. A ledger that kept
        # the first would refuse the write forever.
        receipts = ReadReceipts()

        with receipts.using():
            _record(receipts, "README.md", whole=False)
            _record(receipts, "README.md", whole=True)
            receipt = receipts.seen("README.md")

        assert receipt is not None
        assert receipt.covers_whole_file

    def test_commands_start_unrun(self) -> None:
        receipts = ReadReceipts()

        with receipts.using():
            assert not receipts.commands_ran()
            receipts.note_command_ran()
            assert receipts.commands_ran()


class TestOneTurnDoesNotInheritAnother:
    """The property that makes "per turn" true rather than approximate.

    A receipt carried into the next turn is a claim that the model has *just*
    seen a file, made about a file as it stood minutes or hours ago -- which is
    precisely the belief this whole mechanism exists to distrust, now wearing
    the gate's own approval.
    """

    def test_a_second_turn_starts_empty(self) -> None:
        receipts = ReadReceipts()

        with receipts.using():
            _record(receipts, "README.md")
        with receipts.using():
            assert receipts.seen("README.md") is None
            assert not receipts.commands_ran()

    def test_leaving_a_turn_restores_the_one_outside_it(self) -> None:
        # `using` restores rather than clears, for the reason `ProjectFileScope`
        # does: a turn that runs inside another must not blank the outer one's
        # receipts on its way out.
        receipts = ReadReceipts()

        with receipts.using():
            _record(receipts, "outer.md")
            with receipts.using():
                _record(receipts, "inner.md")
                assert receipts.seen("outer.md") is None
            assert receipts.seen("outer.md") is not None
            assert receipts.seen("inner.md") is None
