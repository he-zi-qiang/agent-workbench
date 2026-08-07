"""The interactive session: frame parsing, stage folding, and what it prints.

The transport pieces are tested against the wire format rather than a live
server, because the shapes that break them are the ones a server sends rarely:
a heartbeat, a resumed cursor, a frame split across lines.
"""

from __future__ import annotations

import io

from agent_workbench.apps.cli.live import (
    DETAIL_MARK,
    FAIL_MARK,
    STEP_MARK,
    LiveStages,
    banner,
)
from agent_workbench.apps.cli.repl import sse_frames
from agent_workbench.apps.cli.stages import (
    Stage,
    chat_stage_of,
    task_stage_of,
    title_of,
)


def _lines(text: str) -> list[str]:
    """As `httpx.iter_lines` yields them: no trailing newline on any line."""

    return text.split("\n")


class TestFrames:
    def test_one_frame_carries_its_cursor_and_payload(self) -> None:
        frames = list(
            sse_frames(_lines('id: ses_1:4\nevent: RunStarted\ndata: {"a": 1}\n\n'))
        )

        assert frames == [("ses_1:4", {"a": 1})]

    def test_a_heartbeat_is_not_a_frame(self) -> None:
        """The server sends `: heartbeat` so a silent socket is not mistaken
        for a dead one. Reading it as data would put junk on the queue."""

        frames = list(sse_frames(_lines(': heartbeat\n\nid: s:1\ndata: {"a": 1}\n\n')))

        assert frames == [("s:1", {"a": 1})]

    def test_the_cursor_carries_forward_until_a_new_one_arrives(self) -> None:
        """Resume is by the last id seen, so a frame without one keeps it."""

        frames = list(
            sse_frames(_lines('id: s:1\ndata: {"a": 1}\n\ndata: {"a": 2}\n\n'))
        )

        assert [cursor for cursor, _ in frames] == ["s:1", "s:1"]

    def test_a_multi_line_data_field_is_rejoined(self) -> None:
        frames = list(sse_frames(_lines('data: {"a":\ndata: 1}\n\n')))

        assert frames == [(None, {"a": 1})]

    def test_an_unparsable_frame_is_skipped_rather_than_raised(self) -> None:
        """One bad frame must not end a subscription that is still delivering."""

        frames = list(sse_frames(_lines('data: not json\n\ndata: {"a": 1}\n\n')))

        assert frames == [(None, {"a": 1})]

    def test_a_frame_with_no_blank_line_yet_is_not_yielded_early(self) -> None:
        assert list(sse_frames(_lines('data: {"a": 1}'))) == []


class TestLiveStages:
    def test_without_a_terminal_each_stage_prints_once_when_it_settles(self) -> None:
        """Piped to a file the output has to be a transcript, not cursor moves."""

        out = io.StringIO()
        live = LiveStages(stream=out, interactive=False)

        live.active("检索资料", "转圈")
        live.done("retrieve", "检索资料", "6 个片段")
        live.done("retrieve", "检索资料", "6 个片段")

        # The step, then the line hanging under it saying what it produced.
        assert out.getvalue() == (f"{STEP_MARK} 检索资料\n  {DETAIL_MARK}  6 个片段\n")

    def test_with_a_terminal_the_running_line_is_rewritten_in_place(self) -> None:
        out = io.StringIO()
        live = LiveStages(stream=out, interactive=True)

        live.active("检索资料")
        live.active("检索资料")

        # One clear per redraw, and nothing committed to the scrollback yet.
        assert out.getvalue().count("\x1b[2K") == 1
        assert "\n" not in out.getvalue()

    def test_a_failed_stage_is_marked_differently(self) -> None:
        out = io.StringIO()
        live = LiveStages(stream=out, interactive=False)

        live.done("review", "检查与修订", "", failed=True)

        assert out.getvalue().startswith(FAIL_MARK)

    def test_clear_drops_a_half_drawn_line(self) -> None:
        out = io.StringIO()
        live = LiveStages(stream=out, interactive=True)

        live.active("检索资料")
        live.clear()
        live.done("retrieve", "检索资料")

        assert out.getvalue().endswith(f"{STEP_MARK} 检索资料\n")


class TestBanner:
    def test_the_frame_closes_on_a_line_of_chinese(self) -> None:
        """CJK is two columns wide; counting characters lands the right edge
        mid-glyph, which is every line of this banner."""

        lines = banner("Agent Workbench", "/help 看命令", colour=False).split("\n")

        assert len({len(line.encode("utf-8")) for line in lines}) >= 1
        widths = {_columns(line) for line in lines}
        assert len(widths) == 1


def _columns(text: str) -> int:
    from agent_workbench.apps.cli.live import _width

    return _width(text)


class TestStages:
    def test_chat_events_land_in_the_stage_a_reader_expects(self) -> None:
        assert chat_stage_of("ContextBuilt") == "retrieve"
        assert chat_stage_of("RetrievalRejected") == "retrieve"
        assert chat_stage_of("ModelCompleted") == "answer"
        assert chat_stage_of("AnswerCommitted") == "publish"

    def test_an_event_belonging_to_no_stage_is_not_forced_into_one(self) -> None:
        assert chat_stage_of("ToolProgress") is None

    def test_task_nodes_group_the_way_the_run_reads(self) -> None:
        # The two research nodes fan out in parallel and read as one step.
        assert task_stage_of("research_internal") == "research"
        assert task_stage_of("research_external") == "research"
        assert task_stage_of("route") == "plan"

    def test_a_lifecycle_event_has_no_stage(self) -> None:
        assert task_stage_of(None) is None

    def test_an_unlisted_node_becomes_its_own_stage(self) -> None:
        """A graph that grew a node shows it, rather than having it swallowed."""

        assert task_stage_of("summarise_twice") == "summarise_twice"
        assert title_of("summarise_twice", task=True) == "summarise_twice"

    def test_a_stage_fails_on_a_failed_run_not_a_failed_tool(self) -> None:
        """A refused tool is routine -- research proposes a search, policy says
        no, and the Task still succeeds. Marking that red paints a finished run
        as broken."""

        assert not Stage(key="a", title="a", events=[_event("ToolFailed")]).failed
        assert Stage(key="a", title="a", events=[_event("RunFailed")]).failed


def _event(event_type: str) -> object:
    class _Envelope:
        pass

    envelope = _Envelope()
    envelope.event_type = event_type  # type: ignore[attr-defined]
    return envelope
