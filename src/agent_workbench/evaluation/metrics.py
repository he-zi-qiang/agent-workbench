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
    """What one question retrieved, and what it should have."""

    question: str
    expected_document_id: str
    retrieved_document_ids: tuple[str, ...]
    latency_ms: float = 0.0

    @property
    def rank(self) -> int | None:
        """1-based position of the expected document, or ``None`` if absent."""

        for position, document_id in enumerate(self.retrieved_document_ids, start=1):
            if document_id == self.expected_document_id:
                return position
        return None


def recall_at_k(outcomes: Sequence[RetrievalOutcome], *, k: int) -> float:
    """Share of questions whose expected document appears in the top k.

    The headline number for a retriever: if the answer is not in the candidate
    set, nothing downstream can recover it.
    """

    if not outcomes:
        return 0.0
    hits = sum(1 for o in outcomes if o.rank is not None and o.rank <= k)
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
    "mrr": mrr,
    "retrieval_latency_ms": retrieval_latency_ms,
}

# A recall@k where k is at least the corpus size is arithmetic, not a
# measurement: every document is retrieved every time. The registry stops at 3
# because the fixed corpus holds six documents, and a metric that cannot fail
# is a metric that cannot report a regression.


__all__ = [
    "RETRIEVAL_METRICS",
    "RetrievalOutcome",
    "mrr",
    "recall_at_k",
    "retrieval_latency_ms",
]
