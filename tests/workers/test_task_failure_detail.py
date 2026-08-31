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
from agent_workbench.workflows.structured_output import StructuredOutputError
from agent_workbench.workflows.task_handlers import TaskNodeRunFailedError

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


def _run_failed_node() -> TaskNodeRunFailedError:
    """The shape that killed a real Task once external_search stopped timing out.

    The research node looped on pages that all redirected to the same place
    until it ran out of tokens. `budget_exceeded` was recorded on the run; the
    console showed only `TaskNodeRunFailedError`.
    """

    return TaskNodeRunFailedError(
        node="research_external",
        outcome=AgentOutcome(
            agent_run_id="run_1",
            status="failed",
            stop_reason="token_budget",
            error=ErrorInfo(
                code="budget_exceeded",
                message="the run passed its ceiling: token_budget",
                retryable=False,
            ),
        ),
        state=STATE,
        reason="token budget exhausted",
    )


def test_a_structured_node_failure_reports_its_code_not_its_class() -> None:
    detail = _failure_detail(_run_failed_node(), "start")

    assert "budget_exceeded" in detail
    assert "research_external" in detail
    assert "not retryable" in detail
    assert "TaskNodeRunFailedError" not in detail


def _decode_failure(reason: str, cause: Exception | None) -> TaskNodeRunFailedError:
    """A structured node whose run succeeded and whose output would not decode.

    ``outcome.error`` is ``None`` on purpose and not as a shortcut: that is what
    a decode failure *is*. The model answered, the provider raised nothing, and
    the value it produced was not the one the node required.
    """

    error = TaskNodeRunFailedError(
        node="critic",
        outcome=AgentOutcome(
            agent_run_id="run_1", status="completed", stop_reason="completed"
        ),
        state=STATE,
        reason=reason,
    )
    if cause is not None:
        error.__cause__ = cause
    return error


def test_a_decode_failure_says_which_schema_it_missed() -> None:
    detail = _failure_detail(
        _decode_failure("critic JSON did not satisfy the review schema", None), "start"
    )

    assert "critic" in detail
    assert "did not satisfy the review schema" in detail


def test_the_four_ways_a_review_fails_to_decode_do_not_collapse() -> None:
    """The measured subject of known-gaps C-05.

    The 2026-08-13 failure recorded two competing hypotheses and could not
    choose between them, and this is why: every decode failure reached the
    console as one sentence. A reader could not tell "the critic reviewed the
    wrong revision" from "the critic ran before there was a draft", so the
    record kept both and resolved neither.
    """

    details = [
        _failure_detail(
            _decode_failure(
                "critic JSON did not satisfy the review schema",
                StructuredOutputError(message),
            ),
            "start",
        )
        for message in (
            "critic ran before synthesis produced a draft",
            "critic output has an invalid shape",
            "critic reviewed a different draft",
            "critic reviewed a different revision",
        )
    ]

    assert len(set(details)) == 4, "the four causes must not collapse into one"
    assert "before synthesis produced a draft" in details[0]
    assert "reviewed a different revision" in details[3]


def test_a_decode_failure_never_quotes_what_the_model_actually_wrote() -> None:
    """The rule this whole function exists for, at the one place it now bends.

    Reading the cause is safe because a ``StructuredOutputError``'s own message
    is repo-authored. Reading its *chain* would not be: the pydantic error
    underneath quotes the input that failed validation, and that input is model
    output. This pins the boundary at one link.
    """

    leaked = ValueError(
        "1 validation error for ReviewResult\n  input_value='PROMPT FRAGMENT'"
    )
    cause = StructuredOutputError("critic output has an invalid shape")
    cause.__cause__ = leaked

    detail = _failure_detail(
        _decode_failure("critic JSON did not satisfy the review schema", cause), "start"
    )

    assert "critic output has an invalid shape" in detail
    assert "PROMPT FRAGMENT" not in detail
    assert "validation error for ReviewResult" not in detail


def test_a_node_with_no_reason_keeps_the_older_sentence() -> None:
    """The control. ``AgentNodeFailedError`` carries no ``reason`` -- its empty
    branch means no artifact was written, not a value that would not decode --
    so a change aimed at decode failures must leave it alone."""

    detail = _failure_detail(_empty_node(), "start")

    assert detail == "the understand step did not produce usable output during start"
