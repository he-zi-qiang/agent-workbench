"""Event envelope rules: durability, cursors and payload agreement."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import get_args

import pytest
from pydantic import ValidationError

from agent_workbench.domain.events import (
    DURABLE_EVENT_TYPES,
    EVENT_DURABILITY,
    TRANSIENT_EVENT_TYPES,
    AnswerCommitted,
    AnswerWithheld,
    ChatTurnExpired,
    EventEnvelope,
    EventType,
    ModelDelta,
    RunCompleted,
    TaskFailed,
    ToolProgress,
    ToolProposed,
)
from agent_workbench.domain.runs import BudgetUsage

TIMESTAMP = datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)


def _envelope(**overrides: object) -> EventEnvelope:
    defaults: dict[str, object] = {
        "payload": RunCompleted(stop_reason="completed", usage=BudgetUsage()),
        "stream_id": "stream_1",
        "run_id": "run_1",
        "timestamp": TIMESTAMP,
        "sequence": 7,
        "event_id": "evt_1",
    }
    merged = defaults | overrides
    return EventEnvelope.for_payload(**merged)  # pyright: ignore[reportArgumentType]


def test_every_event_type_declares_a_durability() -> None:
    assert set(get_args(EventType)) == set(EVENT_DURABILITY)


def test_only_streamed_chatter_is_transient() -> None:
    """Everything else must survive a reconnect via the durable log."""

    # Both model deltas are here for one reason: they stream at token rate and
    # their durable trace is the event that closes the call (ModelCompleted,
    # carrying `text` and `thinking_preview`).
    assert sorted(TRANSIENT_EVENT_TYPES) == [
        "ModelDelta",
        "ModelThinkingDelta",
        "ToolProgress",
    ]
    assert "ModelCompleted" in DURABLE_EVENT_TYPES
    assert "AnswerCommitted" in DURABLE_EVENT_TYPES
    assert "AnswerWithheld" in DURABLE_EVENT_TYPES
    assert "ChatTurnExpired" in DURABLE_EVENT_TYPES
    assert TRANSIENT_EVENT_TYPES.isdisjoint(DURABLE_EVENT_TYPES)


def test_only_the_answer_commit_event_makes_checked_text_public() -> None:
    committed = AnswerCommitted(text="checked")
    withheld = AnswerWithheld(text="safe refusal")

    assert committed.text == "checked"
    assert withheld.text == "safe refusal"
    assert withheld.reason_code == "sources_changed"


def test_answer_events_round_trip_through_the_discriminated_envelope() -> None:
    envelope = _envelope(payload=AnswerCommitted(text="checked"))

    restored = EventEnvelope.model_validate_json(envelope.model_dump_json())

    assert isinstance(restored.payload, AnswerCommitted)
    assert restored.payload.text == "checked"


def test_chat_turn_expiry_round_trips_as_its_own_durable_terminal_event() -> None:
    envelope = _envelope(payload=ChatTurnExpired(turn_id="turn_1"))

    restored = EventEnvelope.model_validate_json(envelope.model_dump_json())

    assert restored.event_type == "ChatTurnExpired"
    assert restored.durability == "durable"
    assert isinstance(restored.payload, ChatTurnExpired)
    assert restored.payload == ChatTurnExpired(turn_id="turn_1")


def test_chat_turn_expiry_has_only_fixed_safe_terminal_fields() -> None:
    serialized = json.loads(ChatTurnExpired(turn_id="turn_1").model_dump_json())

    assert serialized == {
        "kind": "ChatTurnExpired",
        "turn_id": "turn_1",
        "status": "failed",
        "stop_reason": "deadline",
        "error_code": "stale_execution",
        "retryable": False,
    }
    assert {
        "answer",
        "text",
        "output_text",
        "output_ref",
        "citations",
        "authorized_revisions",
    }.isdisjoint(serialized)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "committed"),
        ("stop_reason", "completed"),
        ("error_code", "provider_error"),
        ("retryable", True),
        ("output_text", "candidate answer"),
    ],
)
def test_chat_turn_expiry_cannot_be_repurposed(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        ChatTurnExpired.model_validate({"turn_id": "turn_1", field: value})


def test_task_lifecycle_failure_is_a_detail_free_machine_fact() -> None:
    payload = TaskFailed(task_id="task_1", epoch=3, attempt=2)
    serialized = json.loads(payload.model_dump_json())

    assert serialized == {
        "kind": "TaskFailed",
        "task_id": "task_1",
        "epoch": 3,
        "attempt": 2,
        "status": "failed",
        "reason_code": "execution_failed",
    }
    assert {"detail", "exception", "prompt", "secret"}.isdisjoint(serialized)


def test_durability_follows_the_payload_not_the_caller() -> None:
    delta = _envelope(
        payload=ModelDelta(model_call_id="mc_1", text="hi"),
        sequence=None,
    )

    assert delta.event_type == "ModelDelta"
    assert delta.durability == "transient"


def test_a_transient_event_must_not_claim_a_cursor_position() -> None:
    """`(stream_id, sequence)` is the SSE replay cursor; deltas are not stored."""

    with pytest.raises(ValidationError, match="transient event carries none"):
        _envelope(
            payload=ToolProgress(tool_call_id="toolu_1", message="halfway"),
            sequence=3,
        )


def test_a_durable_event_must_carry_its_stream_sequence() -> None:
    with pytest.raises(ValidationError, match="sequence assigned by its stream"):
        _envelope(sequence=None)


def test_a_declared_durability_cannot_contradict_the_event_type() -> None:
    payload = ModelDelta(model_call_id="mc_1", text="hi")

    with pytest.raises(ValidationError, match="is transient, not durable"):
        EventEnvelope(
            event_id="evt_1",
            stream_id="stream_1",
            run_id="run_1",
            event_type="ModelDelta",
            durability="durable",
            timestamp=TIMESTAMP,
            payload=payload,
            sequence=1,
        )


def test_event_type_and_payload_must_agree() -> None:
    with pytest.raises(ValidationError, match="disagrees with payload"):
        EventEnvelope(
            event_id="evt_1",
            stream_id="stream_1",
            run_id="run_1",
            event_type="RunFailed",
            durability="durable",
            timestamp=TIMESTAMP,
            payload=RunCompleted(stop_reason="completed"),
            sequence=1,
        )


def test_timestamps_are_normalized_to_utc() -> None:
    local = datetime(2026, 7, 25, 11, 14, 15, tzinfo=timezone(timedelta(hours=8)))

    envelope = _envelope(timestamp=local)

    assert envelope.timestamp == TIMESTAMP
    assert envelope.timestamp.utcoffset() == timedelta(0)


def test_a_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _envelope(timestamp=datetime(2026, 7, 25, 3, 14, 15))


def test_tool_proposals_record_a_digest_instead_of_arguments() -> None:
    """The log is read by operators and shipped to tracing backends."""

    payload = ToolProposed(
        tool_call_id="toolu_1",
        tool_name="knowledge_search",
        argument_bytes=42,
        argument_sha256="b" * 64,
        risk="read",
    )
    serialized = json.loads(payload.model_dump_json())

    assert "arguments" not in serialized
    assert serialized["argument_sha256"] == "b" * 64


def test_sequence_starts_at_one() -> None:
    with pytest.raises(ValidationError):
        _envelope(sequence=0)


def test_task_events_can_carry_their_graph_node() -> None:
    envelope = _envelope(task_id="task_1", graph_node_id="node_export")

    assert envelope.task_id == "task_1"
    assert envelope.graph_node_id == "node_export"
