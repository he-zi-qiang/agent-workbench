"""A complete stack with no external dependency.

Every field below is typed as its port, not as the class that satisfies it, so
the type checker verifies structural conformance here rather than leaving it to
be discovered at runtime. Assembling the stack in one place also means the CLI
slice, the runtime tests and the recovery harness all start from the same
deterministic set of adapters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from agent_workbench.adapters.events import ScopedEventSink
from agent_workbench.adapters.memory import (
    InMemoryArtifactStore,
    InMemoryConversationStore,
    InMemoryEventLog,
)
from agent_workbench.adapters.models.fake import FakeModel, ScriptedTurn
from agent_workbench.adapters.policy import EnvelopePolicyEngine
from agent_workbench.adapters.tools import (
    StaticToolRegistry,
    read_document_tool,
    text_statistics_tool,
)
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import CancellationSource
from agent_workbench.ports.conversation_store import ConversationStore
from agent_workbench.ports.event_log import EventLogPort, EventScope, EventSink
from agent_workbench.ports.model import ModelPort
from agent_workbench.ports.policy import PolicyEngine
from agent_workbench.ports.tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class FakeStack:
    """Ports wired to deterministic, in-process implementations."""

    model: ModelPort
    registry: ToolRegistry
    policy: PolicyEngine
    events: EventLogPort
    conversations: ConversationStore
    artifacts: ArtifactStore
    cancellation: CancellationSource

    def sink(self, scope: EventScope) -> EventSink:
        return ScopedEventSink(log=self.events, scope=scope)


def fake_stack(
    *,
    turns: Sequence[ScriptedTurn] = (),
    corpus: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
    repeat_last_turn: bool = False,
) -> FakeStack:
    """Build a stack from a model script and an in-memory corpus."""

    registry = StaticToolRegistry(
        [
            read_document_tool(corpus if corpus is not None else {}),
            text_statistics_tool(),
        ]
    )
    return FakeStack(
        model=FakeModel(turns, repeat_last=repeat_last_turn),
        registry=registry,
        policy=EnvelopePolicyEngine(registry=registry),
        events=InMemoryEventLog(clock=clock),
        conversations=InMemoryConversationStore(),
        artifacts=InMemoryArtifactStore(),
        cancellation=CancellationSource(),
    )


__all__ = ["FakeStack", "fake_stack"]
