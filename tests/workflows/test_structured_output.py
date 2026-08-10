"""The shared decode boundary, pinned where both its callers can rely on it.

These behaviours were asserted through the graph nodes before ADR-036 lifted
the functions out of ``task_handlers``; they are pinned here directly so that
the module a second caller (triage) imports is the module under test, and so
the re-exports in ``task_handlers`` remain the same objects.
"""

from __future__ import annotations

import pytest

from agent_workbench.domain.messages import TextBlock
from agent_workbench.workflows import task_handlers
from agent_workbench.workflows.structured_output import (
    StructuredOutputError,
    StructuredOutputFramingError,
    json_object,
    restatement_messages,
)


def test_one_json_object_decodes() -> None:
    assert json_object('{"graph": "general"}') == {"graph": "general"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        'sure, here it is: {"a": 1}',
        '{"a": 1}\nthanks!',
        '```json\n{"a": 1}\n```',
        '{"a": 1',
        '["not", "an", "object"]',
        # Duplicate keys and non-standard constants surface as framing too:
        # the docstring's own claim is "every refusal here is a framing one",
        # and their StructuredOutputError is a ValueError the parse guard
        # re-raises. The non-framing class exists for *claims* -- an answer
        # that parses but says something the contract forbids -- and those are
        # raised by each caller's validate step, not by this function.
        '{"a": 1, "a": 2}',
        '{"a": NaN}',
    ],
)
def test_every_refusal_here_is_a_framing_one(text: str) -> None:
    with pytest.raises(StructuredOutputFramingError):
        json_object(text)


def test_a_wrong_claim_is_not_a_framing_failure() -> None:
    """The non-framing parent stays distinct so validate steps can use it.

    The control group is the parametrized test above: everything
    ``json_object`` itself refuses is framing, and the parent class exists
    for callers whose *validation* rejects a parseable answer (ADR-034 §3.2).
    """

    error = StructuredOutputError("claim rejected by a validate step")
    assert not isinstance(error, StructuredOutputFramingError)


def test_restatement_replays_the_unreadable_answer_as_the_models_turn() -> None:
    replayed, asked = restatement_messages("not json at all")
    assert replayed.role == "assistant"
    assert any(
        isinstance(block, TextBlock) and block.text == "not json at all"
        for block in replayed.content
    )
    assert asked.role == "user"


def test_an_empty_answer_replays_nothing() -> None:
    (asked,) = restatement_messages("")
    assert asked.role == "user"


def test_task_handlers_reexports_the_same_classes() -> None:
    """An except clause written against either module must catch both."""

    assert task_handlers.StructuredOutputError is StructuredOutputError
    assert task_handlers.StructuredOutputFramingError is StructuredOutputFramingError
