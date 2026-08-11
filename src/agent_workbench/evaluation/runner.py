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
from typing import cast

from agent_workbench.evaluation.metrics import RETRIEVAL_METRICS, RetrievalOutcome


@dataclass(frozen=True, slots=True)
class GoldQuestion:
    """One question, and the document or documents that answer it.

    One entry is the classic shape. Several entries mean the answer is spread
    across the corpus, and the question exists to measure exactly that: a
    top-k retriever can ace every single-document question while never
    surfacing both halves of a cross-document one (the falsification baseline
    ADR-037's graph arm has to beat).
    """

    question: str
    document_ids: tuple[str, ...]


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

    def outcomes_to_json(self) -> str:
        """Per-question detail, in a file of its own.

        Deliberately not folded into ``to_json``. Those reports are compared
        byte for byte across changes -- that is how the ADR-033 refactor was
        shown not to move any ranking -- and a score file that grew a payload
        of questions would end that comparison for a reason unrelated to
        retrieval.

        What it is for: an aggregate says *how many* questions lost part of
        their answer, and cannot say *which document* went missing. The
        difference decides what to build. ``missing_document_ids`` is the
        column worth reading.
        """

        return json.dumps(
            {
                "index_identity": self.index_identity,
                "gold_digest": self.gold_digest,
                "question_count": self.question_count,
                "outcomes": [
                    {
                        "question": outcome.question,
                        "expected_document_ids": list(outcome.expected_document_ids),
                        "retrieved_document_ids": list(outcome.retrieved_document_ids),
                        # The whole point of this file.
                        "missing_document_ids": [
                            document_id
                            for document_id in outcome.expected_document_ids
                            if document_id not in outcome.retrieved_document_ids
                        ],
                        "rank": outcome.rank,
                        "coverage_rank": outcome.coverage_rank,
                    }
                    for outcome in self.outcomes
                ],
            },
            ensure_ascii=False,
            indent=2,
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
        questions.append(_gold_question(record, source=f"{path}:{line_number}"))
    if not questions:
        raise ValueError(f"{path} contains no questions")
    return GoldSet(
        questions=tuple(questions),
        digest=hashlib.sha256(raw).hexdigest()[:16],
    )


def _gold_question(record: dict[str, object], *, source: str) -> GoldQuestion:
    """One line, in either of its two shapes, or a refusal that names the line.

    ``document_id`` (a string) is the original single-document form and every
    existing line; ``document_ids`` (a non-empty list) is the cross-document
    form. Exactly one must be present: a line carrying both would leave the
    scorer to pick which claim to measure against.
    """

    if "question" not in record:
        raise ValueError(f"{source} is missing question")
    single = record.get("document_id")
    several = record.get("document_ids")
    if (single is None) == (several is None):
        raise ValueError(
            f"{source} must carry exactly one of document_id, document_ids"
        )
    if single is not None:
        if not isinstance(single, str) or not single:
            raise ValueError(f"{source} document_id must be a non-empty string")
        expected = (single,)
    else:
        if not isinstance(several, list) or not several:
            raise ValueError(
                f"{source} document_ids must be a non-empty list of strings"
            )
        names: list[str] = []
        for item in cast("list[object]", several):
            if not isinstance(item, str) or not item:
                raise ValueError(
                    f"{source} document_ids must be a non-empty list of strings"
                )
            names.append(item)
        if len(set(names)) != len(names):
            raise ValueError(f"{source} document_ids repeats a document")
        expected = tuple(names)
    return GoldQuestion(question=str(record["question"]), document_ids=expected)


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
                expected_document_ids=question.document_ids,
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
