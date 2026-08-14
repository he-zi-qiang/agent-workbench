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
from agent_workbench.apps.cli.repl import Repl, sse_frames
from agent_workbench.apps.cli.stages import (
    Stage,
    chat_stage_of,
    order_of,
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

    def test_a_live_frame_cannot_move_the_resume_position(self) -> None:
        """The server now sends id-less frames, and carrying forward is safe.

        A transient event has no position, so its frame has no ``id`` line.
        Under the rule above it reports the cursor that was already in effect,
        and the pump assigns only truthy cursors -- so a live frame can only
        ever write back the value already held. The durable frame after it
        still advances normally.

        Pinned because the alternative reading is tempting and wrong: resetting
        the cursor per frame would make live frames yield ``None``, which is
        equally safe here but would silently change what this function's tuple
        means for every other caller.
        """

        frames = list(
            sse_frames(
                _lines(
                    'id: s:1\ndata: {"a": 1}\n\n'
                    'event: ModelDelta\ndata: {"a": 2}\n\n'
                    'event: stream.degraded\ndata: {"dropped_events": 3}\n\n'
                    'id: s:2\ndata: {"a": 3}\n\n'
                )
            )
        )

        assert [cursor for cursor, _ in frames] == ["s:1", "s:1", "s:1", "s:2"]

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

    def test_v2_nodes_have_stages_of_their_own_rather_than_falling_through(
        self,
    ) -> None:
        """One table serves both graphs: only visited stages render, so the
        interleaved order is correct for each. The load-bearing half is
        `review` -- v2's reviewer lands in the same stage as v1's critic
        because they are the same step to a reader, which is why the server
        shared the vocabulary in the first place (ADR-031 §2.1)."""

        assert task_stage_of("work") == "work"
        assert title_of("work", task=True) == "动手做事"
        assert task_stage_of("review") == "review"
        assert title_of("review", task=True) == "检查与修订"
        # And work is ordered inside the run, not appended after delivery the
        # way an unlisted node would be.
        assert order_of("work", task=True) < order_of("review", task=True)
        assert order_of("review", task=True) < order_of("deliver", task=True)

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


class TestGraphChoice:
    """`/graph` decides what `/task` submits, and silence stays silence."""

    @staticmethod
    def _repl(handler: object) -> tuple[Repl, io.StringIO]:
        import httpx

        output = io.StringIO()
        client = httpx.Client(
            base_url="http://api.test",
            transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        )
        return (
            Repl(
                client,
                {"x-tenant-id": "tenant_a", "x-principal-id": "user_1"},
                output,
                interactive=False,
                colour=False,
            ),
            output,
        )

    def _submitted_body(
        self,
        repl: Repl,
        triage: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Drive one `/task` to completion and return what it POSTed.

        ``triage`` is what POST /v1/tasks/triage answers; the default is the
        endpoint's own disabled shape, so a test that says nothing about
        triage measures the deployment-default path.
        """

        import json as jsonlib

        import httpx

        captured: dict[str, object] = {}
        triage_calls: list[dict[str, object]] = []
        captured["_triage_calls"] = triage_calls

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/v1/tasks/triage":
                triage_calls.append(jsonlib.loads(request.content))
                return httpx.Response(200, json=triage or {"status": "default"})
            if request.method == "POST" and request.url.path == "/v1/tasks":
                captured.update(jsonlib.loads(request.content))
                return httpx.Response(201, json={"task_id": "task_1"})
            if request.url.path.endswith("/timeline"):
                return httpx.Response(
                    200, json={"task_id": "task_1", "events": [], "cursor": None}
                )
            # The status poll: terminal at once, so _follow_task returns
            # without sleeping through a poll interval.
            return httpx.Response(200, json={"task_id": "task_1", "status": "failed"})

        client = httpx.Client(
            base_url="http://api.test", transport=httpx.MockTransport(handler)
        )
        repl.client = client
        repl._task("整理这批文件")
        return captured

    def test_auto_with_a_default_verdict_sends_no_graph(self) -> None:
        """The deployment's default must stay the server's decision: a repl
        that always sent its own idea of the default would freeze that idea
        into every Task it submits (ADR-031 §2.3). Auto is the mode since
        ADR-036, and a triage that answers "default" resolves to exactly the
        old silence -- plus a provenance block saying nobody decided."""

        repl, _ = self._repl(lambda _: None)
        body = self._submitted_body(repl)

        assert "graph" not in body
        assert body["intent"] == {
            "graph_decided_by": "default",
            "wants_report_decided_by": "default",
        }

    def test_auto_submits_a_decided_verdict_with_its_provenance(self) -> None:
        repl, output = self._repl(lambda _: None)
        body = self._submitted_body(
            repl,
            triage={
                "status": "decided",
                "graph": "general",
                "wants_report": True,
                "reason": "要把事做完",
            },
        )

        assert body["graph"] == "general"
        assert body["wants_report"] is True
        assert body["intent"] == {
            "graph_decided_by": "model",
            "wants_report_decided_by": "model",
            "reason": "要把事做完",
        }
        assert len(body["_triage_calls"]) == 1  # type: ignore[arg-type]
        # The verdict is visible, not silent: one line of provenance.
        assert "模型判定" in output.getvalue()

    def test_auto_asks_and_a_piped_session_takes_the_default_out_loud(self) -> None:
        repl, output = self._repl(lambda _: None)
        body = self._submitted_body(
            repl,
            triage={"status": "ask", "question": "要报告还是要执行？"},
        )

        assert "graph" not in body
        assert "要报告还是要执行？" in output.getvalue()
        assert "非交互" in output.getvalue()

    def test_the_chosen_graph_travels_with_the_submission_and_skips_triage(
        self,
    ) -> None:
        """An explicit pin never consults the model. The control group is the
        decided-verdict test above, whose triage endpoint is called once."""

        repl, _ = self._repl(lambda _: None)
        repl._command("/graph general")
        body = self._submitted_body(repl)

        assert body["graph"] == "general"
        assert body["intent"] == {
            "graph_decided_by": "user",
            "wants_report_decided_by": "default",
        }
        assert body["_triage_calls"] == []

    def test_default_returns_to_not_sending_and_claims_nothing(self) -> None:
        repl, _ = self._repl(lambda _: None)
        repl._command("/graph general")
        repl._command("/graph default")
        body = self._submitted_body(repl)

        assert "graph" not in body
        assert "intent" not in body
        assert body["_triage_calls"] == []

    def test_an_interactive_ask_takes_the_readers_answer_as_their_own(
        self, monkeypatch: object
    ) -> None:
        """The chip flow, in terminal form: the answer is an explicit choice
        recorded as the reader's, not as the model's."""

        import builtins

        repl, output = self._repl(lambda _: None)
        repl.interactive = True
        monkeypatch.setattr(builtins, "input", lambda prompt="": "2")  # type: ignore[attr-defined]
        body = self._submitted_body(
            repl,
            triage={"status": "ask", "question": "要报告还是要执行？"},
        )

        assert body["graph"] == "general"
        assert body["intent"] == {
            "graph_decided_by": "user",
            "wants_report_decided_by": "default",
        }
        assert "要报告还是要执行？" in output.getvalue()

    def test_an_unknown_choice_changes_nothing_and_prints_usage(self) -> None:
        repl, output = self._repl(lambda _: None)
        repl._command("/graph v2_general")

        assert repl.task_graph == "auto"
        assert "auto|research|general|default" in output.getvalue()
