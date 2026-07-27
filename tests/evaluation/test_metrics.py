"""The metrics themselves, against rankings whose scores are known by hand.

A runner that executes is not a runner that measures. These fix each metric
against inputs where the right answer is arithmetic, so a change to the
implementation has to disagree with a number somebody can check rather than
with whatever it produced last time.
"""

from __future__ import annotations

import pytest

from agent_workbench.evaluation import RETRIEVAL_METRICS, RetrievalOutcome


def _outcome(*retrieved: str, expected: str = "doc_a", latency: float = 0.0):
    return RetrievalOutcome(
        question="q",
        expected_document_id=expected,
        retrieved_document_ids=retrieved,
        latency_ms=latency,
    )


def test_the_rank_is_one_based() -> None:
    """Rank 1 means first; a zero-based rank would make MRR twice too large."""

    assert _outcome("doc_a", "doc_b").rank == 1
    assert _outcome("doc_b", "doc_a").rank == 2


def test_a_missing_document_has_no_rank() -> None:
    assert _outcome("doc_b", "doc_c").rank is None


def test_recall_counts_only_within_k() -> None:
    outcomes = [_outcome("doc_b", "doc_c", "doc_a")]

    assert RETRIEVAL_METRICS["recall_at_1"](outcomes) == 0.0
    assert RETRIEVAL_METRICS["recall_at_3"](outcomes) == 1.0


def test_recall_is_a_share_of_questions() -> None:
    outcomes = [
        _outcome("doc_a"),
        _outcome("doc_b"),
        _outcome("doc_a"),
        _outcome("doc_c"),
    ]

    assert RETRIEVAL_METRICS["recall_at_1"](outcomes) == 0.5


def test_mrr_is_the_mean_of_reciprocal_ranks() -> None:
    """Ranks 1 and 2 give (1 + 0.5) / 2."""

    outcomes = [_outcome("doc_a"), _outcome("doc_b", "doc_a")]

    assert RETRIEVAL_METRICS["mrr"](outcomes) == pytest.approx(0.75)


def test_mrr_scores_a_miss_as_zero() -> None:
    """Not as a small number: a miss contributes nothing, it is not nearly right."""

    outcomes = [_outcome("doc_a"), _outcome("doc_z")]

    assert RETRIEVAL_METRICS["mrr"](outcomes) == pytest.approx(0.5)


def test_mrr_distinguishes_first_from_third() -> None:
    """What recall alone cannot say, and what a context budget cares about."""

    first = [_outcome("doc_a", "doc_x", "doc_y")]
    third = [_outcome("doc_x", "doc_y", "doc_a")]

    assert RETRIEVAL_METRICS["recall_at_3"](first) == RETRIEVAL_METRICS["recall_at_3"](
        third
    )
    assert RETRIEVAL_METRICS["mrr"](first) > RETRIEVAL_METRICS["mrr"](third)


def test_latency_is_a_median_not_a_mean() -> None:
    """One slow question must not move the number that describes the run."""

    outcomes = [
        _outcome("doc_a", latency=10.0),
        _outcome("doc_a", latency=11.0),
        _outcome("doc_a", latency=900.0),
    ]

    assert RETRIEVAL_METRICS["retrieval_latency_ms"](outcomes) == 11.0


def test_an_even_number_of_latencies_averages_the_middle_two() -> None:
    outcomes = [_outcome("doc_a", latency=t) for t in (10.0, 20.0, 30.0, 40.0)]

    assert RETRIEVAL_METRICS["retrieval_latency_ms"](outcomes) == 25.0


def test_every_metric_handles_an_empty_run() -> None:
    """A run with no questions is a configuration error, not a crash."""

    for name, metric in RETRIEVAL_METRICS.items():
        assert metric([]) == 0.0, name
