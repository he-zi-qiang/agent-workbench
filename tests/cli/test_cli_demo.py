"""The CLI slice: one command in, one transcript out.

The golden files are the point of this suite. A vertical slice that only
asserts "something was printed" would not notice a renamed event field, a lost
sequence or an answer that stopped streaming, and those are exactly the
regressions the walking skeleton exists to catch early.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent_workbench.apps.cli.demo import DEMO_REPLY
from agent_workbench.apps.cli.main import EXIT_COMPLETED, EXIT_FAILED, main
from agent_workbench.apps.cli.rendering import TIMELINE_HEADER

GOLDEN_DIR = Path(__file__).parent / "golden"

SCENARIOS: dict[str, tuple[tuple[str, ...], str]] = {
    "completed_text": (("demo",), "demo_completed.txt"),
    "completed_json": (("demo", "--format", "json"), "demo_completed.jsonl"),
    "refusal_text": (
        ("demo", "--propose-tool", "read_document"),
        "demo_tool_refusal.txt",
    ),
    "refusal_json": (
        ("demo", "--propose-tool", "read_document", "--format", "json"),
        "demo_tool_refusal.jsonl",
    ),
}


def _run(*argv: str) -> tuple[int, str]:
    stream = io.StringIO()
    code = main(argv, stream=stream)
    return code, stream.getvalue()


def _records(transcript: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in transcript.splitlines() if line]


def test_the_demo_answers_and_reports_success() -> None:
    code, transcript = _run("demo")

    assert code == EXIT_COMPLETED
    assert DEMO_REPLY in transcript
    assert "outcome: completed (completed)" in transcript


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_transcript_is_byte_identical_across_runs(name: str) -> None:
    """A demo that is not reproducible cannot be pinned by a golden file."""

    argv, _ = SCENARIOS[name]

    assert _run(*argv) == _run(*argv)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_transcript_matches_the_committed_golden_file(name: str) -> None:
    argv, filename = SCENARIOS[name]
    _, transcript = _run(*argv)

    assert transcript == (GOLDEN_DIR / filename).read_text(encoding="utf-8")


def test_the_live_json_transcript_carries_the_token_deltas() -> None:
    _, transcript = _run("demo", "--format", "json")
    records = _records(transcript)
    event_types = [
        record["event"]["event_type"]
        for record in records
        if record["record"] == "event"
    ]

    assert "ModelDelta" in event_types
    assert records[-1]["record"] == "outcome"


def test_the_replayed_timeline_omits_the_token_deltas() -> None:
    """What replay returns is what a reconnecting client would receive."""

    _, transcript = _run("demo")
    timeline = transcript.split(TIMELINE_HEADER, maxsplit=1)[1]

    assert "ModelDelta" not in timeline
    assert "ModelCompleted" in timeline
    assert "RunCompleted" in timeline


def test_transient_events_never_claim_a_cursor_position() -> None:
    _, transcript = _run("demo", "--format", "json")
    deltas = [
        record["event"]
        for record in _records(transcript)
        if record["record"] == "event" and record["event"]["event_type"] == "ModelDelta"
    ]

    assert deltas
    assert all(delta["sequence"] is None for delta in deltas)


def test_the_durable_count_matches_the_replayed_stream() -> None:
    _, transcript = _run("demo", "--format", "json")
    records = _records(transcript)
    durable_live = [
        record
        for record in records
        if record["record"] == "event" and record["event"]["sequence"] is not None
    ]
    outcome_record = records[-1]

    assert outcome_record["durable_event_count"] == len(durable_live)


def test_the_transcript_never_contains_the_prompt() -> None:
    """No event carries a prompt body, so no consumer of events can print one."""

    prompt = "canary-prompt-must-not-be-logged"
    _, text = _run("demo", "--prompt", prompt, "--format", "json")

    assert prompt not in text


def test_the_scripted_reply_flows_through_to_the_answer() -> None:
    _, transcript = _run("demo", "--reply", "A shorter answer.")

    assert "A shorter answer." in transcript
    assert DEMO_REPLY not in transcript


def test_a_proposed_tool_call_fails_the_run() -> None:
    """The skeleton owns no tool loop, and says so instead of dropping it."""

    code, transcript = _run("demo", "--propose-tool", "read_document")

    assert code == EXIT_FAILED
    assert "ToolProposed" in transcript
    assert "owns no tool loop" in transcript
    assert "outcome: failed (error)" in transcript


def test_the_refusal_records_a_digest_rather_than_the_arguments() -> None:
    _, transcript = _run("demo", "--propose-tool", "read_document", "--format", "json")
    proposed = next(
        record["event"]["payload"]
        for record in _records(transcript)
        if record["record"] == "event"
        and record["event"]["event_type"] == "ToolProposed"
    )

    assert "arguments" not in proposed
    assert len(proposed["argument_sha256"]) == 64
    assert proposed["argument_bytes"] > 0
    assert "doc_1" not in transcript


def test_an_unregistered_tool_choice_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run("demo", "--propose-tool", "rm_rf")

    assert excinfo.value.code == 2


def test_a_missing_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 2
