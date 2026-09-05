"""Process-memory implementations of the store ports.

They are real implementations of the contracts, not stubs: ordering, tenant
scoping and not-found semantics behave the way the PostgreSQL and object-store
adapters will have to behave. What they do not provide is durability, which is
why they belong to local development, the walking skeleton and deterministic
tests rather than to any deployed profile.
"""

from agent_workbench.adapters.memory.artifact_store import InMemoryArtifactStore
from agent_workbench.adapters.memory.chat_expiration import (
    InMemoryChatExpirationCoordinator,
)
from agent_workbench.adapters.memory.chat_release import (
    InMemoryChatReleaseCoordinator,
)
from agent_workbench.adapters.memory.conversation_store import (
    InMemoryConversationStore,
)
from agent_workbench.adapters.memory.event_log import InMemoryEventLog
from agent_workbench.adapters.memory.projects import InMemoryProjectStore
from agent_workbench.adapters.memory.worker_presence import (
    InMemoryWorkerPresenceStore,
)

__all__ = [
    "InMemoryArtifactStore",
    "InMemoryChatExpirationCoordinator",
    "InMemoryChatReleaseCoordinator",
    "InMemoryConversationStore",
    "InMemoryEventLog",
    "InMemoryProjectStore",
    "InMemoryWorkerPresenceStore",
]
