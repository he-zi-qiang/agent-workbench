"""Reading exactly one JSON object out of a model turn, and asking again once.

Lifted out of ``task_handlers`` because ADR-036 gave the same decode a second
caller: submission-time triage runs the same "one object, no framing, one
corrective turn" contract the structured graph nodes do, and two copies of the
framing rules would be two places for them to disagree about what counts as
one object.

Only the state-free pieces live here. The graph-node version of the corrective
turn -- resolving a second invocation, absorbing both runs' budgets, failing
the node -- stays in ``task_handlers``, because it is inseparable from leases
and ``TaskState``. What both callers share is the boundary itself (ADR-034):
``StructuredOutputFramingError`` is the one failure a second turn may fix, and
everything else is a claim the model made and got wrong, which asking again
would only nudge toward an answer that passes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from agent_workbench.domain.messages import Message, assistant_message, user_message


class StructuredOutputError(ValueError):
    """A model response is not the exact structured value its caller requires."""


class StructuredOutputFramingError(StructuredOutputError):
    """The message was not exactly one JSON object, whatever it contained.

    Separated from its parent because this is the one failure a second turn can
    fix, and the boundary is what keeps that turn honest (ADR-034 §3.2). A
    message with a sentence in front of its object says nothing about whether
    the answer is right; a message that *is* one object but names the wrong
    draft, or an item with no locator, is a claim the model made and got wrong.
    Asking again there would be nudging a model toward an answer that passes.
    """


def json_object(text: str) -> dict[str, Any]:
    """Reject fences, tails, duplicate keys and non-standard JSON constants.

    Every refusal here is a framing one: the message the model sent was not
    exactly one JSON object. Nothing in this function reads an object out of a
    message that also contains something else -- a model can describe, quote or
    refuse an object as easily as it can answer with one, and no parser can
    tell those apart (ADR-034 §2).
    """

    if not text or not text.lstrip().startswith("{") or "```" in text:
        raise StructuredOutputFramingError("structured output must be one JSON object")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StructuredOutputFramingError(
            "structured output is not valid JSON"
        ) from error
    if not isinstance(value, dict):
        raise StructuredOutputFramingError("structured output must be a JSON object")
    return cast("dict[str, Any]", value)


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredOutputError("structured output has duplicate object keys")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise StructuredOutputError(f"invalid JSON constant: {value}")


def restatement_messages(answer: str) -> tuple[Message, ...]:
    """The corrective turn: the message that could not be read, and the ask.

    The unreadable answer is replayed as what it was -- the model's own turn --
    so the object it must send again is the one it already reached rather than
    one this code went looking for. A run with an empty answer replays nothing,
    because there is no turn to quote.
    """

    asked = user_message(
        "Your last message was not exactly one JSON object, so it could not be "
        "read. Send that JSON object again as your entire message: no "
        "narration before or after it, no Markdown, no code fence. Restate the "
        "answer you already reached and add nothing to it."
    )
    if not answer:
        return (asked,)
    return (assistant_message(text=answer), asked)


__all__ = [
    "StructuredOutputError",
    "StructuredOutputFramingError",
    "json_object",
    "restatement_messages",
]
