"""Offline evaluation of retrieval against a fixed corpus."""

from agent_workbench.evaluation.metrics import RETRIEVAL_METRICS, RetrievalOutcome
from agent_workbench.evaluation.runner import (
    EvaluationReport,
    GoldQuestion,
    GoldSet,
    digest_corpus,
    evaluate_retrieval,
    load_gold_set,
)

__all__ = [
    "RETRIEVAL_METRICS",
    "EvaluationReport",
    "GoldQuestion",
    "GoldSet",
    "RetrievalOutcome",
    "digest_corpus",
    "evaluate_retrieval",
    "load_gold_set",
]
