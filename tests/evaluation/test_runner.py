"""The runner: what it loads, what it records, and what it refuses.

The scores it produces are only meaningful with a real embedder, which CI does
not have. What CI can check is everything around them -- that the gold set is
read faithfully, that a report says what produced it, and that a retriever's
ranking reaches the metrics unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_workbench.evaluation import (
    GoldSet,
    evaluate_retrieval,
    load_gold_set,
)

GOLD_PATH = Path("evals/rag/gold.jsonl")


def _write(tmp_path: Path, *records: dict[str, str]) -> Path:
    path = tmp_path / "gold.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


# --- the committed gold set --------------------------------------------------


def test_the_committed_gold_set_meets_the_exit_condition() -> None:
    """WP04 asks for at least twenty questions over a fixed corpus."""

    gold = load_gold_set(GOLD_PATH)

    assert len(gold) >= 20


def test_every_gold_question_names_documents_that_exist() -> None:
    """A question pointing at nothing would score zero for the wrong reason."""

    corpus = {f"doc_{p.stem}" for p in Path("evals/rag/corpus").glob("*.md")}
    gold = load_gold_set(GOLD_PATH)

    named = {doc for q in gold.questions for doc in q.document_ids}
    missing = sorted(named - corpus)

    assert missing == []


def test_the_gold_set_covers_more_than_one_document() -> None:
    """All questions on one document would make retrieval look perfect."""

    gold = load_gold_set(GOLD_PATH)

    assert len({doc for q in gold.questions for doc in q.document_ids}) >= 5


def test_the_committed_gold_set_has_a_cross_document_section() -> None:
    """The falsification baseline needs questions top-k can fail at: at
    least a dozen whose answer spans two or more documents."""

    gold = load_gold_set(GOLD_PATH)

    cross = [q for q in gold.questions if len(q.document_ids) >= 2]

    assert len(cross) >= 12


# --- loading -----------------------------------------------------------------


def test_the_digest_changes_when_a_question_changes(tmp_path: Path) -> None:
    """A gold set edited between runs is the easiest way to fake an improvement."""

    one = load_gold_set(_write(tmp_path, {"question": "a", "document_id": "doc_a"}))
    other = load_gold_set(_write(tmp_path, {"question": "b", "document_id": "doc_a"}))

    assert one.digest != other.digest


def test_a_record_missing_a_field_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="document_id"):
        load_gold_set(_write(tmp_path, {"question": "a"}))


def test_the_cross_document_form_loads_in_order(tmp_path: Path) -> None:
    gold = load_gold_set(
        _write(
            tmp_path,
            {"question": "a", "document_ids": ["doc_a", "doc_b"]},  # type: ignore[dict-item]
        )
    )

    assert gold.questions[0].document_ids == ("doc_a", "doc_b")


def test_a_record_carrying_both_forms_is_refused(tmp_path: Path) -> None:
    """Two claims about the same question would leave the scorer to pick."""

    with pytest.raises(ValueError, match="exactly one"):
        load_gold_set(
            _write(
                tmp_path,
                {
                    "question": "a",
                    "document_id": "doc_a",
                    "document_ids": ["doc_b"],  # type: ignore[dict-item]
                },
            )
        )


def test_an_empty_or_duplicated_document_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty list"):
        load_gold_set(
            _write(tmp_path, {"question": "a", "document_ids": []})  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="repeats"):
        load_gold_set(
            _write(
                tmp_path,
                {"question": "a", "document_ids": ["doc_a", "doc_a"]},  # type: ignore[dict-item]
            )
        )


def test_an_empty_gold_set_is_refused(tmp_path: Path) -> None:
    """Every metric would report zero, which reads like a total failure."""

    path = tmp_path / "gold.jsonl"
    path.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no questions"):
        load_gold_set(path)


# --- the run -----------------------------------------------------------------


def _gold(tmp_path: Path) -> GoldSet:
    return load_gold_set(
        _write(
            tmp_path,
            {"question": "one", "document_id": "doc_a"},
            {"question": "two", "document_id": "doc_b"},
        )
    )


def test_a_perfect_retriever_scores_one(tmp_path: Path) -> None:
    async def retrieve(question: str) -> Sequence[str]:
        return ["doc_a"] if question == "one" else ["doc_b"]

    report = asyncio.run(
        evaluate_retrieval(_gold(tmp_path), index_identity="ideal", retrieve=retrieve)
    )

    assert report.scores["recall_at_1"] == 1.0
    assert report.scores["mrr"] == 1.0


def test_a_retriever_that_finds_nothing_scores_zero(tmp_path: Path) -> None:
    """The control: the runner is not reporting ones regardless of the input."""

    async def retrieve(question: str) -> Sequence[str]:
        return ["doc_unrelated"]

    report = asyncio.run(
        evaluate_retrieval(_gold(tmp_path), index_identity="blind", retrieve=retrieve)
    )

    assert report.scores["recall_at_3"] == 0.0
    assert report.scores["mrr"] == 0.0


def test_a_report_records_what_produced_it(tmp_path: Path) -> None:
    """A percentage without these is not comparable to anything."""

    async def retrieve(question: str) -> Sequence[str]:
        return ["doc_a"]

    gold = _gold(tmp_path)
    report = asyncio.run(
        evaluate_retrieval(gold, index_identity="bge-m3@main+approx", retrieve=retrieve)
    )

    assert report.index_identity == "bge-m3@main+approx"
    assert report.gold_digest == gold.digest
    assert report.question_count == 2


def test_latency_comes_from_the_injected_clock(tmp_path: Path) -> None:
    """Otherwise the number depends on how busy the machine was."""

    ticks = iter([0.0, 0.25, 1.0, 1.5])

    async def retrieve(question: str) -> Sequence[str]:
        return ["doc_a"]

    report = asyncio.run(
        evaluate_retrieval(
            _gold(tmp_path),
            index_identity="clocked",
            retrieve=retrieve,
            monotonic=lambda: next(ticks),
        )
    )

    assert report.scores["retrieval_latency_ms"] == pytest.approx(375.0)


def test_the_report_serialises_without_its_raw_outcomes(tmp_path: Path) -> None:
    """A report is meant to be committed as evidence, not to carry a corpus."""

    async def retrieve(question: str) -> Sequence[str]:
        return ["doc_a"]

    report = asyncio.run(
        evaluate_retrieval(_gold(tmp_path), index_identity="x", retrieve=retrieve)
    )
    rendered = json.loads(report.to_json())

    assert set(rendered) == {
        "index_identity",
        "gold_digest",
        "question_count",
        "scores",
    }
