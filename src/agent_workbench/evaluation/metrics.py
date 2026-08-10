"""Retrieval metrics, defined once so two reports cannot disagree.

Each metric is a named function over the same evidence: for one question, the
documents retrieved in rank order and the document that should have been found.
Keeping them in a registry rather than inline in a runner is what makes an
ablation comparable -- dense and hybrid are scored by the same code, so a
difference between them is a difference in retrieval.

Every metric here answers a question about *ranking*, not about text. Whether
an answer was faithful is a different kind of measurement, needs a judge, and
is deliberately not smuggled in beside these.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """What one question retrieved, and what it should have.

    ``expected_document_ids`` holds one entry for the classic single-document
    question and several for a cross-document one, whose answer is spread over
    the corpus. The distinction exists because top-k retrieval can look
    perfect on the first kind while structurally unable to answer the second
    -- which is exactly what ``full_coverage_at_k`` below measures and the
    single-hit metrics cannot.
    """

    question: str
    expected_document_ids: tuple[str, ...]
    retrieved_document_ids: tuple[str, ...]
    latency_ms: float = 0.0

    @property
    def rank(self) -> int | None:
        """1-based position of the first expected document, or ``None``.

        "First relevant" keeps recall@k and MRR meaning for a cross-document
        question exactly what they mean for a single-document one: did
        anything relevant surface, and how high. What they stop being able to
        say -- was the *whole* answer there -- is ``coverage_rank``'s job.
        """

        for position, document_id in enumerate(self.retrieved_document_ids, start=1):
            if document_id in self.expected_document_ids:
                return position
        return None

    @property
    def coverage_rank(self) -> int | None:
        """1-based depth at which every expected document has appeared.

        ``None`` when any expected document is absent. For a single-document
        question this equals ``rank``; for a cross-document one it is how
        deep a context window must reach before it holds the whole answer.
        """

        deepest = 0
        for expected in self.expected_document_ids:
            try:
                position = self.retrieved_document_ids.index(expected) + 1
            except ValueError:
                return None
            deepest = max(deepest, position)
        return deepest if deepest else None


def recall_at_k(outcomes: Sequence[RetrievalOutcome], *, k: int) -> float:
    """Share of questions with *an* expected document in the top k.

    The headline number for a retriever: if the answer is not in the candidate
    set, nothing downstream can recover it. On a cross-document question this
    credits the first relevant hit -- deliberately, so the number stays
    comparable across the whole gold set -- and ``full_coverage_at_k`` is the
    one that refuses partial credit.
    """

    if not outcomes:
        return 0.0
    hits = sum(1 for o in outcomes if o.rank is not None and o.rank <= k)
    return hits / len(outcomes)


def full_coverage_at_k(outcomes: Sequence[RetrievalOutcome], *, k: int) -> float:
    """Share of questions whose *every* expected document is in the top k.

    A cross-document question can earn recall@k while missing half its
    answer; a report that showed only recall would call that success. This is
    the metric that cannot be satisfied that way, and on single-document
    questions it degenerates to recall@k -- so a gap between the two numbers
    is exactly the cross-document questions failing.
    """

    if not outcomes:
        return 0.0
    hits = sum(
        1 for o in outcomes if o.coverage_rank is not None and o.coverage_rank <= k
    )
    return hits / len(outcomes)


def mrr(outcomes: Sequence[RetrievalOutcome]) -> float:
    """Mean reciprocal rank.

    Distinguishes "found it first" from "found it eighth", which recall alone
    cannot -- and which decides what actually fits in a context budget.
    """

    if not outcomes:
        return 0.0
    return sum(0.0 if o.rank is None else 1.0 / o.rank for o in outcomes) / len(
        outcomes
    )


def retrieval_latency_ms(outcomes: Sequence[RetrievalOutcome]) -> float:
    """Median, not mean.

    A mean latency is moved by one slow question; a median describes the run.
    """

    if not outcomes:
        return 0.0
    ordered = sorted(o.latency_ms for o in outcomes)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# The registry. A report names metrics from here rather than computing its own,
# so two reports of the same run cannot disagree about what a number means.
RETRIEVAL_METRICS: dict[str, Callable[[Sequence[RetrievalOutcome]], float]] = {
    "recall_at_1": lambda outcomes: recall_at_k(outcomes, k=1),
    "recall_at_3": lambda outcomes: recall_at_k(outcomes, k=3),
    "full_coverage_at_3": lambda outcomes: full_coverage_at_k(outcomes, k=3),
    "mrr": mrr,
    "retrieval_latency_ms": retrieval_latency_ms,
}

# A recall@k where k is at least the corpus size is arithmetic, not a
# measurement: every document is retrieved every time. The registry stops at 3
# because the harness retrieves TOP_K = 3 -- a coverage@5 over a 3-deep list
# could never be satisfied, and a metric that cannot fail is a metric that
# cannot report a regression. The same bound is what makes full_coverage@3
# hard for a two-document question: both halves of the answer must be in the
# top three chunks' documents.


__all__ = [
    "RETRIEVAL_METRICS",
    "RetrievalOutcome",
    "full_coverage_at_k",
    "mrr",
    "recall_at_k",
    "retrieval_latency_ms",
]
