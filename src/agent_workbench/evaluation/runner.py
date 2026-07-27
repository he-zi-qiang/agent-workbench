"""Running a gold set against a retriever, and reporting what happened.

The runner does not know which retriever it is measuring. That is the point of
an ablation: dense, hybrid and hybrid-with-rerank are scored by the same code
over the same questions, so a difference between two reports is a difference in
retrieval rather than in how it was counted.

A report records what produced it -- the index identity, the gold set digest,
the question count -- because a number without those is not comparable to
anything. Two runs that disagree are only interesting if they were measuring
the same thing, and that is exactly what a bare percentage cannot tell you.

Nothing here judges an answer. Faithfulness and citation accuracy need a model
in the loop and belong to a separate runner with separate evidence; mixing them
in would make a retrieval regression and a generation regression look alike.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_workbench.evaluation.metrics import RETRIEVAL_METRICS, RetrievalOutcome


@dataclass(frozen=True, slots=True)
class GoldQuestion:
    """One question, and the document that answers it."""

    question: str
    document_id: str


@dataclass(frozen=True, slots=True)
class GoldSet:
    """A fixed set of questions, identified by its own contents."""

    questions: tuple[GoldQuestion, ...]
    digest: str

    def __len__(self) -> int:
        return len(self.questions)


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Scores, and enough about the run to know what they describe."""

    index_identity: str
    gold_digest: str
    question_count: int
    scores: dict[str, float]
    outcomes: tuple[RetrievalOutcome, ...] = field(repr=False, default=())

    def to_json(self) -> str:
        return json.dumps(
            {
                "index_identity": self.index_identity,
                "gold_digest": self.gold_digest,
                "question_count": self.question_count,
                "scores": self.scores,
            },
            indent=2,
            sort_keys=True,
        )


def load_gold_set(path: Path) -> GoldSet:
    """Read a gold set and fingerprint it.

    The digest travels into the report because a score is meaningless without
    knowing which questions produced it -- and a gold set edited between two
    runs is the easiest way to make an improvement out of nothing.
    """

    raw = path.read_bytes()
    questions: list[GoldQuestion] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        missing = {"question", "document_id"} - set(record)
        if missing:
            raise ValueError(
                f"{path}:{line_number} is missing {', '.join(sorted(missing))}"
            )
        questions.append(
            GoldQuestion(question=record["question"], document_id=record["document_id"])
        )
    if not questions:
        raise ValueError(f"{path} contains no questions")
    return GoldSet(
        questions=tuple(questions),
        digest=hashlib.sha256(raw).hexdigest()[:16],
    )


async def evaluate_retrieval(
    gold: GoldSet,
    *,
    index_identity: str,
    retrieve: Callable[[str], Awaitable[Sequence[str]]],
    monotonic: Callable[[], float] = time.monotonic,
) -> EvaluationReport:
    """Ask every question, score the answers, and say what was measured.

    ``retrieve`` returns document ids in rank order. Anything narrower would
    tie this to one retriever; anything wider would let a caller pass in
    something already scored.
    """

    outcomes: list[RetrievalOutcome] = []
    for question in gold.questions:
        started = monotonic()
        retrieved = await retrieve(question.question)
        elapsed_ms = (monotonic() - started) * 1000
        outcomes.append(
            RetrievalOutcome(
                question=question.question,
                expected_document_id=question.document_id,
                retrieved_document_ids=tuple(retrieved),
                latency_ms=elapsed_ms,
            )
        )

    return EvaluationReport(
        index_identity=index_identity,
        gold_digest=gold.digest,
        question_count=len(gold),
        scores={name: metric(outcomes) for name, metric in RETRIEVAL_METRICS.items()},
        outcomes=tuple(outcomes),
    )


__all__ = [
    "EvaluationReport",
    "GoldQuestion",
    "GoldSet",
    "evaluate_retrieval",
    "load_gold_set",
]
