"""How the host reads the container's stderr, now that it carries two things.

The container's stderr used to be one thing -- diagnostics -- drained to EOF and
handed to the log. It now also carries the framed preview of the running script
(ADR-069), and these are about the seam: which lines become progress, which stay
diagnostics, and what happens when a line is neither.

They drive ``_read_records`` directly over a fake stream rather than through a
container, because what is under test is the parsing and not the sandbox. The
container end is exercised in ``test_sandbox_bootstrap.py`` and, for real, in
``test_sandbox_isolation.py``.
"""

from __future__ import annotations

import asyncio
import json

from agent_workbench.apps.sandbox_mcp._bootstrap import PROGRESS_PREFIX
from agent_workbench.apps.sandbox_mcp.executor import _read_records

MAX = 64 * 1024


def _record(channel: str, text: str) -> bytes:
    body = json.dumps({"channel": channel, "text": text})
    return (PROGRESS_PREFIX + body + "\n").encode("utf-8")


def _stream(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


def _drain(payload: bytes) -> tuple[bytes, int, list[tuple[str, str]]]:
    seen: list[tuple[str, str]] = []

    async def sink(channel: str, text: str) -> None:
        seen.append((channel, text))

    async def scenario() -> tuple[bytes, int]:
        return await _read_records(_stream(payload), MAX, sink)

    kept, total = asyncio.run(scenario())
    return kept, total, seen


def test_a_record_is_dispatched_and_kept_out_of_the_diagnostics() -> None:
    """Both halves matter.

    A record that were also *kept* would put this transport's own framing into
    the operator log that ``_first_line`` reads -- an operator diagnosing a
    broken sandbox would find the script's output where the runtime's error
    should be.
    """

    kept, _, seen = _drain(
        b"docker: something went wrong\n"
        + _record("stdout", "line one\n")
        + _record("stderr", "a warning\n")
    )

    assert seen == [("stdout", "line one\n"), ("stderr", "a warning\n")]
    assert kept == b"docker: something went wrong\n"


def test_a_line_that_is_not_a_record_stays_a_diagnostic() -> None:
    payload = b"plain stderr\nmore of it\n"
    kept, total, seen = _drain(payload)

    assert seen == []
    assert kept == payload
    assert total == len(payload)


def test_a_malformed_record_is_filed_as_stderr_rather_than_guessed_at() -> None:
    """Every failure to read a well-formed record goes the same way.

    That direction is the safe one: a strange log line shown to an operator is
    recoverable, where a malformed record dispatched as script output would put
    framing in front of a reader as though the script had printed it.
    """

    broken = [
        PROGRESS_PREFIX + "not json at all\n",
        PROGRESS_PREFIX + '["a list, not an object"]\n',
        PROGRESS_PREFIX + '{"channel": "stdout"}\n',
        PROGRESS_PREFIX + '{"channel": "sideways", "text": "x"}\n',
        PROGRESS_PREFIX + '{"channel": "stdout", "text": 7}\n',
    ]
    payload = "".join(broken).encode("utf-8")
    kept, _, seen = _drain(payload)

    assert seen == []
    assert kept == payload


def test_a_record_split_across_reads_is_still_one_record() -> None:
    """A pipe hands over whatever arrived, not whatever is complete.

    The reader assembles lines itself for exactly this reason, so a record that
    lands in two chunks is not two half-records -- and the half that arrives
    first is not filed as a diagnostic while its other half is still in flight.
    """

    whole = _record("stdout", "a line that arrived in pieces\n")
    seen: list[tuple[str, str]] = []

    async def sink(channel: str, text: str) -> None:
        seen.append((channel, text))

    async def scenario() -> tuple[bytes, int]:
        reader = asyncio.StreamReader()
        reader.feed_data(whole[:20])
        reader.feed_data(whole[20:])
        reader.feed_eof()
        return await _read_records(reader, MAX, sink)

    kept, _ = asyncio.run(scenario())

    assert seen == [("stdout", "a line that arrived in pieces\n")]
    assert kept == b""


def test_a_final_line_with_no_newline_is_not_lost() -> None:
    payload = PROGRESS_PREFIX + json.dumps({"channel": "stdout", "text": "last"})
    _, _, seen = _drain(payload.encode("utf-8"))

    assert seen == [("stdout", "last")]


def test_a_sink_that_raises_does_not_stop_the_drain() -> None:
    """The container is already running; a failed preview must not end it.

    The assertion is on the record *after* the one that raised: an exception
    that escaped would take the whole drain with it, and with it the envelope
    the caller is waiting for.
    """

    seen: list[str] = []

    async def sink(channel: str, text: str) -> None:
        del channel
        seen.append(text)
        if text == "boom\n":
            raise RuntimeError("the subscriber went away")

    async def scenario() -> tuple[bytes, int]:
        payload = (
            _record("stdout", "before\n")
            + _record("stdout", "boom\n")
            + _record("stdout", "after\n")
        )
        return await _read_records(_stream(payload), MAX, sink)

    kept, _ = asyncio.run(scenario())

    assert seen == ["before\n", "boom\n", "after\n"]
    assert kept == b""


def test_diagnostics_are_still_capped_and_the_true_size_still_reported() -> None:
    """The contract `_read_capped` had, unchanged.

    Records do not count toward the cap, and should not: the cap exists to
    bound what one run puts in the operator log, and a record never goes there.
    """

    # Newline-terminated, because that is what a diagnostic is. An unbroken
    # 128 KB with no newline in it would swallow the record that follows into
    # the same line -- correctly, since a record is defined as a *line* -- and
    # the case under test here is the ordinary one.
    noise = (b"x" * 127 + b"\n") * (MAX * 2 // 128)
    payload = noise + _record("stdout", "quiet\n")
    kept, total, seen = _drain(payload)

    assert len(kept) == MAX
    assert total == len(noise)
    assert seen == [("stdout", "quiet\n")]


def test_no_sink_is_not_a_special_case_for_the_caller() -> None:
    """A caller that wants no preview still gets its diagnostics.

    And still does not get the records in them -- dropping a record is the
    behaviour whether or not anybody asked for it, because the alternative is
    that the log's contents depend on who was listening.
    """

    async def scenario() -> tuple[bytes, int]:
        payload = b"docker: broken\n" + _record("stdout", "line\n")
        return await _read_records(_stream(payload), MAX, None)

    kept, _ = asyncio.run(scenario())

    assert kept == b"docker: broken\n"
