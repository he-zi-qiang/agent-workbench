"""ADR-081's cut, at the part that can be decided without a model.

The interesting property here is not "the list got shorter". It is that the
list which comes back is still a *legal* conversation: runtime invariant 1
gives every exposed ``tool_call_id`` exactly one result, and the obvious
implementation of "keep the last N messages" breaks it about half the time --
whenever N happens to land between an assistant's ``tool_use`` and the ``tool``
message answering it. A provider rejects that with a 400 that says nothing
about compaction, so the failure would surface as the very symptom ADR-080 was
written to stop relaying.
"""

from __future__ import annotations

import pytest

from agent_workbench.domain.messages import (
    Message,
    assistant_message,
    tool_message,
    user_message,
)
from agent_workbench.domain.tools import ToolCall, ToolResult
from agent_workbench.runtime.compaction import (
    KEEP_LAST_MESSAGES,
    SUMMARY_MARKER,
    conversation_chars,
    plan_compaction,
    render_for_summary,
    scaled_tokens_after,
)


def _call(index: int) -> ToolCall:
    return ToolCall(
        tool_call_id=f"toolu_{index:020d}",
        tool_name="read_document",
        arguments={"document_id": f"doc_{index}"},
    )


def _pair(index: int) -> tuple[Message, Message]:
    """One assistant turn and the tool message answering it."""

    call = _call(index)
    return (
        assistant_message(text=f"Looking at {index}.", tool_calls=(call,)),
        tool_message((ToolResult.succeeded(call, content=f"body {index}"),)),
    )


def _conversation(pairs: int) -> list[Message]:
    messages: list[Message] = [user_message("Find out who owns fusion.")]
    for index in range(pairs):
        messages.extend(_pair(index))
    return messages


def _pairing_is_legal(messages: list[Message]) -> bool:
    """Every exposed call has exactly one result, and no result is an orphan."""

    proposed = [
        call.tool_call_id for message in messages for call in message.tool_calls()
    ]
    answered = [
        block.tool_call_id
        for message in messages
        for block in message.content
        if block.kind == "tool_result"
    ]
    return sorted(proposed) == sorted(answered)


class TestWhatIsRemoved:
    def test_a_conversation_too_short_to_shorten_is_left_alone(self) -> None:
        """``None`` is an answer, and the caller has to treat it as one.

        Four long messages cannot be made shorter by removing the middle,
        and returning an empty plan instead would spend a model call to
        discover that -- leaving the run in the state that provoked it, one
        compaction poorer.
        """

        for length in range(1, KEEP_LAST_MESSAGES + 2):
            messages = _conversation(0) + [user_message("x")] * (length - 1)
            assert plan_compaction(messages) is None

    def test_the_first_message_always_survives(self) -> None:
        # The message that says what the run is *for*. It is also the one
        # several providers require a conversation to begin with, so keeping
        # it means compaction can never produce a shape that was already
        # illegal before it ran.
        messages = _conversation(pairs=8)
        plan = plan_compaction(messages)

        assert plan is not None
        assert plan.head == (messages[0],)
        assert messages[0] not in plan.removed

    def test_nothing_is_both_removed_and_kept(self) -> None:
        messages = _conversation(pairs=8)
        plan = plan_compaction(messages)

        assert plan is not None
        assert [*plan.head, *plan.removed, *plan.kept] == messages

    def test_a_middle_that_is_one_unbreakable_span_is_refused(self) -> None:
        """The boundary walks forward and runs out of conversation.

        Contrived on purpose: a long run of ``tool`` messages with nothing to
        cut between. The point is that the refusal is explicit rather than an
        index that quietly goes out of range -- and that it refuses instead of
        cutting somewhere illegal.
        """

        call = _call(0)
        messages: list[Message] = [
            user_message("go"),
            assistant_message(text="", tool_calls=(call,)),
            *[
                tool_message((ToolResult.succeeded(_call(n), content="x"),))
                for n in range(KEEP_LAST_MESSAGES + 3)
            ],
        ]

        assert plan_compaction(messages) is None

    def test_keep_last_must_keep_something(self) -> None:
        with pytest.raises(ValueError, match="keep_last"):
            plan_compaction(_conversation(pairs=8), keep_last=0)


