"""A recorded chain of reasoning keeps its conclusion.

``bounded()`` cuts from the end, which is right for the things it was written
for -- a prompt preview, a proposed tool call -- where the opening identifies
what was previewed. Reasoning is the opposite shape: it runs "here is what I
saw, therefore here is what I will do", so cutting from the end drops the
decision, every time, reliably. A reader scrolling back to a tool call they did
not expect is asking *why did it run that*, and the answer is in the last
sentence.
"""

import pytest

from agent_workbench.domain.schema import (
    THINKING_TEXT_LIMIT,
    bounded_thinking,
)


@pytest.mark.parametrize(
    "length",
    [0, 1, THINKING_TEXT_LIMIT - 1, THINKING_TEXT_LIMIT],
)
def test_a_chain_that_fits_is_returned_whole(length: int) -> None:
    value = "x" * length

    assert bounded_thinking(value) == value


@pytest.mark.parametrize(
    "length",
    [THINKING_TEXT_LIMIT + 1, THINKING_TEXT_LIMIT * 2, THINKING_TEXT_LIMIT * 10],
)
def test_a_chain_that_does_not_fit_keeps_both_ends(length: int) -> None:
    # Distinguishable ends, so "kept the tail" cannot pass by accident on a
    # string of one repeated character.
    head = "起: 先读工作区"
    tail = "结论: 改 config.py 的第 12 行"
    filler = "中" * (length - len(head) - len(tail))
    value = head + filler + tail

    cut = bounded_thinking(value)

    assert len(cut) <= THINKING_TEXT_LIMIT
    assert cut.startswith(head)
    # The half `bounded()` would have thrown away.
    assert cut.endswith(tail)


def test_the_gap_is_named_rather_than_left_to_look_continuous() -> None:
    # Without a marker the two ends read as one continuous argument that was
    # never made -- the same failure as joining separate calls' reasoning.
    cut = bounded_thinking("头" * (THINKING_TEXT_LIMIT * 2))

    assert "中段省略" in cut


def test_the_conclusion_gets_a_quarter_of_the_budget() -> None:
    # Not halves: the account that explains a decision is normally longer than
    # the decision, and a reader with the setup can infer more than one with
    # only the end. Pinned because it is a judgement call somebody will
    # otherwise "simplify" to //2 without noticing it changes what survives.
    cut = bounded_thinking("a" * (THINKING_TEXT_LIMIT * 3))
    marker = "中段省略"
    head, _, tail = cut.partition(marker)

    assert len(tail) < len(head)
