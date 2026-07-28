"""What the process does when the reranker is there, and when it is not.

The asymmetry with the embedder is the thing worth testing. A missing embedder
removes chat; a missing reranker only makes its answers worse. So the assertions
here are that a reranker failure never costs the capability, and that its
absence is still recorded -- an unreranked process answers questions exactly
like a reranked one, so nothing about the response reveals which was running.
"""

from __future__ import annotations

import pytest

from agent_workbench.adapters.reranking.bge_reranker import BgeReranker
from agent_workbench.bootstrap.projections import RerankerConfig
from agent_workbench.bootstrap.reranker_factory import (
    RerankerUnavailable,
    build_reranker,
)
from agent_workbench.ports.reranker import RerankerUnavailableError

CONFIG = RerankerConfig(
    model_id="BAAI/bge-reranker-v2-m3",
    revision="main",
    batch_size=8,
    device="auto",
    timeout_seconds=15.0,
)


class StubEncoder:
    def predict(
        self,
        sentences: list[tuple[str, str]],
        *,
        batch_size: int,
        convert_to_numpy: bool,
        show_progress_bar: bool,
    ) -> list[float]:
        return [0.0 for _ in sentences]


def test_a_loadable_model_becomes_a_reranker() -> None:
    reranker = build_reranker(CONFIG, loader=lambda *_a, **_k: StubEncoder())

    assert isinstance(reranker, BgeReranker)
    assert reranker.identity == "BAAI/bge-reranker-v2-m3@main"


def test_a_missing_runtime_is_an_absence_with_a_reason() -> None:
    """Not an exception. A process without the extra still serves chat."""

    def loader(*_args: object, **_kwargs: object) -> object:
        raise RerankerUnavailableError("the real reranker needs the extra")

    reranker = build_reranker(CONFIG, loader=loader)

    assert isinstance(reranker, RerankerUnavailable)
    assert "extra" in reranker.reason


def test_missing_weights_are_reported_differently_from_a_missing_runtime() -> None:
    """The two have different fixes: an install versus a download."""

    def loader(*_args: object, **_kwargs: object) -> object:
        raise OSError("no such file")

    reranker = build_reranker(CONFIG, loader=loader)

    assert isinstance(reranker, RerankerUnavailable)
    assert "weights" in reranker.reason
    assert "unreranked" in reranker.reason


def test_auto_becomes_no_explicit_device() -> None:
    """ "auto" is this project's word, not the library's."""

    seen: dict[str, object] = {}

    def loader(*_args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return StubEncoder()

    build_reranker(CONFIG, loader=loader)

    assert seen["device"] is None


def test_an_explicit_device_is_passed_through() -> None:
    seen: dict[str, object] = {}

    def loader(*_args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return StubEncoder()

    build_reranker(
        RerankerConfig(
            model_id=CONFIG.model_id,
            revision=CONFIG.revision,
            batch_size=CONFIG.batch_size,
            device="cpu",
            timeout_seconds=CONFIG.timeout_seconds,
        ),
        loader=loader,
    )

    assert seen["device"] == "cpu"


def test_a_non_positive_batch_size_is_still_a_refusal() -> None:
    """A configuration that cannot mean anything is not an absence."""

    with pytest.raises(ValueError, match="batch_size"):
        build_reranker(
            RerankerConfig(
                model_id=CONFIG.model_id,
                revision=CONFIG.revision,
                batch_size=0,
                device="auto",
                timeout_seconds=CONFIG.timeout_seconds,
            ),
            loader=lambda *_a, **_k: StubEncoder(),
        )