class TestTheCutIsLegal:
    def test_the_shortened_conversation_still_pairs_every_call(self) -> None:
        """The property the whole module exists for, swept over every length.

        Every one of these conversations is legal before compaction. A
        ``keep_last`` that landed between a ``tool_use`` and its answer would
        make the rebuilt one illegal, and the provider's complaint would be a
        400 with nothing in it about compaction.
        """

        for pairs in range(1, 12):
            messages = _conversation(pairs)
            assert _pairing_is_legal(messages), f"fixture is broken at {pairs}"
            for keep_last in range(1, 2 * pairs + 2):
                plan = plan_compaction(messages, keep_last=keep_last)
                if plan is None:
                    continue
                rebuilt = plan.rebuilt("did some reading")
                assert _pairing_is_legal(rebuilt), (
                    f"pairs={pairs} keep_last={keep_last} left an unanswered call"
                )
                assert len(rebuilt) < len(messages)

    def test_the_kept_tail_never_opens_with_a_tool_message(self) -> None:
        for pairs in range(1, 12):
            for keep_last in range(1, 2 * pairs + 2):
                plan = plan_compaction(_conversation(pairs), keep_last=keep_last)
                if plan is None:
                    continue
                assert plan.kept[0].role != "tool"

    def test_the_summary_is_the_agents_own_account_and_says_so(self) -> None:
        """An ``assistant`` message, marked.

        The role is the true one -- it is this agent's record of its own
        earlier work. The marker is what stops the model reading a paragraph
        it does not remember writing as something it wrote verbatim.

        The shape this must never take is a ``user`` message: that would put
        words in the user's mouth, and the transcript would say a person said
        something nobody said.
        """

        plan = plan_compaction(_conversation(pairs=8))
        assert plan is not None
        rebuilt = plan.rebuilt("read four files, none mentioned fusion")

        summary = rebuilt[1]
        assert summary.role == "assistant"
        assert summary.text().startswith(SUMMARY_MARKER)
        assert "read four files" in summary.text()
        assert not any(message.role == "user" for message in rebuilt[1:])


class TestWhatTheSummariserIsShown:
    def test_tool_calls_and_their_results_both_appear(self) -> None:
        # A transcript, not a replayed conversation: a model handed a partial
        # message list would continue the dialogue, and could propose a tool.
        # Handed a transcript it describes one, which is the job.
        rendered = render_for_summary(_conversation(pairs=3)[1:])

        assert "(called read_document)" in rendered
        assert "body 0" in rendered
        assert "Looking at 2." in rendered

    def test_an_over_long_excerpt_drops_the_oldest_and_says_how_much(self) -> None:
        """Oldest-first, announced.

        Dropping the newest would summarise a history that stops before the
        part that made the conversation too long, which is the half that
        matters. Announcing it is what keeps the summary from reading complete.
        """

        rendered = render_for_summary(_conversation(pairs=40)[1:], max_chars=400)

        assert len(rendered) < 600
        assert "characters of this excerpt are not shown" in rendered
        # The tail survived; the head did not.
        assert "body 39" in rendered
        assert "body 0" not in rendered

    def test_an_empty_span_renders_to_nothing_rather_than_to_noise(self) -> None:
        assert render_for_summary(()) == ""


class TestTheNumbersInTheEvent:
    def test_after_is_never_larger_than_the_measured_before(self) -> None:
        # `tokens_before` is measured -- the provider's own count for the
        # request that was too large. `after` is that number scaled by the
        # character ratio, so it is an estimate, and an estimate that came out
        # *above* the thing it is a reduction of would be visibly wrong in the
        # event stream.
        assert scaled_tokens_after(1000, chars_before=100, chars_after=40) == 400
        assert scaled_tokens_after(1000, chars_before=100, chars_after=999) == 1000
        assert scaled_tokens_after(1000, chars_before=0, chars_after=10) == 0
        assert scaled_tokens_after(0, chars_before=100, chars_after=50) == 0

    def test_shortening_a_conversation_shortens_its_character_count(self) -> None:
        messages = _conversation(pairs=8)
        plan = plan_compaction(messages)
        assert plan is not None

        before = conversation_chars(messages)
        after = conversation_chars(plan.rebuilt("short"))

        assert 0 < after < before
