"""PostgreSQL persistence.

PostgreSQL is the source of truth for conversations, documents, the ingestion
outbox and, later, the task registry and run events. This package holds the
schema those facts live in and the repositories that read and write them; the
vector index is a derived copy and lives elsewhere.
"""

from agent_workbench.adapters.persistence.chat_expiration import (
    PostgresChatExpirationCoordinator,
)
from agent_workbench.adapters.persistence.chat_release import (
    PostgresChatReleaseCoordinator,
)
from agent_workbench.adapters.persistence.conversation_store import (
    PostgresConversationStore,
)
from agent_workbench.adapters.persistence.documents import PostgresDocumentStore
from agent_workbench.adapters.persistence.engine import create_query_engine
from agent_workbench.adapters.persistence.event_log import PostgresEventLog
from agent_workbench.adapters.persistence.models import metadata
from agent_workbench.adapters.persistence.outbox import PostgresOutbox
from agent_workbench.adapters.persistence.task_registry import PostgresTaskRegistry

__all__ = [
    "PostgresChatExpirationCoordinator",
    "PostgresChatReleaseCoordinator",
    "PostgresConversationStore",
    "PostgresDocumentStore",
    "PostgresEventLog",
    "PostgresOutbox",
    "PostgresTaskRegistry",
    "create_query_engine",
    "metadata",
]
