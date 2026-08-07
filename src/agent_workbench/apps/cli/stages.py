"""Grouping events into the stages a reader follows.

The same six Task stages and three Chat stages the web console shows, kept here
so the terminal and the browser tell one story. A node this table has not heard
of still appears -- as its own stage, at the end -- because a graph that grew a
step should show it rather than have it swallowed by a stale table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from agent_workbench.domain.events import EventEnvelope

#: Task graph nodes, grouped the way the run reads rather than the way it
#: compiles: `route` is bookkeeping between planning and research, and the two
#: research nodes fan out in parallel.
TASK_STAGES: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("understand", "理解目标", ("understand",)),
    ("plan", "制定计划", ("plan", "route")),
    ("research", "收集资料", ("research_internal", "research_external")),
    ("synthesize", "撰写草稿", ("synthesize",)),
    ("review", "检查与修订", ("critic", "quality_gate")),
    ("deliver", "确认与产出", ("approval", "export")),
)

#: Chat has no graph, so its stages are grouped by event type instead.
CHAT_STAGES: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("retrieve", "检索资料", ("ContextBuilt", "RetrievalRejected")),
    (
        "answer",
        "生成回答",
        ("RunStarted", "ModelStarted", "ModelCompleted", "RunCompleted"),
    ),
    (
        "publish",
        "核对与发布",
        ("AnswerCommitted", "UngroundedAnswerCommitted", "AnswerWithheld"),
    ),
)

#: After these a run produces nothing further.
FINAL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "TaskSucceeded",
        "TaskFailed",
        "TaskCancelled",
        "TaskDeadLettered",
        "AnswerCommitted",
        "UngroundedAnswerCommitted",
        "AnswerWithheld",
        "ChatTurnExpired",
    }
)

#: A stage carrying one of these went wrong. `ToolFailed` is deliberately absent:
#: a denied or failing tool is recorded on the step and the graph routinely
#: continues past it -- research proposes a search, policy refuses it, and the
#: run still succeeds. A stage failed when its *run* did.
FAILURE_EVENTS: Final[frozenset[str]] = frozenset(
    {"RunFailed", "TaskFailed", "TaskDeadLettered"}
)


@dataclass(slots=True)
class Stage:
    """One stage, and the events that landed in it."""

    key: str
    title: str
    events: list[EventEnvelope] = field(default_factory=list[EventEnvelope])

    @property
    def failed(self) -> bool:
        return any(one.event_type in FAILURE_EVENTS for one in self.events)


def task_stage_of(graph_node_id: str | None) -> str | None:
    """The stage a Task event belongs to, or ``None`` for lifecycle events."""

    if graph_node_id is None:
        return None
    for key, _title, nodes in TASK_STAGES:
        if graph_node_id in nodes:
            return key
    return graph_node_id


def chat_stage_of(event_type: str) -> str | None:
    for key, _title, kinds in CHAT_STAGES:
        if event_type in kinds:
            return key
    return None


def order_of(key: str, *, task: bool) -> int:
    """Where a stage sits in the run, not when its first event happened.

    These differ, and following arrival order reads wrong: the runtime emits
    ``RunStarted`` before the retrieval it is about to use, so "生成回答" would
    be printed above "检索资料" on every grounded turn.
    """

    table = TASK_STAGES if task else CHAT_STAGES
    for index, (candidate, _title, _members) in enumerate(table):
        if candidate == key:
            return index
    return len(table)  # An unlisted node sorts after the ones we know.


def title_of(key: str, *, task: bool) -> str:
    table = TASK_STAGES if task else CHAT_STAGES
    for candidate, title, _members in table:
        if candidate == key:
            return title
    # An unlisted graph node: show the id rather than inventing a name for it.
    return key


__all__ = [
    "CHAT_STAGES",
    "FAILURE_EVENTS",
    "FINAL_EVENTS",
    "TASK_STAGES",
    "Stage",
    "chat_stage_of",
    "order_of",
    "task_stage_of",
    "title_of",
]
