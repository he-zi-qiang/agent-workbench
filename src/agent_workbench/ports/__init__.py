"""Protocols the core depends on and adapters implement.

A port names a capability the system needs without naming a vendor: a model
that streams, a store that keeps bytes, a log that orders events. Every
concrete integration -- Anthropic, PostgreSQL, Qdrant, LlamaIndex, LangGraph --
sits behind one of these and converts at its own boundary.

This package covers the model, tool, agent, event and store boundaries that the
walking skeleton and the runtime need. Knowledge retrieval, task registry,
approval, telemetry and sandbox ports are defined by their own work packages,
when there is an implementation to check them against; freezing a protocol that
nothing has exercised is how contracts drift before their first user.
"""

from agent_workbench.ports.agent_executor import AgentExecutor
from agent_workbench.ports.artifact_store import ArtifactStore
from agent_workbench.ports.cancellation import (
    CancellationSource,
    CancellationToken,
    NullCancellationToken,
)
from agent_workbench.ports.chat_expiration import ChatExpirationCoordinator
from agent_workbench.ports.chat_release import (
    ChatReleaseCoordinator,
    EvidenceRevisionGuard,
)
from agent_workbench.ports.conversation_store import (
    AuthorizedRevision,
    ChatTurnBusyError,
    ChatTurnClaim,
    ChatTurnConflictError,
    ChatTurnLeaseExpiredError,
    ChatTurnResult,
    ChatTurnStatus,
    ChatTurnStore,
    ConversationSession,
    ConversationStore,
    IdempotencyKey,
    RequestHash,
    StoredChatTurn,
    StoredMessage,
    chat_turn_terminal_event_key,
)
from agent_workbench.ports.event_log import (
    EventCursor,
    EventLogPort,
    EventScope,
    EventSink,
)
from agent_workbench.ports.model import (
    ModelEvent,
    ModelPort,
    ModelRequest,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCallProposed,
    ModelUsageReported,
)
from agent_workbench.ports.policy import PolicyEngine
from agent_workbench.ports.task_workflow import (
    GraphVersion,
    TaskWorkflowPort,
    TaskWorkflowResult,
    WorkflowDisposition,
    WorkflowGraphVersionMismatchError,
    WorkflowThreadAlreadyExistsError,
    WorkflowThreadNotFoundError,
)
from agent_workbench.ports.tools import (
    ToolBinding,
    ToolHandler,
    ToolInvocation,
    ToolRegistry,
)

__all__ = [
    "AgentExecutor",
    "ArtifactStore",
    "AuthorizedRevision",
    "CancellationSource",
    "CancellationToken",
    "ChatExpirationCoordinator",
    "ChatReleaseCoordinator",
    "ChatTurnBusyError",
    "ChatTurnClaim",
    "ChatTurnConflictError",
    "ChatTurnLeaseExpiredError",
    "ChatTurnResult",
    "ChatTurnStatus",
    "ChatTurnStore",
    "ConversationSession",
    "ConversationStore",
    "EventCursor",
    "EventLogPort",
    "EventScope",
    "EventSink",
    "EvidenceRevisionGuard",
    "GraphVersion",
    "IdempotencyKey",
    "ModelEvent",
    "ModelPort",
    "ModelRequest",
    "ModelStreamCompleted",
    "ModelTextDelta",
    "ModelToolCallProposed",
    "ModelUsageReported",
    "NullCancellationToken",
    "PolicyEngine",
    "RequestHash",
    "StoredChatTurn",
    "StoredMessage",
    "TaskWorkflowPort",
    "TaskWorkflowResult",
    "ToolBinding",
    "ToolHandler",
    "ToolInvocation",
    "ToolRegistry",
    "WorkflowDisposition",
    "WorkflowGraphVersionMismatchError",
    "WorkflowThreadAlreadyExistsError",
    "WorkflowThreadNotFoundError",
    "chat_turn_terminal_event_key",
]
