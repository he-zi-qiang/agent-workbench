"""Deadline arithmetic: the shortest bound wins, and it says which one it was."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_workbench.runtime.budgets import (
    ModelCallDeadline,
    effective_model_deadline,
    effective_tool_timeout,
    remaining_run_seconds,
)

NOW = datetime(2026, 7, 25, 3, 14, 15, tzinfo=UTC)


def test_a_run_without_a_deadline_has_no_remaining_time_to_report() -> None:
    assert remaining_run_seconds(None, now=NOW) is None


def test_remaining_time_counts_down_and_can_go_negative() -> None:
    assert remaining_run_seconds(NOW + timedelta(seconds=30), now=NOW) == 30
    assert remaining_run_seconds(NOW - timedelta(seconds=5), now=NOW) == -5


def test_a_call_with_no_bounds_is_unbounded() -> None:
    deadline = effective_model_deadline(
        envelope_seconds=None,
        run_deadline=None,
        now=NOW,
    )

    assert deadline == ModelCallDeadline(seconds=None, source="none")
    assert deadline.expired is False


def test_the_envelope_applies_when_the_run_has_no_deadline() -> None:
    deadline = effective_model_deadline(
        envelope_seconds=120,
        run_deadline=None,
        now=NOW,
    )

    assert (deadline.seconds, deadline.source) == (120, "model_envelope")


def test_the_run_deadline_applies_when_there_is_no_envelope() -> None:
    deadline = effective_model_deadline(
        envelope_seconds=None,
        run_deadline=NOW + timedelta(seconds=9),
        now=NOW,
    )

    assert (deadline.seconds, deadline.source) == (9, "run_deadline")


def test_the_shorter_bound_wins() -> None:
    """Two independent timers would let a call wait for the longer of them."""

    near = effective_model_deadline(
        envelope_seconds=120,
        run_deadline=NOW + timedelta(seconds=9),
        now=NOW,
    )
    far = effective_model_deadline(
        envelope_seconds=5,
        run_deadline=NOW + timedelta(seconds=900),
        now=NOW,
    )

    assert (near.seconds, near.source) == (9, "run_deadline")
    assert (far.seconds, far.source) == (5, "model_envelope")


def test_a_tie_is_reported_as_the_run_deadline() -> None:
    """Its expiry ends the run, so it is the stricter consequence to report."""

    deadline = effective_model_deadline(
        envelope_seconds=30,
        run_deadline=NOW + timedelta(seconds=30),
        now=NOW,
    )

    assert deadline.source == "run_deadline"


def test_a_passed_deadline_is_expired() -> None:
    deadline = effective_model_deadline(
        envelope_seconds=120,
        run_deadline=NOW - timedelta(seconds=1),
        now=NOW,
    )

    assert deadline.expired is True
    assert deadline.source == "run_deadline"


def test_the_source_decides_how_the_run_reports_it() -> None:
    """Out of run deadline is a budget outcome; an overrun call is a provider one."""

    run = ModelCallDeadline(seconds=0, source="run_deadline")
    envelope = ModelCallDeadline(seconds=5, source="model_envelope")

    assert run.stop_reason() == "deadline"
    assert run.to_error().code == "budget_exceeded"
    assert run.to_error().retryable is False

    assert envelope.stop_reason() == "error"
    assert envelope.to_error().code == "provider_error"
    assert envelope.to_error().retryable is True


def test_a_tool_is_bounded_by_its_own_timeout_when_the_run_has_room() -> None:
    assert effective_tool_timeout(30, run_budget_seconds=None) == 30
    assert effective_tool_timeout(30, run_budget_seconds=120) == 30


def test_a_tool_cannot_outlive_the_run_that_authorized_it() -> None:
    assert effective_tool_timeout(3600, run_budget_seconds=10) == 10
    assert effective_tool_timeout(30, run_budget_seconds=-2) == -2
