"""An interactive session: ask, watch it work, read the answer.

The other subcommands are one request each, which is right for scripting and
wrong for using. This one keeps a session open, streams what the run is doing
while it does it, and folds each stage to a line -- the same three Chat stages
and six Task stages the web console shows, so a reader who has seen one
recognises the other.

Stages are folded by default and opened with ``/steps``. Folded, a finished
turn is three lines; opened, it is every durable event with the prompt and tool
arguments the deployment chose to record (ADR-019). The default is folded
because the interesting question during a run is "where is it", and afterwards
it is usually still "what did it answer".

Two transports, because the server offers two. Chat has an SSE endpoint, so one
long-lived subscription runs beside the turns and feeds a queue; the answer
request blocks a worker thread while this one renders what arrives. Tasks have
no SSE, so their timeline is polled -- the same thing the web console does, and
for the same reason.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any, Final, TextIO

import httpx

from agent_workbench.apps.cli.http import CliHttpError, response_json
from agent_workbench.apps.cli.live import (
    LiveStages,
    banner,
    colour_enabled,
    paint,
)
from agent_workbench.apps.cli.rendering import summarize_payload
from agent_workbench.apps.cli.stages import (
    FINAL_EVENTS,
    Stage,
    chat_stage_of,
    order_of,
    task_stage_of,
    title_of,
)
from agent_workbench.domain.events import EventEnvelope

TITLE: Final[str] = "Agent Workbench"
SUBTITLE: Final[str] = "/help 看命令 · /exit 退出"

HELP: Final[str] = """  /kb <id>        选一个知识库；/kb none 回到自由回答
  /task <目标>     提交一个 Task，实时看它推进
  /steps          展开上一轮的每一步（提示词、工具参数、原始事件）
  /session        显示当前会话与知识库
  /new            开一个新会话
  /help  /exit"""

#: How often a Task's timeline is re-read. Matches the web console.
TASK_POLL_SECONDS: Final[float] = 2.5
#: Fast enough that the spinner reads as motion rather than as a stutter.
SPIN_SECONDS: Final[float] = 0.12
#: How long to keep reading after the answer returns, waiting for the events
#: that describe it. Generous because it ends early on the commit event.
DRAIN_SECONDS: Final[float] = 12.0


def sse_frames(lines: Iterator[str]) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """``(cursor, payload)`` per frame, skipping comments and heartbeats.

    Written against the wire format rather than a client library: the server
    sends ``id``/``event``/``data`` and a bare ``:`` heartbeat, and parsing four
    line shapes is smaller than a dependency.
    """

    cursor: str | None = None
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                with contextlib.suppress(json.JSONDecodeError):
                    yield cursor, json.loads("\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            cursor = value
        elif field == "data":
            data.append(value)


class Repl:
    """One interactive session against a running API."""

    def __init__(
        self,
        client: httpx.Client,
        headers: Mapping[str, str],
        stream: TextIO,
        *,
        interactive: bool,
        knowledge_base_id: str | None = None,
        colour: bool = True,
    ) -> None:
        self.client = client
        self.headers = dict(headers)
        self.stream = stream
        self.interactive = interactive
        self.colour = colour and colour_enabled(stream)
        self.session_id: str | None = None
        self.knowledge_base_id = knowledge_base_id
        self.last_stages: list[Stage] = []
        self._turn = 0
        self._events: queue.Queue[EventEnvelope] = queue.Queue()
        self._stop = threading.Event()
        self._pump: threading.Thread | None = None
        self._last_cursor: str | None = None

    # -- the loop ----------------------------------------------------------

    def run(self, lines: Iterator[str]) -> int:
        self._say(banner(TITLE, SUBTITLE, colour=self.colour))
        try:
            for raw in lines:
                line = raw.strip()
                if line == "":
                    continue
                if line.startswith("/"):
                    if self._command(line) is False:
                        break
                    continue
                self._ask(line)
        finally:
            self._stop.set()
        return 0

    def _command(self, line: str) -> bool:
        name, _, rest = line.partition(" ")
        rest = rest.strip()
        if name in ("/exit", "/quit"):
            return False
        if name == "/help":
            self._say(HELP)
        elif name == "/kb":
            self.knowledge_base_id = None if rest in ("", "none", "off") else rest
            self._say(f"  知识库：{self.knowledge_base_id or '不使用 · 自由回答'}")
        elif name == "/session":
            self._say(
                f"  会话：{self.session_id or '（还没开始）'}\n"
                f"  知识库：{self.knowledge_base_id or '不使用 · 自由回答'}"
            )
        elif name == "/new":
            self._stop.set()
            self._pump = None
            self._stop = threading.Event()
            self.session_id = None
            self.last_stages = []
            self._last_cursor = None
            self._say("  已开新会话。")
        elif name == "/steps":
            self._print_steps()
        elif name == "/task":
            if rest == "":
                self._say("  用法：/task <目标>")
            else:
                self._task(rest)
        else:
            self._say(f"  未知命令 {name}，/help 看可用的。")
        return True

    # -- chat --------------------------------------------------------------

    def _ask(self, question: str) -> None:
        if self.session_id is None:
            try:
                opened = self._json(
                    "POST", "/v1/chat/sessions", json={"title": question[:80]}
                )
            except CliHttpError as error:
                self._fail(error)
                return
            self.session_id = str(opened["session_id"])
            self._start_pump(self.session_id)

        session_id = self.session_id
        self._turn += 1
        answers: queue.Queue[Any] = queue.Queue(maxsize=1)

        def ask() -> None:
            try:
                answers.put(
                    self._json(
                        "POST",
                        f"/v1/chat/sessions/{session_id}/messages",
                        headers={"Idempotency-Key": f"repl-{session_id}-{self._turn}"},
                        json={
                            "question": question,
                            "answer_mode": (
                                "rag" if self.knowledge_base_id else "direct"
                            ),
                            **(
                                {"knowledge_base_id": self.knowledge_base_id}
                                if self.knowledge_base_id
                                else {}
                            ),
                        },
                    )
                )
            except BaseException as caught:
                answers.put(caught)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        stages = self._render_until(answers, task=False)
        worker.join(timeout=5.0)

        self.last_stages = stages
        answered = answers.get() if not answers.empty() else None
        if isinstance(answered, BaseException):
            self._fail(answered)
        elif isinstance(answered, dict):
            self._print_answer(answered)

    def _render_until(self, answers: queue.Queue[Any], *, task: bool) -> list[Stage]:
        """Fold arriving events into stage lines until the request returns.

        Stages settle at the end rather than as they go, because a chat turn's
        events interleave across them: the runtime emits ``RunStarted`` (生成回答)
        before the ``ContextBuilt`` (检索资料) it is about to use, and settling
        on "a later stage started receiving" would print those two in the order
        they arrived rather than the order they happened. The spinner still
        names the stage receiving events, so the run is not silent meanwhile.
        """

        stages: list[Stage] = []
        live = self._live()
        started = time.monotonic()

        while answers.empty():
            drained = False
            while True:
                try:
                    envelope = self._events.get_nowait()
                except queue.Empty:
                    break
                drained = True
                key = chat_stage_of(envelope.event_type)
                if key is None:
                    continue
                _record(stages, key, envelope, task=task)
            elapsed = time.monotonic() - started
            if stages:
                current = _ordered(stages, task=task)[-1]
                live.active(current.title, _note(current), elapsed)
            elif not drained:
                live.active("正在提交", "", elapsed)
            time.sleep(SPIN_SECONDS)

        # The answer returns over HTTP before its events have been read back
        # off the log, so the last stages would otherwise be missing from a
        # turn that had already finished. Waits for the commit event rather
        # than a fixed pause: the poll interval is a deployment's choice, and a
        # pause tuned to one value is wrong at every other.
        deadline = time.monotonic() + DRAIN_SECONDS
        while time.monotonic() < deadline:
            try:
                envelope = self._events.get(timeout=0.2)
            except queue.Empty:
                continue
            key = chat_stage_of(envelope.event_type)
            if key is not None:
                _record(stages, key, envelope, task=task)
            if envelope.event_type in FINAL_EVENTS:
                break
        live.clear()
        for stage in _ordered(stages, task=task):
            live.done(stage.key, stage.title, _note(stage), failed=stage.failed)
        return _ordered(stages, task=task)

    # -- the subscription --------------------------------------------------

    def _start_pump(self, session_id: str) -> None:
        """One SSE subscription for the whole session, feeding a queue.

        Long-lived rather than per-turn: resubscribing would replay from the
        beginning unless every turn tracked its own cursor, and a stream that
        outlives the turn is exactly what SSE is for. Resume is by
        ``Last-Event-ID`` so a dropped connection does not lose events.
        """

        stop = self._stop

        def pump() -> None:
            while not stop.is_set():
                headers = dict(self.headers)
                if self._last_cursor:
                    headers["Last-Event-ID"] = self._last_cursor
                try:
                    with self.client.stream(
                        "GET",
                        f"/v1/chat/sessions/{session_id}/events",
                        headers=headers,
                        # No read timeout: an idle stream is the normal state,
                        # and the server's heartbeat is what proves it is alive.
                        timeout=httpx.Timeout(None, connect=10.0),
                    ) as response:
                        if response.status_code >= 400:
                            return
                        for cursor, payload in sse_frames(response.iter_lines()):
                            if stop.is_set():
                                return
                            if cursor:
                                self._last_cursor = cursor
                            try:
                                self._events.put(EventEnvelope.model_validate(payload))
                            except Exception:
                                continue
                except httpx.HTTPError:
                    if stop.is_set():
                        return
                    time.sleep(1.0)

        self._pump = threading.Thread(target=pump, daemon=True)
        self._pump.start()

    # -- task --------------------------------------------------------------

    def _task(self, objective: str) -> None:
        try:
            created = self._json(
                "POST",
                "/v1/tasks",
                headers={"Idempotency-Key": f"repl-task-{self._turn}-{time.time_ns()}"},
                json={
                    "objective": objective,
                    "max_revisions": 2,
                    "wants_report": _mentions_report(objective),
                },
            )
        except CliHttpError as error:
            self._fail(error)
            return
        task_id = str(created["task_id"])
        self._say(self._dim(f"  {task_id}"))
        self.last_stages = self._follow_task(task_id)

    def _follow_task(self, task_id: str) -> list[Stage]:
        stages: list[Stage] = []
        live = self._live()
        started = time.monotonic()
        seen: set[str] = set()
        settled: set[str] = set()
        decided: set[str] = set()
        cursor: str | None = None

        while True:
            payload = self._json(
                "GET",
                f"/v1/tasks/{task_id}/timeline",
                params={"limit": 200, **({"cursor": cursor} if cursor else {})},
            )
            for raw in payload.get("events", []):
                try:
                    envelope = EventEnvelope.model_validate(raw)
                except Exception:
                    continue
                if envelope.event_id in seen:
                    continue
                seen.add(envelope.event_id)
                if envelope.event_type == "TaskApprovalRequested":
                    approval_id = str(getattr(envelope.payload, "approval_id", ""))
                    if approval_id and approval_id not in decided:
                        decided.add(approval_id)
                        live.clear()
                        self._decide(approval_id)
                    continue
                key = task_stage_of(envelope.graph_node_id)
                if key is None:
                    continue
                _record(stages, key, envelope, task=True)
                running = stages[-1].key
                for earlier in _ordered(stages, task=True):
                    if earlier.key == running or earlier.key in settled:
                        continue
                    settled.add(earlier.key)
                    live.done(
                        earlier.key,
                        earlier.title,
                        _note(earlier),
                        failed=earlier.failed,
                    )
            cursor = payload.get("cursor") or cursor

            task = self._json("GET", f"/v1/tasks/{task_id}")
            status = str(task.get("status", ""))
            if status in ("succeeded", "failed", "cancelled", "dead_letter"):
                live.clear()
                ordered = _ordered(stages, task=True)
                for stage in ordered:
                    live.done(stage.key, stage.title, _note(stage), failed=stage.failed)
                self._print_task_outcome(task, ordered)
                return ordered
            elapsed = time.monotonic() - started
            if stages:
                current = _ordered(stages, task=True)[-1]
                note = "等待你确认" if status == "waiting_approval" else _note(current)
                live.active(current.title, note, elapsed)
            else:
                live.active("排队中", "", elapsed)
            time.sleep(TASK_POLL_SECONDS)

    def _decide(self, approval_id: str) -> None:
        """Ask here rather than sending the reader to another surface."""

        approval = self._json("GET", f"/v1/approvals/{approval_id}")
        if approval.get("status") != "pending":
            return
        self._say("\n  要生成并导出这份报告吗？批准后继续导出；拒绝则到此为止。")
        answer = input("  [y/N] ").strip().lower()
        decision = "approved" if answer in ("y", "yes", "是") else "rejected"
        # The server treats this as the idempotency key: a repeat is the same
        # answer arriving twice, a higher one supersedes.
        version = int(approval.get("decision_version", 0)) + 1
        self._json(
            "POST",
            f"/v1/approvals/{approval_id}/decisions",
            headers={"Idempotency-Key": f"repl-decide-{approval_id}-{version}"},
            json={"decision": decision, "decision_version": version},
        )
        self._say(f"  已{'批准' if decision == 'approved' else '拒绝'}。")

    # -- output ------------------------------------------------------------

    def _print_answer(self, answered: Mapping[str, Any]) -> None:
        text = str(answered.get("answer", "")).strip()
        citations = answered.get("citations") or []
        self._say("")
        if text:
            self._say(text)
        if citations:
            label = f"\n  引用 {len(citations)} 条："
            self._say(self._dim(label))
            for one in citations[:8]:
                self._say(
                    f"    · {one.get('chunk_id', '?')}"
                    if isinstance(one, dict)
                    else f"    · {one}"
                )
        elif answered.get("grounded") is False:
            self._say("\n  未经证据核实：这条回答由模型直接作答，没有引用。")
        self._say("")

    def _print_task_outcome(self, task: Mapping[str, Any], stages: list[Stage]) -> None:
        status = str(task.get("status", ""))
        self._say("")
        if status != "succeeded":
            detail = task.get("status_detail")
            self._say(f"  任务{status}" + (f"：{detail}" if detail else ""))
            self._say("")
            return
        for stage in stages:
            for envelope in stage.events:
                artifact = getattr(envelope.payload, "artifact", None)
                if artifact is None:
                    continue
                name = getattr(artifact, "filename", None) or getattr(
                    artifact, "kind", "artifact"
                )
                self._say(f"  产出 {name}  ({artifact.artifact_id})")
        self._say("  完成。/steps 看每一步做了什么。")
        self._say("")

    def _print_steps(self) -> None:
        if not self.last_stages:
            self._say("  还没有可展开的步骤。")
            return
        for stage in self.last_stages:
            self._say(f"\n  {stage.title}")
            for envelope in stage.events:
                summary = summarize_payload(envelope.payload)
                head = f"    {envelope.event_type}"
                self._say(f"{head}  {summary}" if summary else head)
                for label, value in _recorded_inputs(envelope):
                    self._say(f"      {label}: {value}")
        self._say("")

    def _dim(self, text: str) -> str:
        """Secondary text: ids, counts, anything the eye should skip past."""

        return paint(text, "\x1b[2m", enabled=self.colour)

    def _live(self) -> LiveStages:
        return LiveStages(
            stream=self.stream, interactive=self.interactive, colour=self.colour
        )

    def _say(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def _fail(self, error: BaseException) -> None:
        if isinstance(error, CliHttpError):
            self._say(f"  失败：{error.code}")
        else:
            self._say(f"  失败：{type(error).__name__}: {error}")

    def _json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        merged = {**self.headers, **(headers or {})}
        return response_json(
            self.client.request(method, path, headers=merged, **kwargs)
        )


def _ordered(stages: list[Stage], *, task: bool) -> list[Stage]:
    """The stages a run has reached, in the order the run reaches them."""

    return sorted(stages, key=lambda stage: order_of(stage.key, task=task))


def _record(
    stages: list[Stage], key: str, envelope: EventEnvelope, *, task: bool
) -> None:
    for stage in stages:
        if stage.key == key:
            stage.events.append(envelope)
            return
    stages.append(Stage(key=key, title=title_of(key, task=task), events=[envelope]))


def _note(stage: Stage) -> str:
    """The one detail worth putting beside a folded stage."""

    for envelope in reversed(stage.events):
        payload = envelope.payload
        kind = envelope.event_type
        if kind == "ContextBuilt":
            return f"{getattr(payload, 'chunk_count', 0)} 个片段"
        if kind == "RetrievalRejected":
            return (
                "检索结果未被采用"
                if getattr(payload, "chunk_count", 0)
                else "没有可用的资料"
            )
        if kind == "ModelStarted":
            return str(getattr(payload, "model_id", ""))
        if kind == "ToolCompleted":
            return "工具已完成"
        if kind == "AnswerCommitted":
            return f"{len(getattr(payload, 'citations', ()) or ())} 条引用"
        if kind == "UngroundedAnswerCommitted":
            return "未经证据核实"
    return ""


def _recorded_inputs(envelope: EventEnvelope) -> list[tuple[str, str]]:
    """The prompt and tool arguments, when the deployment records them."""

    found: list[tuple[str, str]] = []
    prompt = getattr(envelope.payload, "prompt_preview", None)
    if prompt:
        found.append(("提示词", _clip(str(prompt))))
    arguments = getattr(envelope.payload, "argument_preview", None)
    if arguments:
        found.append(("工具参数", _clip(str(arguments))))
    return found


def _clip(text: str, limit: int = 400) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def _mentions_report(objective: str) -> bool:
    return any(word in objective for word in ("报告", "文件", "导出", "report"))


__all__ = ["HELP", "SUBTITLE", "TITLE", "Repl", "sse_frames"]
