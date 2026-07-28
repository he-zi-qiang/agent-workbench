"""The reranker adapter, against a stand-in and against the real weights.

Split deliberately. Everything a stand-in can answer -- batching, pair
construction, positional alignment, the refusal of a miscounted result -- is
asserted here and runs everywhere, including CI, which does not install the
optional extra. Everything only the real model can answer is gated behind the
weights environment variable and says so when it skips, rather than quietly
reporting a pass for a check that never ran.

The stand-in records what it was asked, because the interesting defects in this
adapter are in what it hands the model: a cross-encoder scores (query, passage)
pairs, and an adapter that built (passage, query) or reused one query for the
wrong passage would still return the right number of plausible floats.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from agent_workbench.adapters.reranking.bge_reranker import (
    BgeReranker,
    CrossEncoderModel,
)
from agent_workbench.ports.reranker import RerankerContractError, RerankerPort

MODEL_ID = "BAAI/bge-reranker-v2-m3"
REVISION = "main"
WEIGHTS_ENV_VAR = "AGENT_WORKBENCH_TEST_EMBEDDING_MODEL"

QUERY = "how does the lease get fenced"
PASSAGES = (
    "A fencing token is issued with every lease renewal.",
    "The cafeteria serves lunch until two.",
    "Leases expire after ninety seconds without a heartbeat.",
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


PredictCall = tuple[tuple[tuple[str, str], ...], int]


@dataclass
class RecordingCrossEncoder:
    """Scores by position, and remembers exactly how it was called."""

    scores: tuple[float, ...] | None = None
    calls: list[PredictCall] = field(default_factory=list[PredictCall])

    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        batch_size: int,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> Sequence[float]:
        self.calls.append((tuple(sentences), batch_size))
        if self.scores is not None:
            return list(self.scores)
        return [float(len(sentences) - index) for index in range(len(sentences))]


def _load(
    model: CrossEncoderModel | None = None, **overrides: object
) -> tuple[BgeReranker, RecordingCrossEncoder]:
    encoder = RecordingCrossEncoder() if model is None else model
    reranker = BgeReranker.load(
        model_id=MODEL_ID,
        revision=REVISION,
        loader=lambda *_args, **_kwargs: encoder,
        **overrides,  # pyright: ignore[reportArgumentType]
    )
    return reranker, encoder  # pyright: ignore[reportReturnType]


def test_the_adapter_satisfies_the_port() -> None:
    reranker, _ = _load()

    assert isinstance(reranker, RerankerPort)


def test_the_identity_carries_the_revision() -> None:
    """Two revisions of one reranker rank differently and raise nothing."""

    reranker, _ = _load()

    assert reranker.identity == f"{MODEL_ID}@{REVISION}"


async def test_each_pair_is_the_query_then_the_passage_in_order() -> None:
    """Reversed pairs still produce plausible floats, so assert the order."""

    reranker, encoder = _load()

    await reranker.rerank(QUERY, PASSAGES)

    pairs, _ = encoder.calls[0]
    assert pairs == tuple((QUERY, passage) for passage in PASSAGES)


async def test_scores_come_back_aligned_with_the_passages() -> None:
    reranker, _ = _load(RecordingCrossEncoder(scores=(0.25, 9.0, -3.0)))

    scores = await reranker.rerank(QUERY, PASSAGES)

    assert scores == (0.25, 9.0, -3.0)


async def test_a_miscounted_result_is_refused_rather_than_returned() -> None:
    """Positional alignment is the contract; a short list would misattribute."""

    reranker, _ = _load(RecordingCrossEncoder(scores=(1.0, 2.0)))

    with pytest.raises(RerankerContractError, match="2 scores"):
        await reranker.rerank(QUERY, PASSAGES)


async def test_no_passages_does_not_reach_the_model() -> None:
    """An empty list has nothing to rank, and a forward pass would be waste."""

    reranker, encoder = _load()

    assert await reranker.rerank(QUERY, ()) == ()
    assert encoder.calls == []


async def test_the_configured_batch_size_reaches_the_model() -> None:
    """A batch size nothing applies is a configuration key that does nothing."""

    reranker, encoder = _load(batch_size=3)

    await reranker.rerank(QUERY, PASSAGES)

    _, batch_size = encoder.calls[0]
    assert batch_size == 3


def test_a_non_positive_batch_size_is_refused() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        _load(batch_size=0)


# --- what only the real model can answer -------------------------------------


def _real() -> BgeReranker:
    if not os.environ.get(WEIGHTS_ENV_VAR):
        pytest.skip(
            f"{WEIGHTS_ENV_VAR} is not set: the real reranker contract needs "
            "the 'embedding' extra and local weights"
        )
    return BgeReranker.load(model_id=MODEL_ID, revision=REVISION)


async def test_the_real_model_prefers_the_passage_about_the_query() -> None:
    """The one claim a stand-in cannot support: that this model ranks usefully.

    Deliberately coarse. It asserts that a passage answering the question
    outscores an unrelated one -- not a margin, not a rank correlation, because
    those are gold-set questions and belong in the ablation report, where they
    can be reproduced against a named revision.
    """

    reranker = _real()

    scores = await reranker.rerank(QUERY, PASSAGES)

    assert scores[0] > scores[1]
    assert scores[2] > scores[1]
