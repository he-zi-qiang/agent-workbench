"""What a failed Task tells the person who submitted it.

``status_detail`` reaches the event log and the API, so it may never quote a
provider's exception text -- those carry request bodies and prompt fragments.
For a long time that meant it carried only the exception's class name, which is
safe and says nothing: a transient network blip reached the console as
``AgentNodeFailedError``. The run had already classified the cause into a closed
``ErrorCode`` vocabulary one layer down, and that is what these pin.
"""

from __future__ import annotations

from agent_workbench.application.task_research import EvidenceUnavailableError
from agent_workbench.domain.errors import ErrorInfo
from agent_workbench.domain.runs import AgentOutcome
from agent_workbench.domain.tasks import TaskState
from agent_workbench.workers.task import _failure_detail
from agent_workbench.workflows.agent_nodes import AgentNodeFailedError

STATE = TaskState(task_id="task_1", objective="Explain hybrid retrieval.")


def _failed_node(error: ErrorInfo) -> AgentNodeFailedError:
    return AgentNodeFailedError(
        node="understand",
        outcome=AgentOutcome(
            agent_run_id="run_1",
            status="failed",
            stop_reason="error",
            error=error,
        ),
        state=STATE,
    )


def _empty_node() -> AgentNodeFailedError:
    """A run that finished cleanly and produced no artifact.

    The other half of the raise condition in ``AgentNode.run``, and the only
    way an ``AgentNodeFailedError`` carries no ``ErrorInfo`` -- the domain
    refuses to build a *failed* outcome without one.
    """

    return AgentNodeFailedError(
        node="understand",
        outcome=AgentOutcome(
            agent_run_id="run_1",
            status="completed",
            stop_reason="completed",
        ),
        state=STATE,
    )


def test_a_failed_node_reports_the_run_s_error_code() -> None:
    detail = _failure_detail(
        _failed_node(
            ErrorInfo(
                code="provider_error",
                message="the request to the provider failed: ProxyError",
                retryable=True,
            )
        ),
        "start",
    )

    assert "understand" in detail
    assert "provider_error" in detail
    assert "(retryable)" in detail
    # The class name is what this replaced, and the provider's text is what it
    # still must not carry.
    assert "AgentNodeFailedError" not in detail
    assert "ProxyError" not in detail


def test_a_non_retryable_cause_says_so() -> None:
    detail = _failure_detail(
        _failed_node(
            ErrorInfo(code="policy_denied", message="denied", retryable=False)
        ),
        "resume_with_approval",
    )

    assert "(not retryable)" in detail


def test_a_node_that_failed_without_an_error_still_names_the_step() -> None:
    detail = _failure_detail(_empty_node(), "start")

    assert "understand" in detail
    assert "did not produce usable output" in detail


def test_any_other_exception_keeps_reporting_only_its_type() -> None:
    """The fallback is unchanged: an unclassified exception has no safe text."""

    detail = _failure_detail(RuntimeError("secret prompt fragment"), "start")

    assert detail == "the graph raised RuntimeError during start"
    assert "secret prompt fragment" not in detail


def test_missing_evidence_says_which_way_it_was_missing() -> None:
    """The three ways a Task loses its evidence must not read identically.

    Each of these killed a real Task on 2026-08-13 and every one of them
    reached the console as "the graph raised EvidenceUnavailableError during
    start", which named the class and nothing a reader could act on.
    """

    details = [
        _failure_detail(EvidenceUnavailableError(message), "start")
        for message in (
            "internal research requires a knowledge base",
            "external search returned no evidence",
            "external_search exceeded its 30s timeout",
        )
    ]

    assert len(set(details)) == 3, "the three causes must not collapse into one"
    assert "requires a knowledge base" in details[0]
    assert "returned no evidence" in details[1]
    assert "30s timeout" in details[2]
    for detail in details:
        assert "EvidenceUnavailableError" not in detail, "the class name is not a cause"


def test_missing_evidence_still_names_the_action_it_died_during() -> None:
    detail = _failure_detail(EvidenceUnavailableError("no evidence"), "resume")

    assert "during resume" in detail
